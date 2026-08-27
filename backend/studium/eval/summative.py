"""Summative assessment: the closed-book examination (evaluation §11).

The learner-facing half of this subsystem. A summative attempt is a
*demonstration*, not a rehearsal (§3): closed-book, on unseen problems, single
submission per criterion, graded strictly, and -- when it passes -- credentialed
with a signed portfolio item.

**Closed-book is enforced, not documented.** §3 is explicit that "any softening
of the closed-book rule turns assessment into practice-with-different-branding
and loses the credentialing value", so each of §11.2's six conditions has an
enforcement point rather than a note in a docstring:

======================  =========================================================
Condition               Where it is enforced
======================  =========================================================
Primitives disabled     ``PRIMITIVE_MATRIX``: no rule lists
                        ``SUMMATIVE_ASSESSMENT`` in ``valid_from``, so
                        ``InvalidPrimitive`` is raised before dispatch. Already
                        true before this subsystem; :func:`assert_closed_book`
                        asserts it stays true.
Interrupts disabled     ``TRANSITIONS``: ``LEARNER_INTERRUPT`` has rows from
                        LECTURING and TUTORIAL only. Same shape, same guard.
Retrieval disabled      :func:`assert_closed_book`, called by the read
                        endpoints. The *system's* retrieval continues for the
                        Evaluator (§11.2), so this cannot be a global switch.
Hints disabled          :func:`assert_closed_book`; the bench does not render
                        a ladder it is not given.
Single submission       :func:`submit_response` refuses a second write to the
                        same criterion, and ``uq_assessment_responses_pair``
                        refuses it again underneath.
Timed (optional)        :func:`time_remaining`, from the definition's
                        ``time_limit_minutes`` and the attempt's ``started_at``.
======================  =========================================================

Two of the six were already structural, which is worth saying plainly: the
state machine's shape had the closed-book rule half-implemented by accident,
and the half it had was the half nobody wrote down.

**Grading and passing stay separate.** The Evaluator grades criteria;
``studium.assessment.compute_pass`` decides the pass. Data layer §3's first
invariant, and it is what stops a model from credentialing anyone.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.assessment import compute_pass, grade_attempt, is_fully_graded

log = logging.getLogger(__name__)

#: The session mode §11.2 names. Reserved in data layer §6.0's ``session_mode``
#: enum since v1.0, unused until now.
MODE = "summative_assessment"

#: §11.2's capabilities, and whether a summative session has them. Written as
#: data so :func:`assert_closed_book` cannot drift from the prose, and so a new
#: capability added later fails loudly here rather than quietly defaulting to
#: allowed.
CLOSED_BOOK: dict[str, bool] = {
    "primitives": False,
    "learner_retrieval": False,
    "citations": False,
    "concept_graph": False,
    "hints": False,
    "interrupts": False,
    "tutor": False,
    "revision": False,
    # The system's own retrieval continues: §11.2 is explicit that "grading
    # needs source material for reference". This is the one capability whose
    # value differs between the learner and the Evaluator, which is why the key
    # says whose retrieval it is.
    "evaluator_retrieval": True,
}

#: §11.5's retake floor.
RETAKE_COOLDOWN_DAYS = 7

#: §11.5's manual guard: a retake must not repeat problems, so the pool has to
#: hold more than one attempt's worth. Two attempts' worth is the minimum that
#: makes the *first* retake possible at all.
MIN_POOL_MULTIPLE = 2


class ClosedBookViolation(PermissionError):
    """A capability was requested that §11.2 disables during assessment.

    A ``PermissionError`` rather than a ``ValueError``: the request is
    well-formed and the answer is no. The API layer turns it into a 403 with
    §11.2's message.
    """

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(
            f"{capability} is not available during assessment. Summative "
            f"assessment is closed-book (§11.2); softening it would make the "
            f"credential meaningless."
        )


class AssessmentError(ValueError):
    """An assessment definition or attempt is not usable."""


def assert_closed_book(mode: str | None, capability: str) -> None:
    """Refuse a capability §11.2 disables. No-op outside assessment mode.

    Called by the read endpoints and the primitive dispatcher. Unknown
    capabilities raise rather than defaulting to allowed: a capability added to
    the product that nobody classified is exactly the one that quietly leaks
    the answer.
    """
    if mode != MODE:
        return
    if capability not in CLOSED_BOOK:
        raise AssessmentError(
            f"capability {capability!r} is not classified in "
            f"summative.CLOSED_BOOK. Add it there deliberately -- defaulting "
            f"an unclassified capability to allowed is how the closed-book "
            f"rule erodes."
        )
    if not CLOSED_BOOK[capability]:
        raise ClosedBookViolation(capability)


# --- §11.3 assessment definitions ------------------------------------------


@dataclass(frozen=True, slots=True)
class Problem:
    """One problem in an assessment definition."""

    id: str
    concept: str
    prompt: str
    expected_key_points: tuple[str, ...]
    rubric_criterion_id: uuid.UUID | None = None
    max_score: int = 4
    time_estimate_minutes: int = 8


@dataclass(frozen=True, slots=True)
class AssessmentDefinition:
    """§11.3: a named collection of problems bound to a subject."""

    slug: str
    subject: str
    title: str
    concepts: tuple[str, ...]
    problems: tuple[Problem, ...]
    passing_threshold: float = 0.70
    time_limit_minutes: int | None = None
    path: Path | None = None

    @property
    def estimated_minutes(self) -> int:
        return sum(p.time_estimate_minutes for p in self.problems)

    def problem(self, problem_id: str) -> Problem:
        for problem in self.problems:
            if problem.id == problem_id:
                return problem
        raise AssessmentError(f"no problem {problem_id!r} in {self.slug}")


def parse_definition(path: Path) -> AssessmentDefinition:
    """Parse one ``content/subjects/{slug}/assessments/*.yaml`` file (§11.3)."""
    from studium.eval.datasets import DatasetError, load_yaml

    raw = load_yaml(path)
    problems_raw = raw.get("problems")
    header = raw.get("assessment")
    problems: list[Problem] = []
    issues: list[str] = []

    if not isinstance(header, Mapping):
        raise DatasetError(["missing top-level 'assessment:' mapping"], path=path)
    if not isinstance(problems_raw, list) or not problems_raw:
        issues.append("problems: must be a non-empty list")
        problems_raw = []

    threshold = float(header.get("passing_threshold", 0.70))
    if not 0.0 < threshold <= 1.0:
        issues.append(f"passing_threshold must be in (0, 1], got {threshold}")

    time_limit = header.get("time_limit_minutes")
    if time_limit is not None and (not isinstance(time_limit, int) or time_limit < 1):
        issues.append(f"time_limit_minutes must be a positive integer, got {time_limit!r}")
        time_limit = None

    seen: set[str] = set()
    for index, raw_problem in enumerate(problems_raw):
        if not isinstance(raw_problem, Mapping):
            issues.append(f"problem {index}: expected a mapping")
            continue
        problem_id = str(raw_problem.get("id", "")).strip()
        if not problem_id:
            issues.append(f"problem {index}: id is required")
            continue
        if problem_id in seen:
            issues.append(f"problem {problem_id!r} is declared twice")
        seen.add(problem_id)

        key_points = raw_problem.get("expected_key_points") or []
        if not isinstance(key_points, list) or not key_points:
            # Without key points the Evaluator grades against nothing and
            # returns a number anyway -- the same failure meta-grading has, and
            # here it decides whether someone gets credentialed.
            issues.append(
                f"problem {problem_id!r}: expected_key_points must be a "
                f"non-empty list; the Evaluator grades against them"
            )

        criterion = raw_problem.get("rubric_criterion_id")
        criterion_id: uuid.UUID | None = None
        if criterion:
            try:
                criterion_id = uuid.UUID(str(criterion))
            except ValueError:
                issues.append(
                    f"problem {problem_id!r}: rubric_criterion_id "
                    f"{criterion!r} is not a UUID"
                )

        problems.append(
            Problem(
                id=problem_id,
                concept=str(raw_problem.get("concept", "")),
                prompt=str(raw_problem.get("prompt", "")).strip(),
                expected_key_points=tuple(str(k) for k in key_points),
                rubric_criterion_id=criterion_id,
                max_score=int(raw_problem.get("max_score", 4)),
                time_estimate_minutes=int(raw_problem.get("time_estimate_minutes", 8)),
            )
        )

    concepts = tuple(str(c) for c in (header.get("concepts") or []))
    if not concepts:
        issues.append("assessment.concepts must name at least one concept")

    unknown = {p.concept for p in problems} - set(concepts)
    if unknown:
        # A problem on a concept the assessment does not claim to cover means
        # either the concept list or the problem is wrong, and a credential
        # naming the wrong concepts is a false claim the signature would make
        # tamper-evident but not untrue.
        issues.append(
            f"problems reference concept(s) {sorted(unknown)} not listed in "
            f"assessment.concepts"
        )

    if time_limit and problems:
        estimated = sum(p.time_estimate_minutes for p in problems)
        if estimated > time_limit:
            issues.append(
                f"problems estimate {estimated} minutes against a "
                f"{time_limit}-minute limit; §11.2 collects unsubmitted "
                f"responses as-is on expiry, so this guarantees a truncated "
                f"attempt"
            )

    if issues:
        raise DatasetError(issues, path=path)

    return AssessmentDefinition(
        slug=str(header.get("slug", "")),
        subject=str(header.get("subject", "")),
        title=str(header.get("title", "")),
        concepts=concepts,
        problems=tuple(problems),
        passing_threshold=threshold,
        time_limit_minutes=time_limit,
        path=path,
    )


def parse_definitions(directory: Path) -> list[AssessmentDefinition]:
    from studium.eval.datasets import DatasetError, yaml_files

    definitions: list[AssessmentDefinition] = []
    problems: list[str] = []
    for path in yaml_files(directory):
        try:
            definitions.append(parse_definition(path))
        except DatasetError as exc:
            problems.extend(f"{path.name}: {p}" for p in exc.problems)
    if problems:
        raise DatasetError(problems, path=directory)
    return definitions


# --- §11.1 trigger and §11.5 retake policy ---------------------------------


@dataclass(frozen=True, slots=True)
class Eligibility:
    """Whether a learner may start an attempt now, and why not if not."""

    allowed: bool
    reasons: tuple[str, ...] = ()
    next_eligible_at: dt.datetime | None = None

    def render(self) -> str:
        if self.allowed:
            return "eligible"
        return "\n".join(f"  - {reason}" for reason in self.reasons)


def check_eligibility(
    session: Session,
    *,
    user_id: uuid.UUID,
    learner_subject_id: uuid.UUID,
    definition: AssessmentDefinition,
    now: dt.datetime | None = None,
) -> Eligibility:
    """§11.5's retake policy, plus §11.2's pool guard.

    Two rules, and the second is the interesting one. The cooldown is
    mechanical. The pool guard is §11.5's "if the pool is small enough that a
    retake would repeat problems, the reviewer must expand the pool before
    retakes are permitted" -- described there as "a manual guard rather than an
    automated policy", which read literally means nothing checks it. Checked
    here anyway: a retake that silently repeats the problems the learner
    already saw is no longer assessment on unseen material, and §3 makes
    unseen material the thing that separates a credential from a transcript.
    Automating the *check* does not automate the fix; expanding the pool is
    still the reviewer's work.
    """
    now = now or dt.datetime.now(dt.UTC)
    reasons: list[str] = []
    next_eligible: dt.datetime | None = None

    previous = session.execute(
        sql(
            """
            SELECT id, started_at, submitted_at, passed
              FROM assessment_attempts
             WHERE user_id = :user_id
               AND learner_subject_id = :lsid
               AND mode = 'summative'
             ORDER BY started_at DESC
             LIMIT 1
            """
        ),
        {"user_id": user_id, "lsid": learner_subject_id},
    ).one_or_none()

    if previous is not None and previous.submitted_at is not None:
        elapsed = now - _aware(previous.submitted_at)
        cooldown = dt.timedelta(days=RETAKE_COOLDOWN_DAYS)
        if elapsed < cooldown:
            next_eligible = _aware(previous.submitted_at) + cooldown
            reasons.append(
                f"the last attempt was {elapsed.days} day(s) ago; §11.5 sets a "
                f"{RETAKE_COOLDOWN_DAYS}-day minimum between attempts "
                f"(eligible {next_eligible:%Y-%m-%d})"
            )
        if previous.passed:
            reasons.append(
                "this assessment has already been passed; a further attempt "
                "would issue a second credential for the same demonstration"
            )

    if previous is not None:
        pool = len(definition.problems)
        needed = _criteria_per_attempt(definition) * MIN_POOL_MULTIPLE
        if pool < needed:
            reasons.append(
                f"the problem pool holds {pool} problem(s) and a retake needs "
                f"at least {needed} to avoid repeating what the learner has "
                f"already seen (§11.5). The reviewer must expand the pool."
            )

    return Eligibility(
        allowed=not reasons, reasons=tuple(reasons), next_eligible_at=next_eligible
    )


def _criteria_per_attempt(definition: AssessmentDefinition) -> int:
    """How many problems one attempt draws. Every problem, at MVP.

    §11.3's definition has no sampling parameter, so an attempt is the whole
    pool. That makes the retake guard above bite immediately, which is correct
    and is the point: a pool with no spare problems cannot support a retake,
    and saying so at the first retake is better than discovering it after
    issuing a credential for a repeat.
    """
    return len(definition.problems)


# --- attempts --------------------------------------------------------------


@dataclass
class Attempt:
    """A live summative attempt."""

    id: uuid.UUID
    definition: AssessmentDefinition
    user_id: uuid.UUID
    learner_subject_id: uuid.UUID
    session_id: uuid.UUID | None
    started_at: dt.datetime
    threshold: float
    answered: set[str] = field(default_factory=set)

    @property
    def remaining(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.definition.problems if p.id not in self.answered)

    def time_remaining(self, now: dt.datetime | None = None) -> dt.timedelta | None:
        """§11.2's optional timer. ``None`` when the assessment is untimed."""
        if not self.definition.time_limit_minutes:
            return None
        now = now or dt.datetime.now(dt.UTC)
        deadline = self.started_at + dt.timedelta(
            minutes=self.definition.time_limit_minutes
        )
        return max(dt.timedelta(0), deadline - now)

    def expired(self, now: dt.datetime | None = None) -> bool:
        remaining = self.time_remaining(now)
        return remaining is not None and remaining <= dt.timedelta(0)


def start_attempt(
    session: Session,
    *,
    definition: AssessmentDefinition,
    user_id: uuid.UUID,
    learner_subject_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    triggered_by: str = "learner_initiated",
    now: dt.datetime | None = None,
) -> Attempt:
    """Open an attempt (§11.1). Checks eligibility first.

    ``triggered_by`` maps §11.1's two paths onto the existing
    ``assessment_trigger`` enum: a learner starting one from the desk is
    ``learner_initiated``, and the Curator's milestone offer is ``unit_gate``.
    The enum predates this spec and already had both; no addition needed.
    """
    now = now or dt.datetime.now(dt.UTC)
    eligibility = check_eligibility(
        session,
        user_id=user_id,
        learner_subject_id=learner_subject_id,
        definition=definition,
        now=now,
    )
    if not eligibility.allowed:
        raise AssessmentError(
            "cannot start this assessment:\n" + eligibility.render()
        )

    attempt_id = session.execute(
        sql(
            """
            INSERT INTO assessment_attempts
                (user_id, learner_subject_id, mode, triggered_by, threshold,
                 proctored, started_at, session_id)
            VALUES
                (:user_id, :lsid, 'summative',
                 CAST(:trigger AS assessment_trigger), :threshold, TRUE,
                 :started_at, :session_id)
            RETURNING id
            """
        ),
        {
            "user_id": user_id,
            "lsid": learner_subject_id,
            "trigger": triggered_by,
            # Snapshotted from the definition, not read live at grading time:
            # a reviewer editing passing_threshold next month must not
            # retroactively flip a historical pass. Matches how
            # assessment_attempts.threshold already works for the subject-level
            # default.
            "threshold": definition.passing_threshold,
            # proctored=TRUE is what §11.2's conditions amount to in the
            # schema's vocabulary: the supports were withdrawn.
            "started_at": now,
            "session_id": session_id,
        },
    ).scalar_one()

    return Attempt(
        id=attempt_id,
        definition=definition,
        user_id=user_id,
        learner_subject_id=learner_subject_id,
        session_id=session_id,
        started_at=now,
        threshold=definition.passing_threshold,
    )


def submit_response(
    session: Session,
    attempt: Attempt,
    *,
    problem_id: str,
    response: str,
    now: dt.datetime | None = None,
) -> uuid.UUID:
    """Record one answer. Single submission, no revision (§11.2).

    Refuses a second submission for the same criterion here *and* relies on
    ``uq_assessment_responses_pair`` underneath. Two guards because they fail
    differently: this one tells the learner what happened, and the constraint
    catches a concurrent double-submit that passed the check.

    No verdict is returned. §11.4: "the learner sees only the final scores per
    criterion after all criteria are submitted". Returning one here would let a
    learner infer a wrong answer and change their approach to the remaining
    problems, which is the open-book feedback loop assessment removes.
    """
    problem = attempt.definition.problem(problem_id)

    if problem_id in attempt.answered:
        raise ClosedBookViolation("revision")

    criterion_id, snapshot = _criterion(session, problem)

    response_id = session.execute(
        sql(
            """
            INSERT INTO assessment_responses
                (attempt_id, rubric_criterion_id, criterion_snapshot,
                 prompt_shown, learner_response)
            VALUES
                (:attempt_id, :criterion_id, CAST(:snapshot AS jsonb),
                 :prompt, :response)
            RETURNING id
            """
        ),
        {
            "attempt_id": attempt.id,
            "criterion_id": criterion_id,
            "snapshot": _json(snapshot),
            "prompt": problem.prompt,
            "response": response,
        },
    ).scalar_one()

    attempt.answered.add(problem_id)
    return response_id


def collect_expired(
    session: Session, attempt: Attempt, *, now: dt.datetime | None = None
) -> list[uuid.UUID]:
    """§11.2: "on expiration, unsubmitted responses are collected as-is".

    Writes an empty response for every unanswered problem, so the attempt is
    fully graded rather than perpetually incomplete. An empty answer grades 0,
    which is the honest outcome -- the alternative, excluding unanswered
    criteria from the weighted mean, would let a learner who answered one
    problem well and ran out of time score higher than one who attempted
    everything.
    """
    now = now or dt.datetime.now(dt.UTC)
    if not attempt.expired(now):
        return []

    written: list[uuid.UUID] = []
    for problem in attempt.remaining:
        written.append(
            submit_response(
                session, attempt, problem_id=problem.id, response="", now=now
            )
        )
    return written


def finalize(
    session: Session,
    attempt: Attempt,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Score the attempt and issue a credential if it passed (§11.5).

    The Evaluator has already graded each response by this point; this is the
    arithmetic and the consequence. ``grade_attempt`` computes the weighted
    mean and ``compute_pass`` decides the pass -- neither is a model call, per
    data layer §3's first invariant.

    A signing failure does not fail the attempt (§16). The pass is recorded
    either way and the credential is retried as a background job, because the
    learner did the work and an operational problem on our side must not
    unmake that.
    """
    now = now or dt.datetime.now(dt.UTC)

    if not is_fully_graded(session, attempt.id):
        raise AssessmentError(
            f"attempt {attempt.id} has ungraded responses; the Evaluator must "
            f"visit every criterion before the attempt is scored"
        )

    session.execute(
        sql(
            "UPDATE assessment_attempts SET submitted_at = COALESCE(submitted_at, :now)"
            " WHERE id = :id"
        ),
        {"id": attempt.id, "now": now},
    )
    row = grade_attempt(session, attempt.id)
    row.graded_at = now
    session.flush()

    passed = compute_pass(row.score, float(row.threshold))
    result: dict[str, Any] = {
        "attempt_id": attempt.id,
        "score": float(row.score or 0.0),
        "threshold": float(row.threshold),
        "passed": bool(passed),
        "portfolio_item_id": None,
        "credential_error": None,
    }

    if not passed:
        # §11.5: no portfolio item, the learner sees what they missed, and the
        # Curator schedules review. The last is the Curator's own work, driven
        # by the mastery evidence the grading already wrote.
        return result

    from studium.eval.credentials import SigningKeyUnavailable, issue_credential

    try:
        result["portfolio_item_id"] = issue_credential(
            session,
            user_id=attempt.user_id,
            learner_subject_id=attempt.learner_subject_id,
            subject_slug=attempt.definition.subject,
            kind="assessment_pass",
            score=float(row.score or 0.0),
            passing_threshold=float(row.threshold),
            criteria_results=_criteria_results(session, attempt.id),
            assessment_id=attempt.id,
            session_id=attempt.session_id,
            title=f"{attempt.definition.title}",
        )
    except SigningKeyUnavailable as exc:
        log.error("credential deferred for attempt %s: %s", attempt.id, exc)
        result["credential_error"] = str(exc)

    return result


def _criteria_results(session: Session, attempt_id: uuid.UUID) -> list[dict[str, Any]]:
    """§12.2's ``criteria_results`` breakdown.

    Carries the criterion identity, its weight and the grade. **Not**
    ``key_points`` or ``missing_points``: a credential is published to whoever
    the learner shows it to, and §11 forbids key points reaching even the
    learner. Putting them in a signed, externally verifiable document would
    publish the rubric's answers to the world and make every future attempt at
    this assessment open-book.
    """
    rows = session.execute(
        sql(
            """
            SELECT rc.slug, rc.weight, r.grade
              FROM assessment_responses r
              JOIN rubric_criteria rc ON rc.id = r.rubric_criterion_id
             WHERE r.attempt_id = :id
             ORDER BY rc.slug
            """
        ),
        {"id": attempt_id},
    ).all()
    return [
        {"criterion": row.slug, "weight": int(row.weight), "grade": int(row.grade or 0)}
        for row in rows
    ]


def _criterion(session: Session, problem: Problem) -> tuple[uuid.UUID, dict[str, Any]]:
    """Resolve a problem to its rubric criterion and snapshot it.

    The snapshot is what makes a three-week-old attempt still show the
    criterion the learner was actually assessed against (data layer §6.8), and
    it carries ``key_points`` -- which ``acl.project_assessment_response``
    strips before it reaches a learner.
    """
    if problem.rubric_criterion_id is None:
        raise AssessmentError(
            f"problem {problem.id!r} has no rubric_criterion_id; §11.3 binds "
            f"every problem to a criterion, and grading writes to "
            f"assessment_responses.rubric_criterion_id which is NOT NULL"
        )

    row = session.execute(
        sql(
            """
            SELECT id, slug, prompt, key_points, weight, min_words,
                   status::text AS status
              FROM rubric_criteria WHERE id = :id
            """
        ),
        {"id": problem.rubric_criterion_id},
    ).one_or_none()
    if row is None:
        raise AssessmentError(
            f"problem {problem.id!r} names rubric criterion "
            f"{problem.rubric_criterion_id} which does not exist"
        )

    return row.id, {
        "id": str(row.id),
        "slug": row.slug,
        "prompt": row.prompt,
        "key_points": row.key_points,
        "weight": int(row.weight),
        "min_words": int(row.min_words),
        "status": row.status,
        # What the definition asked for, alongside what the criterion says.
        # They can disagree -- the YAML is authored separately from the rubric
        # rows -- and a reviewer looking at an old attempt needs to see both.
        "definition_key_points": list(problem.expected_key_points),
    }


def _json(value: Mapping[str, Any]) -> str:
    import json

    from studium.asyncdb import jsonable

    return json.dumps(jsonable(dict(value)), ensure_ascii=False)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def load_attempt(
    session: Session, attempt_id: uuid.UUID, definition: AssessmentDefinition
) -> Attempt:
    """Rehydrate an attempt after a disconnect (§16 row 4).

    "Session state preserves; on reconnect, the learner can resume. Time budget
    continues to count against the total." The second sentence is why
    ``started_at`` is read from the row rather than reset: a learner who
    disconnects with five minutes left resumes with five minutes left, and a
    reset would turn a dropped connection into a way to buy time.
    """
    row = session.execute(
        sql(
            """
            SELECT id, user_id, learner_subject_id, session_id, started_at,
                   threshold, submitted_at
              FROM assessment_attempts WHERE id = :id AND mode = 'summative'
            """
        ),
        {"id": attempt_id},
    ).one_or_none()
    if row is None:
        raise AssessmentError(f"no summative attempt {attempt_id}")

    answered = {
        r.slug
        for r in session.execute(
            sql(
                """
                SELECT rc.slug
                  FROM assessment_responses r
                  JOIN rubric_criteria rc ON rc.id = r.rubric_criterion_id
                 WHERE r.attempt_id = :id
                """
            ),
            {"id": attempt_id},
        ).all()
    }
    # Criterion slugs back to problem ids, so `remaining` is computed in the
    # definition's vocabulary rather than the schema's.
    by_criterion = {
        str(p.rubric_criterion_id): p.id for p in definition.problems
    }
    answered_problems = {
        by_criterion[str(cid)]
        for cid in _answered_criterion_ids(session, attempt_id)
        if str(cid) in by_criterion
    }

    return Attempt(
        id=row.id,
        definition=definition,
        user_id=row.user_id,
        learner_subject_id=row.learner_subject_id,
        session_id=row.session_id,
        started_at=_aware(row.started_at),
        threshold=float(row.threshold),
        answered=answered_problems or answered,
    )


def _answered_criterion_ids(session: Session, attempt_id: uuid.UUID) -> Sequence[Any]:
    return [
        r.rubric_criterion_id
        for r in session.execute(
            sql(
                "SELECT rubric_criterion_id FROM assessment_responses "
                "WHERE attempt_id = :id"
            ),
            {"id": attempt_id},
        ).all()
    ]
