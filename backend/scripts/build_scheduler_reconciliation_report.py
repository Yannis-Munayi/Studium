"""Generate the scheduler-verification and spec-reconciliation report as .docx.

Written as a script rather than by hand so the numbers in it come from one
place and can be regenerated. Run from the backend directory:

    python scripts/build_scheduler_reconciliation_report.py
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

OUT = (
    Path(__file__).resolve().parents[2]
    / "Documentation"
    / "Planning"
    / "Sub System 1 - Data Layer"
    / "Version 3"
    / "scheduler-verification-and-spec-reconciliation.docx"
)


def bullets(doc: Document, items: list[str]) -> None:
    for item in items:
        doc.add_paragraph(item, style="List Bullet")


def table(doc: Document, headers: list[str], rows: list[list[str]]) -> None:
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Light Grid Accent 1"
    for cell, text in zip(t.rows[0].cells, headers, strict=True):
        cell.text = text
        for p in cell.paragraphs:
            for r in p.runs:
                r.bold = True
    for row in rows:
        cells = t.add_row().cells
        for cell, text in zip(cells, row, strict=True):
            cell.text = text
    doc.add_paragraph()


def mono(doc: Document, paragraph_text: str, code: str, tail: str = "") -> None:
    """A paragraph with one monospace run in it."""
    p = doc.add_paragraph(paragraph_text)
    run = p.add_run(code)
    run.font.name = "Consolas"
    if tail:
        p.add_run(tail)


def code_line(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.name = "Consolas"
    run.font.size = Pt(9)


def build() -> Path:
    doc = Document()
    doc.styles["Normal"].font.size = Pt(10.5)

    doc.add_heading(
        "Scheduler Verification and Data Layer Spec Reconciliation", 0
    )
    sub = doc.add_paragraph(
        "Subsystems 7 and 1  •  Data layer specification v1.2  •  1 September 2026"
    )
    sub.alignment = WD_ALIGN_PARAGRAPH.LEFT

    doc.add_paragraph(
        "The task was to wire three orphaned schedulers. They were already "
        "wired, and verifying that against a real database is most of what "
        "follows. What was not sound was the test protecting them: the one "
        "guard whose entire job is to stop those three jobs going orphaned "
        "again searched the module’s source text for their names, and the "
        "module’s docstring names all three. It could not fail. Deleting the "
        "erasure stage — reinstating the bug where right-to-erasure never "
        "completes — left it green. Fixing that led into the v1.2 spec, where "
        "§6.1 and §10.2 turned out to describe a users table nobody built: "
        "five phantom objects, including two columns the erasure procedure is "
        "written against.",
        style="Intense Quote",
    )

    # --- 1 -----------------------------------------------------------------
    doc.add_heading("1. What “verified” means in this report", 1)
    doc.add_paragraph(
        "Verified means the behaviour was exercised against a real Postgres 16 "
        "(the pinned pgvector/pgvector:pg16 image from "
        "backend/docker-compose.test.yml) and observed to be correct. Every "
        "count below comes from a run made while writing this report."
    )
    doc.add_paragraph("It does not mean, and this report does not claim:")
    bullets(
        doc,
        [
            "That anything is deployed. Nothing is. The scheduler’s 02:00 UTC "
            "firing has never been observed outside a test.",
            "That the advisory lock has been exercised under real concurrency. "
            "It is tested single-process; the multi-instance case it exists for "
            "arrives with §14.1’s auto-scaling trigger.",
            "That the spec corrections were validated against anything but the "
            "schema, the models, and the migrations. They are documentation.",
        ],
    )

    # --- 2 -----------------------------------------------------------------
    doc.add_heading("2. The three schedulers were already wired", 1)
    doc.add_paragraph(
        "Not by this session. The chain was committed on branch yannis as part "
        "of the subsystem 7 build, and it is complete end to end:"
    )
    code_line(
        doc,
        "fly.toml STUDIUM_RETENTION_WORKER=1 → app lifespan → RetentionScheduler",
    )
    code_line(
        doc,
        "  → 02:00 UTC daily → _run_pass (pg_try_advisory_lock) → run_nightly",
    )
    doc.add_paragraph(
        "run_nightly executes four stages, each failure-isolated so that one "
        "raising does not stop the others:"
    )
    table(
        doc,
        ["Stage", "Calls", "Covered by"],
        [
            [
                "retention",
                "ops.retention.run_retention",
                "§12.1’s pass; writes per-policy audit rows",
            ],
            [
                "erasure_purge",
                "privacy.purge_expired_soft_deletes",
                "Tier 2: purges an expired soft delete, spares one inside the window",
            ],
            [
                "mastery_decay",
                "jobs.cost_rollup.refresh_decay",
                "Tier 2: new this session (see §4)",
            ],
            [
                "consistency",
                "jobs.retention.check_dangling_chunk_refs",
                "Tier 2: queues a dangling ref, de-duplicates on a second pass",
            ],
        ],
    )
    doc.add_paragraph(
        "The deployment genuinely arms it: fly.toml sets the enabling variable "
        "to “1”, and a deployment test pins that value so it cannot be dropped "
        "silently. The erasure column names are consistent across privacy.py, "
        "the User model and the stage’s dry-run SQL, so the purge is functional "
        "rather than merely present."
    )
    mono(
        doc,
        "Evidence: 31 of 31 tests in ",
        "tests/online/test_ops_db.py",
        " passed against Postgres, including the hard-delete of an expired soft "
        "delete, the sparing of an account still inside the 30-day window, and "
        "the dry run that changes nothing.",
    )

    # --- 3 -----------------------------------------------------------------
    doc.add_heading("3. The guard protecting them could not fail", 1)
    doc.add_paragraph(
        "One test exists specifically to stop these three jobs losing their "
        "caller again. As written, it did this:"
    )
    code_line(doc, "source = inspect.getsource(nightly)")
    code_line(doc, "for function in (three names):")
    code_line(doc, "    assert function in source")
    doc.add_paragraph(
        "studium/ops/nightly.py opens with a docstring table naming all three "
        "orphans and explaining what breaks without each. The assertion was "
        "therefore satisfied by the prose that documents the bug, entirely "
        "independently of whether the code that fixes it still existed."
    )
    doc.add_paragraph("Confirmed two ways rather than by reading:")
    bullets(
        doc,
        [
            "Extracted the module docstring alone and checked each of the three "
            "names appears in it. All three do.",
            "Deleted the erasure_purge stage from run_nightly — reinstating the "
            "exact defect, an erasure request that never completes — and ran the "
            "suite. The test stayed green.",
        ],
    )
    doc.add_paragraph(
        "Replaced with a version that monkeypatches the three functions and "
        "asserts run_nightly actually called them, in order. That version fails "
        "on the same mutation, which is the property the original never had. "
        "The stage was then restored; nightly.py is byte-identical to its "
        "committed state."
    )
    doc.add_paragraph(
        "This is the dual of the “structurally unpassable gate” recorded during "
        "the CI triage of 28 August. There, a gate could never go green. Here, "
        "a guard could never go red. Both are verification paths that nothing "
        "verifies — and a test whose subject is “X has a caller” is unusually "
        "prone to it, because the cheap implementation greps for X and X’s name "
        "is all over the file explaining why the test exists.",
        style="Intense Quote",
    )

    # --- 4 -----------------------------------------------------------------
    doc.add_heading("4. mastery_decay ran, and proved nothing", 1)
    doc.add_paragraph(
        "The decay stage passed everywhere it was exercised, against an empty "
        "concept_mastery table — where a no-op and a correct full-table UPDATE "
        "are the same observation: zero rows, no error. The symptom of the "
        "orphaning was a stale column, so a test that never has a stale value "
        "cannot detect it."
    )
    doc.add_paragraph(
        "Added a Tier 2 test seeding a row with p_known 0.9, evidence a year "
        "old, and a decayed column still holding 0.9 — the state a year with no "
        "scheduler leaves. It asserts the decayed value drops, that the raw "
        "posterior is untouched, and that the result stays within [0, 1]. Gating "
        "is deliberately unaffected by this column, which is exactly why nobody "
        "noticed it was frozen: unlock computes decay in SQL at query time, and "
        "what reads the stored column is sorting and reporting."
    )

    # --- 5 -----------------------------------------------------------------
    doc.add_heading("5. A flag raised and withdrawn: the test database port", 1)
    doc.add_paragraph(
        "This report’s first draft claimed the online tier silently skips for "
        "developers because tests/conftest.py defaults to port 5432 while "
        "docker-compose.test.yml publishes 5433. That was wrong, and acting on "
        "it would have broken the intended path."
    )
    doc.add_paragraph(
        "The Makefile exports the test database URL at 5433 for every target, "
        "with the intent stated in a comment: the online tests use the "
        "throwaway instance so that a test run never touches the development "
        "database. The 5432 default is the bare-pytest fallback, and it points "
        "at the development compose file’s studium_test database, which the "
        "init script creates. Both paths work. The skips observed were caused "
        "by the development container having been stopped for 27 hours."
    )
    doc.add_paragraph(
        "Recorded here rather than quietly dropped, because a withdrawn finding "
        "is information: it says the two compose files are doing different jobs "
        "deliberately, which the next person to trip over a skip will want.",
        style="Intense Quote",
    )

    # --- 6 -----------------------------------------------------------------
    doc.add_heading("6. The v1.2 spec described a users table nobody built", 1)
    doc.add_paragraph(
        "The reconciliation began as a single stale column name and turned out "
        "to be five phantom objects across §6.0, §6.1, §6.11 and §10.2 — none "
        "of which exists anywhere in backend/:"
    )
    table(
        doc,
        ["v1.2 claimed", "Actually built"],
        [
            [
                "soft_deleted_at + hard_delete_after",
                "one deleted_at; window computed as deleted_at + DISPUTE_WINDOW_DAYS",
            ],
            [
                "is_system BOOLEAN NOT NULL",
                "a reserved id, 00000000-0000-7000-8000-000000000001",
            ],
            [
                "user_role enum, values include 'system'",
                "role TEXT + CHECK, values include 'admin'",
            ],
            [
                "deleted_users_aggregate table",
                "cost_ledger rows with user_id IS NULL",
            ],
            ["users.preferences", "lives on user_profiles"],
        ],
    )
    doc.add_paragraph(
        "The direction of the drift mattered more than its size, because it "
        "decides whether the fix is a documentation edit or a migration. What "
        "settled it was v1.1: it uses deleted_at fourteen times, including the "
        "index definition byte-for-byte as the code implements it, and contains "
        "no occurrence of any phantom name. v1.1 agreed with the build; v1.2 is "
        "where it broke. The redline was not a reliable index of this — the "
        "change appears only as a parenthetical inside a list of things that "
        "did not change, never as one of the 25 numbered items."
    )
    doc.add_paragraph(
        "Both markdown files in this folder were corrected: the §6.0 enum "
        "block, the §6.1 DDL and its surrounding prose, the §10.1 retention "
        "table, the §10.2 erasure procedure rewritten against the actual code "
        "path, the §6.11 merge description, and the redline’s parenthetical and "
        "V13 entry."
    )

    doc.add_heading("6.1 The erasure procedure, as actually executed", 2)
    doc.add_paragraph(
        "§10.2 previously specified anonymising the learner’s own identity "
        "columns at request time. The code deliberately does not, and one of "
        "the steps could not execute at all: email is NOT NULL, so there is no "
        "nulling it in place. The built design keeps the identity for the "
        "dispute window precisely so an accidental erasure can be reversed, and "
        "frees the address immediately by other means — the unique index on "
        "email is partial on deleted_at IS NULL, so a soft-deleted row drops "
        "out of it. On instruction, the spec was aligned to the code. §10.2 now "
        "documents the real sequence, including the ordering constraint that "
        "the response redaction must run before the attempt owner is nulled, "
        "because it finds its rows through that column."
    )

    # --- 7 -----------------------------------------------------------------
    doc.add_heading("7. Two requirements kept, not deleted", 1)
    doc.add_paragraph(
        "“Align the spec to the build” is the right instruction for drift, and "
        "the wrong one for scope. Two items in §10.2 are not descriptions of a "
        "built mechanism that drifted — they are requirements with no built "
        "equivalent at all. Deleting them to make the document true would have "
        "discarded work nobody had decided against. Both are now marked in "
        "§10.2 as unbuilt and tracked in the redline’s open items."
    )
    table(
        doc,
        ["Requirement", "Current behaviour", "Consequence"],
        [
            [
                "Credential retention exception",
                "No is_credential column; portfolio_items cascades on erasure",
                "A hard delete destroys the credentials the spec says must stay "
                "verifiable",
            ],
            [
                "Erasure purge audit row",
                "purge_expired_soft_deletes deletes and returns a count, writing "
                "no retention_actions row",
                "The audit log records that erasure was requested; nothing records "
                "that it completed",
            ],
        ],
    )
    doc.add_paragraph(
        "The second is the sharper one. Every ordinary retention policy writes "
        "an audit row, and §12.2 asks for a trail answering “why did that data "
        "go away”. The one deletion carrying a statutory deadline is the only "
        "one that leaves no trace. It is a few lines inside the erasure_purge "
        "stage to close, and it was left alone only because it is a behaviour "
        "change rather than the documentation reconciliation that was asked for.",
        style="Intense Quote",
    )

    # --- 8 -----------------------------------------------------------------
    doc.add_heading("8. What was run", 1)
    table(
        doc,
        ["Suite", "Result"],
        [
            ["tests/ops (offline, Tier 1)", "all passed"],
            ["tests/online/test_ops_db.py (Tier 2, Postgres)", "31 passed"],
            ["tests/integrity/test_erasure.py", "passed"],
            ["Combined final run", "348 passed"],
        ],
    )
    mono(
        doc,
        "Code changes are confined to two test files. ",
        "git diff",
        " over backend/ shows tests/online/test_ops_db.py and "
        "tests/ops/test_retention_worker.py and nothing else; the mutation used "
        "to prove the guard was reverted.",
    )

    # --- 9 -----------------------------------------------------------------
    doc.add_heading("9. Open", 1)
    bullets(
        doc,
        [
            "The .docx twins of the v1.2 spec and the redline are stale relative "
            "to the corrected markdown. They are generated from it by pandoc — "
            "the giveaway is the KeywordTok and StringTok character styles, which "
            "pandoc emits only when writing docx from fenced code — and pandoc is "
            "not installed on this machine. They were deliberately not "
            "hand-patched: editing generated output is overwritten by the next "
            "build and hides the divergence.",
            "The credential retention exception and the missing erasure audit row "
            "(§7 above) are recorded, not fixed.",
            "The v1.1 changelog entry still credits that revision with adding "
            "deleted_users_aggregate. Left as a historical record of what the "
            "revision claimed rather than rewriting past entries.",
        ],
    )

    doc.add_paragraph()
    closing = doc.add_paragraph(
        "The schedulers were fine. What was not fine was everything standing "
        "guard over them: a regression test that could not fail, a decay stage "
        "asserted against an empty table, and a specification describing "
        "columns the erasure code has never used. The three orphaned jobs were "
        "found because someone went looking for callers; these were found "
        "because the fix was checked rather than assumed."
    )
    closing.style = "Intense Quote"

    doc.core_properties.title = (
        "Scheduler Verification and Data Layer Spec Reconciliation"
    )
    doc.core_properties.subject = (
        "Subsystems 7 and 1, against data layer specification v1.2"
    )
    doc.core_properties.author = "Studium build"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    return OUT


if __name__ == "__main__":
    print(f"wrote {build()}")
