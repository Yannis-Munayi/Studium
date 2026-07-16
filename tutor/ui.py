"""Console helpers shared by the lesson player and assessment."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

console = Console()


def panel(text: str, title: str, style: str = "cyan") -> None:
    console.print(Panel(Markdown(text), title=title, border_style=style))


def read_multiline(prompt: str) -> str:
    """Read a multi-line answer; the student finishes with an empty line."""
    console.print(f"[bold]{prompt}[/bold] [dim](finish with an empty line)[/dim]")
    lines: list[str] = []
    while True:
        try:
            line = input("> " if not lines else "  ")
        except EOFError:
            break
        if line.strip() == "":
            break
        lines.append(line)
    return "\n".join(lines).strip()
