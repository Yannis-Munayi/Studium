"""End-of-unit mastery assessment: the Feynman gate.

The student explains each rubric concept in their own words; one grading call
scores every criterion (0/1/2) against the key points extracted at ingestion.
The pass decision is computed here in code — weighted score >= 75% — never by
asking the model "did they pass?".

Grading runs in a fresh context, separate from the tutoring conversation, so
an encouraging tutor persona cannot inflate grades.
"""

from __future__ import annotations

import json

from rich.table import Table

from .catalog import Unit
from .client import MODEL, get_client, usage_cost
from .models import GradeReport, UnitPack
from .progress import PASS_THRESHOLD, Progress
from .ui import console, panel, read_multiline

GRADER_SYSTEM = """\
You are a strict but fair examiner grading a student's spoken-style
explanations against a rubric. For each criterion, compare the student's
explanation to the listed key points and score:

  2 — explanation covers the key points accurately in the student's own words
  1 — partially correct: some key points present, or minor inaccuracies
  0 — missing, wrong, restates the question, or "I don't know"

Rules: award credit only for content actually present in the student's answer.
Do not reward confident tone, length, or vocabulary without substance. List
concretely which key points were missing or wrong in missing_points. Feedback
is 1-2 sentences per criterion, specific enough to study from. Use each
criterion's exact id from the rubric in criterion_id.
"""


def grade_answers(client, pack: UnitPack, answers: list[dict]) -> tuple[GradeReport, float, dict[str, int], object]:
    """One grading call for the whole exam. Returns (report, score, per_criterion, usage).

    `answers` items: {criterion_id, concept, student_explanation}.
    Shared by the terminal flow and the web app.
    """
    rubric_json = json.dumps([c.model_dump() for c in pack.rubric], indent=1)
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=GRADER_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Rubric:\n{rubric_json}\n\n"
                f"Student explanations:\n{json.dumps(answers, indent=1)}"
            ),
        }],
        output_format=GradeReport,
    )
    report = response.parsed_output
    if report is None:
        raise RuntimeError("Grading response could not be parsed — please retake.")
    score, per_criterion = _score(report, pack)
    return report, score, per_criterion, response.usage


def _score(report: GradeReport, pack: UnitPack) -> tuple[float, dict[str, int]]:
    by_id = {g.criterion_id: g for g in report.criterion_grades}
    earned, possible, per_criterion = 0.0, 0.0, {}
    for i, crit in enumerate(pack.rubric):
        grade = by_id.get(crit.id)
        if grade is None and i < len(report.criterion_grades):
            grade = report.criterion_grades[i]  # fall back to order
        points = grade.score if grade else 0
        per_criterion[crit.id] = points
        earned += (points / 2) * crit.weight
        possible += crit.weight
    return (earned / possible if possible else 0.0), per_criterion


def run_assessment(unit: Unit, pack: UnitPack, progress: Progress) -> bool:
    """Run the exam. Returns True on pass."""
    client = get_client()

    panel(
        f"You will explain **{len(pack.rubric)} concepts** from *{unit.title}* "
        "in your own words, as if teaching them to a classmate. Definitions, "
        "examples, and reasoning all count.\n\n"
        f"**Pass mark: {PASS_THRESHOLD:.0%}.** Below that, you review the "
        "lesson and retake the assessment.",
        "Mastery assessment", style="red",
    )

    answers: list[dict] = []
    for i, crit in enumerate(pack.rubric, 1):
        console.print(f"\n[bold]({i}/{len(pack.rubric)})[/bold] Explain: [cyan]{crit.concept}[/cyan]")
        answers.append({
            "criterion_id": crit.id,
            "concept": crit.concept,
            "student_explanation": read_multiline("Your explanation") or "(no answer)",
        })

    console.print("\n[dim]Grading...[/dim]")
    report, score, per_criterion, resp_usage = grade_answers(client, pack, answers)
    passed = score >= PASS_THRESHOLD
    progress.record_attempt(unit.key, score, passed, per_criterion)

    # -- results ---------------------------------------------------------------
    by_id = {g.criterion_id: g for g in report.criterion_grades}
    table = Table(title=f"Results — {unit.title}", show_lines=True)
    table.add_column("Concept", max_width=32)
    table.add_column("Score", justify="center")
    table.add_column("Feedback")
    for i, crit in enumerate(pack.rubric):
        grade = by_id.get(crit.id) or (
            report.criterion_grades[i] if i < len(report.criterion_grades) else None
        )
        points = per_criterion[crit.id]
        color = {2: "green", 1: "yellow", 0: "red"}[points]
        feedback = grade.feedback if grade else "(not graded)"
        if grade and grade.missing_points:
            feedback += "\nMissing: " + "; ".join(grade.missing_points)
        table.add_row(crit.concept, f"[{color}]{points}/2[/{color}]", feedback)
    console.print(table)

    usage, cost = usage_cost(resp_usage)
    console.print(f"[dim]grading call: {usage} | ~${cost:.2f}[/dim]")

    verdict = (
        f"[bold green]PASSED[/bold green] — {score:.0%}"
        if passed else
        f"[bold red]NOT YET[/bold red] — {score:.0%} (need {PASS_THRESHOLD:.0%})"
    )
    console.print(f"\n{verdict}")
    panel(report.overall_feedback, "Examiner's summary",
          style="green" if passed else "red")
    if not passed:
        weakest = [c.concept for c in pack.rubric if per_criterion[c.id] < 2]
        console.print(
            "[yellow]Review these before retaking:[/yellow] " + "; ".join(weakest)
        )
    return passed
