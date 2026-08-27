"""The read surface behind the desk, the journal and the session close (§20).

The runtime's own endpoints stream a session. These serve the four surfaces that
read what a session produced -- frontend §6.1, §6.4, §6.5 and §12 -- against
tables the data layer already ships and the runtime already writes. Nothing here
is new schema; what was missing was HTTP.

**Identity arrives in a header, and it is identity, not authentication.**
``POST /api/session`` takes a raw ``user_id`` in its body and trusts it; these
take ``X-Studium-User`` and trust that. Neither is a check. The frontend's Next
proxy is the single place that sets the header -- it resolves the learner
server-side and strips any inbound value, so a browser cannot choose who it is
*through the app* -- but anyone who can reach FastAPI directly can still set it
by hand, exactly as they can post any ``user_id`` today. That is the standing
gap (frontend DIVERGENCES F7); this endpoint set does not widen it and does not
close it.

What it does do is make the scoping structural: every function in
:mod:`studium.reads` takes the learner and filters on it, and a route that
forgot to pass it would not compile into a working query.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field

from studium import reads
from studium.asyncdb import read_db, run_db

log = logging.getLogger(__name__)

router = APIRouter(tags=["surfaces"])

#: Set by the frontend's proxy from the server-side session (see module
#: docstring). Named with the app's own prefix so nothing upstream can mistake
#: it for a standard auth header and treat it as one.
USER_HEADER = "X-Studium-User"

JournalStatus = Literal["open", "partial", "resolved", "archived"]

#: Frontend §6.4's history timeline. The full ``journal_event_kind`` enum from
#: data layer §6.7 -- all eight, including the two the frontend's first schema
#: omitted, which would have failed the client's parse the moment a hypothesis
#: was revised.
JournalEventKind = Literal[
    "created",
    "revisited",
    "partially_addressed",
    "resolved",
    "reopened",
    "archived",
    "hypothesis_updated",
    "learner_note_added",
]


# --- identity --------------------------------------------------------------


async def current_user(
    x_studium_user: Annotated[str | None, Header()] = None,
) -> uuid.UUID:
    """The learner these rows belong to.

    401 rather than a default user when the header is absent. A read path that
    quietly fell back to a seeded account would serve someone else's journal to
    an unauthenticated caller and look like it was working.
    """
    if not x_studium_user:
        raise HTTPException(
            status_code=401,
            detail=f"{USER_HEADER} is required; the frontend proxy sets it.",
        )
    try:
        return uuid.UUID(x_studium_user)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{USER_HEADER} is not a uuid"
        ) from exc


# --- response models -------------------------------------------------------


class LearnerProfileOut(BaseModel):
    id: uuid.UUID
    display_name: str
    timezone: str = "America/Toronto"
    default_session_minutes: int = 90
    show_cost: bool = False


class RecentSessionOut(BaseModel):
    id: uuid.UUID
    mode: str
    started_at: str
    ended_at: str | None = None
    duration_minutes: float | None = None
    concepts_touched: list[str] = Field(default_factory=list)


class SyllabusConceptOut(BaseModel):
    id: uuid.UUID
    name: str


class JournalEntryOut(BaseModel):
    id: uuid.UUID
    learner_subject_id: uuid.UUID
    concept_id: uuid.UUID | None = None
    concept_name: str | None = None
    status: JournalStatus
    summary: str
    summary_author: Literal["learner", "tutor"] = "tutor"
    #: Null unless :data:`studium.reads.SERVE_HYPOTHESIS_TO_LEARNER` is on. The
    #: field is declared rather than dropped so the shape does not change when
    #: that decision does.
    hypothesis: str | None = None
    learner_note: str | None = None
    first_seen_at: str
    last_touched_at: str


class JournalEventOut(BaseModel):
    id: uuid.UUID
    kind: JournalEventKind
    at: str
    session_id: uuid.UUID | None = None


class JournalEntryDetailOut(JournalEntryOut):
    history: list[JournalEventOut] = Field(default_factory=list)


class ConceptMasteryOut(BaseModel):
    concept_id: uuid.UUID
    concept_name: str
    #: The raw BKT posterior after the most recent evidence.
    p_known: float
    #: The same value under the forgetting curve, recomputed at read time. This
    #: is the number every other consumer reads (data layer C1) and the one the
    #: desk renders.
    p_known_decayed: float


class DeskOut(BaseModel):
    learner: LearnerProfileOut
    open_session: RecentSessionOut | None = None
    recent_sessions: list[RecentSessionOut] = Field(default_factory=list)
    syllabus_next: list[SyllabusConceptOut] = Field(default_factory=list)
    open_journal_entries: list[JournalEntryOut] = Field(default_factory=list)
    mastery: list[ConceptMasteryOut] = Field(default_factory=list)


class MasteryDeltaOut(BaseModel):
    concept_id: uuid.UUID
    concept_name: str
    before: float
    after: float


class SessionSummaryOut(BaseModel):
    session_id: uuid.UUID
    summary: str
    concepts_touched: list[MasteryDeltaOut] = Field(default_factory=list)
    open_threads: list[str] = Field(default_factory=list)
    next_focus_concept_id: uuid.UUID | None = None
    next_focus_concept_name: str | None = None
    duration_minutes: float
    cost_usd: float | None = None


class JournalPatch(BaseModel):
    """§6.4's entry actions and §12.1's note.

    ``hypothesis`` is absent by design: it is the Confusion-Tracker's inference
    and a learner may read it (when §11 allows) but never write it. Leaving it
    out of the model is what makes that true of the endpoint rather than of a
    check inside it.
    """

    status: JournalStatus | None = None
    summary: str | None = None
    learner_note: str | None = None

    def is_empty(self) -> bool:
        return self.status is None and self.summary is None and self.learner_note is None


# --- endpoints -------------------------------------------------------------


@router.get("/api/user/me/desk", response_model=DeskOut)
async def get_desk(
    user_id: Annotated[uuid.UUID, Depends(current_user)],
) -> dict[str, Any]:
    """Everything §6.1's desk shows, in one round trip."""
    try:
        return await read_db(lambda s: reads.desk(s, user_id))
    except reads.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/user/me/journal", response_model=list[JournalEntryOut])
async def get_journal(
    user_id: Annotated[uuid.UUID, Depends(current_user)],
    status: Annotated[
        str | None, Query(description="Comma-separated journal_status values.")
    ] = None,
    learner_subject_id: Annotated[
        str | None, Query(description="Comma-separated enrollment ids.")
    ] = None,
    concept_ids: Annotated[
        str | None, Query(description="Comma-separated concept ids.")
    ] = None,
    since: Annotated[
        str | None, Query(description="ISO date; §6.4 defaults to the last 30 days.")
    ] = None,
    q: Annotated[str | None, Query(description="Substring over summary and note.")] = None,
) -> list[dict[str, Any]]:
    """§6.4's filtered list, across every subject the learner is enrolled in.

    Filters arrive as comma-separated strings rather than repeated query
    parameters because that is what the client already sends, and a list
    serialisation the two ends disagree about fails as an empty result rather
    than as an error.
    """
    statuses = _split(status)
    for value in statuses:
        if value not in ("open", "partial", "resolved", "archived"):
            raise HTTPException(status_code=422, detail=f"unknown status {value!r}")

    return await read_db(
        lambda s: reads.journal_entries(
            s,
            user_id,
            statuses=statuses or None,
            subject_ids=_split_uuids(learner_subject_id, "learner_subject_id"),
            concept_ids=_split_uuids(concept_ids, "concept_ids"),
            since=_parse_since(since),
            search=q,
        )
    )


@router.get("/api/journal/{entry_id}", response_model=JournalEntryDetailOut)
async def get_journal_entry(
    entry_id: Annotated[uuid.UUID, Path()],
    user_id: Annotated[uuid.UUID, Depends(current_user)],
) -> dict[str, Any]:
    try:
        return await read_db(lambda s: reads.journal_entry(s, user_id, entry_id))
    except reads.NotFound as exc:
        # 404 for "not yours" as well as "not there". A 403 would confirm the
        # entry exists to someone who does not own it.
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/api/journal/{entry_id}", response_model=JournalEntryOut)
async def patch_journal_entry(
    entry_id: Annotated[uuid.UUID, Path()],
    body: JournalPatch,
    user_id: Annotated[uuid.UUID, Depends(current_user)],
) -> dict[str, Any]:
    """§6.4's actions and §12.1's autosave.

    An empty patch is a 422 rather than a no-op 200. The autosave fires on a
    debounce and the resolve button fires on a click; neither has a path that
    sends nothing, so a body with no fields is a client bug worth reporting
    rather than absorbing.
    """
    if body.is_empty():
        raise HTTPException(
            status_code=422, detail="patch must set at least one of status, summary, learner_note"
        )
    try:
        return await run_db(
            lambda s: reads.patch_journal_entry(
                s,
                user_id,
                entry_id,
                status=body.status,
                summary=body.summary,
                learner_note=body.learner_note,
            )
        )
    except reads.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/session/{session_id}/summary", response_model=SessionSummaryOut)
async def get_session_summary(
    session_id: Annotated[uuid.UUID, Path()],
    user_id: Annotated[uuid.UUID, Depends(current_user)],
) -> dict[str, Any]:
    """§6.5's close modal.

    404 means "not summarised yet", which is a state the close endpoint already
    reports as ``summarised: false`` and the client already has copy for.
    """
    try:
        return await read_db(lambda s: reads.session_summary(s, user_id, session_id))
    except reads.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- parameter parsing -----------------------------------------------------


def _split(raw: str | None) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()] if raw else []


def _split_uuids(raw: str | None, field: str) -> list[uuid.UUID] | None:
    values = _split(raw)
    if not values:
        return None
    try:
        return [uuid.UUID(value) for value in values]
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{field} must be comma-separated uuids"
        ) from exc


def _parse_since(raw: str | None) -> dt.datetime | None:
    """§6.4's date range. Absent means the whole history, not the default window.

    The 30-day default belongs to the client, which owns the filter UI and has
    to show the learner which window they are looking at. A server that silently
    applied one would make "no filters" mean something different from what the
    empty filter panel says.
    """
    if not raw:
        return None

    candidates = [raw]
    # An unencoded `+` in a query string decodes to a space, which turns
    # `...T04:00:00+00:00` into `...T04:00:00 00:00` and fails to parse. That is
    # the single most common way an ISO timestamp arrives here wrong, and it is
    # a caller's encoding bug rather than a different instant -- so it is
    # recovered rather than rejected. The plain form is tried first, because a
    # space is also a legal ISO date/time separator and must not be rewritten.
    if " " in raw:
        head, _, tail = raw.rpartition(" ")
        candidates.append(f"{head}+{tail}")

    for candidate in candidates:
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)

    raise HTTPException(
        status_code=422, detail="since must be an ISO 8601 date or datetime"
    )
