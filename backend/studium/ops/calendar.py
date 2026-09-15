"""The operational calendar, and its keeper (SPEC_DEBT SD10).

SD10's complaint: the infrastructure spec puts nine recurring obligations on
the operator and exactly one of them -- the nightly retention pass -- has a
mechanism. Every other row depends on a human remembering, and §3 is unusually
firm that it must not: "Rotation is scheduled, not reactive. ... skipping
rotation 'because nothing's wrong' is how the muscle atrophies."

SD10 offered three resolutions and this is the second of them, narrowed. The
entry imagined reading the last occurrence of each task out of the database --
``retention_actions.ran_at``, ``signing_keys.activated_at`` and so on -- and
noted that two of the obligations have no durable record anywhere, "which is a
small table nobody has specified". This implementation specifies no table. The
record is a YAML file in the repository, and that choice is the design:

* **It survives the operator.** A reminder in someone's calendar leaves with
  them. A file in the repo is inherited by whoever clones it next, along with
  the runbook each row points at.
* **It is reviewable.** ``--complete`` changes two lines, so ``git log -p
  content/operational-calendar.yml`` is the audit trail -- who said the drill
  was done, when, and in which commit. A database row would have to grow an
  actor column and a reason column to say as much.
* **It cannot silently disagree with the deployment**, because it is deployed
  with it. A dashboard configured out-of-band drifts; a file in the image does
  not.

The cost, stated plainly: nothing *runs* this. It reports when asked, which is
one step better than the memory it replaces and one step short of an alert.
``--due-this-week`` exits non-zero when something is overdue precisely so the
step that closes that gap is a line in a cron job rather than more code here.

**Completion advances from the day the work was done, not from the date it was
due.** An item three weeks overdue that is then completed is not still overdue;
advancing from ``next_due`` would leave it that way, and an operator who saw a
completed obligation still listed as late would stop believing the list.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: The calendar itself. In ``content/`` rather than ``docs/`` because it is
#: data the CLI reads and writes, not prose; shipped in the image by the
#: Dockerfile's ``COPY . .``, so ``fly ssh console`` can answer "what is due".
DEFAULT_PATH = BACKEND_ROOT / "content" / "operational-calendar.yml"

#: Time-triggered cadences and the interval each advances by. Ordered
#: shortest-first, which is the order ``--list-all`` breaks ties in.
CADENCE_MONTHS: dict[str, int] = {"monthly": 1, "quarterly": 3, "annually": 12}
CADENCE_DAYS: dict[str, int] = {"daily": 1, "weekly": 7}

#: Event-triggered obligations. They carry no ``next_due`` -- there is no date
#: at which adding a source becomes overdue -- and are surfaced by
#: ``--list-all`` rather than by the date queries. Naming the events rather
#: than allowing any ``on_*`` string keeps a typo from creating an obligation
#: that is never triggered by anything and never reported as broken.
EVENT_CADENCES: frozenset[str] = frozenset(
    {"on_source_add", "on_migration_downgrade"}
)

CADENCES: frozenset[str] = frozenset(
    set(CADENCE_MONTHS) | set(CADENCE_DAYS) | EVENT_CADENCES
)

REQUIRED_FIELDS = (
    "name",
    "description",
    "cadence",
    "next_due",
    "owner",
    "runbook",
    "last_completed",
)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _read(path: Path) -> str:
    """Read with newline translation off, so the file's own endings survive.

    ``Path.read_text`` / ``write_text`` translate by default: read on any
    platform gives ``\\n``, and write on Windows gives ``\\r\\n``. Round-tripping
    an LF file through them on a Windows workstation rewrites **every line**,
    which turns a one-word completion into a whole-file diff and takes the git
    history -- the entire reason this calendar is a file rather than a table --
    with it. ``newline=""`` on both sides makes the round trip a no-op.

    ``open`` rather than ``Path.read_text(newline=...)`` because that keyword
    arrived in 3.12's successor and pyproject's floor is 3.12.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _dominant_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


class CalendarError(ValueError):
    """The calendar file is unusable. Carries every problem, not the first.

    One problem at a time turns a malformed file into a sequence of edit-run
    cycles, and the operator reaching for this command is usually not the
    person who wrote the entry.
    """

    def __init__(self, problems: list[str], *, path: Path | None = None) -> None:
        self.problems = problems
        self.path = path
        where = f"{path}: " if path else ""
        super().__init__(where + "; ".join(problems))


@dataclass(frozen=True, slots=True)
class Obligation:
    name: str
    description: str
    cadence: str
    owner: str
    runbook: str
    #: None for the event-triggered cadences, and only for those.
    next_due: dt.date | None = None
    last_completed: dt.date | None = None

    @property
    def event_triggered(self) -> bool:
        return self.cadence in EVENT_CADENCES

    def days_until(self, today: dt.date) -> int | None:
        if self.next_due is None:
            return None
        return (self.next_due - today).days

    def status(self, today: dt.date) -> str:
        days = self.days_until(today)
        if days is None:
            return "on event"
        if days < 0:
            return f"OVERDUE by {-days}d"
        if days == 0:
            return "due today"
        return f"in {days}d"

    def render(self, today: dt.date) -> str:
        due = self.next_due.isoformat() if self.next_due else "--"
        done = self.last_completed.isoformat() if self.last_completed else "never"
        return (
            f"  {self.name:30} {self.cadence:22} {due:12} "
            f"{self.status(today):16} {self.owner:8} last: {done}"
        )


# --- reading ---------------------------------------------------------------


def load(path: Path | None = None) -> list[Obligation]:
    """Parse and validate the whole file, or raise with every problem in it."""
    import yaml

    path = path or DEFAULT_PATH
    try:
        raw = _read(path)
    except OSError as exc:
        raise CalendarError([f"cannot read: {exc}"], path=path) from exc

    try:
        # safe_load, never load: the same reasoning as
        # ``ingestion.authoring.load_yaml``. This file is edited by hand and
        # full load constructs arbitrary Python objects.
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise CalendarError([f"invalid YAML: {exc}"], path=path) from exc

    if not isinstance(parsed, dict) or "obligations" not in parsed:
        raise CalendarError(["expected a top-level 'obligations:' list"], path=path)
    entries = parsed["obligations"]
    if not isinstance(entries, list) or not entries:
        raise CalendarError(["'obligations' must be a non-empty list"], path=path)

    problems: list[str] = []
    obligations: list[Obligation] = []
    seen: set[str] = set()

    for index, entry in enumerate(entries):
        label = f"obligation {index}"
        if not isinstance(entry, dict):
            problems.append(f"{label}: expected a mapping")
            continue
        name = entry.get("name")
        if isinstance(name, str):
            label = name
        problems_before = len(problems)
        obligation = _parse_entry(entry, label, problems, seen)
        if obligation is not None and len(problems) == problems_before:
            obligations.append(obligation)

    if problems:
        raise CalendarError(problems, path=path)
    return obligations


def _parse_entry(
    entry: dict, label: str, problems: list[str], seen: set[str]
) -> Obligation | None:
    missing = [f for f in REQUIRED_FIELDS if f not in entry]
    if missing:
        # Including the two nullable fields. Their *absence* and their being
        # null are different states -- "never completed" is a fact worth
        # writing down -- and `--complete` rewrites values in place, so a key
        # that is not there is a key it cannot set.
        problems.append(f"{label}: missing required field(s) {', '.join(missing)}")
        return None

    name = entry["name"]
    if not isinstance(name, str) or not _NAME_RE.match(name):
        problems.append(f"{label}: name must be lower_snake_case")
        return None
    if name in seen:
        problems.append(f"{label}: duplicate name")
        return None
    seen.add(name)

    cadence = entry["cadence"]
    if cadence not in CADENCES:
        problems.append(
            f"{name}: cadence {cadence!r} is not one of {sorted(CADENCES)}"
        )
        return None

    next_due = _parse_date(entry["next_due"], f"{name}.next_due", problems)
    last_completed = _parse_date(
        entry["last_completed"], f"{name}.last_completed", problems
    )

    event = cadence in EVENT_CADENCES
    if event and next_due is not None:
        problems.append(
            f"{name}: cadence {cadence} is event-triggered, so next_due must be "
            f"null -- a date here is a deadline nothing will ever advance"
        )
    if not event and next_due is None:
        problems.append(f"{name}: cadence {cadence} needs a next_due date")

    for field in ("description", "owner", "runbook"):
        value = entry[field]
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{name}: {field} must be a non-empty string")

    if problems:
        return None
    return Obligation(
        name=name,
        description=entry["description"],
        cadence=cadence,
        owner=entry["owner"],
        runbook=entry["runbook"],
        next_due=next_due,
        last_completed=last_completed,
    )


def _parse_date(value: object, label: str, problems: list[str]) -> dt.date | None:
    if value is None:
        return None
    # PyYAML resolves an unquoted YYYY-MM-DD to a date already; a quoted one
    # arrives as a string. Both are accepted, and anything else is not -- an
    # entry that read `next_due: soon` would otherwise validate.
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            pass
    problems.append(f"{label}: expected an ISO date (YYYY-MM-DD) or null")
    return None


def check_runbooks(obligations: list[Obligation]) -> list[str]:
    """Runbooks that no longer resolve to a file.

    A calendar of dead links is the failure SD10 describes wearing a different
    hat: the reminder fires, the operator opens the runbook, and there is
    nothing there. Checked separately from :func:`load` because it depends on
    where the process is running -- the repository has ``docs/``, the deployed
    image does not, and "the file is missing" and "we are not in a checkout"
    are different answers.
    """
    repo_root = BACKEND_ROOT.parent
    if not (repo_root / "docs").is_dir():
        return []
    missing = []
    for obligation in obligations:
        # `path#anchor` is allowed: several obligations point at one runbook.
        target = obligation.runbook.split("#", 1)[0].strip()
        if not target or not (repo_root / target).is_file():
            missing.append(f"{obligation.name}: runbook {obligation.runbook!r} not found")
    return missing


# --- queries ---------------------------------------------------------------


def due_within(
    obligations: list[Obligation], days: int, *, today: dt.date
) -> list[Obligation]:
    """Everything due on or before ``today + days``, overdue items included.

    Overdue items are the point of asking. A window query that dropped them
    would answer "nothing due this week" for a rotation that has been late
    since March.
    """
    horizon = today + dt.timedelta(days=days)
    return sorted(
        (o for o in obligations if o.next_due is not None and o.next_due <= horizon),
        key=lambda o: (o.next_due, o.name),
    )


def overdue(obligations: list[Obligation], *, today: dt.date) -> list[Obligation]:
    return [
        o for o in obligations if o.next_due is not None and o.next_due < today
    ]


def in_order(obligations: list[Obligation]) -> list[Obligation]:
    """Sorted by next_due, event-triggered items last."""
    return sorted(
        obligations,
        key=lambda o: (o.next_due is None, o.next_due or dt.date.max, o.name),
    )


def for_event(obligations: list[Obligation], event: str) -> list[Obligation]:
    """The obligations a given event triggers (``on_source_add`` and friends)."""
    return sorted(
        (o for o in obligations if o.cadence == event), key=lambda o: o.name
    )


# --- advancing -------------------------------------------------------------


def advance(cadence: str, from_date: dt.date) -> dt.date | None:
    """The next due date after completing ``cadence`` work on ``from_date``."""
    if cadence in EVENT_CADENCES:
        return None
    if cadence in CADENCE_DAYS:
        return from_date + dt.timedelta(days=CADENCE_DAYS[cadence])
    if cadence in CADENCE_MONTHS:
        return _add_months(from_date, CADENCE_MONTHS[cadence])
    raise CalendarError([f"unknown cadence {cadence!r}"])


def _add_months(date: dt.date, months: int) -> dt.date:
    """Calendar months, clamping the day to the end of the target month.

    Quarterly work completed on 31 May is due on 31 August, and quarterly work
    completed on 31 December is due on 31 March -- but 31 November does not
    exist, so a +3 from 31 August lands on the 30th. Clamping keeps the
    interval a whole number of months, which is what "quarterly" means to the
    person reading the row; the alternative (90 days) drifts a rotation
    backwards through the year until it lands in a holiday.
    """
    month_index = date.month - 1 + months
    year = date.year + month_index // 12
    month = month_index % 12 + 1
    last_day = _days_in_month(year, month)
    return dt.date(year, month, min(date.day, last_day))


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).day


# --- writing ---------------------------------------------------------------


def complete(
    name: str, *, path: Path | None = None, today: dt.date | None = None
) -> Obligation:
    """Mark ``name`` done today and advance its next_due. Returns the new row.

    **Rewrites two lines, not the file.** Round-tripping through
    ``yaml.safe_dump`` would be five lines of code and would delete every
    comment in the file, reorder nothing predictably, and turn each completion
    into a whole-file diff -- destroying exactly the property that made a YAML
    file in the repo the right answer to SD10. So the two values are
    substituted in place, with the surrounding bytes untouched.
    """
    path = path or DEFAULT_PATH
    today = today or dt.date.today()

    obligations = load(path)
    current = next((o for o in obligations if o.name == name), None)
    if current is None:
        raise CalendarError(
            [
                f"no obligation named {name!r}; known names are "
                f"{', '.join(sorted(o.name for o in obligations))}"
            ],
            path=path,
        )

    next_due = advance(current.cadence, today)
    # join(splitlines(keepends=True)) is the identity, so everything outside
    # the two substituted lines is returned byte for byte.
    lines = _read(path).splitlines(keepends=True)
    start, end = _entry_span(lines, name, path)

    updated = _set_field(lines[start:end], "last_completed", today.isoformat())
    updated = _set_field(
        updated, "next_due", next_due.isoformat() if next_due else "null"
    )
    _write(path, "".join(lines[:start] + updated + lines[end:]))

    # Re-read rather than construct the answer: the point of printing the entry
    # is for the operator to check what is now in the file, and a value built
    # in memory would report the intent even if the substitution missed.
    refreshed = next(o for o in load(path) if o.name == name)
    return refreshed


#: A YAML sequence item, either ``- name: x`` or a bare ``-`` on its own line
#: with the mapping beneath it. Both are valid and this file is edited by hand,
#: so ``complete`` has to find an entry written either way rather than
#: refusing one on a formatting grounds the operator was never told about.
_ITEM_RE = re.compile(r"^(?P<indent>[^\S\r\n]*)-(?P<rest>[^\S\r\n].*|\s*)$")


def _entry_span(lines: list[str], name: str, path: Path) -> tuple[int, int]:
    """The half-open line range of the sequence item holding ``name``."""
    name_re = re.compile(rf"^[^\S\r\n]*(-[^\S\r\n]+)?name:[^\S\r\n]*{re.escape(name)}\s*$")

    start = indent = None
    for index, line in enumerate(lines):
        if not name_re.match(line):
            continue
        item = _ITEM_RE.match(line)
        if item is not None:  # `- name: x`
            start, indent = index, len(item.group("indent"))
            break
        # `name: x` under a bare `-`: walk back past comments and blanks to
        # the marker, so the rewritten span is the whole item.
        for back in range(index - 1, -1, -1):
            marker = _ITEM_RE.match(lines[back])
            if marker is not None:
                start, indent = back, len(marker.group("indent"))
                break
            if lines[back].strip() and not lines[back].lstrip().startswith("#"):
                break
        break

    if start is None or indent is None:
        raise CalendarError(
            [
                f"{name!r} parses but its sequence item could not be located "
                f"for rewriting -- the 'name:' line was not found under a '-' "
                f"marker. Edit the entry by hand."
            ],
            path=path,
        )

    # The next item at the *same* indent. Matching any depth would end the span
    # at a markdown bullet inside a folded description.
    for index in range(start + 1, len(lines)):
        item = _ITEM_RE.match(lines[index])
        if item is not None and len(item.group("indent")) == indent:
            return start, index
    return start, len(lines)


def _set_field(block: list[str], field: str, value: str) -> list[str]:
    # The terminator is captured rather than assumed: rewriting a CRLF line as
    # LF would show up as a changed line in the diff for no reason, and one
    # stray ending in an otherwise consistent file is worse than none.
    pattern = re.compile(rf"^(\s*{field}:)[^\S\r\n]*[^\r\n]*(\r?\n?)$")
    out = []
    replaced = False
    for line in block:
        match = pattern.match(line)
        if match and not replaced:
            out.append(f"{match.group(1)} {value}{match.group(2)}")
            replaced = True
        else:
            out.append(line)
    if not replaced:  # pragma: no cover -- load() requires every field
        raise CalendarError([f"no {field}: line in the entry to rewrite"])
    return out


def append(obligation: Obligation, *, path: Path | None = None) -> None:
    """Add a new obligation to the end of the file.

    Written as text for the same reason :func:`complete` is: the file's
    comments explain where each cadence came from, and a dump would drop them.
    """
    path = path or DEFAULT_PATH
    existing = load(path)
    if any(o.name == obligation.name for o in existing):
        raise CalendarError([f"{obligation.name!r} is already in the calendar"],
                            path=path)
    if obligation.cadence not in CADENCES:
        raise CalendarError(
            [f"cadence {obligation.cadence!r} is not one of {sorted(CADENCES)}"],
            path=path,
        )

    due = obligation.next_due.isoformat() if obligation.next_due else "null"
    done = (
        obligation.last_completed.isoformat() if obligation.last_completed else "null"
    )
    text = _read(path)
    eol = _dominant_newline(text)
    if text and not text.endswith(("\n", "\r")):
        text += eol
    text += eol.join(
        [
            f"  - name: {obligation.name}",
            f'    description: "{obligation.description}"',
            f"    cadence: {obligation.cadence}",
            f"    next_due: {due}",
            f"    owner: {obligation.owner}",
            f"    runbook: {obligation.runbook}",
            f"    last_completed: {done}",
            "",
        ]
    )
    _write(path, text)
    # Parse what was written rather than trusting it. An unescaped quote in a
    # description turns the file into one nobody can read, and finding that out
    # now beats finding it out from `--due-this-week` next Monday.
    load(path)


def render(obligations: list[Obligation], *, today: dt.date, title: str) -> str:
    lines = [f"{title} (as of {today.isoformat()})", ""]
    if not obligations:
        lines.append("  nothing")
    else:
        lines += [o.render(today) for o in obligations]
    late = overdue(obligations, today=today)
    lines.append("")
    lines.append(
        f"  {len(obligations)} obligation(s), {len(late)} overdue"
        + (f": {', '.join(o.name for o in late)}" if late else "")
    )
    return "\n".join(lines)
