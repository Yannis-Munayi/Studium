"""Anthropic client setup and shared request helpers."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import anthropic

MODEL = "claude-opus-4-8"

# $ per million tokens (Claude Opus 4.8)
PRICE_INPUT = 5.00
PRICE_OUTPUT = 25.00
PRICE_CACHE_WRITE_1H = 10.00   # 2x input for 1-hour TTL writes
PRICE_CACHE_READ = 0.50        # 0.1x input


def _windows_user_env(name: str) -> str | None:
    """Read a per-user environment variable straight from the registry.

    `setx` writes there, but already-running shells (and terminals spawned by
    an already-running VS Code) never see the update — so fall back to the
    registry instead of making students restart their editor.
    """
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value or None
    except OSError:
        return None


def get_client() -> anthropic.Anthropic:
    # Zero-arg client resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
    # or an `ant auth login` profile automatically.
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        saved = _windows_user_env("ANTHROPIC_API_KEY")
        if saved:
            os.environ["ANTHROPIC_API_KEY"] = saved
    return anthropic.Anthropic()


def pdf_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        "title": path.stem,
    }


def cached_system(persona: str, grounding: str) -> list[dict]:
    """System prompt with the stable unit grounding as a cached prefix.

    Caching is a prefix match: persona and grounding are byte-stable for the
    whole study session, so every lesson question and grading call after the
    first reads them at ~10% of the normal input price.
    """
    return [
        {"type": "text", "text": persona},
        {
            "type": "text",
            "text": grounding,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]


def usage_cost(usage) -> tuple[str, float]:
    """Human-readable usage summary and estimated cost in USD."""
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (
        inp * PRICE_INPUT + out * PRICE_OUTPUT
        + cw * PRICE_CACHE_WRITE_1H + cr * PRICE_CACHE_READ
    ) / 1_000_000
    parts = [f"in={inp:,}", f"out={out:,}"]
    if cw:
        parts.append(f"cache_write={cw:,}")
    if cr:
        parts.append(f"cache_read={cr:,}")
    return " ".join(parts), cost
