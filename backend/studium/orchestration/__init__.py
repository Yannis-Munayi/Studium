"""Coordination: state machine, streaming, primitives, handoff, effects (§7, §8, §15, §20)."""

from .effects import EffectApplicationError, apply_effects
from .handoff import AgentRegistry, handoff, refocus
from .primitives import PRIMITIVE_HANDLERS, dispatch_primitive
from .state_machine import Event, GuardContext, SessionStateMachine, State
from .streaming import InterruptionState, sentence_boundary_iter, sse_stream

__all__ = [
    "PRIMITIVE_HANDLERS",
    "AgentRegistry",
    "EffectApplicationError",
    "Event",
    "GuardContext",
    "InterruptionState",
    "SessionStateMachine",
    "State",
    "apply_effects",
    "dispatch_primitive",
    "handoff",
    "refocus",
    "sentence_boundary_iter",
    "sse_stream",
]
