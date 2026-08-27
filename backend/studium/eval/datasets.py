"""Golden dataset authoring format (evaluation §7.2, §7.3).

YAML under ``content/evaluation/{dataset_slug}/``, parsed and validated here.
The repo is the source of truth (§4); :mod:`studium.eval.sync` materialises
these into the database for query and reporting, and nothing reads the database
copy to decide what to run.

**Validation happens before a run, not during one.** ``studium eval validate``
resolves every check name against :mod:`studium.eval.checks`, compiles every
parameter set, and cross-checks the declared ``grading_kind`` against the
properties actually present. A dataset that fails validation never starts a
run. This matters more here than in ingestion's authoring: a run that
discovers on entry 14 of 20 that a check name was misspelled has already spent
fourteen entries' worth of real money, and §7.4 puts a full regression at
$5-15.

**Errors are collected, not raised on the first one.** Same reasoning as
``ingestion.authoring.AuthoringError``: an author fixing a 20-entry dataset
wants the list.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from studium.models.evaluation import GRADING_KINDS

from . import checks
from .checks import META_GRADED, CheckError

#: §5 addition 1's enum, and §7.1's list.
DATASET_KINDS = (
    "agent_output",
    "retrieval_quality",
    "grading_calibration",
    "content_quality",
)

#: The seven agents plus the two non-agent identities. A dataset may only name
#: one of the seven -- ``learner`` and ``system`` do not have prompts.
EVALUABLE_AGENTS = (
    "orchestrator",
    "curator",
    "lecturer",
    "tutor",
    "evaluator",
    "confusion_tracker",
    "reviewer",
)

#: §7.1: ``grading_calibration`` is "meta-evaluation of the grader", so it is
#: the Evaluator or it is nothing.
CALIBRATION_AGENT = "evaluator"

#: §13.2's defaults, applied when a dataset's YAML omits the block. Chosen to
#: match the spec's own worked example rather than invented: a dataset with no
#: stated tolerance gets the one §13.2 shows.
DEFAULT_TOLERANCE: dict[str, float] = {
    "aggregate_score_drop_max": 0.05,
    "per_entry_failure_max": 2,
}

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_]{1,80}$")
_ENTRY_KEY = re.compile(r"^[a-z0-9][a-z0-9_]{0,80}$")


def order_passages(entry_input: Mapping[str, Any]) -> dict[str, Any]:
    """Put ``retrieved_passages`` into citation order, once, at parse time.

    ``[P1]`` means "the first passage the agent was given". Two things need to
    agree on which passage that is: :mod:`studium.eval.fixtures`, which builds
    the context the agent sees, and :mod:`studium.eval.checks`, which resolves
    a ``from:`` list of chunk ids back to ordinals. If they sort differently,
    every ``from``-restricted citation check measures a different passage than
    the agent cited -- and *passes*, because the counts still line up. Nothing
    in the output would look wrong.

    So the order is decided here and both sides read list order afterwards.
    Sorted by the authored id, which is deterministic and legible: an author
    reading the YAML can see which passage is ``[P1]``. The runtime sorts by
    ``chunk_id`` (retrieval §12) and a fixture's chunk ids are derived from
    these slugs, so sorting by the slug is the same contract expressed in the
    only identifier the YAML actually has.
    """
    passages = entry_input.get("retrieved_passages")
    if not isinstance(passages, list) or not passages:
        return dict(entry_input)

    ordered = sorted(
        (p for p in passages if isinstance(p, Mapping)),
        key=lambda p: str(p.get("id", p.get("chunk_id", ""))),
    )
    return {**entry_input, "retrieved_passages": [dict(p) for p in ordered]}


class DatasetError(ValueError):
    """A dataset file cannot be used. Carries every problem, not just the first."""

    def __init__(self, problems: Sequence[str], *, path: Path | None = None) -> None:
        self.problems = list(problems)
        self.path = path
        where = f"{path}: " if path else ""
        joined = "\n  - ".join(self.problems)
        super().__init__(f"{where}{len(self.problems)} problem(s):\n  - {joined}")


@dataclass(frozen=True, slots=True)
class Entry:
    """One authored case (§7.2's ``entries:`` list)."""

    key: str
    index: int
    input: dict[str, Any]
    expected: dict[str, Any]
    grading_kind: str
    rubric: dict[str, Any] | None = None
    notes: str | None = None

    @property
    def properties(self) -> list[dict[str, Any]]:
        """The ``expected.properties`` list, or empty for retrieval entries."""
        raw = self.expected.get("properties") or []
        return [p for p in raw if isinstance(p, Mapping)]  # type: ignore[misc]

    @property
    def is_stub(self) -> bool:
        """A stub written by ``studium review flag --to-dataset`` (§10.2).

        §3 is categorical that datasets are authored: the flag action captures
        the fixture input that produced a production defect, and a human then
        says what the correct output would have been. Until they do, the entry
        has an input and no expectation, and running it would score whatever
        the agent did against nothing -- reporting a pass for the very defect
        it was created from.
        """
        return not self.expected or (
            not self.properties
            and not self.expected.get("relevant_chunks")
            and self.expected.get("stub") is True
        )


@dataclass(frozen=True, slots=True)
class Dataset:
    """One dataset file, parsed and validated (§7.1)."""

    slug: str
    kind: str
    description: str
    entries: tuple[Entry, ...]
    agent: str | None = None
    version: int = 1
    active: bool = True
    regression_tolerance: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_TOLERANCE)
    )
    path: Path | None = None

    @property
    def runnable_entries(self) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if not e.is_stub)

    @property
    def stub_count(self) -> int:
        return len(self.entries) - len(self.runnable_entries)


# --- parsing ---------------------------------------------------------------


def yaml_files(directory: Path) -> list[Path]:
    """Every ``.yaml``/``.yml`` under ``directory``, sorted.

    Sorted so a dataset split across several files (§7.2: "or split by entry
    group for large datasets") assigns ``entry_index`` deterministically. An
    unsorted walk would renumber every entry the day someone added a file whose
    name sorted earlier, and ``evaluation_results`` rows point at entry ids
    whose index is part of their unique key.
    """
    if not directory.is_dir():
        raise DatasetError([f"not a directory: {directory}"])
    return sorted(
        p for p in directory.rglob("*") if p.suffix in (".yaml", ".yml") and p.is_file()
    )


def load_yaml(path: Path) -> dict[str, Any]:
    """Parse one file. ``safe_load``, never ``load``.

    Same reasoning as ``ingestion.authoring.load_yaml``: these files live in a
    content directory a reviewer edits, full ``load`` constructs arbitrary
    Python objects, and one day the directory will contain something someone
    else wrote.
    """
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetError([f"cannot read: {exc}"], path=path) from exc

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DatasetError([f"invalid YAML: {exc}"], path=path) from exc

    if parsed is None:
        raise DatasetError(["file is empty"], path=path)
    if not isinstance(parsed, dict):
        raise DatasetError(
            [f"expected a mapping at the top level, got {type(parsed).__name__}"],
            path=path,
        )
    return parsed


def parse_dataset(path: Path) -> Dataset:
    """Parse and validate one dataset file. Raises :class:`DatasetError`."""
    raw = load_yaml(path)
    problems: list[str] = []

    header = raw.get("dataset")
    if not isinstance(header, Mapping):
        raise DatasetError(["missing top-level 'dataset:' mapping"], path=path)

    slug = str(header.get("slug", "")).strip()
    if not _SLUG.match(slug):
        problems.append(
            f"dataset.slug {slug!r} must be lowercase alphanumerics and "
            f"underscores, 2-81 characters"
        )

    kind = str(header.get("kind", "")).strip()
    if kind not in DATASET_KINDS:
        problems.append(f"dataset.kind {kind!r} is not one of {list(DATASET_KINDS)}")

    agent = header.get("agent")
    agent = str(agent).strip() if agent else None
    problems.extend(_validate_agent(kind, agent))

    description = str(header.get("description", "")).strip()
    if not description:
        # §7.1: each dataset "addresses one specific evaluation question". A
        # dataset that cannot say which one is a dataset nobody can retire
        # correctly, because §7.3's retirement rule turns on whether the
        # property it tested still applies.
        problems.append("dataset.description is required and says what this tests")

    version = header.get("version", 1)
    if not isinstance(version, int) or version < 1:
        problems.append(f"dataset.version must be an integer >= 1, got {version!r}")
        version = 1

    tolerance, tolerance_problems = _parse_tolerance(header.get("regression_tolerance"))
    problems.extend(tolerance_problems)

    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        problems.append("entries: must be a non-empty list")
        raw_entries = []

    entries: list[Entry] = []
    seen_keys: set[str] = set()
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, Mapping):
            problems.append(f"entry {index}: expected a mapping")
            continue
        entry, entry_problems = _parse_entry(raw_entry, index=index, kind=kind)
        problems.extend(entry_problems)
        if entry is None:
            continue
        if entry.key in seen_keys:
            problems.append(f"entry {entry.key!r} is declared twice")
        seen_keys.add(entry.key)
        entries.append(entry)

    if problems:
        raise DatasetError(problems, path=path)

    return Dataset(
        slug=slug,
        kind=kind,
        agent=agent,
        version=version,
        description=description,
        active=bool(header.get("active", True)),
        regression_tolerance=tolerance,
        entries=tuple(entries),
        path=path,
    )


def parse_directory(directory: Path) -> list[Dataset]:
    """Parse every dataset under ``directory``, reporting all problems at once.

    A single :class:`DatasetError` carrying every file's problems, rather than
    stopping at the first bad file -- ``studium eval validate
    content/evaluation/`` is run to find out what is wrong with the whole tree.
    """
    problems: list[str] = []
    datasets: list[Dataset] = []
    slugs: dict[str, Path] = {}

    for path in yaml_files(directory):
        try:
            dataset = parse_dataset(path)
        except DatasetError as exc:
            problems.extend(f"{_rel(path, directory)}: {p}" for p in exc.problems)
            continue
        if dataset.slug in slugs:
            problems.append(
                f"{_rel(path, directory)}: slug {dataset.slug!r} is already used by "
                f"{_rel(slugs[dataset.slug], directory)}; "
                f"golden_datasets.slug is UNIQUE"
            )
            continue
        slugs[dataset.slug] = path
        datasets.append(dataset)

    if problems:
        raise DatasetError(problems)
    return datasets


# --- validation internals --------------------------------------------------


def _validate_agent(kind: str, agent: str | None) -> list[str]:
    """§7.1's kind/agent agreement, matching the table's CHECK constraint."""
    needs_agent = kind in ("agent_output", "grading_calibration")

    if needs_agent and not agent:
        return [
            f"dataset.agent is required for kind {kind!r}: §13.4 resolves a "
            f"prompt change to the datasets it affects by agent identity, so a "
            f"dataset with no agent would never be selected and the change "
            f"would ship unevaluated"
        ]
    if not needs_agent and agent:
        return [
            f"dataset.agent is set to {agent!r} but kind {kind!r} does not test "
            f"one agent's response"
        ]
    if agent and agent not in EVALUABLE_AGENTS:
        return [f"dataset.agent {agent!r} is not one of {list(EVALUABLE_AGENTS)}"]
    if kind == "grading_calibration" and agent != CALIBRATION_AGENT:
        return [
            f"grading_calibration is meta-evaluation of the grader (§7.2), so "
            f"agent must be {CALIBRATION_AGENT!r}, not {agent!r}"
        ]
    return []


def _parse_tolerance(raw: Any) -> tuple[dict[str, float], list[str]]:
    """§13.2's per-dataset tolerance block."""
    tolerance = dict(DEFAULT_TOLERANCE)
    if raw is None:
        return tolerance, []
    if not isinstance(raw, Mapping):
        return tolerance, ["dataset.regression_tolerance must be a mapping"]

    problems: list[str] = []
    for key, value in raw.items():
        if key not in DEFAULT_TOLERANCE:
            problems.append(
                f"dataset.regression_tolerance: unknown key {key!r}; "
                f"expected {sorted(DEFAULT_TOLERANCE)}"
            )
            continue
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            problems.append(
                f"dataset.regression_tolerance.{key} must be a number >= 0, "
                f"got {value!r}"
            )
            continue
        tolerance[key] = float(value)

    drop = tolerance["aggregate_score_drop_max"]
    if drop > 1.0:
        # Scores are 0..1, so a tolerance above 1.0 admits any regression at
        # all -- a gate configured to never fire, which reads in review like a
        # gate that is on.
        problems.append(
            f"dataset.regression_tolerance.aggregate_score_drop_max is {drop}, "
            f"but scores are 0.0-1.0; a tolerance above 1.0 can never block"
        )
    return tolerance, problems


def _parse_entry(
    raw: Mapping[str, Any], *, index: int, kind: str
) -> tuple[Entry | None, list[str]]:
    problems: list[str] = []
    where = f"entry {raw.get('id', index)}"

    key = str(raw.get("id", "")).strip()
    if not _ENTRY_KEY.match(key):
        problems.append(
            f"{where}: id must be lowercase alphanumerics and underscores "
            f"(e.g. entry_001), got {key!r}"
        )

    entry_input = raw.get("input")
    if not isinstance(entry_input, Mapping) or not entry_input:
        problems.append(f"{where}: input: must be a non-empty mapping")
        entry_input = {}

    expected = raw.get("expected")
    if expected is None:
        expected = {}
    elif not isinstance(expected, Mapping):
        problems.append(f"{where}: expected: must be a mapping")
        expected = {}

    rubric = raw.get("rubric")
    if rubric is not None and not isinstance(rubric, Mapping):
        problems.append(f"{where}: rubric: must be a mapping")
        rubric = None

    entry = Entry(
        key=key or f"entry_{index:03d}",
        index=index,
        # Ordered once, here. See order_passages: this is the only place the
        # citation numbering is decided.
        input=order_passages(entry_input),
        expected=dict(expected),
        grading_kind="deterministic",
        rubric=dict(rubric) if rubric else None,
        notes=str(raw["notes"]).strip() if raw.get("notes") else None,
    )

    if entry.is_stub:
        # A stub is legal in the tree and illegal in a run. Reported as a
        # warning at the CLI rather than an error here, so `flag --to-dataset`
        # can commit one and the reviewer can fill it in on their own schedule.
        return entry, problems

    if kind == "retrieval_quality":
        problems.extend(_validate_retrieval_entry(entry, where))
        derived = "deterministic"
    else:
        property_problems, derived = _validate_properties(entry, where)
        problems.extend(property_problems)

    declared = raw.get("grading_kind")
    if declared is not None:
        declared = str(declared).strip()
        if declared not in GRADING_KINDS:
            problems.append(
                f"{where}: grading_kind {declared!r} is not one of "
                f"{list(GRADING_KINDS)}"
            )
        elif declared != derived:
            # The declared value is checked against the properties rather than
            # trusted. §5's column comment allows only two values while §7.2's
            # own worked example declares a third ('hybrid'), so authors will
            # get this wrong -- and a 'deterministic' entry carrying a
            # meta_graded property would silently skip the Evaluator call and
            # report a pass on a property nothing evaluated. See
            # DIVERGENCES-EVALUATION (E2).
            problems.append(
                f"{where}: grading_kind says {declared!r} but the properties "
                f"are {derived!r}; the properties are what get graded"
            )

    if derived != "deterministic" and entry.rubric is None:
        rubric_bearing = any(
            p.get("check") == META_GRADED and p.get("rubric") for p in entry.properties
        )
        if not rubric_bearing:
            problems.append(
                f"{where}: {derived} grading needs a rubric, either on the "
                f"meta_graded property or on the entry"
            )

    return (
        Entry(
            key=entry.key,
            index=entry.index,
            input=entry.input,
            expected=entry.expected,
            grading_kind=derived,
            rubric=entry.rubric,
            notes=entry.notes,
        ),
        problems,
    )


def _validate_properties(entry: Entry, where: str) -> tuple[list[str], str]:
    """Resolve every property's check and derive the entry's grading kind."""
    problems: list[str] = []
    properties = entry.properties

    if not properties:
        problems.append(
            f"{where}: expected.properties must list at least one property; "
            f"an entry that asserts nothing cannot fail"
        )
        return problems, "deterministic"

    names: set[str] = set()
    deterministic = meta = False

    for position, prop in enumerate(properties):
        label = prop.get("name") or f"property {position}"
        if not prop.get("name"):
            problems.append(f"{where}: {label} has no name")
        elif str(prop["name"]) in names:
            problems.append(f"{where}: property {prop['name']!r} is declared twice")
        else:
            names.add(str(prop["name"]))

        check_name = prop.get("check")
        if not check_name:
            problems.append(f"{where}/{label}: no check declared")
            continue
        check_name = str(check_name)

        if check_name == META_GRADED:
            meta = True
            if not str(prop.get("rubric", "")).strip() and entry.rubric is None:
                problems.append(
                    f"{where}/{label}: a meta_graded property needs a rubric; "
                    f"without one the Evaluator is asked to judge against "
                    f"nothing and will return a confident number anyway"
                )
            continue

        deterministic = True
        try:
            spec = checks.get(check_name)
        except CheckError as exc:
            problems.append(f"{where}/{label}: {exc}")
            continue
        problems.extend(
            f"{where}/{label}: {problem}" for problem in spec.validate_params(prop)
        )

    if meta and deterministic:
        return problems, "hybrid"
    if meta:
        return problems, "meta_graded"
    return problems, "deterministic"


def _validate_retrieval_entry(entry: Entry, where: str) -> list[str]:
    """§9.1's shape: a query in, judged chunk ids out."""
    problems: list[str] = []

    if not entry.input.get("concept_id"):
        problems.append(f"{where}: retrieval input needs a concept_id")

    k = entry.input.get("k")
    if k is not None and (not isinstance(k, int) or k < 1):
        problems.append(f"{where}: k must be a positive integer, got {k!r}")

    relevant = entry.expected.get("relevant_chunks")
    if not isinstance(relevant, list) or not relevant:
        problems.append(
            f"{where}: expected.relevant_chunks must be a non-empty list; "
            f"recall@k divides by it, and an empty list makes every result "
            f"score 1.0"
        )
        relevant = []

    misleading = entry.expected.get("misleading_chunks") or []
    if not isinstance(misleading, list):
        problems.append(f"{where}: expected.misleading_chunks must be a list")
        misleading = []

    overlap = {str(c) for c in relevant} & {str(c) for c in misleading}
    if overlap:
        # §9.1 makes these a two-tier judgment, not overlapping sets. A chunk
        # in both would score as a hit for recall and a hit for the misleading
        # rate at the same time, which is not a judgment anyone made.
        problems.append(
            f"{where}: chunk(s) {sorted(overlap)} are listed as both relevant "
            f"and misleading"
        )
    return problems


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:  # pragma: no cover -- rglob results are always under root
        return str(path)


def iter_entries(datasets: Sequence[Dataset]) -> Iterator[tuple[Dataset, Entry]]:
    for dataset in datasets:
        for entry in dataset.entries:
            yield dataset, entry
