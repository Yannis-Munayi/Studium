"""The HTTP surface (agent runtime §20).

SSE from FastAPI to the client, one-directional. The interrupt arrives on a
separate short POST rather than through the streaming channel, because SSE
carries server-to-client only -- §20 is explicit that WebSockets are not used.

The frontend is subsystem 4's job. §26 says a curl-driven integration test
suffices to demonstrate the runtime works, and that is what this surface is
sized for: enough to drive a session end to end, no rendering concerns.

**Orchestrators live in memory, keyed by session.** §7 makes that a deliberate
choice -- no ``current_state`` column, no synchronisation bug surface. The
registry below is therefore process-local, which is correct at MVP scale (one
node, three learners) and is the first thing to move to Redis when it is not.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium import observability
from studium.agents.orchestrator import LearnerInput, Orchestrator
from studium.agents.schemas import PRIMITIVE_NAMES
from studium.api.reads import router as reads_router
from studium.asyncdb import run_db
from studium.ops import scheduler as scheduler_module
from studium.ops.scheduler import RetentionScheduler
from studium.orchestration.handoff import AgentRegistry
from studium.orchestration.state_machine import (
    PRIMITIVE_MATRIX,
    Event,
    GuardContext,
    IllegalTransition,
    InvalidPrimitive,
    State,
    primitive_is_valid,
    reconstruct_state,
)
from studium.orchestration.streaming import sse_stream
from studium.retrieval import CacheWarmer, default_retriever
from studium.session import memory
from studium.session.budget_gate import BudgetExceededError
from studium.session.escalation import escalation_status
from studium.session.lifecycle import NoEnrollmentError

log = logging.getLogger(__name__)

#: Retrieval §14. Off unless STUDIUM_WARM_CACHE is set, because it makes a
#: retrieval call every few minutes forever -- which is the intended behaviour
#: in a deployment and a surprise everywhere else. A test run, a migration
#: check, or a developer starting the app to look at one endpoint should not
#: quietly begin billing a reranker on a timer.
WARM_CACHE_ENV = "STUDIUM_WARM_CACHE"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the background work that lives alongside the app.

    Three things: retrieval §14's cache warmer, infrastructure §12.1's nightly
    retention pass, and infrastructure §7's observability wiring. All three are
    off or inert unless configured, and none of them can fail startup -- an
    exception here takes down the whole product for the sake of a background
    task, which is the wrong trade for every one of them.
    """
    warmer: CacheWarmer | None = None
    scheduler: RetentionScheduler | None = None

    # §7. Before anything else, so a failure in the two blocks below is
    # reportable. Returns what actually came up rather than raising.
    app.state.observability = observability.configure(app)

    if os.environ.get(WARM_CACHE_ENV) == "1":
        warmer = CacheWarmer(retriever=registry.retriever())
        warmer.start()
        app.state.cache_warmer = warmer
    else:
        app.state.cache_warmer = None
        log.info("cache warming disabled; set %s=1 to enable", WARM_CACHE_ENV)

    # §12.1. Off by default for the reason the warmer is: a developer running
    # the API to look at one endpoint, or a test constructing the app, must not
    # start a task whose job is deleting rows.
    if scheduler_module.enabled():
        scheduler = RetentionScheduler()
        scheduler.start()
        app.state.retention_scheduler = scheduler
    else:
        app.state.retention_scheduler = None
        log.info(
            "retention worker disabled; set %s=1 to enable (§12.1)",
            scheduler_module.ENABLE_ENV,
        )

    try:
        yield
    finally:
        if warmer is not None:
            await warmer.stop()
        if scheduler is not None:
            await scheduler.stop()


app = FastAPI(
    title="Studium Agent Runtime",
    version="1.1.0",
    description="Subsystem 2: the agents that produce every learner-facing behaviour.",
    lifespan=lifespan,
)

#: §7's correlation id, on every request and every log line under it.
#:
#: Added with ``add_middleware`` rather than as a decorated ``@app.middleware``
#: because it is a pure ASGI class: Starlette's ``BaseHTTPMiddleware`` buffers
#: the response body, which would turn §20's token-by-token stream into one
#: block arriving at the end. See CorrelationIdMiddleware's docstring.
app.add_middleware(observability.CorrelationIdMiddleware)

#: The desk, journal and session-summary reads (frontend §6.1, §6.4, §6.5).
#:
#: A separate module rather than more routes here: these are reads over rows the
#: runtime produced, with no Orchestrator, no streaming and no in-memory state,
#: and mixing them into a file whose whole subject is "one session's live turn"
#: would blur the one distinction that makes this surface easy to reason about.
app.include_router(reads_router)


class OrchestratorRegistry:
    """Process-local Orchestrators, one per active session.

    A session whose Orchestrator is missing (process restarted, or the request
    landed on a different worker) gets a fresh one with its state rebuilt from
    the ``session_turns`` tail, per §7's persistence note. The rebuild is
    coarse by design -- it recovers the mode, not a mid-sentence interruption,
    because the stream that interruption belonged to died with the process.
    """

    def __init__(self) -> None:
        self._by_session: dict[uuid.UUID, Orchestrator] = {}
        self._agents: AgentRegistry | None = None
        self._retriever: Any = None

    def retriever(self) -> Any:
        """The one retriever the agents share.

        Held here rather than left to ``AgentRegistry.build``'s default because
        the cache warmer has to warm *this* instance: the result cache lives on
        the retriever, so warming a second one would fill a cache nothing reads
        and leave every real call cold (§14).
        """
        if self._retriever is None:
            self._retriever = default_retriever()
        return self._retriever

    def agents(self) -> AgentRegistry:
        if self._agents is None:
            self._agents = AgentRegistry.build(retriever=self.retriever())
        return self._agents

    def get(self, session_id: uuid.UUID) -> Orchestrator | None:
        return self._by_session.get(session_id)

    def put(self, session_id: uuid.UUID, orchestrator: Orchestrator) -> None:
        self._by_session[session_id] = orchestrator

    async def resolve(self, session_id: uuid.UUID) -> Orchestrator:
        existing = self._by_session.get(session_id)
        if existing is not None:
            return existing

        orchestrator = Orchestrator(agents=self.agents())
        context = await memory.assemble_context(session_id)
        orchestrator.machine.state = reconstruct_state(
            context.recent_turns, mode=context.mode
        )
        orchestrator.exchange_index = _last_exchange_index(context.recent_turns)
        log.info(
            "rebuilt orchestrator for session %s in state %s",
            session_id, orchestrator.machine.state,
        )
        self._by_session[session_id] = orchestrator
        return orchestrator

    def release(self, session_id: uuid.UUID) -> None:
        self._by_session.pop(session_id, None)


registry = OrchestratorRegistry()

#: Distinguishes "this artifact has no citations" from "this artifact does not
#: exist" -- the citations query alone returns an empty list for both.
_ARTIFACT_EXISTS = sql("SELECT 1 FROM content_artifacts WHERE id = :artifact_id")

#: Intents a client may declare on a turn, skipping the §8 classifier.
#:
#: The eight ``primitive:*`` members of ``Intent`` are excluded deliberately:
#: they have their own field, and accepting them here would give one signal two
#: spellings that could disagree. ``interrupt`` is excluded because it has its
#: own endpoint (§20), and ``end_session`` because it has its own too -- a turn
#: that declared either would be asking the streaming channel to do a job the
#: out-of-band channels exist to do.
#:
#: What is left is the four intents a surface can genuinely know better than a
#: classifier can infer: the bench's Submit is an *answer* whatever words are in
#: the box, and "Continue lecture" is *next* even when the learner typed nothing.
DECLARABLE_INTENTS = frozenset({"question", "answer", "comment", "next", "back"})


def get_registry() -> OrchestratorRegistry:
    return registry


# --- request / response models --------------------------------------------


class StartSessionRequest(BaseModel):
    user_id: uuid.UUID
    mode: str = "tutorial"
    focus_concept_id: uuid.UUID | None = None
    learner_subject_id: uuid.UUID | None = None
    target_duration_minutes: int = 90


class StartSessionResponse(BaseModel):
    session_id: uuid.UUID
    state: str
    #: The mode the session actually has. Not always the one that was
    #: requested: §20 allows one active session per learner, so opening a
    #: second returns the first, whatever mode *it* was started in. The client
    #: renders from this rather than from what it sent. See R15.
    mode: str
    #: True when an existing active session was returned instead of a new one.
    resumed: bool = False


class TurnRequest(BaseModel):
    text: str = ""
    primitive: str | None = Field(
        default=None,
        description="An explicit primitive button press. Skips intent classification.",
    )
    intent: str | None = Field(
        default=None,
        description=(
            "An intent the client already knows. Skips classification, like "
            "'primitive' does. For a control whose meaning is not in doubt -- "
            "the bench's Submit is an answer, whatever the words are."
        ),
    )


class PrimitiveRequest(BaseModel):
    """v1.0.1 §3.1: "with primitive name in body"."""

    primitive: str
    #: Optional context the learner typed alongside the button press.
    text: str = ""


class PracticeSubmitRequest(BaseModel):
    answer: str


class ResumeRequest(BaseModel):
    """v1.0.1 §5.2's body for the resume endpoint."""

    resumption_note_seen: bool = True


class ResumeResponse(BaseModel):
    session_id: uuid.UUID
    state: str
    resumption_note_seen: bool


class EscalateRequest(BaseModel):
    """v1.0.1 §5.2's body for the escalate endpoint."""

    reason: str = "learner_initiated"


class EscalateResponse(BaseModel):
    session_id: uuid.UUID
    state: str
    reason: str
    #: Why the server agreed, so a support conversation about an escalation has
    #: an answer that is not "the model decided".
    threshold: dict[str, Any] = Field(default_factory=dict)


class InterruptResponse(BaseModel):
    accepted: bool
    state: str
    detail: str


class CloseRequest(BaseModel):
    reason: str = "learner_stop"


class CloseResponse(BaseModel):
    session_id: uuid.UUID
    ended: bool
    summarised: bool
    errors: list[str] = Field(default_factory=list)


# --- endpoints -------------------------------------------------------------


@app.post("/api/session", response_model=StartSessionResponse, status_code=201)
async def start_session(
    body: StartSessionRequest,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> StartSessionResponse:
    """Open a session and run OPENING (§16)."""
    orchestrator = Orchestrator(agents=reg.agents())
    try:
        session_id = await orchestrator.start_session(
            body.user_id,
            body.mode,
            body.focus_concept_id,
            learner_subject_id=body.learner_subject_id,
            target_duration_minutes=body.target_duration_minutes,
        )
    except BudgetExceededError as exc:
        # 402 rather than 403: the request is well-formed and the learner is
        # authorised; they are over a spend cap, which is what 402 means.
        raise HTTPException(
            status_code=402,
            detail={
                "message": exc.learner_message(),
                "scope": exc.scope,
                "reset_at": exc.reset_at.isoformat(),
            },
        ) from exc
    except NoEnrollmentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    reg.put(session_id, orchestrator)
    return StartSessionResponse(
        session_id=session_id,
        state=orchestrator.machine.state.value,
        mode=orchestrator.opened_mode or body.mode,
        resumed=orchestrator.resumed,
    )


@app.post("/api/session/{session_id}/turn")
async def take_turn(
    session_id: uuid.UUID,
    body: TurnRequest,
    request: Request,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> StreamingResponse:
    """Stream one turn's response as SSE (§20)."""
    if body.primitive and body.primitive not in PRIMITIVE_NAMES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown primitive {body.primitive!r}; expected one of {list(PRIMITIVE_NAMES)}",
        )
    if body.intent is not None and body.intent not in DECLARABLE_INTENTS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown intent {body.intent!r}; expected one of "
                f"{sorted(DECLARABLE_INTENTS)}"
            ),
        )

    orchestrator = await reg.resolve(session_id)
    learner_input = LearnerInput(
        text=body.text, primitive=body.primitive, intent=body.intent  # type: ignore[arg-type]
    )

    # A fresh interrupt state per turn: an interrupt signalled during the
    # previous turn must not cut this one short.
    orchestrator.interruption.clear()

    return StreamingResponse(
        sse_stream(orchestrator.handle_turn(session_id, learner_input)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Without this an nginx or CDN buffer holds the whole stream and
            # delivers it as one block, which looks exactly like the server
            # having hung (§25 forward-refs SSE proxying to Infrastructure).
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _streamed(orchestrator: Any, session_id: uuid.UUID, learner_input: LearnerInput) -> StreamingResponse:
    """One turn, streamed. Shared by every endpoint that produces a turn."""
    orchestrator.interruption.clear()
    return StreamingResponse(
        sse_stream(orchestrator.handle_turn(session_id, learner_input)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/session/{session_id}/primitive")
async def invoke_primitive(
    session_id: uuid.UUID,
    body: PrimitiveRequest,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> StreamingResponse:
    """Invoke a tutorial primitive (v1.0.1 §3.1's named emission path).

    ``POST /turn`` with a ``primitive`` field still works and is what the built
    frontend uses; this is the endpoint §7's table names, and the one that can
    answer §3.2's validity question *before* streaming starts. That ordering is
    the point: a 400 the client can render beats an SSE stream that opens,
    says nothing useful, and closes.
    """
    if body.primitive not in PRIMITIVE_NAMES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown primitive {body.primitive!r}; expected one of {list(PRIMITIVE_NAMES)}",
        )

    orchestrator = await reg.resolve(session_id)
    state = orchestrator.machine.state

    # §3.2: "Silent no-op is not acceptable -- the failure must be surfaced to
    # the client so the UI can present a clear message."
    if not primitive_is_valid(body.primitive, state):
        raise HTTPException(
            status_code=400,
            detail={
                "message": str(InvalidPrimitive(body.primitive, state)),
                "primitive": body.primitive,
                "state": state.value,
                "valid_from": sorted(
                    s.value for s in PRIMITIVE_MATRIX[body.primitive].valid_from
                ),
            },
        )

    return _streamed(
        orchestrator, session_id, LearnerInput(text=body.text, primitive=body.primitive)
    )


@app.post("/api/session/{session_id}/practice/submit")
async def submit_practice(
    session_id: uuid.UUID,
    body: PracticeSubmitRequest,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> StreamingResponse:
    """Submit a practice or review answer (§3.1: LAB and REVIEW share this).

    Declared as an ``answer`` intent rather than left to the classifier: the
    learner pressed a submit button on a problem, so there is nothing to infer,
    and paying a Haiku call to re-derive a signal the client already sent is the
    waste §8 avoids for primitives too.
    """
    orchestrator = await reg.resolve(session_id)
    return _streamed(
        orchestrator, session_id, LearnerInput(text=body.answer, intent="answer")
    )


@app.post("/api/session/{session_id}/resume", response_model=ResumeResponse)
async def resume_lecture(
    session_id: uuid.UUID,
    body: ResumeRequest,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> ResumeResponse:
    """Emit ``question_resolved`` from the resumption card (v1.0.1 §5.2).

    Returns the new state rather than streaming the recap. §5.2 has the bridge
    arrive "via a new SSE stream (the interrupt closed the previous one)", and
    the client opens that by posting the next turn -- so this endpoint's job is
    the transition, and keeping it a plain JSON response means the client can
    tell "the resume was accepted" from "the recap is still generating".
    """
    orchestrator = await reg.resolve(session_id)
    state = orchestrator.machine.state

    if state is not State.PAUSED_FOR_QUESTION:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"cannot resume from {state.value}: a lecture is only "
                    "resumable while it is paused for a question"
                ),
                "state": state.value,
            },
        )

    try:
        target, _ = orchestrator.machine.fire(
            Event.QUESTION_RESOLVED,
            GuardContext(
                learner_satisfied=True, resume_state=orchestrator.machine.resume_state
            ),
        )
    except IllegalTransition as exc:  # pragma: no cover -- guarded above
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    log.info("session %s resumed to %s", session_id, target)
    return ResumeResponse(
        session_id=session_id,
        state=target.value,
        resumption_note_seen=body.resumption_note_seen,
    )


@app.post("/api/session/{session_id}/escalate", response_model=EscalateResponse)
async def escalate_to_office_hours(
    session_id: uuid.UUID,
    body: EscalateRequest,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> EscalateResponse:
    """Emit ``escalate`` (v1.0.1 §5.2).

    §5.2 asks for the threshold to be re-checked here as defence in depth. Not
    because the client is expected to misbehave -- because the threshold is
    time-based, so a card rendered at nine minutes is still on screen at eleven
    and the server is the only party that knows which side of the line the
    press actually landed on.
    """
    orchestrator = await reg.resolve(session_id)
    state = orchestrator.machine.state

    if state is not State.PAUSED_FOR_QUESTION:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"cannot escalate from {state.value}: office hours is "
                    "reached from a paused lecture"
                ),
                "state": state.value,
            },
        )

    status = await escalation_status(session_id)
    if not status.should_offer:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "the escalation threshold has not been reached -- "
                    f"{status.reason}"
                ),
                **status.as_payload(),
            },
        )

    target, _ = orchestrator.machine.fire(Event.ESCALATE)
    # §5.2: the session's persisted mode changes too, or a reconstructed
    # Orchestrator (§7) would rebuild it as whatever it was before.
    await run_db(lambda s: _set_mode(s, session_id, "office_hours"))

    log.info("session %s escalated to office hours (%s)", session_id, status.reason)
    return EscalateResponse(
        session_id=session_id,
        state=target.value,
        reason=body.reason,
        threshold=status.as_payload(),
    )


def _set_mode(session: Session, session_id: uuid.UUID, mode: str) -> None:
    session.execute(
        sql("UPDATE learning_sessions SET mode = :mode WHERE id = :sid"),
        {"mode": mode, "sid": session_id},
    )


@app.post("/api/session/{session_id}/interrupt", response_model=InterruptResponse)
async def interrupt(
    session_id: uuid.UUID,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> InterruptResponse:
    """Raise a hand mid-stream (§20).

    Deliberately cheap and always 200: the client fires this the instant the
    learner gestures, and an interrupt that arrives when nothing is streaming
    is a no-op rather than an error worth showing anyone.
    """
    orchestrator = reg.get(session_id)
    if orchestrator is None:
        return InterruptResponse(
            accepted=False,
            state="unknown",
            detail="no active stream for this session on this node",
        )

    accepted = orchestrator.signal_interrupt()
    return InterruptResponse(
        accepted=accepted,
        state=orchestrator.machine.state.value,
        detail=(
            "interrupt signalled; the current sentence will finish"
            if accepted
            else f"nothing interruptible in state {orchestrator.machine.state.value}"
        ),
    )


@app.post("/api/session/{session_id}/close", response_model=CloseResponse)
async def close(
    session_id: uuid.UUID,
    body: CloseRequest,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> CloseResponse:
    """Run CLOSING and release the in-memory state (§16)."""
    orchestrator = await reg.resolve(session_id)
    from studium.session.lifecycle import close_session

    context = await memory.assemble_context(session_id)
    report = await close_session(context, orchestrator.agents, reason=body.reason)
    reg.release(session_id)

    return CloseResponse(
        session_id=session_id,
        ended=report.ended,
        summarised=report.summarised,
        errors=report.errors,
    )


@app.get("/api/session/{session_id}/state")
async def session_state(
    session_id: uuid.UUID,
    reg: OrchestratorRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Current runtime state. Diagnostic surface for the curl-driven tests."""
    orchestrator = await reg.resolve(session_id)
    return {
        "session_id": str(session_id),
        "state": orchestrator.machine.state.value,
        "persisted_mode": orchestrator.machine.persisted_mode,
        "exchange_index": orchestrator.exchange_index,
        "interruptible": orchestrator.machine.is_interruptible,
        "transitions": [
            {
                "from": r.source.value,
                "event": r.event.value,
                "to": r.target.value,
                "effect": r.effect,
                "at": r.at.isoformat(),
            }
            for r in orchestrator.machine.log
        ],
    }


@app.get("/api/artifacts/{artifact_id}/citations")
async def artifact_citations(artifact_id: uuid.UUID) -> dict[str, Any]:
    """Resolve an artifact's ``[Pn]`` markers to passages (retrieval §12).

    One call per artifact, not per marker: a lecture segment carries six or
    more citations and the client renders them together, so per-marker requests
    would be six round trips to draw one paragraph.

    The numbering is reconstructed from the stored ``content_citations`` rows in
    ``chunk_id`` order -- the same order retrieval numbered them in. Nothing
    stores the passage number itself, because chunk ids are stable and passage
    numbers are per-call ephemera.

    An artifact with no citations returns an empty list rather than a 404: an
    ungrounded artifact is a real thing the client must render (as prose with
    no markers), and a 404 would make it indistinguishable from a bad id.
    """
    from studium.asyncdb import read_db
    from studium.retrieval import resolve_artifact_citations

    exists = await read_db(
        lambda s: s.execute(
            _ARTIFACT_EXISTS, {"artifact_id": artifact_id}
        ).scalar()
    )
    if not exists:
        raise HTTPException(status_code=404, detail=f"no artifact {artifact_id}")

    citations = await read_db(
        lambda s: resolve_artifact_citations(s, artifact_id)
    )
    return {
        "artifact_id": str(artifact_id),
        "citations": [c.as_json() for c in citations],
    }


@app.get("/api/portfolio/verify/{item_id}")
async def verify_portfolio_item(item_id: uuid.UUID) -> dict[str, Any]:
    """Public credential verification (evaluation §12.4).

    **Unauthenticated on purpose.** "Verification does not require an account
    or authentication" -- a credential whose check requires the issuer's
    permission is a credential nobody outside can rely on. The verifier can
    also skip this endpoint entirely and check the signature against the
    published public key offline, which is the point of signing rather than
    attesting.

    **Credentials only.** ``portfolio_items`` also holds the learner's *work* --
    proofs, essays, notebooks. §12 assumes the table holds credentials alone;
    in this schema it does not (DIVERGENCES-EVALUATION E1), so an
    unauthenticated lookup by id would publish a learner's coursework to anyone
    who could guess a UUID. ``credentials.verify_item`` filters by kind, and a
    non-credential id returns the same 404 as an unknown one -- so the endpoint
    cannot be used to probe which ids exist.

    A credential that exists but does not verify returns 200 with
    ``valid: false``, not an error. "Is this genuine" is a question, and "no"
    is an answer a verifier needs to be able to read.
    """
    from studium.asyncdb import read_db
    from studium.eval.credentials import verify_item

    result = await read_db(lambda s: verify_item(s, item_id))
    if not result.found:
        raise HTTPException(status_code=404, detail=f"no credential {item_id}")

    return {
        "item_id": str(item_id),
        "valid": result.valid,
        "item": result.item,
        "signature": result.signature,
        "issuer_public_key_id": result.key_id,
        "key_retired": result.key_retired,
        # Infrastructure §11.4. Non-null means the signature verified *and* the
        # issuer key was compromised inside a window covering this credential.
        # Reported beside `valid` rather than folded into it: the signature is
        # genuinely valid, and what the verifier needs to decide is whether
        # only Studium could have produced it.
        "key_compromised_at": (
            result.key_compromised_at.isoformat() if result.key_compromised_at else None
        ),
        "reason": result.reason,
    }


@app.get("/api/portfolio/keys")
async def portfolio_keys() -> dict[str, Any]:
    """The published issuer keys (§12.3, §12.4).

    Retired keys are listed alongside the current one: "Old public keys remain
    published so historical portfolio items stay verifiable." A verifier that
    could not find the key a two-year-old credential names would have no way to
    tell a rotated key from a forged one.
    """
    from studium.asyncdb import read_db
    from studium.eval.credentials import published_keys

    keys = await read_db(published_keys)
    return {
        "issuer": "studium.app",
        "algorithm": "ed25519",
        "keys": [
            {
                "key_id": k["key_id"],
                "public_key": k["public_key"],
                "current": k["current"],
                "activated_at": k["activated_at"].isoformat() if k["activated_at"] else None,
                "retired_at": k["retired_at"].isoformat() if k["retired_at"] else None,
            }
            for k in keys
        ],
    }


@app.get("/api/signing-keys")
async def signing_keys() -> dict[str, Any]:
    """Infrastructure §11.3's published key list.

    "Public keys are published at ``https://studium.app/api/signing-keys`` as a
    JSON array. Verifiers fetch this to obtain the key needed to verify a
    portfolio item. The endpoint is unauthenticated and cacheable."

    The evaluation build shipped the same data at ``/api/portfolio/keys``
    before this spec named a path, and that URL is what
    ``studium-web``'s verify page and the existing tests call. Both are served:
    a published verification endpoint is a URL other people's code holds, and
    moving it to satisfy a later spec would break every verifier that had
    already read the first one. This is the canonical path going forward. See
    DIVERGENCES-INFRASTRUCTURE (N1).

    ``compromised_at`` is reported per key (§11.4). A verifier that fetches
    this list is exactly the audience §11.4 step 3's notice is for, and putting
    the flag beside the key means they get it without reading a notice.
    """
    payload = await portfolio_keys()
    from sqlalchemy import text as _sql

    from studium.asyncdb import read_db

    compromised = await read_db(
        lambda s: {
            r.key_id: r.compromised_at.isoformat()
            for r in s.execute(
                _sql(
                    "SELECT key_id, compromised_at FROM signing_keys "
                    " WHERE compromised_at IS NOT NULL"
                )
            )
        }
    )
    for key in payload["keys"]:
        key["compromised_at"] = compromised.get(key["key_id"])
    payload["canonical_path"] = "/api/signing-keys"
    return payload


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness, and what background work is actually running.

    §4.2: "``GET /health`` returns 200 when the FastAPI process is healthy
    (checked separately for Postgres connection and Anthropic reachability);
    Fly's load balancer routes only to healthy instances."

    **The separation §4.2 asks for is load-bearing and is the reason this
    endpoint is more careful than it looks.** Fly routes on the status code, so
    a 503 takes the instance out of the pool. Postgres being unreachable is
    worth that -- the instance genuinely cannot serve. Anthropic being
    unreachable is not: every instance would fail the same check at the same
    moment, Fly would find nothing healthy to route to, and a provider outage
    that agent runtime §16 already degrades gracefully would become a total
    outage of a product that still serves the desk, the journal and every
    read. So provider reachability is *reported* and never *gates*.

    Anthropic is not called here either. §7.4's uptime monitor polls this every
    five minutes; a real API call on each would be a standing bill and would
    make the health check's latency depend on a third party. What is reported
    is whether the key is configured, which is the failure a deploy actually
    produces.
    """
    warmer: CacheWarmer | None = getattr(request.app.state, "cache_warmer", None)
    scheduler: RetentionScheduler | None = getattr(
        request.app.state, "retention_scheduler", None
    )

    database = await _database_health()
    body: dict[str, Any] = {
        "status": "ok" if database["reachable"] else "degraded",
        "subsystem": "agent-runtime",
        "version": "1.0.0",
        "environment": os.environ.get("STUDIUM_ENV", "local"),
        "release": os.environ.get("FLY_MACHINE_VERSION", "dev"),
        "database": database,
        "providers": {
            "anthropic_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "voyage_key_configured": bool(os.environ.get("VOYAGE_API_KEY")),
            "note": (
                "configuration only, not reachability: a live call per health "
                "check would bill on a five-minute timer, and a provider "
                "outage must not empty Fly's routing pool"
            ),
        },
        "cache_warming": warmer.status() if warmer else {"running": False},
        "retention": scheduler.status() if scheduler else {"running": False},
        "observability": getattr(request.app.state, "observability", {}),
    }
    if not database["reachable"]:
        # 503 so Fly stops routing here. The one dependency whose absence makes
        # this instance genuinely unable to serve.
        raise HTTPException(status_code=503, detail=body)
    return body


async def _database_health() -> dict[str, Any]:
    """One trivial query, with the failure captured rather than raised."""
    from studium.asyncdb import read_db

    try:
        version = await read_db(lambda s: s.execute(sql("SHOW server_version")).scalar_one())
    except Exception as exc:  # noqa: BLE001 -- reported, not raised
        log.warning("health check could not reach Postgres: %s", exc)
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"reachable": True, "server_version": version}


def _last_exchange_index(turns: list[dict[str, Any]]) -> int:
    """Recover the exchange counter so a rebuilt Orchestrator does not restart it."""
    best = 0
    for turn in turns:
        payload = turn.get("input") or {}
        if isinstance(payload, dict):
            best = max(best, int(payload.get("exchange_index", 0) or 0))
    return best
