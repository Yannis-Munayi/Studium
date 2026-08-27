"""The secret and configuration registry (infrastructure §6).

§3: "The distinction between 'configuration that can be committed' (feature
flags, timeouts, model choices) and 'secrets that cannot' is bright and
enforced." This module is where it is bright. Every environment variable the
backend reads is declared once, classified, and given the places it has to be
present. Three things then check themselves against this list rather than
against each other:

* ``.env.example`` -- §6.2's committed file of names with placeholder values.
  A Tier 1 test regenerates it and fails on a diff, so a new secret cannot be
  added to the code and forgotten in the file a new operator reads.
* ``fly.toml`` -- the deployment's non-secret ``[env]`` block. The same test
  asserts that nothing classified as a secret appears there, which is the
  failure mode worth automating: a key pasted into a committed TOML is a key in
  the git history forever.
* ``studium ops secrets check`` -- run against a live environment, before and
  after a rotation. Reports what is missing for the environment it is told it
  is in, and refuses to print any value.

**Names are the contract, not values.** §16's Tier 1 line is "Secret variable
names match between production, local, and CI configurations". A name that
drifts between the three is the kind of thing that surfaces as "it works on my
machine and the deploy comes up with no Voyage key", which reads as a provider
outage for the first twenty minutes.

**Absence is classified, not assumed.** ``required_in`` distinguishes a secret
whose absence takes the product down (``ANTHROPIC_API_KEY`` in production) from
one whose absence degrades a subsystem loudly (``VOYAGE_API_KEY``: retrieval
falls back to keyword-only, per retrieval §16) from one whose absence is
correct in that environment (``SENTRY_DSN`` locally). Reporting all three as
"missing" is how a check gets ignored.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

#: The three places §6.2 puts values. Production is Fly secrets, local is
#: ``.env.local``, CI is GitHub Actions secrets.
ENVIRONMENTS: tuple[str, ...] = ("production", "local", "ci")


@dataclass(frozen=True, slots=True)
class Variable:
    """One environment variable the backend or frontend reads."""

    name: str
    #: What it is for, in a sentence. Printed by ``secrets check``; also the
    #: comment written into ``.env.example``, so it is the first thing a new
    #: operator reads about it.
    purpose: str
    #: True when disclosure would allow unauthorised access, enable fraud, or
    #: compromise learner data (§6.1's three tests). Secrets never appear in a
    #: committed file, in ``fly.toml``, in logs or in an API response.
    secret: bool
    #: Environments where absence is a defect. A variable absent everywhere
    #: listed here is what ``secrets check`` reports as an error.
    required_in: frozenset[str] = frozenset()
    #: Environments where absence degrades something, loudly and on purpose.
    #: Reported as a warning with the consequence named.
    degrades_in: frozenset[str] = frozenset()
    #: What happens when it is absent in a ``degrades_in`` environment.
    degradation: str = ""
    #: §6.3's cadence. "quarterly", "annually", "on rebuild", "never", or "".
    rotation: str = ""
    #: Placeholder written into ``.env.example``. Never a real value, and for
    #: secrets never anything that could be mistaken for one.
    example: str = ""
    #: Which app reads it. Both apps' variables are registered here so the
    #: "names match" check spans the deployment rather than half of it.
    app: str = "backend"
    #: Free-text notes carried into the generated file.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def category(self) -> str:
        return "secret" if self.secret else "config"


_ALL = frozenset(ENVIRONMENTS)
_DEPLOYED = frozenset({"production", "ci"})


REGISTRY: tuple[Variable, ...] = (
    # --- §6.1 API keys -----------------------------------------------------
    Variable(
        name="ANTHROPIC_API_KEY",
        purpose="Model access for every agent (agent runtime §4).",
        secret=True,
        required_in=_ALL,
        rotation="quarterly",
        example="sk-ant-...",
        notes=(
            "Read by the Anthropic SDK directly, so the name is theirs rather "
            "than ours and cannot be prefixed STUDIUM_.",
        ),
    ),
    Variable(
        name="VOYAGE_API_KEY",
        purpose="Embeddings for hybrid retrieval (retrieval §6).",
        secret=True,
        degrades_in=_ALL,
        degradation=(
            "retrieval runs on stub embeddings and falls back to keyword-only "
            "(retrieval §16). Search still answers; ranking quality is not what "
            "was measured."
        ),
        rotation="quarterly",
        example="pa-...",
    ),
    Variable(
        name="LANGFUSE_PUBLIC_KEY",
        purpose="LLM tracing (§7.1). Paired with LANGFUSE_SECRET_KEY.",
        secret=False,
        degrades_in=_DEPLOYED,
        degradation="no LLM traces; agent_traces rows are still written.",
        rotation="quarterly",
        example="pk-lf-...",
        notes=(
            "Public in Langfuse's own sense -- it identifies the project and "
            "cannot write without the secret half. Kept out of fly.toml anyway: "
            "it is half of a credential pair, and splitting a pair across a "
            "committed file and a secret store is how the wrong half gets "
            "rotated.",
        ),
    ),
    Variable(
        name="LANGFUSE_SECRET_KEY",
        purpose="LLM tracing (§7.1).",
        secret=True,
        degrades_in=_DEPLOYED,
        degradation="no LLM traces; agent_traces rows are still written.",
        rotation="quarterly",
        example="sk-lf-...",
    ),
    Variable(
        name="LANGFUSE_HOST",
        purpose="Langfuse Cloud endpoint (§7.1). Self-hosting is a v2 option.",
        secret=False,
        example="https://cloud.langfuse.com",
    ),
    Variable(
        name="SENTRY_DSN",
        purpose="Error reporting (§7.2).",
        secret=True,
        degrades_in=_DEPLOYED,
        degradation=(
            "unhandled exceptions reach the Fly log and nowhere else. No alert "
            "fires for the §8.1 error-rate condition, because nothing is "
            "counting."
        ),
        rotation="quarterly",
        example="https://<key>@<org>.ingest.sentry.io/<project>",
        notes=(
            "A DSN is write-only and often treated as public. Classified secret "
            "here because anyone holding it can flood the error budget, and the "
            "§8.1 error-rate alert reads that budget.",
        ),
    ),
    # --- §6.1 database URLs ------------------------------------------------
    Variable(
        name="STUDIUM_DATABASE_URL",
        purpose="Application connection. Cannot UPDATE or DELETE the "
        "append-only tables (migration 0003).",
        secret=True,
        required_in=_ALL,
        rotation="never",
        example="postgresql+psycopg://studium:studium@localhost:5432/studium",
        notes=("Contains credentials, which is what makes it a secret.",),
    ),
    Variable(
        name="STUDIUM_OWNER_DATABASE_URL",
        purpose="Owner connection for the retention, erasure and roll-up jobs, "
        "which must DELETE from append-only tables.",
        secret=True,
        required_in=frozenset({"production"}),
        degrades_in=frozenset({"local", "ci"}),
        degradation=(
            "falls back to STUDIUM_DATABASE_URL, which is correct locally where "
            "both roles are the same superuser and wrong in production, where "
            "the retention worker would fail its first DELETE."
        ),
        rotation="never",
        example="postgresql+psycopg://studium:studium@localhost:5432/studium",
    ),
    # --- §6.1 session and signing ------------------------------------------
    Variable(
        name="STUDIUM_SESSION_SECRET",
        purpose="Cookie authentication signing (§6.1).",
        secret=True,
        rotation="on rebuild",
        example="<32 random bytes, base64>",
        notes=(
            "Declared and unused. Frontend §14's auth is a placeholder "
            "(DIVERGENCES-FRONTEND F7) and nothing signs a cookie yet. It is "
            "here because §6.1 names it and §6.3 gives it a rotation trigger -- "
            "'rotated when auth infrastructure gets rebuilt' -- and a secret "
            "that appears in the registry the day auth lands is a secret nobody "
            "has to remember to add. See DIVERGENCES-INFRASTRUCTURE (N7).",
        ),
    ),
    Variable(
        name="STUDIUM_SIGNING_KEY",
        purpose="Ed25519 private seed for portfolio credentials (§11, "
        "evaluation §12.3). 32 bytes, base64.",
        secret=True,
        required_in=frozenset({"production"}),
        degrades_in=frozenset({"local", "ci"}),
        degradation=(
            "credentials cannot be issued; the learner sees evaluation §16's "
            "message and a retry job picks it up. Work items are hash-chained "
            "and recorded as unsigned rather than failing the turn."
        ),
        rotation="annually",
        example="<32 random bytes, base64>",
        notes=(
            "§9.1: the one thing whose loss cannot be recovered from the "
            "running system. Two independent encrypted copies, per §9.1.",
        ),
    ),
    Variable(
        name="STUDIUM_SIGNING_KEY_ID",
        purpose="Stable public name of the current signing key, quoted in every "
        "credential's issuer_public_key_id.",
        secret=False,
        required_in=frozenset({"production"}),
        example="studium-2026",
        notes=(
            "Not a secret -- it is published at /api/portfolio/keys. It lives "
            "in Fly secrets anyway, because splitting a key and its identifier "
            "across two stores is how a rotation updates one and not the other.",
        ),
    ),
    # --- §6.1 object storage (provisioned when §14.3 fires) ----------------
    Variable(
        name="STUDIUM_STORAGE_BACKEND",
        purpose="Which studium.storage implementation serves source files: "
        "'local' (Fly volume) or 'r2'.",
        secret=False,
        example="local",
        notes=("§4.5: migration to R2 is a configuration change, not a code change.",),
    ),
    Variable(
        name="STUDIUM_SOURCE_STORAGE_ROOT",
        purpose="Filesystem root for the local storage backend (§4.5).",
        secret=False,
        example="/data/sources",
    ),
    Variable(
        name="STUDIUM_R2_BUCKET",
        purpose="Cloudflare R2 bucket for source files, once §14.3's 30GB "
        "trigger fires.",
        secret=False,
        example="studium-sources",
    ),
    Variable(
        name="STUDIUM_R2_ENDPOINT",
        purpose="R2 S3-compatible endpoint.",
        secret=False,
        example="https://<account>.r2.cloudflarestorage.com",
    ),
    Variable(
        name="STUDIUM_R2_ACCESS_KEY_ID",
        purpose="R2 credentials (§6.1, 'when provisioned').",
        secret=True,
        rotation="quarterly",
        example="<r2 access key id>",
    ),
    Variable(
        name="STUDIUM_R2_SECRET_ACCESS_KEY",
        purpose="R2 credentials (§6.1, 'when provisioned').",
        secret=True,
        rotation="quarterly",
        example="<r2 secret access key>",
    ),
    # --- configuration, committed on purpose --------------------------------
    Variable(
        name="STUDIUM_ENV",
        purpose="Which environment this process is (§5.1). Isolates local "
        "activity in Langfuse traces and selects the §6 required-secret set.",
        secret=False,
        required_in=frozenset({"production"}),
        example="local",
    ),
    Variable(
        name="STUDIUM_WARM_CACHE",
        purpose="Retrieval §14's cache warmer. Off unless set to 1, because it "
        "bills a reranker on a timer forever.",
        secret=False,
        example="0",
    ),
    Variable(
        name="STUDIUM_RETENTION_WORKER",
        purpose="§12.1's nightly retention pass, in-process. Off unless set to "
        "1, so a developer's checkout never deletes anything.",
        secret=False,
        example="0",
    ),
    Variable(
        name="STUDIUM_ALERT_EMAIL",
        purpose="Where §8.3's alerts are delivered. One address at MVP.",
        secret=False,
        required_in=frozenset({"production"}),
        example="ops@example.com",
    ),
    Variable(
        name="STUDIUM_RUN_PAID_TESTS",
        purpose="Tier 3 opt-in. Every paid test skips unless this is 1.",
        secret=False,
        example="0",
    ),
    Variable(
        name="OTEL_EXPORTER_OTLP_ENDPOINT",
        purpose="Where OpenTelemetry spans go (§7.3). Langfuse accepts OTLP, "
        "so LLM and HTTP traces share a trace id.",
        secret=False,
        example="https://cloud.langfuse.com/api/public/otel",
    ),
    # --- frontend -----------------------------------------------------------
    Variable(
        name="STUDIUM_BACKEND_ORIGIN",
        purpose="Backend origin the Next proxy forwards to (§4.7's internal "
        "DNS). Server-side only; the browser never learns it.",
        secret=False,
        required_in=frozenset({"production"}),
        example="http://studium-backend.internal:8000",
        app="frontend",
    ),
    Variable(
        name="STUDIUM_ALLOW_UNAUTHENTICATED",
        purpose="Runs the frontend with the placeholder learner identity "
        "outside development (frontend §14, DIVERGENCES-FRONTEND F7).",
        secret=False,
        example="0",
        app="frontend",
        notes=(
            "Set to 1 in the MVP deployment, deliberately and visibly: there is "
            "no auth to run instead. It is in this registry so 'why is this "
            "single-tenant' has a written answer, and so the day auth lands the "
            "variable's removal is a diff someone reviews.",
        ),
    ),
)

BY_NAME: dict[str, Variable] = {v.name: v for v in REGISTRY}

#: §6.3's quarterly rotation set, in the order the procedure walks them.
QUARTERLY_ROTATION: tuple[str, ...] = tuple(
    v.name for v in REGISTRY if v.rotation == "quarterly"
)


def secrets_for(app: str = "backend") -> tuple[Variable, ...]:
    return tuple(v for v in REGISTRY if v.app == app and v.secret)


def config_for(app: str = "backend") -> tuple[Variable, ...]:
    return tuple(v for v in REGISTRY if v.app == app and not v.secret)


@dataclass(frozen=True, slots=True)
class Finding:
    """One variable's status in one environment."""

    variable: Variable
    level: str  # "error" | "warning" | "ok"
    detail: str

    def render(self) -> str:
        mark = {"error": "MISSING", "warning": "absent", "ok": "ok"}[self.level]
        return f"  [{mark:>7}] {self.variable.name:32} {self.detail}"


def audit(
    environ: Mapping[str, str] | None = None,
    *,
    environment: str = "local",
    app: str = "backend",
) -> list[Finding]:
    """Classify every registered variable against a live environment.

    Present-but-empty counts as absent. A Fly secret set to the empty string is
    a rotation that half-happened, and treating it as present is how the next
    deploy comes up with an SDK constructing itself from ``""``.
    """
    if environment not in ENVIRONMENTS:
        raise ValueError(
            f"{environment!r} is not one of {ENVIRONMENTS}; §6.2 names exactly "
            f"these three places a value can live"
        )
    values = os.environ if environ is None else environ

    findings: list[Finding] = []
    for variable in REGISTRY:
        if variable.app != app:
            continue
        present = bool((values.get(variable.name) or "").strip())
        if present:
            findings.append(Finding(variable, "ok", variable.purpose))
        elif environment in variable.required_in:
            findings.append(
                Finding(variable, "error", f"required in {environment}: {variable.purpose}")
            )
        elif environment in variable.degrades_in:
            findings.append(Finding(variable, "warning", variable.degradation))
        else:
            findings.append(
                Finding(variable, "ok", f"not required in {environment}")
            )
    return findings


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "error"]


def warnings(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "warning"]


# --- .env.example ----------------------------------------------------------

ENV_EXAMPLE_HEADER = """\
# Studium backend environment (infrastructure §6.2).
#
# GENERATED FILE -- do not edit by hand. It is rendered from
# studium/ops/secrets.py by `studium ops secrets render`, and
# tests/ops/test_secrets.py fails on a diff. That is deliberate: §16's Tier 1
# line is "Secret variable names match between production, local, and CI
# configurations", and a hand-maintained example file is the copy that drifts.
#
# Copy to .env.local (gitignored) and fill in. Values here are placeholders and
# none of them work.
#
# Rotation cadences are §6.3's. `studium ops secrets check --environment
# production` reports what a live environment is missing without printing a
# single value.
"""


def render_env_example(app: str = "backend") -> str:
    """The exact contents of ``.env.example``.

    Grouped secrets-first so the file reads as "these are the dangerous ones".
    """
    lines = [ENV_EXAMPLE_HEADER]

    for heading, group in (
        ("Secrets -- never committed, never logged, never in fly.toml", True),
        ("Configuration -- safe to commit; the deployment sets these in fly.toml", False),
    ):
        lines.append(f"\n# --- {heading} " + "-" * max(3, 74 - len(heading)) + "\n")
        for variable in REGISTRY:
            if variable.app != app or variable.secret != group:
                continue
            lines.append(f"# {variable.purpose}")
            for note in variable.notes:
                lines.append(f"# {note}")
            if variable.rotation:
                lines.append(f"# Rotation: {variable.rotation} (§6.3).")
            if variable.degrades_in and variable.degradation:
                lines.append(f"# Absent: {variable.degradation}")
            lines.append(f"{variable.name}={variable.example}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
