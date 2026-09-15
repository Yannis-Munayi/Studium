"""The operational calendar (SPEC_DEBT SD10). Tier 1: no database, no network.

Two things are being tested and they are worth telling apart.

The first is arithmetic -- which obligations a window selects, where a
quarterly cadence lands from the 31st. That is ordinary and the tests are
ordinary.

The second is the file. This calendar earns its place over a reminder service
by being diffable: ``--complete`` has to change two lines and leave the
comments explaining where each cadence came from exactly where they were. A
round trip that reformats the file passes every arithmetic test and destroys
the property the design rests on, so the byte-level tests below are the ones
that matter. Newline handling is in there because ``Path.write_text`` on
Windows rewrites every line of an LF file, silently, and that is precisely the
whole-file diff this is meant to prevent.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from studium.ops import calendar as cal
from studium.ops.cli import main

pytest.importorskip("yaml")

TODAY = dt.date(2026, 9, 6)

FIXTURE = """\
# A comment that must survive every rewrite.

obligations:
  - name: weekly_thing
    description: "Something weekly"
    cadence: weekly
    next_due: 2026-09-11
    owner: dev
    runbook: docs/ops/README.md
    last_completed: null

  # A comment in the middle of the list.
  - name: quarterly_thing
    description: "Something quarterly"
    cadence: quarterly
    next_due: 2026-08-01
    owner: yannis
    runbook: docs/ops/README.md
    last_completed: 2026-05-01

  - name: event_thing
    description: "Something triggered by adding a source"
    cadence: on_source_add
    next_due: null
    owner: yannis
    runbook: docs/ops/README.md
    last_completed: null
"""


@pytest.fixture
def calendar_file(tmp_path: Path) -> Path:
    path = tmp_path / "operational-calendar.yml"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(FIXTURE)
    return path


# --- the shipped calendar --------------------------------------------------


def test_the_shipped_calendar_holds_the_nine_obligations() -> None:
    obligations = cal.load()
    assert len(obligations) == 9
    assert {o.name for o in obligations} == {
        "key_rotation",
        "secret_rotation",
        "backup_drill",
        "signing_key_rotation",
        "migration_downgrade_signoff",
        "license_review",
        "content_review_queue_triage",
        "evaluation_regression_audit",
        "portfolio_credential_audit",
    }


def test_every_shipped_runbook_resolves() -> None:
    """A calendar of dead links is SD10's failure wearing a different hat: the
    reminder fires, the operator opens the runbook, and it is not there."""
    assert cal.check_runbooks(cal.load()) == []


def test_every_shipped_obligation_names_an_owner_and_a_spec_section() -> None:
    for obligation in cal.load():
        assert obligation.owner in {"dev", "yannis"}
        assert "§" in obligation.description, (
            f"{obligation.name} does not say which section put it on the list, "
            f"so nobody can check whether it is still required"
        )


# --- validation ------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


#: The list marker sits on its own line, unlike the shipped calendar's
#: ``- name:``. That is so a test can delete any single field line -- ``name``
#: included -- and still hand ``load`` a well-formed list, rather than a
#: document with no list in it at all. Nothing here is rewritten by
#: ``complete``, which is the only code that cares about the other shape.
ONE_ENTRY = """\
obligations:
  -
    name: {name}
    description: {description}
    cadence: {cadence}
    next_due: {next_due}
    owner: {owner}
    runbook: docs/ops/README.md
    last_completed: null
"""

GOOD_ENTRY = {
    "name": "a_thing",
    "description": '"Something"',
    "cadence": "weekly",
    "next_due": "2026-09-11",
    "owner": "dev",
}


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("cadence", "fortnightly", "is not one of"),
        ("next_due", "soon", "expected an ISO date"),
        ("next_due", "null", "needs a next_due date"),
        ("owner", "''", "must be a non-empty string"),
        ("description", "''", "must be a non-empty string"),
        ("name", "A-Thing", "lower_snake_case"),
    ],
    ids=["bad-cadence", "bad-date", "no-due-date", "empty-owner", "empty-desc", "name"],
)
def test_verify_catches_malformed_entries(
    calendar_file: Path, field: str, value: str, expected: str
) -> None:
    """One entry, broken one way at a time. Each has to be *named*: the
    operator reaching for this command is usually not the person who wrote the
    entry."""
    _write(calendar_file, ONE_ENTRY.format(**{**GOOD_ENTRY, field: value}))

    with pytest.raises(cal.CalendarError) as excinfo:
        cal.load(calendar_file)
    assert any(expected in problem for problem in excinfo.value.problems), (
        excinfo.value.problems
    )


@pytest.mark.parametrize("field", sorted(cal.REQUIRED_FIELDS))
def test_verify_catches_a_missing_field(calendar_file: Path, field: str) -> None:
    """Including the two nullable ones. A `last_completed:` key that is absent
    and one that is null are different states, and `--complete` rewrites values
    in place -- a key that is not there is a key it cannot set."""
    text = ONE_ENTRY.format(**GOOD_ENTRY)
    stripped = "".join(
        line
        for line in text.splitlines(keepends=True)
        if not line.strip().startswith(f"{field}:")
    )
    _write(calendar_file, stripped)

    with pytest.raises(cal.CalendarError, match="missing required field"):
        cal.load(calendar_file)


def test_an_event_cadence_may_not_carry_a_due_date(calendar_file: Path) -> None:
    """A date on an event-triggered row is a deadline nothing will ever
    advance, so it would sit in --due-this-week forever."""
    _write(
        calendar_file,
        FIXTURE.replace(
            "    cadence: on_source_add\n    next_due: null\n",
            "    cadence: on_source_add\n    next_due: 2026-09-09\n",
        ),
    )
    with pytest.raises(cal.CalendarError, match="must be null"):
        cal.load(calendar_file)


def test_a_duplicate_name_is_refused(calendar_file: Path) -> None:
    """``--complete`` takes a name. Two rows with one name make it ambiguous
    which was completed, and the second would silently never advance."""
    _write(calendar_file, FIXTURE + FIXTURE.split("obligations:\n", 1)[1])
    with pytest.raises(cal.CalendarError, match="duplicate name"):
        cal.load(calendar_file)


def test_every_problem_is_reported_not_just_the_first(calendar_file: Path) -> None:
    _write(
        calendar_file,
        FIXTURE.replace("cadence: weekly", "cadence: fortnightly").replace(
            "next_due: 2026-08-01", "next_due: whenever"
        ),
    )
    with pytest.raises(cal.CalendarError) as excinfo:
        cal.load(calendar_file)
    assert len(excinfo.value.problems) >= 2


# --- queries ---------------------------------------------------------------


def test_due_this_week_selects_the_next_seven_days(calendar_file: Path) -> None:
    due = cal.due_within(cal.load(calendar_file), 7, today=TODAY)
    assert [o.name for o in due] == ["quarterly_thing", "weekly_thing"]


def test_overdue_items_are_included_and_sort_first(calendar_file: Path) -> None:
    """A window query that dropped them would answer "nothing due this week"
    for a rotation that has been late since August."""
    due = cal.due_within(cal.load(calendar_file), 7, today=TODAY)
    assert due[0].name == "quarterly_thing"
    assert due[0].status(TODAY) == "OVERDUE by 36d"


def test_event_triggered_items_are_never_due(calendar_file: Path) -> None:
    obligations = cal.load(calendar_file)
    assert "event_thing" not in {
        o.name for o in cal.due_within(obligations, 3650, today=TODAY)
    }
    assert [o.name for o in cal.for_event(obligations, "on_source_add")] == [
        "event_thing"
    ]


def test_list_all_puts_event_triggered_items_last(calendar_file: Path) -> None:
    assert [o.name for o in cal.in_order(cal.load(calendar_file))] == [
        "quarterly_thing",
        "weekly_thing",
        "event_thing",
    ]


# --- advancing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("cadence", "expected"),
    [
        ("daily", dt.date(2026, 9, 7)),
        ("weekly", dt.date(2026, 9, 13)),
        ("monthly", dt.date(2026, 10, 6)),
        ("quarterly", dt.date(2026, 12, 6)),
        ("annually", dt.date(2027, 9, 6)),
    ],
)
def test_advance_by_cadence(cadence: str, expected: dt.date) -> None:
    assert cal.advance(cadence, TODAY) == expected


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (dt.date(2026, 8, 31), 3, dt.date(2026, 11, 30)),  # 31 Nov does not exist
        (dt.date(2026, 12, 31), 3, dt.date(2027, 3, 31)),  # crosses the year
        (dt.date(2027, 1, 31), 1, dt.date(2027, 2, 28)),  # short month
        (dt.date(2028, 1, 31), 1, dt.date(2028, 2, 29)),  # leap year
        (dt.date(2026, 10, 15), 12, dt.date(2027, 10, 15)),
    ],
)
def test_month_arithmetic_clamps_to_the_end_of_the_month(
    start: dt.date, months: int, expected: dt.date
) -> None:
    """Quarterly means three calendar months, not ninety days -- the day count
    drifts a rotation backwards through the year until it lands somewhere
    nobody is working."""
    assert cal._add_months(start, months) == expected


def test_an_event_cadence_advances_to_nothing() -> None:
    assert cal.advance("on_source_add", TODAY) is None


# --- completion, and the file it leaves behind -----------------------------


def test_complete_advances_from_today_not_from_the_due_date(
    calendar_file: Path,
) -> None:
    """``quarterly_thing`` is 36 days overdue. Advancing from ``next_due``
    would leave it overdue after being done, and an operator who saw that would
    stop believing the list."""
    updated = cal.complete("quarterly_thing", path=calendar_file, today=TODAY)
    assert updated.last_completed == TODAY
    assert updated.next_due == dt.date(2026, 12, 6)
    assert updated.status(TODAY) == "in 91d"


def test_complete_changes_two_lines_and_nothing_else(calendar_file: Path) -> None:
    """The property the whole design rests on. A round trip through
    ``yaml.safe_dump`` passes every other test in this file and fails this
    one."""
    before = calendar_file.read_text(encoding="utf-8").splitlines()
    cal.complete("weekly_thing", path=calendar_file, today=TODAY)
    after = calendar_file.read_text(encoding="utf-8").splitlines()

    assert len(before) == len(after)
    changed = [
        i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b
    ]
    assert len(changed) == 2, [
        (before[i], after[i]) for i in changed
    ]


def test_complete_keeps_the_comments(calendar_file: Path) -> None:
    cal.complete("weekly_thing", path=calendar_file, today=TODAY)
    text = calendar_file.read_text(encoding="utf-8")
    assert "# A comment that must survive every rewrite." in text
    assert "# A comment in the middle of the list." in text


@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_complete_preserves_the_files_line_endings(tmp_path: Path, eol: str) -> None:
    """``Path.write_text`` translates on write, so an LF file round-tripped on
    a Windows workstation comes back CRLF -- every line changed, in a diff
    nobody can read, for a one-word completion."""
    path = tmp_path / "cal.yml"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(FIXTURE.replace("\n", eol))

    cal.complete("weekly_thing", path=path, today=TODAY)

    raw = path.read_bytes()
    assert raw.count(b"\r\n") == (0 if eol == "\n" else raw.count(b"\n"))


def test_complete_on_an_event_obligation_leaves_next_due_null(
    calendar_file: Path,
) -> None:
    updated = cal.complete("event_thing", path=calendar_file, today=TODAY)
    assert updated.next_due is None
    assert updated.last_completed == TODAY


def test_complete_refuses_an_unknown_name(calendar_file: Path) -> None:
    with pytest.raises(cal.CalendarError, match="no obligation named"):
        cal.complete("not_a_thing", path=calendar_file, today=TODAY)


def test_completing_twice_advances_twice(calendar_file: Path) -> None:
    """The rewrite has to be re-readable by the parser that produced it, which
    a substitution that broke the indentation would not be."""
    cal.complete("weekly_thing", path=calendar_file, today=TODAY)
    second = cal.complete(
        "weekly_thing", path=calendar_file, today=TODAY + dt.timedelta(days=7)
    )
    assert second.next_due == dt.date(2026, 9, 20)


# --- adding ----------------------------------------------------------------


def test_append_adds_a_readable_entry(calendar_file: Path) -> None:
    cal.append(
        cal.Obligation(
            name="new_thing",
            description="Something new",
            cadence="monthly",
            owner="dev",
            runbook="docs/ops/README.md",
            next_due=dt.date(2026, 10, 1),
        ),
        path=calendar_file,
    )
    obligations = cal.load(calendar_file)
    assert [o.name for o in obligations][-1] == "new_thing"
    assert obligations[-1].next_due == dt.date(2026, 10, 1)
    assert obligations[-1].last_completed is None


def test_append_refuses_a_duplicate(calendar_file: Path) -> None:
    with pytest.raises(cal.CalendarError, match="already in the calendar"):
        cal.append(
            cal.Obligation(
                name="weekly_thing",
                description="A second one",
                cadence="weekly",
                owner="dev",
                runbook="docs/ops/README.md",
                next_due=dt.date(2026, 10, 1),
            ),
            path=calendar_file,
        )


# --- the command line ------------------------------------------------------
#
# The CLI reads the real clock, so these run against a calendar whose dates are
# written relative to today. A fixed-date fixture here would pass now and start
# failing on 11 September 2026, which is the kind of test that gets deleted
# rather than read.


@pytest.fixture
def live_calendar(tmp_path: Path) -> Path:
    today = dt.date.today()
    path = tmp_path / "live.yml"
    _write(
        path,
        "obligations:\n"
        + "".join(
            ONE_ENTRY.format(
                name=name,
                description=f'"{name}"',
                cadence=cadence,
                next_due=due,
                owner="dev",
            ).removeprefix("obligations:\n")
            for name, cadence, due in (
                ("late_thing", "quarterly", (today - dt.timedelta(days=30)).isoformat()),
                ("soon_thing", "weekly", (today + dt.timedelta(days=3)).isoformat()),
                ("later_thing", "monthly", (today + dt.timedelta(days=20)).isoformat()),
                ("event_thing", "on_source_add", "null"),
            )
        ),
    )
    return path


def _run(capsys, *argv: str) -> tuple[int, str]:
    code = main(["ops", "calendar", *argv])
    return code, capsys.readouterr().out


def test_cli_list_all(live_calendar: Path, capsys) -> None:
    code, out = _run(capsys, "--file", str(live_calendar), "--list-all")
    assert code == 0, "a listing reports; it does not judge"
    assert "4 obligation(s)" in out
    for name in ("late_thing", "soon_thing", "later_thing", "event_thing"):
        assert name in out


def test_cli_due_this_week_exits_non_zero_when_something_is_overdue(
    live_calendar: Path, capsys
) -> None:
    """Exit codes are this CLI's interface. Non-zero on *overdue* is what lets
    the last gap SD10 names close with a cron line rather than more code."""
    code, out = _run(capsys, "--file", str(live_calendar), "--due-this-week")
    assert code == 1
    assert "1 overdue: late_thing" in out
    assert "soon_thing" in out
    assert "later_thing" not in out, "20 days away is not this week"


def test_cli_due_this_week_exits_zero_when_nothing_is_late(
    live_calendar: Path, capsys
) -> None:
    cal.complete("late_thing", path=live_calendar)
    code, out = _run(capsys, "--file", str(live_calendar), "--due-this-week")
    assert code == 0
    assert "0 overdue" in out


def test_cli_due_this_month_is_the_wider_window(live_calendar: Path, capsys) -> None:
    _, month = _run(capsys, "--file", str(live_calendar), "--due-this-month")
    assert "later_thing" in month


def test_cli_complete_updates_the_file(live_calendar: Path, capsys) -> None:
    code, out = _run(capsys, "--file", str(live_calendar), "--complete", "soon_thing")
    assert code == 0
    assert "completed soon_thing" in out
    assert "Commit the change" in out

    reloaded = {o.name: o for o in cal.load(live_calendar)}
    assert reloaded["soon_thing"].last_completed == dt.date.today()
    assert reloaded["soon_thing"].next_due == dt.date.today() + dt.timedelta(days=7)
    assert reloaded["late_thing"].last_completed is None, "touched the wrong entry"


def test_cli_verify_reports_problems_and_exits_non_zero(
    calendar_file: Path, capsys
) -> None:
    _write(calendar_file, FIXTURE.replace("cadence: weekly", "cadence: fortnightly"))
    code = main(["ops", "calendar", "--file", str(calendar_file), "--verify"])
    assert code == 1
    assert "is not one of" in capsys.readouterr().err


def test_cli_verify_accepts_the_shipped_calendar(capsys) -> None:
    assert main(["ops", "calendar", "--verify"]) == 0
    assert "9 obligation(s) valid" in capsys.readouterr().out


def test_cli_add_refuses_a_non_interactive_stdin(live_calendar: Path, capsys) -> None:
    """Reading a blank line for every field would write an entry of empty
    strings that ``--verify`` then rejects, in a file the operator did not
    realise had been touched."""
    before = live_calendar.read_bytes()
    code = main(["ops", "calendar", "--file", str(live_calendar), "--add"])
    assert code == 1
    assert "needs a terminal" in capsys.readouterr().err
    assert live_calendar.read_bytes() == before, "refused, but wrote anyway"
