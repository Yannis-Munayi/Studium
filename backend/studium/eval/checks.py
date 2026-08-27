"""Deterministic checks (evaluation §7.2).

A check is a pure function of `(actual output, declared parameters, the entry's
input)`. No database, no model call, no clock. That purity is the whole point:
§3's first principle is that evaluation is measurement, and a measurement that
depends on when you took it measures the clock.

Adding a check kind is "a code change plus test" (§7.2). Concretely: write the
function, decorate it with :func:`check`, declare its parameters, and add a
fixture case to ``tests/eval/test_checks.py``. The registry is what
``studium eval validate`` reads to tell an author that ``at_least_n_citation``
is not a check kind before the entry reaches a run that would have cost money.

**Every check returns a score, not just a boolean.** §5's
``evaluation_results.score`` is ``REAL CHECK (score >= 0.0 AND score <= 1.0)``,
and §13.2 gates on aggregate score drift. A check that only said pass/fail
would quantise every regression to a step function: a Lecturer that went from
citing 6 passages to citing 2 would look identical to one that went from 6 to 1
as long as both cleared a threshold of 2, and §8.1's "grounding rate" metric
would have nothing to average. Where a check has no meaningful gradient
(``contains_none_of`` -- either the forbidden string is there or it is not) the
score is 1.0 or 0.0 and says so in its docstring.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from studium.retrieval.citations import CITATION_PATTERN, parse_citation_markers

#: §7.2's meta-graded escape hatch. Not a deterministic check -- it names the
#: property whose grading routes to the Evaluator instead. Registered here so
#: ``studium eval validate`` accepts it as a ``check:`` value and so §17's
#: "every check name in a YAML entry exists in studium.eval.checks" holds
#: without special-casing.
META_GRADED = "meta_graded"


class CheckError(ValueError):
    """A check was declared with parameters it cannot use.

    Raised at validation time, never at run time. A check that discovers its
    parameters are wrong halfway through a paid run has already spent the
    money, so ``studium eval validate`` resolves every parameter against
    :attr:`CheckSpec.params` before a run is allowed to start.
    """


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One property's verdict on one entry."""

    passed: bool
    #: 0.0-1.0. See the module docstring on why this is not just ``passed``.
    score: float
    #: Human-readable, and written to ``evaluation_results.grading_notes``.
    #: This is what the reviewer reads in a diff, so it names the observed
    #: value rather than restating the rule.
    note: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise CheckError(
                f"check produced score {self.score}, outside 0.0-1.0; "
                f"evaluation_results.score has a CHECK constraint on that range"
            )


CheckFn = Callable[[str, Mapping[str, Any], Mapping[str, Any]], CheckOutcome]


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """A registered check: its function and the parameters it accepts."""

    name: str
    fn: CheckFn
    #: Parameter name -> required. Validated at authoring time.
    params: dict[str, bool] = field(default_factory=dict)
    doc: str = ""

    def validate_params(self, declared: Mapping[str, Any]) -> list[str]:
        """Problems with a YAML property's parameters. Empty means it compiles.

        Returns a list rather than raising for the same reason
        ``AuthoringError`` carries every problem: an author fixing a 20-entry
        dataset wants the whole list, not twenty edit-run cycles.
        """
        problems: list[str] = []
        # 'name' and 'check' are the property's own keys, not the check's.
        given = {k for k in declared if k not in ("name", "check")}
        for param, required in self.params.items():
            if required and param not in given:
                problems.append(f"check {self.name!r} requires parameter {param!r}")
        for extra in sorted(given - set(self.params)):
            problems.append(
                f"check {self.name!r} does not accept parameter {extra!r}; "
                f"accepts {sorted(self.params) or 'none'}"
            )
        return problems


REGISTRY: dict[str, CheckSpec] = {}


def check(name: str, **params: bool) -> Callable[[CheckFn], CheckFn]:
    """Register a deterministic check under ``name``.

    ``params`` maps parameter name to whether it is required.
    """

    def decorate(fn: CheckFn) -> CheckFn:
        if name in REGISTRY:  # pragma: no cover -- import-time programmer error
            raise CheckError(f"check {name!r} is already registered")
        REGISTRY[name] = CheckSpec(name=name, fn=fn, params=params, doc=fn.__doc__ or "")
        return fn

    return decorate


def get(name: str) -> CheckSpec:
    if name not in REGISTRY:
        raise CheckError(
            f"unknown check {name!r}; registered: {', '.join(sorted(REGISTRY))}"
        )
    return REGISTRY[name]


def known() -> frozenset[str]:
    """Every check name a YAML entry may reference, including ``meta_graded``."""
    return frozenset(REGISTRY) | {META_GRADED}


# --- the checks §7.2 enumerates -------------------------------------------


@check("at_least_n_citations", n=True, **{"from": False})
def at_least_n_citations(
    output: str, params: Mapping[str, Any], entry_input: Mapping[str, Any]
) -> CheckOutcome:
    """At least ``n`` distinct ``[Pn]`` markers, optionally from a given set.

    §7.2 gives this both an ``n`` and a ``from`` list. ``from`` names chunk ids,
    while the markers in the text are *ordinals* -- ``[P1]`` means "the first
    passage in the retrieval result", not "chunk 1". Retrieval §12 fixes that
    correspondence: passages are sorted by ``chunk_id`` and numbered from 1, so
    the ordinal resolves through ``entry_input['retrieved_passages']``. Getting
    this backwards is the kind of thing that makes a check pass on the wrong
    evidence, so the resolution is explicit rather than positional-by-luck.

    Score is the fraction of ``n`` achieved, capped at 1.0 -- so a segment that
    cites one of two required passages scores 0.5 rather than 0, which is what
    lets §13.2's aggregate see a partial regression.
    """
    required = int(params["n"])
    if required < 1:
        raise CheckError("at_least_n_citations needs n >= 1")

    passages = _passage_ids(entry_input)
    cited = parse_citation_markers(output, maximum=len(passages) or None)

    restrict_to = params.get("from")
    if restrict_to:
        wanted = {str(c) for c in restrict_to}
        # Ordinal -> chunk id, per retrieval §12's numbering contract.
        cited = [n for n in cited if 1 <= n <= len(passages) and passages[n - 1] in wanted]
        qualifier = f" from {sorted(wanted)}"
    else:
        qualifier = ""

    found = len(cited)
    score = min(1.0, found / required)
    return CheckOutcome(
        passed=found >= required,
        score=score,
        note=f"cited {found} passage(s){qualifier}, needed {required}",
    )


@check("word_count_between", min=True, max=True)
def word_count_between(
    output: str, params: Mapping[str, Any], entry_input: Mapping[str, Any]
) -> CheckOutcome:
    """Word count within ``[min, max]``. §8.1's "length compliance".

    Citation markers are stripped before counting. ``[P1]`` is not a word the
    learner reads, and a segment with twelve citations would otherwise be
    twelve words closer to the ceiling than the same prose with two -- which
    would penalise exactly the grounding §8.1's other metric rewards.

    Score decays linearly outside the band rather than dropping to 0: a
    410-word segment against a 400-word ceiling is a near miss, and scoring it
    the same as a 4000-word one would make the aggregate blind to the
    difference. Zero at double the band's width past either edge.
    """
    low, high = int(params["min"]), int(params["max"])
    if low > high:
        raise CheckError(f"word_count_between: min {low} exceeds max {high}")

    stripped = CITATION_PATTERN.sub(" ", output)
    count = len(stripped.split())

    if low <= count <= high:
        return CheckOutcome(True, 1.0, f"{count} words, within {low}-{high}")

    width = max(high - low, 1)
    overshoot = (low - count) if count < low else (count - high)
    score = max(0.0, 1.0 - overshoot / width)
    side = "under" if count < low else "over"
    return CheckOutcome(
        False, score, f"{count} words, {overshoot} {side} the {low}-{high} band"
    )


@check("contains_all_of", strings=True, case_sensitive=False)
def contains_all_of(
    output: str, params: Mapping[str, Any], entry_input: Mapping[str, Any]
) -> CheckOutcome:
    """Every listed substring is present. Score is the fraction present."""
    wanted = _strings(params)
    haystack = output if params.get("case_sensitive") else output.lower()
    needles = wanted if params.get("case_sensitive") else [w.lower() for w in wanted]

    missing = [w for w, n in zip(wanted, needles, strict=True) if n not in haystack]
    found = len(wanted) - len(missing)
    return CheckOutcome(
        passed=not missing,
        score=found / len(wanted),
        note=(
            f"all {len(wanted)} present"
            if not missing
            else f"{found}/{len(wanted)} present; missing {missing}"
        ),
    )


@check("contains_none_of", strings=True, case_sensitive=False)
def contains_none_of(
    output: str, params: Mapping[str, Any], entry_input: Mapping[str, Any]
) -> CheckOutcome:
    """No listed substring is present.

    Binary by nature: one forbidden phrase is a failure and two are not twice
    the failure. Scored 1.0 or 0.0 accordingly, per the module docstring's rule
    about checks with no meaningful gradient.
    """
    forbidden = _strings(params)
    haystack = output if params.get("case_sensitive") else output.lower()
    needles = forbidden if params.get("case_sensitive") else [w.lower() for w in forbidden]

    hits = [w for w, n in zip(forbidden, needles, strict=True) if n in haystack]
    return CheckOutcome(
        passed=not hits,
        score=0.0 if hits else 1.0,
        note="none present" if not hits else f"found forbidden: {hits}",
    )


@check("citation_targets_valid")
def citation_targets_valid(
    output: str, params: Mapping[str, Any], entry_input: Mapping[str, Any]
) -> CheckOutcome:
    """Every ``[Pn]`` marker resolves to a passage that was supplied.

    §8.1: "Citation validity < 100% blocks deploy (an invalid citation is a
    hallucinated reference; not tolerable)." Binary for that reason -- a
    partial score here would be a number the blocking threshold in
    :mod:`studium.eval.metrics` would have to interpret, and the spec's
    interpretation is that any invalid citation fails.

    Note this does *not* use ``retrieval.has_invalid_citation``: that returns a
    bool and this needs to name the offending markers, because "which citation
    was invented" is the first thing the reviewer will ask.
    """
    supplied = len(_passage_ids(entry_input))
    invalid: list[str] = []
    for match in CITATION_PATTERN.finditer(output):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if start < 1 or max(start, end) > supplied:
            invalid.append(match.group(0))

    return CheckOutcome(
        passed=not invalid,
        score=0.0 if invalid else 1.0,
        note=(
            f"all markers resolve against {supplied} supplied passage(s)"
            if not invalid
            else f"invented citation(s) {invalid} against {supplied} supplied passage(s)"
        ),
    )


@check("numeric_answer_within_tolerance", expected=True, tolerance=False)
def numeric_answer_within_tolerance(
    output: str, params: Mapping[str, Any], entry_input: Mapping[str, Any]
) -> CheckOutcome:
    """The first number in the output is within ``tolerance`` of ``expected``.

    §7.2: "for Evaluator meta-testing". The Evaluator's structured output is a
    grade, and a ``grading_calibration`` entry asserts a known-correct one.

    ``tolerance`` defaults to 0, which is exact match -- §8.3's "scoring
    accuracy: exact match with ground-truth grade". A non-zero tolerance is for
    the continuous case (a mastery posterior), not for grades, which are
    ``IN (0, 1, 2)`` and where "close" is meaningless.

    Score falls off linearly across the tolerance band. With tolerance 0 that
    degenerates to binary, which is correct: there is no partial credit for
    almost matching an integer grade.
    """
    expected = float(params["expected"])
    tolerance = float(params.get("tolerance") or 0.0)
    if tolerance < 0:
        raise CheckError("numeric_answer_within_tolerance: tolerance must be >= 0")

    found = _first_number(output)
    if found is None:
        return CheckOutcome(False, 0.0, f"no number in output; expected {expected}")

    delta = abs(found - expected)
    if delta == 0:
        return CheckOutcome(True, 1.0, f"exactly {expected}")
    if delta <= tolerance:
        return CheckOutcome(
            True, 1.0 - delta / tolerance, f"{found} within {tolerance} of {expected}"
        )
    return CheckOutcome(False, 0.0, f"{found} differs from {expected} by {delta}")


@check("structured_output_matches_schema", schema=True)
def structured_output_matches_schema(
    output: str, params: Mapping[str, Any], entry_input: Mapping[str, Any]
) -> CheckOutcome:
    """The output parses against a named Pydantic model in ``studium.agents``.

    ``schema`` is a class name from :mod:`studium.agents.schemas` -- the models
    every structured agent call is already parsed into. Resolved through an
    allow-list rather than by import path: a YAML file naming an arbitrary
    dotted path would be an import-anything primitive in a content directory a
    reviewer edits, which is the same reasoning that makes
    ``ingestion.authoring`` use ``yaml.safe_load`` and never ``yaml.load``.
    """
    from studium.agents import schemas as agent_schemas

    name = str(params["schema"])
    model = getattr(agent_schemas, name, None)
    if model is None or not _is_pydantic_model(model):
        raise CheckError(
            f"structured_output_matches_schema: {name!r} is not a model in "
            f"studium.agents.schemas"
        )

    import json

    from pydantic import ValidationError

    try:
        payload = json.loads(output)
    except (TypeError, ValueError) as exc:
        return CheckOutcome(False, 0.0, f"output is not JSON: {exc}")

    try:
        model.model_validate(payload)
    except ValidationError as exc:
        return CheckOutcome(
            False, 0.0, f"does not satisfy {name}: {exc.error_count()} error(s)"
        )
    return CheckOutcome(True, 1.0, f"parses as {name}")


# --- helpers ---------------------------------------------------------------

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _first_number(text: str) -> float | None:
    match = _NUMBER.search(text)
    return float(match.group(0)) if match else None


def _strings(params: Mapping[str, Any]) -> list[str]:
    raw = params.get("strings")
    if isinstance(raw, str) or not isinstance(raw, Sequence) or not raw:
        raise CheckError("'strings' must be a non-empty list")
    return [str(s) for s in raw]


def _passage_ids(entry_input: Mapping[str, Any]) -> list[str]:
    """Chunk ids in citation order: ``[Pn]`` is ``_passage_ids(...)[n - 1]``.

    **List order, not sorted here.** ``datasets.order_passages`` sorts once, at
    parse time, and :mod:`studium.eval.fixtures` reads the same list in the
    same order when it builds the agent's context. Sorting again here would be
    a second opinion on the citation numbering, and the two opinions agreeing
    would be a coincidence rather than a guarantee -- a ``from``-restricted
    check would then resolve ordinals to the wrong chunks and still pass,
    because the counts would line up.
    """
    passages = entry_input.get("retrieved_passages") or []
    return [
        str(p.get("id", p.get("chunk_id", "")))
        for p in passages
        if isinstance(p, Mapping)
    ]


def _is_pydantic_model(obj: Any) -> bool:
    from pydantic import BaseModel

    return isinstance(obj, type) and issubclass(obj, BaseModel)
