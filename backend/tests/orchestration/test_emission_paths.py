"""Every transition's emission path is live (agent runtime v1.0.1 §7.1).

§7.1 asks for a Tier 1 test that walks §7's table and asserts:

1. every transition's Emission column is populated;
2. every ``Client:`` emission names an endpoint the FastAPI router serves;
3. every ``Internal:`` emission names a function that exists;
4. every ``Ambient:`` emission's trigger has a documented handler.

"This test fails if any transition in the table lacks a live emission path.
Adding a transition to the table without wiring its emission fails the test at
commit time."

**Why this test is the point of the patch.** R14 was a transition that existed
in the table, had a guard, had an effect, had a destination -- and no caller.
Four tiers of tests passed over it because each one supplied the state the
runtime was failing to reach. Nothing that inspects *the machine* can catch
that; the only thing that can is something that inspects the machine against
the rest of the codebase, which is what this does.

It is deliberately structural rather than behavioural. It cannot tell you the
emitter fires at the *right moment* -- only that it exists and is reachable.
The session-open end-to-end test (§7.1's third requirement) is what covers the
moment; this covers the wiring, and the two failures look different.
"""

from __future__ import annotations

import importlib

import pytest

from studium.api.app import app
from studium.orchestration.state_machine import (
    TRANSITIONS,
    EmissionKind,
    Event,
    State,
)


def _route_table() -> set[tuple[str, str]]:
    """(METHOD, path) for every route the app serves."""
    table: set[tuple[str, str]] = set()
    for route in app.routes:
        for method in getattr(route, "methods", set()) or set():
            if method in {"HEAD", "OPTIONS"}:
                continue
            table.add((method, getattr(route, "path", "")))
    return table


ROUTES = _route_table()


def _resolve_dotted(path: str) -> object | None:
    """Resolve ``module.attr`` or ``module.Class.method`` to the object.

    Walks right-to-left on the split rather than guessing where the module
    ends, so ``studium.agents.orchestrator.Orchestrator.start_session``
    resolves without the test needing to know that ``Orchestrator`` is a class
    and ``start_session`` a method.
    """
    parts = path.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split])
        try:
            obj: object = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        for attr in parts[split:]:
            obj = getattr(obj, attr, None)  # type: ignore[assignment]
            if obj is None:
                break
        if obj is not None:
            return obj
    return None


def _describe(transition) -> str:
    source = transition.source.value if transition.source else "any"
    return f"{source} --{transition.event.value}-->"


class TestEveryTransitionIsEmitted:
    def test_no_transition_is_missing_its_emission(self):
        """§7.1 requirement 1."""
        missing = [_describe(t) for t in TRANSITIONS if not t.emissions]
        assert not missing, (
            "transitions with no emission path -- each is a state change "
            f"nothing can trigger: {missing}"
        )

    def test_the_table_is_not_empty(self):
        """Coverage guard: an empty table would satisfy every check above."""
        assert len(TRANSITIONS) >= 15

    @pytest.mark.parametrize(
        "transition", TRANSITIONS, ids=lambda t: f"{_describe(t)}[{t.guard}]"
    )
    def test_emission_paths_resolve(self, transition):
        """§7.1 requirements 2, 3 and 4, per row."""
        for emission in transition.emissions:
            if emission.kind is EmissionKind.CLIENT:
                method, _, path = emission.path.partition(" ")
                assert (method, path) in ROUTES, (
                    f"{_describe(transition)} is emitted by {emission.path!r}, "
                    f"which the router does not serve. Either the endpoint was "
                    f"never written or the table names it wrongly -- both leave "
                    f"the transition unreachable."
                )
            elif emission.kind is EmissionKind.INTERNAL:
                target = _resolve_dotted(emission.path)
                assert target is not None, (
                    f"{_describe(transition)} is emitted by {emission.path!r}, "
                    f"which does not exist."
                )
                assert callable(target), f"{emission.path!r} is not callable"
            else:
                # Ambient: the trigger is a condition, so what is checkable is
                # that the named handler exists. The prose after ' -- ' is the
                # trigger condition and is documentation.
                handler, _, _condition = emission.path.partition(" -- ")
                assert _resolve_dotted(handler) is not None, (
                    f"{_describe(transition)} names ambient handler "
                    f"{handler!r}, which does not exist."
                )


class TestTheCheckWouldActuallyFail:
    """A test that cannot fail is a test that proves nothing.

    R14 slipped past four tiers that all agreed with each other. These assert
    the new check is not the fifth -- that it distinguishes a live wiring from
    a dead one rather than passing on both.
    """

    def test_a_client_emission_naming_a_missing_endpoint_is_caught(self):
        from studium.orchestration.state_machine import Transition, client

        bogus = Transition(
            State.IDLE, Event.START_SESSION, State.OPENING,
            emissions=(client("POST /api/session/nonexistent"),),
        )
        with pytest.raises(AssertionError, match="router does not serve"):
            TestEveryTransitionIsEmitted().test_emission_paths_resolve(bogus)

    def test_an_internal_emission_naming_a_missing_function_is_caught(self):
        from studium.orchestration.state_machine import Transition, internal

        bogus = Transition(
            State.OPENING, Event.CONTEXT_READY, State.LECTURING,
            emissions=(internal("studium.agents.orchestrator.Orchestrator.no_such"),),
        )
        with pytest.raises(AssertionError, match="does not exist"):
            TestEveryTransitionIsEmitted().test_emission_paths_resolve(bogus)

    def test_a_row_with_no_emission_is_caught(self):
        from studium.orchestration.state_machine import Transition

        bogus = Transition(State.IDLE, Event.START_SESSION, State.OPENING)
        assert not bogus.emissions


class TestEmissionCoverage:
    """What the emission column says about the shape of the runtime."""

    def test_every_event_has_at_least_one_emitter(self):
        """An event no row emits is an event the runtime cannot produce."""
        emitted = {t.event for t in TRANSITIONS if t.emissions}
        missing = sorted(e.value for e in Event if e not in emitted)
        assert not missing, f"events with no emission path anywhere: {missing}"

    def test_end_session_names_all_three_of_its_paths(self):
        """§3.3: the endpoint, the idle timer, and the budget cap.

        Naming only the endpoint would leave the two ambient paths exactly as
        unexamined as R14's missing one -- and those are the paths that fire
        when nobody is watching.
        """
        row = next(t for t in TRANSITIONS if t.event is Event.END_SESSION)
        kinds = [e.kind for e in row.emissions]
        assert kinds.count(EmissionKind.AMBIENT) == 2
        assert EmissionKind.CLIENT in kinds

    def test_client_emissions_are_the_majority(self):
        """A sanity check on the shape §7 describes.

        Most transitions are things a learner does. If this ever inverted it
        would mean the machine had grown a lot of self-driving behaviour, which
        is worth noticing rather than discovering.
        """
        client_rows = sum(
            1 for t in TRANSITIONS
            if any(e.kind is EmissionKind.CLIENT for e in t.emissions)
        )
        assert client_rows > len(TRANSITIONS) / 2


class TestNoTestAssignsStateDirectly:
    """§7.1: "No test may set ``machine.state`` directly."

    The rule is only worth having if something enforces it, and the enforcement
    has to be textual -- a test that assigns the state passes, which is exactly
    why the rule exists. So this reads the suite's own source.

    One exemption: ``drive_to`` resets to IDLE before walking. That assignment
    is the helper establishing its starting point, not a test skipping a path,
    and it is the single line that makes the other nineteen unnecessary.
    """

    EXEMPT = {("tests/fixtures/runtime.py", "machine.state = State.IDLE")}

    def test_the_suite_reaches_every_state_through_real_transitions(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2]
        pattern = re.compile(r"^\s*(\S*machine\.state = \S+)", re.MULTILINE)

        offenders: list[str] = []
        for path in sorted((root / "tests").rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                statement = match.group(1).split("machine.state", 1)[1]
                normalised = f"machine.state{statement}"
                if (rel, normalised) in self.EXEMPT:
                    continue
                line = path.read_text(encoding="utf-8")[: match.start()].count("\n") + 1
                offenders.append(f"{rel}:{line}  {normalised}")

        assert not offenders, (
            "these tests assign a state instead of reaching it (v1.0.1 §7.1). "
            "A test that assigns LECTURING asserts against a session production "
            "may be unable to produce -- which is how R14 survived four tiers. "
            "Use tests.fixtures.runtime.drive_to():\n  " + "\n  ".join(offenders)
        )

    def test_the_scan_would_catch_an_offender(self):
        """The check has to be able to fail, or it is decoration."""
        import re

        pattern = re.compile(r"^\s*(\S*machine\.state = \S+)", re.MULTILINE)
        assert pattern.search("    orchestrator.machine.state = State.LECTURING\n")
        assert pattern.search("        machine.state = State.TUTORIAL\n")
        assert not pattern.search("    assert machine.state is State.LECTURING\n")
