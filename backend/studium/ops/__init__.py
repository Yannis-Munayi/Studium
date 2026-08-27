"""Operations: the running system's envelope (infrastructure spec v1.0).

Subsystem 7 is mostly configuration and integration rather than new behaviour,
and this package is the part of it that is code: the secret registry the
deployment is checked against (§6), the alert conditions (§8), the restore
verification (§9), the retention worker (§12), the cost report (§13), and the
post-deploy smoke test (§10.3). Everything else -- topology, Dockerfiles, the
Fly configuration, the runbooks -- is files rather than modules, and lives in
``backend/fly.toml``, ``studium-web/fly.toml`` and ``docs/ops/``.

**Nothing here is imported by a learner's request path.** The API imports
:mod:`studium.ops.scheduler` for the §12.1 nightly worker and nothing else, so
an operations module that fails to import cannot take the product down with it.
The observability hooks that *are* on the request path live in
:mod:`studium.observability`, which is separately defensive about it.

**Everything here connects as ``studium_owner``.** The retention worker deletes
from tables migration 0003 makes append-only to the application role, the
erasure procedure writes ``audit_log``, and the cost report reads across every
learner. ``studium.ops.session_scope`` is the one place that decides which URL
that is, so no command assembles it by hand.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session, sessionmaker

__all__ = ["owner_session", "session_scope"]

_owner_sessionmaker: sessionmaker[Session] | None = None


def _factory() -> sessionmaker[Session]:
    """Build the owner session factory once, lazily.

    Lazily because importing this package must not open a connection: the
    Tier 1 tests import it on a machine with no Postgres, and ``studium ops
    secrets check`` -- the command you run *because* the deployment is
    misconfigured -- would otherwise fail on the database URL before it could
    tell you which secret was missing.
    """
    global _owner_sessionmaker
    if _owner_sessionmaker is None:
        from studium.db import owner_engine

        _owner_sessionmaker = sessionmaker(
            bind=owner_engine(), expire_on_commit=False, class_=Session
        )
    return _owner_sessionmaker


@contextmanager
def owner_session() -> Iterator[Session]:
    """A session as ``studium_owner``. Rolls back on exception, never commits.

    Committing is the caller's decision, deliberately: several ops commands
    show what they would do and wait for a confirmation, and a context manager
    that committed on the way out would make the dry run and the real run the
    same code path with a different print statement.
    """
    session = _factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


#: Alias reading better at call sites that do commit inside the block.
session_scope = owner_session
