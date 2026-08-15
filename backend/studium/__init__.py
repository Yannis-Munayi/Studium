"""Studium data layer (spec subsystem 1).

Persistence only. Agent behaviour, retrieval ranking, prompt content, and the
API surface belong to later subsystems; what lives here is the schema, the
deterministic rules the schema's invariants depend on, and the lifecycle jobs.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
