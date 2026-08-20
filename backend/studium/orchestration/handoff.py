"""Agent registry and agent-to-agent context handoff (agent runtime §6, §3).

§3's "no cross-agent prompt context" is the rule this module exists to keep. An
agent never receives another agent's prompt or reasoning. When the Curator's
decision has to reach the Lecturer, it travels as an explicit context field or
a payload value -- never as prompt text.

:func:`handoff` is the only sanctioned way to move information between agents
inside a turn, and its signature makes the rule visible: it takes a context and
named values, not another agent's output object.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from studium.agents.confusion_tracker import ConfusionTracker
from studium.agents.curator import Curator
from studium.agents.evaluator import Evaluator
from studium.agents.lecturer import Lecturer
from studium.agents.reviewer import Reviewer
from studium.agents.tutor import Tutor
from studium.llm.client import AnthropicClient
from studium.retrieval import PassageRetriever, default_retriever
from studium.session.context import Passage, SessionContext

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentRegistry:
    """The six specialist agents. The Orchestrator is not one of them.

    §8 makes the Orchestrator a coordinator: it holds this registry, it is not
    held by it. Keeping it out avoids the cycle where routing needs the
    registry and the registry needs routing.
    """

    curator: Curator
    lecturer: Lecturer
    tutor: Tutor
    evaluator: Evaluator
    confusion_tracker: ConfusionTracker
    reviewer: Reviewer

    @classmethod
    def build(
        cls,
        client: AnthropicClient | None = None,
        retriever: PassageRetriever | None = None,
    ) -> AgentRegistry:
        client = client or AnthropicClient()
        # Subsystem 3's handoff point. Until it existed this defaulted to
        # CuratedPointerRetriever, which reads an author's pointers and does no
        # search at all. ``default_retriever`` returns the full hybrid pipeline,
        # falling back to stub embeddings (with a warning) when the Voyage SDK
        # is absent -- still an improvement on the curated-only default, since
        # the keyword half and the curated boost are both real either way.
        retriever = retriever or default_retriever()
        return cls(
            curator=Curator(client),
            lecturer=Lecturer(client, retriever),
            tutor=Tutor(client, retriever),
            evaluator=Evaluator(client),
            confusion_tracker=ConfusionTracker(client),
            reviewer=Reviewer(client, retriever),
        )

    def by_identity(self, identity: str) -> Any:
        try:
            return getattr(self, identity)
        except AttributeError as exc:  # noqa: TRY003
            raise KeyError(f"no agent with identity {identity!r}") from exc


def handoff(
    context: SessionContext,
    *,
    focus_concept: dict[str, Any] | None = None,
    passages: list[Passage] | None = None,
    warnings: list[Any] | None = None,
    exchange_index: int | None = None,
) -> SessionContext:
    """Produce the context for the next agent in a turn.

    A copy, never a mutation. The Orchestrator holds one context per turn and
    hands narrowed views of it to each agent; mutating in place would let a
    Lecturer's retrieval leak into the Confusion-Tracker's view of the same
    turn, which is the cross-agent contamination §3 forbids.
    """
    update: dict[str, Any] = {}
    if focus_concept is not None:
        update["focus_concept"] = focus_concept
    if passages is not None:
        update["passages"] = passages
    if warnings is not None:
        update["warnings"] = warnings
    if exchange_index is not None:
        update["exchange_index"] = exchange_index
    return context.model_copy(update=update) if update else context


def refocus(context: SessionContext, concept_id: str) -> SessionContext:
    """Point the context at a different concept in the same subject.

    Used by ``im_lost`` (the Curator resets focus to the last high-mastery
    concept) and by the Curator's ``next_topic``. Falls back to the unchanged
    context when the concept is not in the loaded graph rather than raising --
    a focus that cannot be resolved is a routing problem, not a reason to drop
    the learner's turn.
    """
    for concept in context.subject_concepts:
        if str(concept["id"]) == str(concept_id):
            # Passages belong to the old concept; clearing them forces the next
            # agent to re-ground rather than cite the wrong source.
            return context.model_copy(
                update={"focus_concept": concept, "passages": []}
            )
    log.warning("cannot refocus on unknown concept %s", concept_id)
    return context
