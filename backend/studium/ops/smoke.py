"""Post-deploy smoke test (infrastructure §10.3).

§10.3: "After deploy completes, run ``studium ops smoke-test`` which exercises:
health check, database connectivity, Anthropic reachability, Voyage
reachability (when provisioned), one full session round-trip against a test
user. Failure alerts."

**The last one costs money, so it is opt-in.** A round trip through the
Orchestrator is a real Lecturer call against a real model on every deploy. At
one deploy a day that is pennies and worth it; at twenty deploys during an
afternoon of fixing something it is both a bill and, worse, twenty sessions in
the reviewer's own history. ``--full`` runs it; without the flag the command
runs the four cheap checks and says which one it skipped and why. §10.3 asks
for the session round-trip and this is the one place the build departs from a
literal reading — see DIVERGENCES-INFRASTRUCTURE (N6).

**Reachability is a real request, not a DNS lookup.** A key that has been
revoked resolves, connects and TLS-handshakes exactly like a good one. The
provider checks below make the cheapest authenticated call each API offers, so
"reachable" means "would answer a real request" rather than "the host exists".

**Every check reports what it did and did not establish.** A green smoke test
after a deploy is read as evidence the deploy is good, so it has to be precise
about the size of that claim.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SmokeCheck:
    name: str
    passed: bool
    detail: str
    duration_ms: int = 0
    skipped: bool = False
    #: Non-blocking checks report and never fail the deploy. Voyage is one:
    #: retrieval degrades to keyword-only rather than stopping (retrieval §16).
    blocking: bool = True

    def render(self) -> str:
        if self.skipped:
            mark = "skip"
        elif self.passed:
            mark = "PASS"
        else:
            mark = "FAIL" if self.blocking else "warn"
        return f"  [{mark:>4}] {self.name:26} {self.duration_ms:>6} ms  {self.detail}"


@dataclass(slots=True)
class SmokeResult:
    checks: list[SmokeCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.passed or c.skipped or not c.blocking for c in self.checks)

    @property
    def failures(self) -> list[SmokeCheck]:
        return [c for c in self.checks if not c.passed and not c.skipped and c.blocking]

    def render(self) -> str:
        lines = ["post-deploy smoke test (§10.3)", ""]
        lines += [c.render() for c in self.checks]
        lines.append("")
        lines.append(
            "  DEPLOY LOOKS HEALTHY" if self.ok
            else f"  {len(self.failures)} blocking failure(s) -- "
            f"`flyctl releases rollback` is ~30 seconds (§10.4)"
        )
        skipped = [c.name for c in self.checks if c.skipped]
        if skipped:
            lines.append(f"  not exercised: {', '.join(skipped)}")
        return "\n".join(lines)


def _timed(fn) -> tuple[object, int]:
    started = time.perf_counter()
    value = fn()
    return value, int((time.perf_counter() - started) * 1000)


def check_database() -> SmokeCheck:
    """Connect, check the server version and TLS, count one table.

    Version and TLS because ``studium.db`` asserts both at startup (§4, §11):
    a deploy onto a Postgres 15 or a plaintext connection comes up and fails on
    a query nobody can attribute.
    """
    from sqlalchemy import text as sql

    def probe() -> str:
        from studium.db import SessionLocal, assert_server_version

        with SessionLocal() as session:
            assert_server_version(session)
            version = session.execute(sql("SHOW server_version")).scalar_one()
            ssl = session.execute(
                sql("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
            ).scalar()
            users = session.execute(sql("SELECT count(*) FROM users")).scalar_one()
        tls = {True: "TLS", False: "NO TLS", None: "TLS unknown"}[ssl]
        return f"Postgres {version}, {tls}, {users} user(s)"

    try:
        detail, ms = _timed(probe)
    except Exception as exc:  # noqa: BLE001
        return SmokeCheck("database", False, f"{type(exc).__name__}: {exc}")
    return SmokeCheck("database", True, str(detail), ms)


def check_migrations() -> SmokeCheck:
    """The deployed schema is at the head this release expects.

    §10.3 runs migrations as a release command, so a mismatch here means the
    release command did not run — which presents as a working deploy until the
    first query touching a new column.
    """
    from sqlalchemy import text as sql

    from studium.ops.restore import head_revision

    def probe() -> str:
        from studium.db import SessionLocal

        with SessionLocal() as session:
            actual = session.execute(
                sql("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
        expected = head_revision()
        if expected and actual != expected:
            raise RuntimeError(
                f"database is at {actual}, this release expects {expected}; "
                f"the release command did not run"
            )
        return f"at {actual}"

    try:
        detail, ms = _timed(probe)
    except Exception as exc:  # noqa: BLE001
        return SmokeCheck("migrations", False, f"{type(exc).__name__}: {exc}")
    return SmokeCheck("migrations", True, str(detail), ms)


def check_anthropic() -> SmokeCheck:
    """One real, minimal message. Cheap and authenticated.

    ``max_tokens=1`` on Haiku: fractions of a cent, and it exercises the whole
    path -- key, network, model name, SDK version. A models.list would be
    cheaper still and would not catch a key without inference permission.
    """
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return SmokeCheck(
            "anthropic", False, "ANTHROPIC_API_KEY is not set", skipped=False
        )

    def probe() -> str:
        from anthropic import Anthropic

        from studium.llm.models import HAIKU

        client = Anthropic()
        response = client.messages.create(
            model=HAIKU.id,
            max_tokens=1,
            messages=[{"role": "user", "content": "ok"}],
        )
        return f"{response.model} answered ({response.stop_reason})"

    try:
        detail, ms = _timed(probe)
    except Exception as exc:  # noqa: BLE001
        return SmokeCheck("anthropic", False, f"{type(exc).__name__}: {exc}")
    return SmokeCheck("anthropic", True, str(detail), ms)


def check_voyage() -> SmokeCheck:
    """§10.3's "when provisioned". Non-blocking by design.

    Retrieval falls back to keyword-only without it (retrieval §16), so a
    Voyage outage is degraded quality rather than a broken deploy, and failing
    the smoke test on it would train someone to pass ``--force``.
    """
    import os

    if not os.environ.get("VOYAGE_API_KEY"):
        return SmokeCheck(
            "voyage",
            True,
            "VOYAGE_API_KEY not set; retrieval runs keyword-only (retrieval §16)",
            skipped=True,
            blocking=False,
        )

    def probe() -> str:
        import asyncio

        from studium.retrieval.providers import VoyageEmbeddings

        vectors = asyncio.run(
            VoyageEmbeddings().embed(["smoke test"], input_type="query")
        )
        return f"embedded 1 text, {len(vectors[0])} dimensions"

    try:
        detail, ms = _timed(probe)
    except Exception as exc:  # noqa: BLE001
        return SmokeCheck("voyage", False, f"{type(exc).__name__}: {exc}", blocking=False)
    return SmokeCheck("voyage", True, str(detail), ms)


def check_health(base_url: str) -> SmokeCheck:
    """``GET /health`` over HTTP, the way Fly's load balancer does it.

    In-process would test the handler; this tests the deployment -- the port
    binding, the proxy, the health-check path Fly is configured with. Those are
    what break on a deploy, and the handler is what does not.
    """

    def probe() -> str:
        import json
        import urllib.request

        with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=10) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            body = json.loads(r.read())
        return (
            f"{body.get('status')} / {body.get('subsystem')} "
            f"v{body.get('version')}, "
            f"retention worker running={body.get('retention', {}).get('running')}"
        )

    try:
        detail, ms = _timed(probe)
    except Exception as exc:  # noqa: BLE001
        return SmokeCheck("health endpoint", False, f"{type(exc).__name__}: {exc}")
    return SmokeCheck("health endpoint", True, str(detail), ms)


def check_signing_key() -> SmokeCheck:
    """A signing round-trip against the published key (§9.2's key restore).

    Signs a probe payload with ``STUDIUM_SIGNING_KEY`` and verifies it against
    what ``signing_keys`` publishes. That is the check that catches the failure
    a rotation actually produces: the secret updated and the public half not
    published, which issues credentials nobody can verify and raises nothing at
    the time.
    """

    def probe() -> str:
        from sqlalchemy import text as sql

        from studium.db import SessionLocal
        from studium.eval.credentials import load_identity, sign, verify

        identity = load_identity()
        payload = {"probe": "smoke-test"}
        signature = sign(payload, identity)

        with SessionLocal() as session:
            published = session.execute(
                sql(
                    "SELECT public_key, retired_at, compromised_at "
                    "  FROM signing_keys WHERE key_id = :k"
                ),
                {"k": identity.key_id},
            ).one_or_none()

        if published is None:
            raise RuntimeError(
                f"key {identity.key_id!r} is configured but not published; "
                f"credentials would be issued and fail verification. "
                f"`studium ops publish-signing-key` fixes it."
            )
        if not verify(payload, signature, published.public_key):
            raise RuntimeError(
                f"the configured private key does not match the published "
                f"public key for {identity.key_id!r}"
            )
        flags = []
        if published.retired_at:
            flags.append("RETIRED")
        if published.compromised_at:
            flags.append("COMPROMISED")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        return f"{identity.key_id} signs and verifies{suffix}"

    try:
        detail, ms = _timed(probe)
    except Exception as exc:  # noqa: BLE001
        from studium.eval.credentials import SigningKeyUnavailable

        blocking = not isinstance(exc, SigningKeyUnavailable)
        return SmokeCheck(
            "signing key",
            False,
            f"{type(exc).__name__}: {exc}",
            blocking=blocking,
        )
    return SmokeCheck("signing key", True, str(detail), ms)


def run(*, base_url: str | None = None, full: bool = False) -> SmokeResult:
    """§10.3's smoke test. ``full`` adds the billable session round trip."""
    result = SmokeResult()
    result.checks.append(check_database())
    result.checks.append(check_migrations())
    if base_url:
        result.checks.append(check_health(base_url))
    else:
        result.checks.append(
            SmokeCheck(
                "health endpoint",
                True,
                "no --base-url given; pass one to exercise the deployed HTTP path",
                skipped=True,
            )
        )
    result.checks.append(check_anthropic())
    result.checks.append(check_voyage())
    result.checks.append(check_signing_key())

    if full:
        result.checks.append(check_session_round_trip())
    else:
        result.checks.append(
            SmokeCheck(
                "session round trip",
                True,
                "billable; pass --full to run it (§10.3, DIVERGENCES N6)",
                skipped=True,
            )
        )
    return result


def check_session_round_trip() -> SmokeCheck:
    """§10.3's "one full session round-trip against a test user".

    Opens a session as the system account, takes one turn, closes it. The
    system account rather than a real learner: an artificial session in a
    person's history is a reporting bug that reads as a data bug, and the
    system account is exactly the synthetic owner data layer §6.12 defines for
    this kind of thing.
    """
    import asyncio
    import uuid

    def probe() -> str:
        from sqlalchemy import text as sql

        from studium.db import SessionLocal
        from studium.models.identity import SYSTEM_USER_ID

        async def drive() -> tuple[uuid.UUID, int]:
            from studium.agents.orchestrator import LearnerInput, Orchestrator
            from studium.orchestration.handoff import AgentRegistry
            from studium.session import memory
            from studium.session.lifecycle import close_session

            orchestrator = Orchestrator(agents=AgentRegistry.build())
            session_id = await orchestrator.start_session(
                SYSTEM_USER_ID, "tutorial", None
            )
            chunks = 0
            async for _ in orchestrator.handle_turn(
                session_id, LearnerInput(text="Smoke test. Reply with one word.")
            ):
                chunks += 1
            context = await memory.assemble_context(session_id)
            await close_session(context, orchestrator.agents, reason="smoke_test")
            return session_id, chunks

        session_id, chunks = asyncio.run(drive())

        with SessionLocal() as session:
            cost = session.execute(
                sql(
                    """
                    SELECT COALESCE(SUM(t.cost_usd), 0)
                      FROM agent_traces t
                      JOIN session_turns st ON st.id = t.session_turn_id
                     WHERE st.session_id = :sid
                    """
                ),
                {"sid": session_id},
            ).scalar_one()
        return f"session {session_id} ran {chunks} chunk(s), cost ${float(cost):.4f}"

    try:
        detail, ms = _timed(probe)
    except Exception as exc:  # noqa: BLE001
        return SmokeCheck("session round trip", False, f"{type(exc).__name__}: {exc}")
    return SmokeCheck("session round trip", True, str(detail), ms)
