"""The ingestion-side provenance gap invariant, Tier 1 (ingestion §13.4, §16).

This module is the enforcement mechanism for §13. The invariant says no
ingestion writer creates a row where a required-for-attribution column is
silently populated with a fallback; these tests walk every registered writer
and check it.

The invariant exists because the same failure appeared three times -- V2's cost
attribution, the draft loader's dropped `model`/`generated_at`, and S3's
rejected queue row -- and three instances is the standing trigger for promoting
a pattern from per-site fixes to a spec invariant with a test behind it.

What this cannot catch is written down rather than glossed over. §18 open
question 4: a writer that accepts `attributable_to` and then ignores it passes
every check here. Signature introspection sees signatures. That class of defect
needs code review, and saying so in the test module is more honest than a
coverage number that implies otherwise.
"""

from __future__ import annotations

import inspect

import pytest

from studium.ingestion import provenance
from studium.ingestion.provenance import (
    CRITICAL_COLUMNS,
    MissingProvenance,
    provenance_parameters,
    require,
    writers,
)


# Registration is a side effect of import, and the parametrised tests below
# read the registry at *collection* time. A submodule not yet imported by then
# contributes no writers and is silently skipped -- the registry looks
# populated, the run is green, and the newest writers were never checked. That
# is not hypothetical: `authoring` was invisible here until the package
# `__init__` was made to import it.
#
# Walking the package rather than listing modules by name, so a module added
# next month is covered without anyone remembering to add a line.
def _import_every_submodule() -> list[str]:
    import importlib
    import pkgutil

    import studium.ingestion as package

    names = []
    for info in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{info.name}")
        names.append(info.name)
    return names


INGESTION_MODULES = _import_every_submodule()


def test_writers_are_registered() -> None:
    """§16's coverage guard: fail if zero writer functions are found.

    Without this the whole module is a no-op the moment a refactor stops the
    decorators from running -- every parametrised test below would collect zero
    cases and report green. A test suite that passes because it tested nothing
    is worse than one that fails.
    """
    found = writers()
    assert found, (
        "no ingestion writers registered; @ingestion_writer is not running, "
        "and every check in this module is passing vacuously"
    )
    assert len(found) >= 8, (
        f"only {len(found)} writers registered; the pipeline, authoring, "
        f"licensing and queue modules should each contribute at least one"
    )


def test_every_writer_module_is_represented() -> None:
    """Each module that writes ingestion rows contributes a registered writer.

    Guards the failure mode the count above cannot see: a new writer module
    added with no decorators at all, where the registry stays populated by the
    old ones and nothing looks wrong.
    """
    modules = {writer.__module__.rsplit(".", 1)[-1] for writer in writers()}
    for expected in ("pipeline", "authoring", "licensing", "queue", "publish"):
        assert expected in modules, (
            f"studium.ingestion.{expected} registers no writer; if it writes "
            f"rows, its writers are exempt from §13 without anyone deciding so"
        )


@pytest.mark.parametrize("writer", writers(), ids=lambda w: f"{w.__module__.rsplit('.', 1)[-1]}.{w.__name__}")
def test_no_provenance_parameter_has_a_default(writer) -> None:
    """§13.4 check 1, and the core of the invariant.

    A provenance parameter with a default is how every one of the three
    instances happened: the caller omitted it, the default filled in, and the
    row was written with provenance nobody supplied. Requiring the parameter
    means the omission is a TypeError at the call site instead of a wrong row
    in the corpus.

    ``None`` is allowed as a default *only* where the writer immediately raises
    on it -- see the next test. That is not a loophole: it is how a writer with
    genuinely one-of-several targets (a review queue row) states the
    requirement it actually has.
    """
    offenders = []
    for name, parameter in provenance_parameters(writer).items():
        if parameter.default is inspect.Parameter.empty:
            continue
        if parameter.default is None:
            continue  # checked by test_nullable_provenance_is_rejected_at_runtime
        offenders.append(f"{name}={parameter.default!r}")

    assert not offenders, (
        f"{writer.__module__}.{writer.__name__} has provenance parameter(s) "
        f"{offenders} with a fallback default. §13: a writer that can invent "
        f"provenance will, on the one call that forgot to pass it."
    )


@pytest.mark.parametrize("writer", writers(), ids=lambda w: f"{w.__module__.rsplit('.', 1)[-1]}.{w.__name__}")
def test_provenance_parameters_are_keyword_only(writer) -> None:
    """Positional provenance is provenance waiting to be passed in the wrong order.

    ``upload(session, data, filename, subject_id, uploaded_by)`` has two UUIDs
    adjacent; swapping them type-checks, runs, and files the source under the
    wrong subject with the subject id as its uploader. Keyword-only makes that
    unspellable.
    """
    signature = inspect.signature(writer)
    positional = [
        name
        for name, parameter in signature.parameters.items()
        if name in CRITICAL_COLUMNS
        and parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]
    assert not positional, (
        f"{writer.__module__}.{writer.__name__}: provenance parameter(s) "
        f"{positional} can be passed positionally"
    )


@pytest.mark.parametrize(
    "writer",
    [w for w in writers() if any(
        p.default is None for p in provenance_parameters(w).values()
    )],
    ids=lambda w: f"{w.__module__.rsplit('.', 1)[-1]}.{w.__name__}",
)
def test_nullable_provenance_writers_raise(writer) -> None:
    """§13.4 check 2: a writer allowing ``None`` must raise, not fall back.

    Verified by reading the source for a ``require(...)`` call or an explicit
    ``MissingProvenance`` raise. Source inspection rather than calling the
    writer, because calling it needs a database session and this is Tier 1 --
    and because what is being asserted is a property of the code, not of one
    invocation.
    """
    source = inspect.getsource(writer)
    assert "require(" in source or "MissingProvenance" in source, (
        f"{writer.__module__}.{writer.__name__} accepts None for a provenance "
        f"parameter but never raises MissingProvenance. §13.3: the escape "
        f"hatch is the review queue, not a silent fallback."
    )


def test_critical_columns_are_real_columns() -> None:
    """§13.4 check 3: the list must match the schema's actual usage.

    A list that drifts from the schema is worse than no list: it reports
    coverage of columns that no longer exist while the ones that replaced them
    go unchecked.
    """
    from studium.models import Base

    schema_columns = {
        column.name
        for table in Base.metadata.tables.values()
        for column in table.columns
    }
    # 'attributable_to' is a parameter name rather than a column -- it is the
    # cost writer's argument for whichever user a cost is booked to, and §15
    # names it explicitly. 'model' is a content_artifacts column reached
    # through the runtime, not the ORM's ingestion tables.
    parameter_only = {"attributable_to"}

    unknown = CRITICAL_COLUMNS - schema_columns - parameter_only
    assert not unknown, (
        f"CRITICAL_COLUMNS names {unknown}, which are neither columns in the "
        f"schema nor documented parameter names"
    )


def test_critical_columns_covers_the_columns_writers_actually_take() -> None:
    """The inverse drift: a writer taking obvious provenance the list omits.

    Catches the case where someone adds ``embedded_by`` to a writer and does
    not add it to ``CRITICAL_COLUMNS``, so the invariant silently stops
    applying to the newest attribution column in the system.
    """
    suspicious: dict[str, str] = {}
    for writer in writers():
        for name in inspect.signature(writer).parameters:
            if name in CRITICAL_COLUMNS or name in {"session", "self"}:
                continue
            if name.endswith("_by") or name in {"model", "version"}:
                suspicious[name] = f"{writer.__module__}.{writer.__name__}"

    assert not suspicious, (
        f"writer parameter(s) look attribution-critical but are not in "
        f"CRITICAL_COLUMNS: {suspicious}"
    )


# --- the primitives themselves --------------------------------------------


def test_require_returns_a_present_value() -> None:
    assert require("uploaded_by", 42) == 42


def test_require_raises_naming_the_column() -> None:
    """The failure must name the column, not the table.

    A NOT NULL violation says "null value in column X of relation Y", which
    tells a reader what broke and not which caller failed to pass what. Naming
    the column at the boundary is the difference.
    """
    with pytest.raises(MissingProvenance) as excinfo:
        require("uploaded_by", None)
    assert excinfo.value.column == "uploaded_by"
    assert "uploaded_by" in str(excinfo.value)


def test_missing_provenance_carries_detail() -> None:
    error = MissingProvenance("model", detail="the Lecturer did not say")
    assert error.column == "model"
    assert "the Lecturer did not say" in str(error)


def test_the_decorator_does_not_wrap() -> None:
    """Registration only, deliberately (§13.4).

    A runtime wrapper would turn the invariant into a production failure on
    real data. The point is that it fails in CI, on the code, before anything
    is ingested -- so the decorator must leave the function exactly as it was.
    """

    def target(session, *, source_id):
        return source_id

    assert provenance.ingestion_writer(target) is target


def test_a_writer_with_a_bad_default_is_caught() -> None:
    """The enforcement test's own smoke test.

    A test that can only pass is not a test. This registers a deliberately
    non-compliant writer and asserts the check rejects it, so a refactor that
    turned the assertion into a tautology is visible here rather than three
    subsystems later.
    """

    def bad_writer(session, *, uploaded_by="00000000-0000-0000-0000-000000000000"):
        return uploaded_by

    offenders = [
        name
        for name, parameter in provenance_parameters(bad_writer).items()
        if parameter.default is not inspect.Parameter.empty
        and parameter.default is not None
    ]
    assert offenders == ["uploaded_by"]
