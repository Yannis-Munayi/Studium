"""Deployment artefacts (infrastructure §4, §5, §10, §16 Tier 1).

The Fly configuration, the Dockerfiles, the CI workflows and the runbooks are
files rather than modules, and they are the half of subsystem 7 that fails
silently: a typo in ``fly.toml`` is a deploy-time error nobody sees until the
deploy, and a runbook naming a command that no longer exists is worse than no
runbook because it is read during an incident.

These tests read the files. They cannot check that a deploy works -- there is
no Fly account in CI (§6.2 scopes the deployment token to a workstation), so
"the pipeline works" is §16's Tier 2 line against a scratch environment, run by
hand. Saying so here rather than letting a green suite imply otherwise.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
WEB = ROOT / "studium-web"
WORKFLOWS = ROOT / ".github" / "workflows"
DOCS = ROOT / "docs" / "ops"


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


# --- §4: topology ----------------------------------------------------------


@pytest.mark.parametrize(
    "path", [BACKEND / "fly.toml", WEB / "fly.toml"], ids=["backend", "frontend"]
)
def test_fly_configs_parse(path: Path) -> None:
    """A syntax error here is a deploy-time failure and nothing earlier.

    Also catches the mistake of commenting a TOML with ``//``, which parses as
    a key and takes the whole file down.
    """
    assert path.is_file(), f"{path} is missing"
    assert _toml(path), f"{path} parsed empty"


def test_both_apps_are_in_toronto() -> None:
    """§4.1. Latency to eastern Canada is 15-40ms; the initial learners are
    in Ontario. A region drift would be silent and would move every learner's
    per-turn latency."""
    assert _toml(BACKEND / "fly.toml")["primary_region"] == "yyz"
    assert _toml(WEB / "fly.toml")["primary_region"] == "yyz"


def test_backend_vm_matches_section_4_2() -> None:
    vm = _toml(BACKEND / "fly.toml")["vm"][0]
    assert vm["size"] == "shared-cpu-2x"
    assert vm["memory"] == "2gb"


def test_frontend_vm_matches_section_4_3() -> None:
    vm = _toml(WEB / "fly.toml")["vm"][0]
    assert vm["size"] == "shared-cpu-1x"
    assert vm["memory"] == "1gb"


def test_autoscaling_is_off_on_both_apps() -> None:
    """§4.2: "Auto-scaling introduces cold-start latency that hurts the
    streaming interruption UX."

    Enabling it also silently multiplies the cache warmer's spend and splits
    the Orchestrator registry -- see docs/ops/scaling.md. Worth a test so it
    cannot be turned on as a one-line convenience.
    """
    backend = _toml(BACKEND / "fly.toml")["services"][0]
    assert backend["auto_start_machines"] is False
    assert backend["auto_stop_machines"] is False
    assert backend["min_machines_running"] == 1

    frontend = _toml(WEB / "fly.toml")["http_service"]
    assert frontend["auto_start_machines"] is False
    assert frontend["auto_stop_machines"] is False


def test_the_backend_has_no_public_ports() -> None:
    """§4.7: "Only the frontend has a public IP."

    This is the test with the largest consequence if it ever fails. The backend
    has no authentication in front of it -- ``POST /api/session`` takes a raw
    ``user_id`` and trusts it (frontend §14, DIVERGENCES-FRONTEND F7) -- so a
    ``ports`` entry here publishes an API where anyone can open a session as
    anyone.
    """
    config = _toml(BACKEND / "fly.toml")
    assert "http_service" not in config, (
        "an [http_service] block publishes the backend at the edge; §4.7 puts "
        "it behind the frontend's proxy on the private network"
    )
    for service in config.get("services", []):
        assert not service.get("ports"), (
            "a public port on the backend exposes an unauthenticated API to "
            "the internet"
        )


def test_the_frontend_is_public_and_forces_https() -> None:
    service = _toml(WEB / "fly.toml")["http_service"]
    assert service["force_https"] is True
    assert service["internal_port"] == 3000


def test_the_backend_health_check_points_at_the_health_endpoint() -> None:
    """§4.2: Fly's load balancer routes only to healthy instances."""
    check = _toml(BACKEND / "fly.toml")["services"][0]["http_checks"][0]
    assert check["path"] == "/health"
    assert check["method"] == "get"


def test_the_frontend_health_check_is_the_url_the_monitor_polls() -> None:
    """§7.4 pings https://studium.app/health.

    A Fly check on a different path would pass while the monitored URL failed,
    which is a check measuring the wrong thing.
    """
    check = _toml(WEB / "fly.toml")["http_service"]["checks"][0]
    assert check["path"] == "/health"
    assert (WEB / "app" / "health" / "route.ts").is_file(), (
        "fly.toml health-checks /health and the frontend has no such route"
    )


def test_migrations_run_as_a_release_command() -> None:
    """§10.3: "Migration failure aborts the deploy; the previous version
    continues serving."

    True only because it is a release command -- a migration run at container
    start would race the health check and let a half-migrated instance take
    traffic.
    """
    deploy = _toml(BACKEND / "fly.toml")["deploy"]
    assert "alembic upgrade head" in deploy["release_command"]


def test_the_backend_mounts_a_volume_for_sources() -> None:
    """§4.5. Bigger than §14.3's 30GB trigger, so the trigger fires with room
    to run the migration rather than with the volume already full."""
    mount = _toml(BACKEND / "fly.toml")["mounts"][0]
    assert mount["destination"] == "/data"
    env = _toml(BACKEND / "fly.toml")["env"]
    assert env["STUDIUM_SOURCE_STORAGE_ROOT"].startswith("/data")


def test_the_retention_worker_is_enabled_in_the_deployment_only() -> None:
    """§12.1. Off in the code, on here. The one place that arms it."""
    assert _toml(BACKEND / "fly.toml")["env"]["STUDIUM_RETENTION_WORKER"] == "1"


# --- Dockerfiles -----------------------------------------------------------


@pytest.mark.parametrize(
    "path", [BACKEND / "Dockerfile", WEB / "Dockerfile"], ids=["backend", "frontend"]
)
def test_images_run_as_a_non_root_user(path: Path) -> None:
    """A container that can rewrite its own application code turns a
    code-execution bug into a persistent one."""
    text = path.read_text(encoding="utf-8")
    assert re.search(r"^USER (?!root)", text, re.MULTILINE), (
        f"{path.name} does not drop root"
    )


@pytest.mark.parametrize(
    "path", [BACKEND / "Dockerfile", WEB / "Dockerfile"], ids=["backend", "frontend"]
)
def test_images_are_multi_stage(path: Path) -> None:
    """The runtime stage has no compiler and no package manager cache, so a
    vulnerability in a build tool is not one in the thing serving learners."""
    text = path.read_text(encoding="utf-8")
    assert text.count("FROM ") >= 2, f"{path.name} is a single-stage build"


@pytest.mark.parametrize(
    "path", [BACKEND / ".dockerignore", WEB / ".dockerignore"], ids=["backend", "frontend"]
)
def test_dockerignore_excludes_env_files(path: Path) -> None:
    """The one that would bake real keys into a published layer."""
    assert path.is_file(), f"{path} is missing"
    text = path.read_text(encoding="utf-8")
    assert ".env" in text
    assert ".env.*" in text
    assert "!.env.example" in text


def test_the_backend_runs_one_uvicorn_worker() -> None:
    """§4.2 sizes one VM for the API plus every background task.

    Each worker gets its own cache warmer (billing a reranker on a timer) and
    its own scheduler. The scheduler is guarded by an advisory lock; the warmer
    is not, and two of them is a cost multiplier nobody would attribute to a
    concurrency setting.
    """
    text = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    assert '"--workers", "1"' in text


# --- §10: CI ---------------------------------------------------------------


REQUIRED_WORKFLOWS = (
    "data-layer",
    "agent-runtime",
    "retrieval",
    "frontend",
    "ingestion",
    "evaluation",
    "infrastructure",
    "prompt-regression",
)


@pytest.mark.parametrize("name", REQUIRED_WORKFLOWS)
def test_every_workflow_section_10_names_exists(name: str) -> None:
    """§10.1 lists four and §10.2 adds four more.

    A workflow that stopped existing is a subsystem whose CI silently stopped
    running, which looks exactly like a subsystem with no failures.
    """
    assert (WORKFLOWS / f"{name}.yml").is_file(), f"{name}.yml is missing"


def test_the_prompt_regression_workflow_does_not_run_on_a_schedule() -> None:
    """§13.3 asks for a weekly full suite; wiring it to `schedule` would run
    $5-15 of model calls at 03:00 with nobody awake to read the result.

    It is on the operational calendar instead. Pinned here because `schedule:`
    is a two-line addition that would look reasonable in review.
    """
    text = (WORKFLOWS / "prompt-regression.yml").read_text(encoding="utf-8")
    triggers = text.split("jobs:")[0]
    assert "schedule:" not in triggers, (
        "the regression suite is wired to a cron; it spends real money with "
        "nobody present. See the header of that workflow."
    )


def test_the_paid_workflow_refuses_fork_pull_requests() -> None:
    """An outside contributor must not be able to spend the Anthropic budget.

    Comments are stripped before the check. The workflow's header explains why
    ``pull_request_target`` is *not* used, and a naive substring search would
    fail on the explanation -- a test that cannot tell a decision from its
    documentation is one that gets its assertion weakened rather than its
    subject fixed.
    """
    text = (WORKFLOWS / "prompt-regression.yml").read_text(encoding="utf-8")
    active = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "pull_request_target" not in active, (
        "pull_request_target hands repository secrets to code from a fork"
    )
    assert "head.repo.full_name == github.repository" in active


# --- §9, §16: runbooks -----------------------------------------------------


REQUIRED_RUNBOOKS = (
    "README.md",
    "postgres-restore.md",
    "volume-restore.md",
    "drill-log.md",
    "signing-keys.md",
    "secrets.md",
    "retention.md",
    "deploy.md",
    "runbook.md",
    "storage-migration.md",
    "scaling.md",
)


@pytest.mark.parametrize("name", REQUIRED_RUNBOOKS)
def test_the_runbook_exists_and_is_not_a_stub(name: str) -> None:
    """§16 Tier 1: "Restore procedure documentation exists and is up to date.\""""
    path = DOCS / name
    assert path.is_file(), f"docs/ops/{name} is missing"
    assert len(path.read_text(encoding="utf-8")) > 400, f"{name} is a stub"


def test_every_ops_command_a_runbook_names_actually_exists() -> None:
    """A runbook naming a command that no longer exists is worse than no
    runbook, because it is read during an incident.

    Checked against the CLI's own parser rather than against a list, so a
    renamed command fails here rather than at 03:00.
    """
    from studium.ops.cli import _build_parser

    parser = _build_parser()
    ops = next(
        action
        for action in parser._subparsers._group_actions  # noqa: SLF001
        for action in [action]
    )
    known = set(ops.choices["ops"]._subparsers._group_actions[0].choices)  # noqa: SLF001

    referenced: set[str] = set()
    for name in REQUIRED_RUNBOOKS:
        for match in re.finditer(
            r"studium ops ([a-z][a-z-]+)", (DOCS / name).read_text(encoding="utf-8")
        ):
            referenced.add(match.group(1))

    unknown = sorted(referenced - known)
    assert not unknown, (
        f"runbooks name `studium ops {unknown}` and the CLI has no such "
        f"command(s). Known: {sorted(known)}"
    )


def test_the_drill_log_states_whether_a_drill_has_run() -> None:
    """§9.3's whole point is that an unexercised backup is unverified.

    A drill log that says nothing about whether a drill has happened is
    indistinguishable from one where they all went fine.
    """
    text = (DOCS / "drill-log.md").read_text(encoding="utf-8")
    assert "unverified" in text or "### 20" in text, (
        "the drill log has neither an entry nor a statement that no drill has "
        "been run"
    )
