"""SSE emission and sentence-boundary interruption (agent runtime §20).

Interruption is a first-class control-flow event, not an error (§3). When a
learner raises their hand mid-lecture the runtime finishes the current
sentence, records where it stopped, and hands off to the Tutor. Getting this
wrong "looks broken to the learner in a way that is uniquely damaging to
trust", so the machinery is here rather than improvised per agent.

**Deltas pass through until an interrupt arrives.** §20 describes the iterator
as buffering tokens and emitting full sentences. Buffering unconditionally
would make every lecture arrive in sentence-sized jumps and give up the reason
to stream at all. Here, deltas are forwarded the moment they arrive, and the
boundary machinery engages only once the interrupt event is set -- which is the
only time a boundary actually matters. The §20 requirement, "stops requesting
new tokens after the next completed sentence emission", holds exactly. See
DIVERGENCES (R10).

Detection is rule-based with three escapes, because the rule is wrong often
enough on real Lecturer output to need them: an abbreviation list, an LLM
fallback after 60 tokens with no boundary, and a hard cut at 80.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)

#: §20: after this many post-interrupt tokens with no boundary, ask a model.
LLM_FALLBACK_AFTER_TOKENS = 60
#: §21: if the model also fails, cut here and mark the trace.
HARD_CUT_AFTER_TOKENS = 80

#: Abbreviations whose trailing period is not a sentence end. Kept short and
#: aimed at what actually appears in technical exposition -- a general
#: abbreviation list would be longer and no more useful here.
ABBREVIATIONS = frozenset(
    {
        "e.g", "i.e", "cf", "vs", "etc", "al", "fig", "eq", "ch", "sec", "no",
        "approx", "resp", "viz", "Dr", "Prof", "Mr", "Ms", "Mrs", "St", "Ph.D",
        "Thm", "Def", "Lem", "Cor", "Prop", "Ex",
    }
)

#: A sentence end: . ! or ? then whitespace then something that starts a new
#: sentence -- a capital, a digit, an opening bracket, or a LaTeX delimiter.
_BOUNDARY = re.compile(r"([.!?])([\"')\]]*)(\s+)(?=[A-Z0-9(\[$\\])")

#: Trailing terminal punctuation at the very end of a buffer.
_TRAILING = re.compile(r"[.!?][\"')\]]*\s*$")


def _preceded_by_abbreviation(text: str, period_index: int) -> bool:
    """Whether the period at ``period_index`` closes a known abbreviation."""
    word = re.search(r"([A-Za-z.]+)$", text[:period_index])
    if not word:
        return False
    token = word.group(1).rstrip(".")
    return token in ABBREVIATIONS or token.split(".")[-1] in ABBREVIATIONS


def _inside_math(text: str, index: int) -> bool:
    """Whether ``index`` sits inside a LaTeX math span.

    A period inside ``$f(x) = 1.5$`` or a display block is not a sentence end,
    and math-heavy segments are exactly where §25's open question 2 expects the
    rule-based detector to struggle.
    """
    before = text[:index]
    if before.count("$$") % 2 == 1:
        return True
    return before.replace("$$", "").count("$") % 2 == 1


def find_boundary(text: str) -> int | None:
    """Index just past the first real sentence end, or ``None``.

    Also matches a buffer that *ends* on terminal punctuation -- the last
    sentence of a segment has no following capital to anchor against.
    """
    for match in _BOUNDARY.finditer(text):
        period = match.start(1)
        if _preceded_by_abbreviation(text, period) or _inside_math(text, period):
            continue
        return match.end(3)

    if _TRAILING.search(text):
        stripped = text.rstrip()
        if not _preceded_by_abbreviation(stripped, len(stripped) - 1) and not _inside_math(
            stripped, len(stripped) - 1
        ):
            return len(text)
    return None


@dataclass(slots=True)
class InterruptionPoint:
    """Where a stream was cut, so the Lecturer can resume from it (§7)."""

    delivered_text: str
    generated_text: str
    boundary_method: str  # 'rule' | 'llm' | 'hard_cut' | 'natural_end'
    tokens_after_interrupt: int = 0

    @property
    def wasted_chars(self) -> int:
        return max(0, len(self.generated_text) - len(self.delivered_text))


@dataclass(slots=True)
class InterruptionState:
    """Per-session interrupt signalling (§20).

    The interrupt arrives on a separate POST, not through the SSE channel --
    SSE is one-directional. The Orchestrator holds one of these per active
    session and sets the event when that POST lands.
    """

    event: asyncio.Event = field(default_factory=asyncio.Event)
    point: InterruptionPoint | None = None

    def signal(self) -> None:
        self.event.set()

    def clear(self) -> None:
        self.event.clear()
        self.point = None

    @property
    def interrupted(self) -> bool:
        return self.event.is_set()


LLMBoundaryCheck = Callable[[str], Awaitable[int | None]]


async def sentence_boundary_iter(
    deltas: AsyncIterator[str],
    state: InterruptionState,
    *,
    llm_boundary_check: LLMBoundaryCheck | None = None,
) -> AsyncIterator[str]:
    """Forward deltas, stopping at a sentence boundary once interrupted.

    Yields the text actually delivered to the learner. On exit,
    ``state.point`` records where the cut landed and how it was found, which is
    what the resume path and the §25 open questions both need.
    """
    delivered: list[str] = []
    generated: list[str] = []
    post_interrupt: list[str] = []
    tokens_after = 0
    method = "natural_end"

    async for delta in deltas:
        generated.append(delta)

        if not state.interrupted:
            delivered.append(delta)
            yield delta
            continue

        # Interrupted: buffer until the sentence closes, then stop pulling.
        post_interrupt.append(delta)
        tokens_after += 1
        buffered = "".join(post_interrupt)

        cut = find_boundary(buffered)
        if cut is not None:
            method = "rule"
        elif tokens_after >= LLM_FALLBACK_AFTER_TOKENS and llm_boundary_check:
            try:
                cut = await llm_boundary_check(buffered)
                if cut is not None:
                    method = "llm"
            except Exception:  # noqa: BLE001 -- fallback failure is not fatal
                log.debug("LLM boundary check failed", exc_info=True)

        if cut is None and tokens_after >= HARD_CUT_AFTER_TOKENS:
            # §21: "cut at 80 tokens post-interrupt and mark the trace".
            cut = len(buffered)
            method = "hard_cut"
            log.warning(
                "sentence-boundary detector ran away past %d tokens; hard cut",
                HARD_CUT_AFTER_TOKENS,
            )

        if cut is not None:
            tail = buffered[:cut]
            delivered.append(tail)
            yield tail
            break

    state.point = InterruptionPoint(
        delivered_text="".join(delivered),
        generated_text="".join(generated),
        boundary_method=method,
        tokens_after_interrupt=tokens_after,
    )


class _Boundary(BaseModel):
    """Where the model says the sentence ends, as an index into the buffer."""

    end_index: int


def make_llm_boundary_check(client: Any, spec_factory: Callable[[str], Any]) -> LLMBoundaryCheck:
    """Build the Haiku fallback boundary detector (§5, §20).

    One of the Orchestrator's two sanctioned reasoning calls. Bounded to a
    single turn and traced under the ``orchestrator`` identity.
    """

    async def check(buffered: str) -> int | None:
        result = await client.parse(spec_factory(buffered), _Boundary, thinking=False)
        boundary = getattr(result.structured, "end_index", None)
        if boundary is None or not (0 < boundary <= len(buffered)):
            return None
        return boundary

    return check


# --- SSE -------------------------------------------------------------------


def sse_event(chunk: Any) -> str:
    """Render one chunk as an SSE frame.

    ``data: {json}\\n\\n`` per §20. ``default=str`` so a UUID or datetime that
    reaches a payload serialises rather than raising inside the response body,
    where the exception would truncate the stream with no useful message.
    """
    payload = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
    return f"data: {json.dumps(payload, default=str)}\n\n"


async def sse_stream(chunks: AsyncIterator[Any]) -> AsyncIterator[str]:
    """Adapt a chunk stream into SSE frames, always closing cleanly.

    A client that disconnects raises ``CancelledError`` here; it is re-raised
    after the terminator so the server-side cleanup in §21 still runs.
    """
    try:
        async for chunk in chunks:
            yield sse_event(chunk)
    except asyncio.CancelledError:
        log.info("client disconnected mid-stream")
        raise
    finally:
        # A terminator lets the client distinguish a finished stream from a
        # dropped connection without a timeout.
        yield "event: done\ndata: {}\n\n"
