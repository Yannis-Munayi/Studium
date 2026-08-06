"""Command-line interface.

  python -m tutor units                     list modules/units and their state
  python -m tutor ingest [--module M] [--unit KEY] [--force]
  python -m tutor learn  [--student NAME] [--unit KEY]
  python -m tutor status [--student NAME]
"""

from __future__ import annotations

import argparse
import getpass
import sys

from rich.table import Table

from . import ingest as ingest_mod
from .assess import run_assessment
from .catalog import Course, Unit, load_course
from .lesson import LessonSession
from .paths import COURSEPACK_DIR, MATERIAL_DIR, PROGRESS_DIR
from .progress import PASS_THRESHOLD, Progress
from .ui import console, panel


def _course() -> Course:
    if not MATERIAL_DIR.exists():
        console.print(f"[red]Course material not found at {MATERIAL_DIR}[/red]")
        sys.exit(1)
    return load_course(MATERIAL_DIR)


def cmd_units(args) -> None:
    course = _course()
    progress = Progress.load(PROGRESS_DIR, args.student)
    table = Table(title=f"{course.title} ({course.name})")
    table.add_column("Unit key", style="cyan", overflow="fold")
    table.add_column("Title")
    table.add_column("Ingested", justify="center")
    table.add_column("Status", justify="center")
    for module in course.modules:
        table.add_row(f"[bold magenta]{module.key}[/bold magenta]",
                      f"[bold magenta]{module.title}[/bold magenta]", "", "")
        for unit in module.units:
            ingested = "yes" if ingest_mod.pack_path(COURSEPACK_DIR, unit).exists() else "-"
            if progress.has_passed(unit.key):
                status = f"[green]passed ({progress.best_score(unit.key):.0%})[/green]"
            elif progress.is_unlocked(course, unit):
                status = "[yellow]unlocked[/yellow]"
            else:
                status = "[dim]locked[/dim]"
            table.add_row(f"  {unit.key}", f"  {unit.title}", ingested, status)
    console.print(table)


def cmd_ingest(args) -> None:
    course = _course()
    console.print(
        "[dim]Ingestion makes one Claude call per unit (whole-unit PDFs go in; "
        "a full lesson pack comes out). Already-ingested units are skipped "
        "unless --force is given.[/dim]"
    )
    ingest_mod.ingest(
        course, COURSEPACK_DIR,
        module_key=args.module, unit_key=args.unit, force=args.force,
        log=console.print,
    )


def _resolve_unit(course: Course, progress: Progress, unit_key: str | None) -> Unit | None:
    if unit_key:
        unit = course.find_unit(unit_key)
        if unit is None:
            console.print(f"[red]No unit '{unit_key}'. See `python -m tutor units`.[/red]")
            return None
        if not progress.is_unlocked(course, unit):
            console.print(
                f"[red]'{unit.title}' is locked — pass the earlier units first "
                f"(>= {PASS_THRESHOLD:.0%} each).[/red]"
            )
            return None
        return unit
    unit = progress.current_unit(course)
    if unit is None:
        console.print("[bold green]Course complete — every unit passed![/bold green]")
    return unit


def cmd_learn(args) -> None:
    course = _course()
    progress = Progress.load(PROGRESS_DIR, args.student)
    unit = _resolve_unit(course, progress, args.unit)
    if unit is None:
        return

    pack = ingest_mod.load_pack(COURSEPACK_DIR, unit)
    if pack is None:
        console.print(f"[yellow]'{unit.title}' has not been ingested yet.[/yellow]")
        if input("Ingest it now (one Claude call)? [y/N] ").strip().lower() != "y":
            return
        from .client import get_client
        pack, summary, cost = ingest_mod.ingest_unit(get_client(), unit, COURSEPACK_DIR)
        console.print(f"[dim]ingested: {summary} | ~${cost:.2f}[/dim]")

    attempts = len(progress.attempts(unit.key))
    if attempts:
        console.print(f"[dim]Previous attempts on this unit: {attempts} "
                      f"(best {progress.best_score(unit.key):.0%})[/dim]")

    finished = LessonSession(unit, pack, args.mode).run()
    if not finished:
        return
    if input("Take the mastery assessment now? [Y/n] ").strip().lower() == "n":
        console.print("[dim]Run `python -m tutor learn` again when ready.[/dim]")
        return

    passed = run_assessment(unit, pack, progress)
    if passed:
        nxt = progress.current_unit(course)
        if nxt:
            console.print(f"\nNext up: [cyan]{nxt.title}[/cyan] — run `python -m tutor learn`.")
        else:
            console.print("\n[bold green]That was the last unit — course complete![/bold green]")
    else:
        console.print("\n[yellow]The unit stays unlocked — review and retake with "
                      "`python -m tutor learn`.[/yellow]")


def cmd_status(args) -> None:
    course = _course()
    progress = Progress.load(PROGRESS_DIR, args.student)
    units = course.ordered_units()
    passed = sum(1 for u in units if progress.has_passed(u.key))
    panel(
        f"Student: **{args.student}**\n\n"
        f"Progress: **{passed}/{len(units)}** units passed "
        f"({passed / len(units):.0%} of the course)",
        "Status", style="magenta",
    )
    table = Table()
    table.add_column("Unit")
    table.add_column("Attempts", justify="center")
    table.add_column("Best score", justify="center")
    table.add_column("Status", justify="center")
    for unit in units:
        n = len(progress.attempts(unit.key))
        best = progress.best_score(unit.key)
        status = ("[green]passed[/green]" if progress.has_passed(unit.key)
                  else "[yellow]unlocked[/yellow]" if progress.is_unlocked(course, unit)
                  else "[dim]locked[/dim]")
        table.add_row(unit.title, str(n) if n else "-",
                      f"{best:.0%}" if best is not None else "-", status)
    console.print(table)


def cmd_serve(args) -> None:
    try:
        from .webapp import serve
        import uvicorn  # noqa: F401
    except ImportError:
        console.print("[red]Web UI needs extra packages: pip install fastapi uvicorn[/red]")
        return
    console.print(f"[bold]λearn[/bold] running — open [cyan]http://127.0.0.1:{args.port}[/cyan] "
                  "(Ctrl+C to stop)")
    serve(port=args.port)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tutor", description=__doc__)
    parser.add_argument("--student", default=getpass.getuser(),
                        help="student name for progress tracking (default: OS username)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("units", help="list modules/units, ingestion and progress state")

    p_ingest = sub.add_parser("ingest", help="build unit packs from the course PDFs")
    p_ingest.add_argument("--module", help="only this module key (e.g. 2_lambda_calculus)")
    p_ingest.add_argument("--unit", help="only this unit key")
    p_ingest.add_argument("--force", action="store_true", help="re-ingest existing packs")

    p_learn = sub.add_parser("learn", help="study the next unlocked unit (lesson + assessment)")
    p_learn.add_argument("--unit", help="study a specific unlocked/passed unit")
    p_learn.add_argument("--mode", choices=["beginner","standard","advanced"], default="standard",
                        help="explanation style for live tutoring and slower explanations")

    sub.add_parser("status", help="show progress summary")

    p_serve = sub.add_parser("serve", help="run the web UI (λearn)")
    p_serve.add_argument("--port", type=int, default=8787)

    args = parser.parse_args(argv)
    {"units": cmd_units, "ingest": cmd_ingest, "learn": cmd_learn,
     "status": cmd_status, "serve": cmd_serve}[args.command](args)
