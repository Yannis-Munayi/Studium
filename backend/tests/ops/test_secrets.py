"""The secret registry and the files it generates (infrastructure §6, §16).

§16 Tier 1: "Secret variable names match between production, local, and CI
configurations."

The registry is the single source; ``.env.example`` is generated from it and
``fly.toml`` is checked against it. What these tests defend is that the three
cannot drift -- which is the failure that surfaces as "it works on my machine
and the deploy comes up with no Voyage key", read as a provider outage for the
first twenty minutes.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from studium.ops import secrets

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
ENV_EXAMPLE = BACKEND / ".env.example"
BACKEND_FLY = BACKEND / "fly.toml"
FRONTEND_FLY = ROOT / "studium-web" / "fly.toml"


def test_registry_is_not_empty() -> None:
    assert len(secrets.REGISTRY) >= 15
    assert secrets.secrets_for("backend"), "no backend secrets registered"


def test_names_are_unique() -> None:
    names = [v.name for v in secrets.REGISTRY]
    assert len(names) == len(set(names)), "a variable is registered twice"


@pytest.mark.parametrize("variable", secrets.REGISTRY, ids=lambda v: v.name)
def test_every_variable_is_documented(variable: secrets.Variable) -> None:
    """Purpose and placeholder are what a new operator reads first."""
    assert variable.purpose.strip(), f"{variable.name} has no stated purpose"
    assert variable.example.strip(), f"{variable.name} has no placeholder value"
    assert variable.app in {"backend", "frontend"}


@pytest.mark.parametrize("variable", secrets.REGISTRY, ids=lambda v: v.name)
def test_degrading_variables_say_what_degrades(variable: secrets.Variable) -> None:
    """A warning that does not name its consequence is a warning nobody acts on.

    ``degrades_in`` without ``degradation`` produces "absent" with no
    explanation, which reads as an error the operator cannot triage.
    """
    if variable.degrades_in:
        assert variable.degradation.strip(), (
            f"{variable.name} degrades in {sorted(variable.degrades_in)} and "
            f"does not say what breaks"
        )


@pytest.mark.parametrize("variable", secrets.REGISTRY, ids=lambda v: v.name)
def test_required_and_degrading_do_not_overlap(variable: secrets.Variable) -> None:
    """One environment, one classification.

    A variable both required and degrading in the same environment would be
    reported as whichever branch ``audit`` checks first, which makes the
    severity an accident of code order.
    """
    overlap = variable.required_in & variable.degrades_in
    assert not overlap, f"{variable.name} is both required and degrading in {overlap}"


@pytest.mark.parametrize("variable", secrets.REGISTRY, ids=lambda v: v.name)
def test_environments_are_known(variable: secrets.Variable) -> None:
    for environment in variable.required_in | variable.degrades_in:
        assert environment in secrets.ENVIRONMENTS, (
            f"{variable.name} names environment {environment!r}; §6.2 defines "
            f"exactly {secrets.ENVIRONMENTS}"
        )


#: What makes a placeholder visibly not a credential. Two shapes, because the
#: registry legitimately has both:
#:
#: * an angle-bracketed or elided token (``<seed>``, ``sk-ant-...``), which
#:   nothing would accept;
#: * a complete localhost URL, which is the *right* placeholder for the
#:   database entries -- a developer copies .env.example to .env.local and it
#:   works against docker-compose. The credential in it is `studium:studium`,
#:   which is docker-compose.yml's, and is committed there already.
_PLACEHOLDER_MARKERS = ("<", "...")
_LOCAL_HOSTS = ("localhost", "127.0.0.1")


@pytest.mark.parametrize("variable", secrets.REGISTRY, ids=lambda v: v.name)
def test_placeholders_are_not_credentials(variable: secrets.Variable) -> None:
    """The committed example must not contain anything usable in production.

    A placeholder that looks like a real key is a placeholder someone will
    eventually replace with a real key and commit, because the diff looks the
    same either way.
    """
    if not variable.secret:
        return
    example = variable.example

    if any(host in example for host in _LOCAL_HOSTS):
        # A localhost URL cannot reach anything outside the machine reading it.
        # Assert it stays that way rather than only that it is a URL.
        assert example.startswith("postgresql"), (
            f"{variable.name}'s placeholder points at localhost and is not a "
            f"database URL; the exemption is for the compose credentials only"
        )
        return

    assert any(marker in example for marker in _PLACEHOLDER_MARKERS), (
        f"{variable.name}'s placeholder {example!r} does not look like a "
        f"placeholder. Use <angle brackets> or a trailing ellipsis."
    )
    # A real Anthropic key is ~100 characters and a base64 Ed25519 seed is 44.
    # A DSN template is longer than either and is made of brackets, so it is
    # the bracket count rather than the length that says it is inert.
    opaque = re.sub(r"<[^>]*>", "", example)
    assert len(opaque) < 40, (
        f"{variable.name}'s placeholder has {len(opaque)} characters outside "
        f"its brackets -- long enough to be a real value"
    )


def test_env_example_matches_the_registry() -> None:
    """§16 Tier 1. The file is generated; a hand-edit is a drift.

    This checks the *function*. CI diffs the **CLI's stdout** against the same
    file, which is a strictly stronger claim -- see the test below, which is
    the one that would have caught the two differing by a trailing newline.
    """
    assert ENV_EXAMPLE.is_file(), (
        "backend/.env.example is missing. §6.2 requires it committed. "
        "Run: studium ops secrets render --write"
    )
    rendered = secrets.render_env_example()
    on_disk = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert on_disk == rendered, (
        ".env.example is stale. Run: studium ops secrets render --write"
    )


def test_the_cli_prints_exactly_what_is_on_disk() -> None:
    """What §16's Tier 1 step actually runs: `render` piped through diff.

    The test above compares ``render_env_example()`` to the file and so cannot
    see the gap this closes: `print(rendered)` appended a second newline, and
    the CI step diffed *that* against the file. The step therefore failed on a
    file that was perfectly current, and `render --write` -- the fix its own
    error message recommends -- produced no diff to explain it. A generator
    whose two output paths disagree is a check that can never pass.

    Line endings are normalised because Python's text-mode stdout writes CRLF
    on Windows while CI runs on Linux; the trailing-newline count, which is
    what broke, survives that normalisation.
    """
    result = subprocess.run(
        [sys.executable, "-m", "studium.ops.cli", "ops", "secrets", "render"],
        cwd=BACKEND,
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=True,
    )
    stdout = result.stdout.replace(b"\r\n", b"\n")
    on_disk = ENV_EXAMPLE.read_bytes().replace(b"\r\n", b"\n")
    assert stdout == on_disk, (
        "`studium ops secrets render` does not print byte-for-byte what "
        "`--write` puts in .env.example, so §16's Tier 1 diff cannot pass "
        "however recently the file was rendered."
    )


def test_env_example_names_every_backend_variable() -> None:
    on_disk = ENV_EXAMPLE.read_text(encoding="utf-8")
    for variable in secrets.REGISTRY:
        if variable.app != "backend":
            continue
        assert f"\n{variable.name}=" in on_disk, (
            f"{variable.name} is registered and absent from .env.example"
        )


@pytest.mark.parametrize("config", [BACKEND_FLY, FRONTEND_FLY], ids=lambda p: p.name)
def test_no_secret_is_assigned_in_a_committed_fly_config(config: Path) -> None:
    """§3: secrets live in Fly secrets, "never in committed environment files".

    Checked against the registry's classification rather than a regex for
    things that look like keys, so a newly registered secret is covered the
    moment it exists rather than the moment someone remembers to add a pattern.
    """
    assert config.is_file(), f"{config} is missing"
    assignments = {
        match.group(1)
        for line in config.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
        for match in [re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=", line)]
        if match
    }
    leaked = sorted(
        name
        for name in assignments
        if name in secrets.BY_NAME and secrets.BY_NAME[name].secret
    )
    assert not leaked, (
        f"{config.name} assigns secret(s) {leaked}. A key in a committed TOML "
        f"is a key in the git history forever."
    )


@pytest.mark.parametrize("config", [BACKEND_FLY, FRONTEND_FLY], ids=lambda p: p.name)
def test_fly_env_variables_are_registered(config: Path) -> None:
    """Anything the deployment sets is something the registry knows about.

    Otherwise ``secrets check`` reports a clean environment while the
    deployment depends on a variable nobody documented -- the drift §16's line
    is about, in the direction that is harder to notice.
    """
    text = config.read_text(encoding="utf-8")
    # Only the [env] block; [build] and [[vm]] keys are Fly's own.
    in_env = False
    unknown: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_env = stripped == "[env]"
            continue
        if not in_env or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name.isupper() and name not in secrets.BY_NAME:
            unknown.append(name)

    # NODE_ENV, PORT, HOSTNAME and the Sentry sample rate are runtime knobs of
    # third-party software rather than Studium configuration. Named explicitly
    # so the exemption is a decision rather than a gap.
    exempt = {"NODE_ENV", "PORT", "HOSTNAME", "SENTRY_TRACES_SAMPLE_RATE"}
    unregistered = sorted(set(unknown) - exempt)
    assert not unregistered, (
        f"{config.name} sets {unregistered}, which studium/ops/secrets.py does "
        f"not know about. Register them or remove them."
    )


def test_audit_classifies_present_absent_and_missing() -> None:
    environ = {"ANTHROPIC_API_KEY": "sk-ant-real", "STUDIUM_DATABASE_URL": "postgres://x"}
    findings = {f.variable.name: f for f in secrets.audit(environ, environment="production")}

    assert findings["ANTHROPIC_API_KEY"].level == "ok"
    assert findings["STUDIUM_DATABASE_URL"].level == "ok"
    # Required in production and absent.
    assert findings["STUDIUM_SIGNING_KEY"].level == "error"
    # Degrades rather than fails.
    assert findings["VOYAGE_API_KEY"].level == "warning"
    assert "keyword-only" in findings["VOYAGE_API_KEY"].detail


def test_present_but_empty_counts_as_absent() -> None:
    """A Fly secret set to "" is a rotation that half-happened.

    Treating it as present is how the next deploy comes up with an SDK
    constructed from an empty string, which fails at the first call rather than
    at startup.
    """
    findings = {
        f.variable.name: f
        for f in secrets.audit(
            {"STUDIUM_SIGNING_KEY": "   "}, environment="production"
        )
    }
    assert findings["STUDIUM_SIGNING_KEY"].level == "error"


def test_audit_rejects_an_unknown_environment() -> None:
    with pytest.raises(ValueError, match="production"):
        secrets.audit({}, environment="staging")


def test_local_requires_less_than_production() -> None:
    """A developer's checkout is not a deployment.

    If local required what production does, `secrets check` would be red on
    every machine and would stop being read -- which is the state that makes a
    genuinely missing production secret invisible.
    """
    local = secrets.errors(secrets.audit({}, environment="local"))
    production = secrets.errors(secrets.audit({}, environment="production"))
    assert len(local) < len(production)


def test_quarterly_rotation_covers_the_provider_keys() -> None:
    """§6.3 names Anthropic and Voyage explicitly."""
    assert "ANTHROPIC_API_KEY" in secrets.QUARTERLY_ROTATION
    assert "VOYAGE_API_KEY" in secrets.QUARTERLY_ROTATION
    for name in secrets.QUARTERLY_ROTATION:
        assert secrets.BY_NAME[name].rotation == "quarterly"


def test_signing_key_rotates_annually() -> None:
    """§6.3: "Signing keys. Annually.\""""
    assert secrets.BY_NAME["STUDIUM_SIGNING_KEY"].rotation == "annually"


def test_the_env_file_is_gitignored() -> None:
    """§6.2: ".env.local file, gitignored."

    The one test here whose failure would be a live secret in the repository.
    """
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignored
    assert ".env.*" in ignored, (
        ".gitignore covers .env and not .env.local, which is the file §6.2 "
        "actually tells an operator to create"
    )
    assert "!.env.example" in ignored, (
        "the example is generated and must be tracked, so the ignore needs an "
        "explicit exception"
    )
