"""Cached prefix assembly (agent runtime §17).

Prompt caching here is architectural, not opportunistic: every agent's system
prompt splits into a byte-stable prefix (persona, task instructions, concept
grounding) and a per-turn suffix (recent turns, current input). The prefix is
designed to hit cache on every call after the first within a session.

**Byte-stability is the whole contract.** A prefix that differs by one byte
between two calls silently costs full input price on the second, with no error
and no signal beyond a zero in ``cache_read_input_tokens``. §17 names three
ways that happens and this module closes all three:

* floats with variable trailing digits -> :func:`fmt_float`, fixed precision;
* retrieved passages in varying order -> sorted by ``chunk_id`` before render;
* non-deterministic dict ordering -> :func:`stable_json`, ``sort_keys=True``.

Everything that reaches a prefix goes through those helpers. The offline test
tier asserts ``build_prefix(same_context)`` is byte-identical across calls, so a
regression here fails a test rather than quietly doubling the bill.

Learner- and upload-authored text is wrapped at the prompt boundary by
``studium.acl.wrap_user_content`` (data layer §11) before it enters any prefix.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from studium.acl import wrap_user_content

from .models import TTL, caches_at, spec_for

#: Per-agent cache TTL (§17 "TTL choice").
#:
#: The spec assigns 5m to the Tutor, 1h to Lecturer / Curator /
#: Confusion-Tracker, and no caching to the Orchestrator's intent classifier.
#: It is silent on the Evaluator and the Reviewer; both are given 1h here.
#: A rubric prefix is stable for as long as the rubric is, and a Reviewer's
#: per-concept prefix outlives the gaps between cards in a review session --
#: both make calls across intervals a 5-minute window would not survive.
#: See DIVERGENCES (R3).
AGENT_TTL: dict[str, TTL | None] = {
    "curator": "1h",
    "lecturer": "1h",
    "tutor": "5m",
    "evaluator": "1h",
    "confusion_tracker": "1h",
    "reviewer": "1h",
    # The intent classifier's prompt is ~400 tokens: below every model's cache
    # minimum, so a marker would buy nothing and bill a write.
    "orchestrator": None,
}


class PrefixError(ValueError):
    """A prefix could not be assembled from the context given."""


@dataclass(frozen=True, slots=True)
class CachedPrefix:
    """One agent's cacheable system prefix, plus what it took to build it."""

    agent: str
    text: str
    #: The tuple of values the prefix is byte-stable across, joined and hashed.
    #: Two calls with the same cache_key must produce the same ``text``.
    cache_key: str
    ttl: TTL | None
    model: str

    @property
    def sha256(self) -> str:
        """Digest of the prefix bytes -- the ``agent_traces`` prompt hash."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def estimated_tokens(self) -> int:
        """Rough token count, used only to check the cache minimum.

        Deliberately an estimate: a real ``count_tokens`` call per turn would
        add a round trip to every request to answer a question whose only
        consumer is a diagnostic. ~3.6 chars/token is close enough on English
        prose with code and LaTeX mixed in to tell 800 tokens from 4000.
        """
        return int(len(self.text) / 3.6)

    def will_cache(self) -> bool:
        """Whether this prefix clears the model's minimum cacheable length.

        False means the ``cache_control`` marker is accepted and ignored: the
        call bills at full input price every time. The Confusion-Tracker on
        Haiku 4.5 is the live risk -- its minimum is 4096 tokens (§17 assumes
        caching works and does not mention the floor).
        """
        return self.ttl is not None and caches_at(self.model, self.estimated_tokens())

    def system_blocks(self) -> list[dict[str, Any]]:
        """The ``system`` parameter: one text block, cache-marked when useful.

        The marker is omitted when the prefix cannot cache, so a doomed write
        is never billed for.
        """
        block: dict[str, Any] = {"type": "text", "text": self.text}
        if self.will_cache():
            cache_control: dict[str, Any] = {"type": "ephemeral"}
            if self.ttl == "1h":
                cache_control["ttl"] = "1h"
            block["cache_control"] = cache_control
        return [block]


# --- determinism helpers ---------------------------------------------------


def fmt_float(value: float | None, *, places: int = 4) -> str:
    """Format a float at fixed precision.

    A mastery estimate rendered as ``0.8500000000000001`` on one turn and
    ``0.85`` on the next invalidates the whole prefix after it. Fixed precision
    makes that impossible. ``None`` renders as ``unknown`` rather than an
    empty string, which reads as a missing value to the model.
    """
    return "unknown" if value is None else f"{value:.{places}f}"


def stable_json(obj: Any) -> str:
    """Serialise deterministically: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def as_mapping(passage: Any) -> Mapping[str, Any]:
    """Normalise a passage to a mapping.

    Callers hold ``Passage`` models (from the context) or plain dicts (from a
    retriever or a JSONB round-trip). Accepting both here means the sort key
    and the citation numbering cannot disagree depending on which shape
    happened to reach them -- which would silently attach every citation to the
    wrong chunk, since both lists are the same length.
    """
    return passage if isinstance(passage, Mapping) else passage.model_dump()


def sorted_passages(passages: Iterable[Any]) -> list[Mapping[str, Any]]:
    """Order retrieved passages by ``chunk_id``.

    Retrieval returns by relevance, and relevance ties break arbitrarily
    between calls. Sorting by a stable key costs nothing -- the passages are
    numbered ``[P1]..[Pn]`` for citation, and their order carries no meaning to
    the model beyond that numbering being consistent within a session.
    """
    return sorted((as_mapping(p) for p in passages), key=lambda p: str(p["chunk_id"]))


def render_passages(passages: Sequence[Any]) -> str:
    """Number passages for citation, with source attribution."""
    if not passages:
        return "(no source passages retrieved for this concept)"
    lines = []
    for index, passage in enumerate(sorted_passages(passages), start=1):
        title = passage.get("source_title") or "untitled source"
        page = passage.get("page_start")
        locator = f", p. {page}" if page is not None else ""
        lines.append(f"[P{index}] ({title}{locator})\n{passage['text'].strip()}")
    return "\n\n".join(lines)


def _key(*parts: Any) -> str:
    """Hash the cache-key tuple into a short stable identifier."""
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def grounding_version(
    *,
    subject_updated_at: Any,
    concept_updated_at: Any,
    max_chunk_updated_at: Any = None,
) -> str:
    """Hash of the grounding inputs (§17).

    Bumps whenever the subject, the focus concept, or the chunks behind it
    change -- which is exactly when a cached prefix ought to be discarded
    rather than reused against stale grounding.
    """
    return _key(subject_updated_at, concept_updated_at, max_chunk_updated_at)


# --- shared prompt fragments ----------------------------------------------

_STANCE_INSTRUCTIONS: dict[str, str] = {
    "formal": (
        "Prefer precise definitions and derivations. Use notation from the "
        "sources.\nMotivate before formalizing. If a proof is needed, sketch "
        "structure before\ndetail."
    ),
    "intuitive": (
        "Lead with the idea behind the formalism. Use analogy and picture "
        "before\nnotation, then connect the picture back to the source's "
        "definitions so the\nlearner can move between them."
    ),
    "applied": (
        "Lead with a problem the concept solves. Derive only what the problem "
        "needs,\nand show the concept doing work before generalising it."
    ),
    "historical": (
        "Trace how the idea arrived: what was tried, what failed, what the "
        "concept\nfixed. Keep the chronology in service of the idea, not the "
        "other way round."
    ),
    "default": (
        "Balance motivation and precision. Define carefully, illustrate once, "
        "then\nstate the general form."
    ),
}


def stance_instructions(stance: str) -> str:
    return _STANCE_INSTRUCTIONS.get(stance, _STANCE_INSTRUCTIONS["default"])


def _concept_graph_summary(concepts: Sequence[Mapping[str, Any]]) -> str:
    """Numbered concept list with depth, load-bearing status, prerequisites.

    Sorted by ``position`` then ``slug`` so two calls in the same session
    render the same list even if the query returned rows in a different order.
    """
    if not concepts:
        return "(no concepts authored for this subject)"
    ordered = sorted(concepts, key=lambda c: (c.get("position", 0), str(c["slug"])))
    lines = []
    for index, concept in enumerate(ordered, start=1):
        prereqs = sorted(str(p) for p in concept.get("prerequisite_slugs", []))
        prereq_text = ", ".join(prereqs) if prereqs else "none"
        flag = " [load-bearing]" if concept.get("is_load_bearing") else ""
        lines.append(
            f"{index}. {concept['slug']} -- {concept['title']}{flag}\n"
            f"   depth {concept.get('depth', 1)}; prerequisites: {prereq_text}"
        )
    return "\n".join(lines)


def _objectives(concept: Mapping[str, Any]) -> str:
    meta = concept.get("metadata") or {}
    objectives = meta.get("learning_objectives") or []
    if not objectives:
        return "(no learning objectives authored for this concept)"
    return "\n".join(f"- {o}" for o in objectives)


def _misconceptions(concept: Mapping[str, Any]) -> str:
    meta = concept.get("metadata") or {}
    items = meta.get("common_misconceptions") or []
    if not items:
        return "(none curated)"
    return "\n".join(f"- {m}" for m in items)


# --- per-agent prefixes ----------------------------------------------------
#
# Prompt text is illustrative and versioned; the agent contract is the
# contract (§1). Editing wording here changes behaviour and must go through the
# prompt-regression suite in tests/agents/test_prompt_regression.py.


def _curator_prefix(ctx: Any) -> tuple[str, str]:
    subject = ctx.subject
    learner_prefs = ctx.learner_subject.get("preferences") or {}
    profile = ctx.learner.get("profile") or {}

    text = f"""You are the Curator for the subject "{subject['title']}", version {subject['version']}.
You own the ordering, pacing, and shape of one learner's path through this
subject. You do not teach; you decide what teaching happens next.

SUBJECT SHAPE
{subject.get('long_description') or '(no description authored)'}

CONCEPT GRAPH (topological summary)
{_concept_graph_summary(ctx.subject_concepts)}

LOAD-BEARING CONCEPTS (prioritize mastery here)
{', '.join(sorted(c['slug'] for c in ctx.subject_concepts if c.get('is_load_bearing'))) or '(none marked)'}

THIS LEARNER
Preferred pace: {learner_prefs.get('pace', 'standard')}
Stated goals: {wrap_user_content(profile.get('stated_goals') or '(none stated)')}
Preferred stance (if any): {learner_prefs.get('preferred_stance', 'default')}

DECISION PRINCIPLES
- Prefer concepts whose prerequisites are all above 0.85 mastery (the unlock
  threshold from the data layer's mastery model).
- Prefer concepts with high load-bearing weight before their dependents.
- If mastery has been stagnant on a concept for more than 2 sessions, pivot
  to a different stance or an adjacent concept rather than repeating.
- Session length is a hard budget. Aim for 1-3 concepts per 90 minutes at
  standard depth.
- The learner's stated goals are a soft steer, not a hard constraint. If the
  goal is unreachable without a prerequisite the learner has not mastered,
  say so and route to the prerequisite."""

    return text, _key(subject["id"], subject["version"], ctx.learner["id"])


def _lecturer_prefix(ctx: Any, *, stance: str) -> tuple[str, str]:
    concept = _require_concept(ctx, "lecturer")
    mastered = sorted(
        c["slug"]
        for c in ctx.focus_neighborhood
        if ctx.mastery_snapshot.get(str(c["id"]), 0.0) > 0.85
    )

    text = f"""You are the Lecturer for Studium. You deliver structured exposition of one
concept at a time to a serious adult learner. You are teaching, not chatting.

CONCEPT
{concept['title']}

CONCEPT CONTEXT
{concept.get('long_description') or '(no description authored)'}

Prerequisite concepts the learner has mastered:
{', '.join(mastered) or '(none yet)'}

Related concepts the learner has seen (do not re-teach; may reference):
{', '.join(sorted(ctx.concepts_seen)) or '(none yet)'}

CANONICAL SOURCES (cite from these; do not import outside content)
{render_passages(ctx.passages)}

STANCE
{stance_instructions(stance)}

TEACHING PRINCIPLES
- Start where the learner is. If a prerequisite is at exactly 0.85, remind
  briefly; if it is at 0.99, do not.
- One idea per segment. If the segment would exceed 350 words, split.
- Every non-trivial claim is grounded in the sources listed above. Cite by
  passage number: [P3], [P7-P8].
- Worked examples are complete: state the setup, do each step, name what
  each step accomplishes.
- End every segment with a brief anchor: what was learned, what comes next.
- Do not tell the learner what they already know. Do not restate the concept
  title as a summary.

CONSTRAINTS
- 200-400 words per segment, hard limit.
- Markdown, minimal formatting. Use inline code fences for symbols, math via
  LaTeX in $...$ or $$...$$, tables only when comparing three or more items.
- Never invent citations. If a claim needs a source you do not have, either
  omit the claim or note that this is a broader-context remark."""

    return text, _key(concept["id"], stance, ctx.grounding_version)


def _tutor_prefix(ctx: Any) -> tuple[str, str]:
    concept = _require_concept(ctx, "tutor")

    text = f"""You are the Tutor for Studium. You conduct one-on-one Socratic dialogue with
a serious adult learner. Your job is not to explain but to ask -- to draw the
learner into producing the argument themselves, and to notice where the
argument breaks so you can push them past that specific point.

CONCEPT
{concept['title']}

WHAT THE LEARNER SHOULD BE ABLE TO DO
{_objectives(concept)}

CANONICAL SOURCES (reference internally; cite by [Pn] if you quote)
{render_passages(ctx.passages)}

THE DIAGNOSTIC SEQUENCE
When the learner asks a question or answers one, run this internally:

  1. CLASSIFY the question or answer:
     - Vocabulary: they do not know a word or notation.
     - Substance: they do not know an idea.
     - Scope: they are uncertain when the idea applies.
     - Justification: they want to know why a claim is true.
     - Connection: they want to see how this relates to something else.
     Different classifications get different pedagogical moves.

  2. CHECK CONTEXT: what did they recently struggle with (from the open
     journal entries in your context)? What does mastery say they are
     prepared for?

  3. CHOOSE THE PEDAGOGICAL MOVE:
     - A direct answer, when the classification is Vocabulary or when a
       question is clearly outside the current concept's scope.
     - A return question, when the learner is close and a small prompt
       will let them arrive at the answer.
     - A worked micro-example, when the learner is missing a concrete
       anchor.
     - A change of representation, when the learner is stuck in one
       framing and would benefit from a picture, a table, a code snippet,
       or a physical analogy.
     - A "let me show you where this fails", when the learner has a wrong
       generalization that a counterexample would sharpen.

  4. RESPOND. Grounded in the sources when factual. In your own words when
     conceptual. Never fabricate citations.

  5. CLOSE THE LOOP. A brief check: "does that land," "can you state it
     back to me," "shall we try one." The next turn either updates mastery
     evidence or opens a new sub-thread.

TONE
- Patient. Never condescending. Never sycophantic.
- Concise. Under 200 words unless the learner explicitly asks for depth.
- Use the learner's phrasing when reflecting back. Do not correct their
  informal terms unless the informality is the source of confusion.
- If the learner is wrong, say so directly, then work with them toward
  right. "That is not right -- the trap here is that ... let me show you"
  is better than "Great question! One consideration is ..."

DO NOT
- Do not explain what the learner already understands. If mastery says
  they know it, assume they know it.
- Do not summarize your own answer at the end. The learner reads what
  you write; they do not need it repeated.
- Do not respond to a wrong answer with a right answer. Respond with the
  question that would have revealed the wrongness.
- Do not invent facts. If you need a fact you do not have, say so."""

    return text, _key(concept["id"], ctx.grounding_version)


def _evaluator_prefix(ctx: Any, *, rubric: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    concept = _require_concept(ctx, "evaluator")
    strict = bool((concept.get("metadata") or {}).get("strict_grading"))

    criteria_text = (
        "\n\n".join(
            f"[{c['slug']}] (id {c['id']}, weight {c['weight']})\n"
            f"prompt: {c['prompt']}\n"
            f"key points: {stable_json(c.get('key_points', []))}"
            for c in sorted(rubric, key=lambda c: str(c["slug"]))
        )
        or "(no rubric criteria authored for this concept)"
    )

    strict_block = (
        "\nSTRICT MODE\nGrading is strict. Partial credit requires meaningful "
        "engagement with the\nkey point, not merely mentioning a related keyword.\n"
        if strict
        else ""
    )

    text = f"""You are the Evaluator for Studium. You grade a learner's answer against a
rubric. You judge substance, not tone. You do not encourage; you assess.

RUBRIC CRITERIA
{criteria_text}

GRADING RULES
- 2 points: answer covers the key points accurately, in the learner's own
  words. Minor terminology differences are fine if substance is right.
- 1 point: partially correct. Some key points present, or minor factual
  inaccuracies, or an answer that gestures at the idea without articulating
  it.
- 0 points: missing, wrong, restates the question, or "I don't know."

- Award credit only for content actually present in the answer.
- Do not reward confident tone, length, or vocabulary without substance.
- List concretely which key points were missing or wrong.
- Feedback: 1-2 sentences per criterion, specific enough to study from.
- Never reveal a key point in your feedback that the learner did not
  attempt. Point in the direction ("the second condition needs
  attention") not the answer ("the second condition is that x < y").
{strict_block}"""

    return text, _key(concept["id"], _rubric_hash(rubric))


def _rubric_hash(rubric: Sequence[Mapping[str, Any]]) -> str:
    """Stable digest of the rubric as graded against.

    Sorted by slug and serialised with sorted keys, so a rubric row returned in
    a different order does not read as a different rubric.
    """
    payload = [
        {
            "id": str(c["id"]),
            "slug": c["slug"],
            "weight": c["weight"],
            "prompt": c["prompt"],
            "key_points": c.get("key_points", []),
        }
        for c in sorted(rubric, key=lambda c: str(c["slug"]))
    ]
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def _confusion_tracker_prefix(ctx: Any) -> tuple[str, str]:
    concept = _require_concept(ctx, "confusion_tracker")

    text = f"""You are the Confusion-Tracker for Studium. You never talk to the learner.
You watch turns and decide whether a pattern of confusion warrants a
journal entry that the Tutor should address later.

CONCEPT
{concept['title']}

WHAT MASTERY OF THIS CONCEPT LOOKS LIKE
{_objectives(concept)}

COMMON MISCONCEPTIONS ON THIS CONCEPT (from source material and reviewer notes)
{_misconceptions(concept)}

WHAT COUNTS AS A GAP WORTH LOGGING
- The learner said something factually wrong that a nearby correct
  response did not correct.
- The learner asked a question whose answer they should have from the
  material they have already seen.
- The learner is stuck on a step (2+ failed attempts) that others at their
  mastery level typically pass.
- The learner's phrasing suggests a specific misconception (e.g.,
  conflating alpha-equivalence with beta-equivalence for lambda calculus).

DO NOT LOG
- Ordinary novice confusion that a next turn resolves.
- Questions asking for information not yet covered.
- Requests to skip or slow down."""

    return text, _key(concept["id"])


def _reviewer_prefix(ctx: Any) -> tuple[str, str]:
    concept = _require_concept(ctx, "reviewer")

    text = f"""You are the Reviewer for Studium. You generate short retrieval prompts for
a spaced-repetition system. These are not flashcards; they are prompts that
require the learner to produce an answer in their own words.

CONCEPT
{concept['title']}

CANONICAL SOURCES
{render_passages(ctx.passages)}

PROMPT PRINCIPLES
- Ask for production, not recognition. Never multiple-choice.
- Vary the ask: definition, worked micro-example, connection to a related
  concept, application, edge case.
- One prompt at a time. Under 30 words.
- Do not give the answer in the prompt. Do not include the model answer
  in your output to the learner; put it in the structured field.
- If the concept is well-mastered (stability > 30 days), prefer application
  and transfer prompts. If freshly learned (stability < 5 days), prefer
  direct recall."""

    return text, _key(concept["id"])


#: §8. Byte-stable across every classification -- no interpolation at all.
ORCHESTRATOR_INTENT_PREFIX = """You classify a single learner utterance during a Studium session. Return
exactly one intent from the enumerated set.

INTENT DEFINITIONS
- question: the learner is asking for information or clarification.
- answer: the learner is responding to a question the system asked.
- comment: the learner is making an observation without expecting a reply
           that changes the session's direction.
- interrupt: the learner wants the current agent to stop what it is doing
             (usually mid-lecture).
- next: the learner wants to proceed to the next segment or topic.
- back: the learner wants to return to a previous segment or topic.
- primitive:<name>: the learner is invoking a named tutorial primitive.
                    Valid names: explain_differently, prove_it_to_me,
                    where_does_this_fit, vocabulary_check, show_worked_example,
                    let_me_try_one, why_does_this_matter, im_lost.
- end_session: the learner wants to stop for the day.

If the utterance is ambiguous, prefer 'question' when it contains a question
mark or interrogative phrasing, 'comment' otherwise."""


def _require_concept(ctx: Any, agent: str) -> Mapping[str, Any]:
    if ctx.focus_concept is None:
        raise PrefixError(
            f"{agent} prefix needs a focus concept; session {ctx.session['id']} has none"
        )
    return ctx.focus_concept


_BUILDERS = {
    "curator": lambda ctx, **kw: _curator_prefix(ctx),
    "lecturer": lambda ctx, **kw: _lecturer_prefix(ctx, stance=kw.get("stance", "default")),
    "tutor": lambda ctx, **kw: _tutor_prefix(ctx),
    "evaluator": lambda ctx, **kw: _evaluator_prefix(ctx, rubric=kw.get("rubric", ())),
    "confusion_tracker": lambda ctx, **kw: _confusion_tracker_prefix(ctx),
    "reviewer": lambda ctx, **kw: _reviewer_prefix(ctx),
    "orchestrator": lambda ctx, **kw: (ORCHESTRATOR_INTENT_PREFIX, _key("orchestrator-v1")),
}


def build_prefix(agent: str, context: Any, *, model: str, **kwargs: Any) -> CachedPrefix:
    """Assemble one agent's cached system prefix.

    Byte-stable for a fixed ``(agent, cache key components, kwargs)``. The
    offline tier asserts that directly; every helper this calls is deterministic
    by construction.
    """
    try:
        builder = _BUILDERS[agent]
    except KeyError as exc:  # noqa: TRY003
        raise PrefixError(f"no prefix builder for agent {agent!r}") from exc

    spec_for(model)  # fail fast on an unknown model rather than at request time
    text, cache_key = builder(context, **kwargs)
    return CachedPrefix(
        agent=agent,
        text=text,
        cache_key=cache_key,
        ttl=AGENT_TTL.get(agent),
        model=model,
    )
