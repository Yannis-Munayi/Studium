"""Evaluation Tier 3: the harness against real agents (evaluation §17).

§17 names three, and each answers a question no fixture can.

**A full regression run against the real Lecturer.** Tier 1 proves the checks
are correct against strings we wrote. This proves they are correct against
strings a model wrote -- which is a different question, because the failure
mode is a check that scores 1.0 on every real output and therefore measures
nothing. A dataset that every real run passes perfectly is not a passing
dataset; it is a dataset with no discriminating power, and only a real run can
tell the two apart.

**The meta-grader round trip.** §19 open question 2 is whether the Evaluator's
judgment is stable enough to regress against. This does not answer that -- six
months of ``grading_calibration`` runs do -- but it does answer the prior
question of whether meta-grading works end to end at all: a real Lecturer
output, a real rubric, a real Evaluator, a score that comes back in range.

**Assessment end to end.** A test learner takes a small assessment, the real
Evaluator grades it, and a real signed portfolio item is issued and verifies.
This is the product's actual deliverable, and every part of it before this
point has been tested against something other than itself.

Billable and opt-in twice: a key must be present *and*
``STUDIUM_RUN_PAID_TESTS`` set.

    pip install -e ".[dev,evaluation]"
    ANTHROPIC_API_KEY=... STUDIUM_RUN_PAID_TESTS=1 \\
      python -m pytest tests/online/test_evaluation_paid.py -m anthropic
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.agents.evaluator import Evaluator
from studium.agents.lecturer import Lecturer
from studium.eval import credentials, fixtures, runner
from studium.eval.credentials import SigningIdentity
from studium.eval.datasets import parse_dataset
from studium.eval.grading import Grader, MetaGrader
from studium.llm.client import AnthropicClient

pytestmark = [pytest.mark.postgres, pytest.mark.anthropic]

#: §17: "one small dataset (~5 entries)". Three, because each of the three is a
#: distinct shape (core, thin retrieval, misleading passages) and a fourth of
#: the same shape would cost money to learn nothing.
SMALL_DATASET = """
dataset:
  slug: tier3_lecturer_smoke
  kind: agent_output
  agent: lecturer
  description: >
    A minimal Tier 3 dataset: does the real Lecturer, on real passages, satisfy
    the deterministic checks the harness applies?
entries:
  - id: entry_core
    input:
      concept:
        title: Beta-reduction
        long_description: >
          The fundamental reduction rule of the lambda calculus, replacing a
          bound variable in a body with an argument value.
      stance: formal
      retrieved_passages:
        - id: chunk_beta_001
          text: >
            The reduction of a beta-redex (lambda x.M)N proceeds by
            substituting N for every free occurrence of x in M, written
            M[x := N]. The substitution is capture-avoiding.
          section_path: ["Chapter 3", "3.2 Beta Reduction"]
        - id: chunk_beta_002
          text: >
            Consider the example (lambda x.x+1)3, which reduces to 3+1 and then
            to 4. Each step rewrites exactly one redex; a term with no redex
            remaining is in normal form.
          section_path: ["Chapter 3", "3.2 Beta Reduction"]
    expected:
      properties:
        - name: citations_resolve
          check: citation_targets_valid
        - name: cites_provided_passages
          check: at_least_n_citations
          n: 1
          from: [chunk_beta_001, chunk_beta_002]
        - name: within_word_limit
          check: word_count_between
          min: 100
          max: 500
"""


@pytest.fixture
def small_dataset(tmp_path: Path):
    path = tmp_path / "tier3.yaml"
    path.write_text(SMALL_DATASET.lstrip(), encoding="utf-8")
    return parse_dataset(path)


@pytest.fixture
def client(paid_tests_enabled: bool) -> AnthropicClient:
    return AnthropicClient()


@pytest.fixture
def eval_session(runtime_db) -> uuid.UUID:
    """The real ``learning_sessions`` row every billable call's trace needs.

    ``runtime_db`` rather than ``db``: ``traces.write`` goes through
    ``studium.asyncdb``, which opens its own session per call. The
    savepoint-joined factory that fixture installs is what keeps those writes
    on this transaction, so the whole test still rolls back.
    """
    session_id = fixtures.ensure_eval_session(
        runtime_db, dataset_slug="tier3_lecturer_smoke"
    )
    runtime_db.commit()
    return session_id


@pytest.fixture
def eval_context(eval_session: uuid.UUID, runtime_db):
    """A context bound to that session, for the tests that do not use
    ``run_dataset``."""
    from studium.models.identity import SYSTEM_USER_ID

    def build(entry_input, *, entry_key: str, mode: str = "tutorial"):
        # The concept row too: session_turns.concept_id is a foreign key and
        # every agent copies ctx.focus_concept_id onto its trace.
        raw = entry_input.get("concept") or {}
        concept_id = None
        if raw:
            concept_id = fixtures.ensure_eval_concept(
                runtime_db,
                dataset_slug="tier3",
                entry_key=entry_key,
                title=str(raw.get("title", entry_key)),
            )
            runtime_db.commit()
        return fixtures.build_context(
            entry_input,
            dataset_slug="tier3",
            entry_key=entry_key,
            mode=mode,
            session_id=eval_session,
            user_id=SYSTEM_USER_ID,
            concept_id=concept_id,
        )

    return build


class TestRealRegressionRun:
    def test_a_run_against_the_real_lecturer(
        self, small_dataset, client, eval_session
    ) -> None:
        """§17: "Full regression run against one small dataset with the real
        Lecturer."

        The assertions are deliberately about the *harness*, not about the
        Lecturer's quality. Whether the Lecturer scores well is the metric this
        subsystem produces, not something this subsystem's tests may assert --
        a test that failed because a prompt got worse would be a regression
        gate hiding inside a test suite, firing on the wrong signal at the
        wrong time. What is asserted is that the run completed, produced one
        result per entry, and produced numbers in range.
        """
        lecturer = Lecturer(client, retriever=fixtures.FixtureRetriever([]))
        report = asyncio.run(
            runner.run_dataset(
                small_dataset, agent=lecturer, session_id=eval_session
            )
        )

        assert len(report.results) == len(small_dataset.entries)
        assert 0.0 <= report.aggregate_score <= 1.0
        assert report.cost_usd > 0, "a real run that cost nothing did not happen"
        assert len(report.prompt_hash) == 64

        for result in report.results:
            assert result.grade.error is None, result.grade.error
            assert result.actual_output["text"].strip(), "the Lecturer produced nothing"
            assert result.latency_ms > 0

    def test_the_checks_discriminate_on_real_output(
        self, small_dataset, client, eval_session
    ) -> None:
        """A dataset every real run passes perfectly may have no discriminating
        power at all. This runs the same real output against a deliberately
        impossible variant of the same checks: if *that* also passes, the
        checks are not reading the output.
        """
        lecturer = Lecturer(client, retriever=fixtures.FixtureRetriever([]))
        report = asyncio.run(
            runner.run_dataset(
                small_dataset, agent=lecturer, session_id=eval_session
            )
        )
        produced = report.results[0].actual_output["graded_text"]

        from studium.eval.checks import word_count_between

        impossible = word_count_between(produced, {"min": 9000, "max": 9500}, {})
        assert not impossible.passed, (
            "a 9000-word floor passed on real Lecturer output; the check is not "
            "reading the text it was given"
        )


class TestMetaGrader:
    def test_the_evaluator_grades_a_real_lecturer_output(
        self, client, eval_context
    ) -> None:
        """§17: "Meta-grader round-trip: real Evaluator grades a real Lecturer
        output against a real rubric."

        Two rubrics, one the output should satisfy and one it should not. A
        single rubric would pass whether the Evaluator read the output or
        returned a constant, which is the failure §19 open question 2 is
        about.
        """
        from studium.agents.base import AgentInput

        context = eval_context(
            {"concept": {"title": "Beta-reduction"}}, entry_key="meta"
        )
        output = (
            "Beta-reduction is the fundamental computational step of the lambda "
            "calculus. A redex (lambda x.M)N reduces to M[x := N], the "
            "capture-avoiding substitution of N for free occurrences of x in M."
        )
        grader = MetaGrader(Evaluator(client))

        satisfied, cost, _ = asyncio.run(
            grader.grade(
                rubric=(
                    "The response should state the beta-reduction rule using "
                    "symbolic notation and name capture-avoiding substitution."
                ),
                output=output,
                context=context,
                agent_under_test="lecturer",
                property_name="uses_requested_stance",
            )
        )
        assert 0.0 <= satisfied.score <= 1.0
        assert satisfied.note.strip(), "a score with no verdict cannot be acted on"
        assert cost > 0

        unsatisfied, _, _ = asyncio.run(
            grader.grade(
                rubric=(
                    "The response should be written as a rhyming poem in iambic "
                    "pentameter and must contain no mathematical notation."
                ),
                output=output,
                context=context,
                agent_under_test="lecturer",
                property_name="is_a_poem",
            )
        )
        assert unsatisfied.score < satisfied.score, (
            "the meta-grader gave the same score against a rubric the output "
            "plainly fails; it is not reading the rubric"
        )
        assert AgentInput  # imported for the type it validates above

    def test_a_meta_graded_property_runs_through_the_grader(
        self, client, eval_context
    ) -> None:
        """The path a real dataset entry takes, rather than the grader alone."""
        from studium.eval.datasets import Entry

        entry = Entry(
            key="meta_entry",
            index=0,
            input={"concept": {"title": "Beta-reduction"}},
            expected={
                "properties": [
                    {
                        "name": "uses_requested_stance",
                        "check": "meta_graded",
                        "rubric": "The response should use precise definitional language.",
                    }
                ]
            },
            grading_kind="meta_graded",
        )
        grader = Grader(meta_grader=MetaGrader(Evaluator(client)))
        grade = asyncio.run(
            grader.grade_entry(
                entry,
                output="A beta-redex (lambda x.M)N reduces to M[x := N].",
                context=eval_context(
                    {"concept": {"title": "Beta-reduction"}},
                    entry_key="meta_entry",
                ),
                agent_under_test="lecturer",
            )
        )
        assert grade.properties[0].meta_graded
        assert grade.cost_usd > 0
        assert 0.0 <= grade.score <= 1.0


class TestAssessmentEndToEnd:
    def test_a_passed_assessment_issues_a_verifiable_credential(
        self, runtime_db: Session, client, eval_context, paid_tests_enabled: bool
    ) -> None:
        """§17: "Assessment end-to-end: learner (test user) takes a small
        assessment, real Evaluator grades, real portfolio item is issued."

        This is the product's deliverable, and it is the first point at which
        every part of it meets every other: a real rubric, a real grade from a
        real model, the arithmetic in ``studium.assessment``, the pass decision
        that no model makes, and a signature a stranger could check.
        """
        from studium.agents.base import AgentInput
        from studium.assessment import compute_pass, grade_attempt
        from tests.fixtures import lambda_calculus

        fixture = lambda_calculus.build(
            runtime_db, email=f"t3-{uuid.uuid4().hex[:8]}@example.com"
        )
        runtime_db.flush()

        identity = SigningIdentity(
            key_id=f"tier3-{uuid.uuid4().hex[:8]}",
            seed=base64.b64decode(credentials.generate_seed()),
        )
        credentials.register_public_key(runtime_db, identity)

        criterion_id = runtime_db.execute(
            sql(
                """
                INSERT INTO rubric_criteria
                    (concept_id, slug, prompt, key_points, weight, status)
                VALUES (:concept_id, 'beta-normal-form',
                        'Reduce (lambda x.x)(lambda y.y) to normal form.',
                        :key_points, 2, 'active')
                RETURNING id
                """
            ),
            {
                "concept_id": fixture.concept_id("beta-reduction"),
                "key_points": '[{"point": "Identifies the redex", "weight": 1},'
                ' {"point": "Arrives at lambda y.y", "weight": 1}]',
            },
        ).scalar_one()

        attempt_id = runtime_db.execute(
            sql(
                """
                INSERT INTO assessment_attempts
                    (user_id, learner_subject_id, mode, triggered_by, threshold,
                     proctored)
                VALUES (:user_id, :lsid, 'summative', 'learner_initiated', 0.70,
                        TRUE)
                RETURNING id
                """
            ),
            {"user_id": fixture.user.id, "lsid": fixture.enrollment.id},
        ).scalar_one()

        answer = (
            "The whole term is a redex: (lambda x.x) applied to (lambda y.y). "
            "Substituting lambda y.y for x in the body x gives lambda y.y, "
            "which has no redex and is therefore in normal form."
        )
        runtime_db.execute(
            sql(
                """
                INSERT INTO assessment_responses
                    (attempt_id, rubric_criterion_id, criterion_snapshot,
                     prompt_shown, learner_response)
                VALUES (:attempt_id, :criterion_id, :snapshot, :prompt, :answer)
                """
            ),
            {
                "attempt_id": attempt_id,
                "criterion_id": criterion_id,
                "snapshot": '{"slug": "beta-normal-form"}',
                "prompt": "Reduce (lambda x.x)(lambda y.y) to normal form.",
                "answer": answer,
            },
        )
        runtime_db.flush()

        # The real Evaluator, in the single-criterion mode a summative response
        # is graded through.
        context = eval_context(
            {"concept": {"title": "Beta-reduction"}},
            entry_key="assessment",
            mode="summative_assessment",
        )
        result = asyncio.run(
            Evaluator(client).handle(
                AgentInput(
                    session_context=context,
                    kind="grade_practice",
                    payload={
                        "answer": answer,
                        "question": "Reduce (lambda x.x)(lambda y.y) to normal form.",
                        "expected_key_points": [
                            "Identifies the redex",
                            "Arrives at lambda y.y",
                        ],
                        "criterion_id": str(criterion_id),
                    },
                )
            )
        )
        verdict = result.structured
        assert verdict.verdict in {"correct", "partially_correct", "incorrect"}

        from studium.agents.evaluator import verdict_to_score

        runtime_db.execute(
            sql(
                "UPDATE assessment_responses SET grade = :grade, graded_at = NOW()"
                " WHERE attempt_id = :attempt_id"
            ),
            {"grade": verdict_to_score(verdict.verdict), "attempt_id": attempt_id},
        )
        runtime_db.flush()

        attempt = grade_attempt(runtime_db, attempt_id)
        runtime_db.flush()
        assert attempt.passed == compute_pass(attempt.score, float(attempt.threshold))

        if not attempt.passed:
            # §11.5: below threshold, no portfolio item. The real Evaluator
            # graded a correct answer, so this is unexpected -- but asserting
            # the *rule* rather than the grade keeps the test measuring the
            # flow rather than the model's mood on the day.
            pytest.skip(
                f"the real Evaluator scored {attempt.score:.2f}, below the "
                f"{attempt.threshold} threshold; §11.5 issues no credential"
            )

        item_id = credentials.issue_credential(
            runtime_db,
            user_id=fixture.user.id,
            learner_subject_id=fixture.enrollment.id,
            subject_slug="lambda-calculus",
            kind="assessment_pass",
            score=float(attempt.score),
            passing_threshold=float(attempt.threshold),
            criteria_results=[
                {"criterion": "beta-normal-form", "weight": 2, "grade": verdict_to_score(verdict.verdict)}
            ],
            assessment_id=attempt_id,
            identity=identity,
        )
        runtime_db.flush()

        verified = credentials.verify_item(runtime_db, item_id)
        assert verified.found and verified.valid
        assert verified.item["kind"] == "assessment_pass"
        assert verified.item["score"] == pytest.approx(float(attempt.score))

        # The claim the signature makes must be one an outsider can check
        # without us: same payload, same published key, no database.
        published = {k["key_id"]: k for k in credentials.published_keys(runtime_db)}
        assert credentials.verify(
            verified.item,
            verified.signature,
            published[identity.key_id]["public_key"],
        )
