"""Async bridge to the synchronous data layer.

Spec §4 makes every agent method ``async def``; the data layer (subsystem 1 §9)
is synchronous SQLAlchemy over psycopg. Something has to reconcile them, and
the choice is deliberate: rather than port the data layer to asyncio -- which
would mean re-testing every query, trigger and isolation guarantee subsystem 1
already verified -- database work runs in a worker thread and the coroutine
awaits it.

That is the right trade at MVP scale. Queries are single-digit milliseconds and
the process serves three learners, so thread-pool hop cost is noise next to a
multi-second model call. It stops being right when connection-pool contention
shows up, which at this scale it will not; the interface here is narrow enough
that swapping in an async engine later touches this file and nothing else.
See DIVERGENCES-RUNTIME.md (R5).

Two rules callers must follow, both enforced by the shape of :func:`run_db`:

* **One transaction per call.** The session opens, commits, and closes inside
  the worker. A caller cannot hold a session across an ``await`` -- which is
  what would actually break, since a model call between two statements pins a
  connection for the length of the generation.
* **Return values, not ORM instances.** Detached instances lazy-load on
  attribute access, and lazy-loading from the event loop thread after the
  session closed raises somewhere far from the cause. Callers return plain
  dicts and scalars; :func:`rows_to_dicts` makes that convenient.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from .db import SessionLocal


async def run_db[T](fn: Callable[[Session], T]) -> T:
    """Run ``fn`` against a fresh session in a worker thread, then commit.

    Rolls back and re-raises on failure, so a partially applied batch of
    ``ToolEffect``s never reaches the database -- the all-or-nothing guarantee
    §8 asks for.
    """
    return await asyncio.to_thread(_run_sync, fn)


def _run_sync[T](fn: Callable[[Session], T]) -> T:
    session = SessionLocal()
    try:
        result = fn(session)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def read_db[T](fn: Callable[[Session], T]) -> T:
    """Read-only variant: rolls back rather than committing.

    Same thread hop, but a read path that accidentally mutates state fails
    visibly at the next read instead of silently persisting.
    """
    return await asyncio.to_thread(_read_sync, fn)


def _read_sync[T](fn: Callable[[Session], T]) -> T:
    session = SessionLocal()
    try:
        return fn(session)
    finally:
        session.rollback()
        session.close()


def jsonable(value: Any) -> Any:
    """Coerce a value into something JSONB and Pydantic both accept.

    UUIDs, datetimes and Decimals all appear in rows headed for a JSONB column
    or a Pydantic model; psycopg will not serialise the first two and Decimal
    loses exactness through ``float`` unless it is a string.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def row_to_dict(row: Any, *, fields: Iterable[str] | None = None) -> dict[str, Any]:
    """Flatten an ORM instance into a plain dict before the session closes.

    ``metadata`` is mapped as ``meta`` on the ORM classes (``metadata`` is
    reserved by SQLAlchemy's declarative base); it is emitted under its column
    name so prompt builders and JSONB round-trips see the schema's spelling.
    """
    if row is None:
        return {}
    if fields is None:
        fields = [c.key for c in row.__table__.columns]
    out: dict[str, Any] = {}
    for name in fields:
        attr = "meta" if name == "metadata" else name
        out[name] = jsonable(getattr(row, attr, None))
    return out


def rows_to_dicts(rows: Sequence[Any], *, fields: Iterable[str] | None = None) -> list[dict[str, Any]]:
    field_list = list(fields) if fields is not None else None
    return [row_to_dict(r, fields=field_list) for r in rows]
