"""One reachability check per database URL per session.

The online tier is meant to *skip* cheaply when no database is running, so the
offline tier stays usable on a laptop with nothing provisioned. Two things
conspired against that:

* An unreachable port is **dropped rather than refused** under the default
  Windows firewall, so a connect costs the full ``connect_timeout`` instead of
  returning immediately -- and libpq applies that timeout *per resolved
  address*, so ``localhost`` (both ``::1`` and ``127.0.0.1``) costs double.
* Nothing cached the answer. Every fixture that wanted a database rediscovered
  the same unreachable port, and a run that should have skipped in seconds took
  over a minute before anyone saw a skip reason.

So the probe happens once per URL and the verdict is reused.
"""

from __future__ import annotations

from functools import cache

import pytest
from sqlalchemy import create_engine, text

#: Per address, so ``localhost`` costs twice this in the worst case. Low enough
#: that discovering "no database" is quick, high enough that a container still
#: coming up is not misreported as absent.
CONNECT_TIMEOUT_SECONDS = 3


@cache
def probe(url: str) -> str | None:
    """Return ``None`` if the database answers, else a short failure reason."""
    engine = create_engine(
        url,
        future=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 -- reported as a skip, not a failure
        return type(exc).__name__
    finally:
        engine.dispose()
    return None


def require_database(url: str, env_var: str) -> None:
    """Skip the calling test unless ``url`` is reachable.

    A skip, not a failure: an absent database means "not checked here", and
    turning that red would train everyone to ignore red.
    """
    reason = probe(url)
    if reason is not None:
        pytest.skip(
            f"no Postgres at {url!r} ({reason}). Start one, then point "
            f"{env_var} at it."
        )
