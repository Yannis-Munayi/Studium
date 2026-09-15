"""Generate the Phase A work-order report as .docx.

Written as a script rather than by hand so the numbers in it come from one
place and can be regenerated. Run from the backend directory:

    python scripts/build_work_order_report.py
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
    / "work-order-2026-09-04-phase-a.docx"
)


def bullets(doc: Document, items: list[str]) -> None:
    for item in items:
        doc.add_paragraph(item, style="List Bullet")


def numbered(doc: Document, items: list[str]) -> None:
    for item in items:
        doc.add_paragraph(item, style="List Number")


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


def build() -> Path:  # noqa: PLR0915 -- a document, written top to bottom
    doc = Document()
    doc.styles["Normal"].font.size = Pt(10.5)

    doc.add_heading("Developer Work Order: Phase A", 0)
    sub = doc.add_paragraph(
        "Tasks 1–3 of 9  •  Amendment v1.2.1 §3.1 and §3.2, SPEC_DEBT SD10  •  "
        "Work order of 4 September 2026  •  Reported 9 September 2026"
    )
    sub.alignment = WD_ALIGN_PARAGRAPH.LEFT

    doc.add_paragraph(
        "Phase A is complete and verified against a live database. Phase B and "
        "Phase C are not started, and the reason is that the inputs they need "
        "are not on this machine — not a shortage of time. The three tasks "
        "that were buildable were all buildable, and each turned out to have "
        "something underneath it: the credential-retention fix the amendment "
        "recommends does not work as written, because portfolio_items has a "
        "second cascade the amendment does not mention; the migration as first "
        "drafted would have failed the first production deploy, in a way only "
        "one test in the project can see; and the operational calendar's "
        "central claim — that a completion is a two-line diff — was false on "
        "Windows until the newline handling was fixed.",
        style="Intense Quote",
    )

    # --- 1 -----------------------------------------------------------------
    doc.add_heading("1. What “verified” means in this report", 1)
    doc.add_paragraph(
        "Verified means the behaviour was exercised against a real Postgres 16 "
        "— the pinned pgvector/pgvector:pg16 image from "
        "backend/docker-compose.test.yml, on port 5433 — and observed to be "
        "correct. Every count in section 7 comes from a run made while writing "
        "this report."
    )
    doc.add_paragraph(
        "It means more than “the tests pass” for the three new guards, because "
        "each was also shown to fail. Section 8 records what was broken and "
        "what went red. A test that passes against a fix and also passes "
        "against its absence is not evidence of anything, and this project has "
        "already been bitten once by exactly that (SPEC_DEBT, the regression "
        "guard that searched a docstring)."
    )
    doc.add_paragraph("It does not mean, and this report does not claim:")
    bullets(
        doc,
        [
            "That anything is deployed. Nothing is. Migration 0013 has been "
            "applied to a throwaway test database and to a scratch migration "
            "database, never to production, which does not exist.",
            "That an erasure has been run against real learner data. Every "
            "erasure exercised here ran against fixture data inside a "
            "transaction that was rolled back.",
            "That a credential has been verified by an external party. §12.4’s "
            "endpoint was exercised in-process, through credentials.verify_item, "
            "against a signing key minted for the test.",
            "That the operational calendar reminds anyone of anything. It "
            "reports when asked and exits non-zero when something is overdue. "
            "Nothing runs it on a schedule yet — see section 5.4.",
            "That the schema round-trips. test_migration_round_trip is skipped "
            "on this machine because pg_dump is not on PATH. The downgrade was "
            "exercised by two other tests instead; the pg_dump schema-identity "
            "check was not run.",
        ],
    )

    # --- 2 -----------------------------------------------------------------
    doc.add_heading("2. Scope: three of nine", 1)
    doc.add_paragraph(
        "The work order lists nine tasks in three phases. Phase A was "
        "deliverable in full. Phase B and Phase C were not, and section 9 says "
        "precisely what each is waiting on rather than leaving it as “needs "
        "Yannis”."
    )
    table(
        doc,
        ["#", "Task", "Phase", "Status"],
        [
            ["1", "Credential retention (migration 0013)", "A", "Done, verified"],
            ["2", "Erasure purge audit row", "A", "Done, verified"],
            ["3", "studium ops calendar", "A", "Done, verified"],
            [
                "4",
                "Michaelson source classification",
                "B",
                "Blocked — no such source row exists anywhere",
            ],
            [
                "5",
                "Sync lambda calculus content",
                "B",
                "Blocked — content package not present",
            ],
            [
                "6",
                "Activate subject and assessments",
                "B",
                "Blocked — nothing to activate until 5",
            ],
            ["7", "First deployment to Fly.io", "C", "Blocked — account, token"],
            ["8", "Account-side observability", "C", "Blocked — three accounts"],
            ["9", "First restore drill", "C", "Blocked — depends on 7"],
        ],
    )
    doc.add_paragraph(
        "Nothing is committed. The work sits in the working tree on branch "
        "yannis: 15 files modified (1,219 insertions, 23 deletions) and 6 files "
        "added (1,543 lines). Two files that appear modified in git status — "
        "tests/online/test_ops_db.py and tests/ops/test_retention_worker.py — "
        "were already modified before this work began and were not touched."
    )

    # --- 3 -----------------------------------------------------------------
    doc.add_heading("3. Task 1 — credentials outlive their learner", 1)

    doc.add_heading("3.1 The defect, restated from the schema", 2)
    doc.add_paragraph(
        "Amendment v1.2.1 §3.1 requires a credential to survive the erasure of "
        "the learner who earned it, because the entire point of one is that a "
        "third party can verify it long after issuance. The schema did the "
        "opposite, and it did so twice over:"
    )
    code_line(doc, "fk_portfolio_items_user_id   (user_id) → users  ON DELETE CASCADE")
    code_line(
        doc,
        "fk_portfolio_items_enrollment (learner_subject_id, user_id)",
    )
    code_line(doc, "                              → learner_subjects  ON DELETE CASCADE")
    doc.add_paragraph(
        "§10 step 3 deletes the enrollment outright and §10 step 4 deletes the "
        "users row thirty days later. Either cascade is sufficient to destroy "
        "the credential. The first learner to earn one and then exercise "
        "right-to-erasure would have lost it permanently, and an external "
        "verifier holding a copy would find that §12.4’s public endpoint no "
        "longer resolves the id. Nothing would have reported this: the erasure "
        "succeeds, the request is honoured, and the loss is silent."
    )

    doc.add_heading("3.2 The recommended fix does not work on this schema", 2)
    doc.add_paragraph(
        "§3.1 offers two implementations and recommends Option B — reassign "
        "user_id to a reserved anonymised id — over Option A, on the grounds "
        "that Option A requires making the owner column nullable and “weakens "
        "the schema”. That reasoning is sound and the recommendation is right, "
        "but Option B on its own is not sufficient here, and the reason is the "
        "composite key above, which the amendment does not mention."
    )
    doc.add_paragraph(
        "Reassigning user_id leaves the row pointing at a learner_subjects row "
        "that step 3 is about to delete. The cascade fires on that key and "
        "takes the credential with it — the exact loss §3.1 exists to prevent, "
        "achieved with the recommended fix correctly applied. A build that "
        "followed the amendment literally would have shipped a credential "
        "retention feature that retains nothing, and the test proving it works "
        "would have to be written badly to pass."
    )
    doc.add_paragraph(
        "The amendment also names the column learner_id. It is user_id, and "
        "has been since migration 0001."
    )

    doc.add_heading("3.3 What was built", 2)
    numbered(
        doc,
        [
            "is_credential BOOLEAN NOT NULL DEFAULT FALSE on portfolio_items, "
            "backfilled from kind, with a CHECK constraint tying the two "
            "together permanently.",
            "learner_subject_id made nullable. This is the concession, and it "
            "is unavoidable rather than chosen: the enrollment is learner-owned "
            "data §10 purges, and a retained credential cannot keep pointing at "
            "it under any implementation. Nulling that column is also what "
            "satisfies the composite key, which is MATCH SIMPLE — a row with "
            "any NULL in the key needs no referent — so the foreign key itself "
            "does not change and still cascades every item erasure has not "
            "detached first.",
            "A reserved anonymised-learner account at "
            "00000000-0000-7000-8000-000000000002, in the same block as "
            "migration 0008’s system user. user_id stays NOT NULL, so the "
            "amendment’s stated reason for preferring Option B holds.",
            "erase_user gains a step 3, before the enrollment delete, that "
            "reassigns and detaches in one UPDATE and writes a "
            "retain_credentials row into audit_log when it moved anything.",
            "erase_user refuses both reserved accounts. Erasing …0002 would "
            "cascade away every credential every erased learner ever earned — "
            "the whole of §3.1’s protection, undone by one command.",
        ],
    )

    doc.add_heading("3.4 Why the flag exists at all, given that kind already says", 2)
    doc.add_paragraph(
        "The amendment asks for is_credential set “where item_type IN "
        "('assessment_pass', 'signed_credential')”. There is no item_type and "
        "no signed_credential. The column is kind, and evaluation §12.1’s two "
        "credential kinds — added to the enum by migration 0011 — are "
        "assessment_pass and subject_completion. credentials.verify_item "
        "already filters on exactly those."
    )
    doc.add_paragraph(
        "So the flag carries no information the row did not already have, and "
        "a redundant flag that can disagree with the enum is worse than no "
        "flag: it decides whether a row survives an irreversible deletion. It "
        "was built anyway, with the CHECK that makes the redundancy safe:"
    )
    code_line(
        doc,
        "CHECK (is_credential = (kind::text IN ('assessment_pass',",
    )
    code_line(doc, "                                       'subject_completion')))")
    doc.add_paragraph(
        "The gain is that erasure selects surviving rows with “AND "
        "is_credential”, which cannot be misread at 02:00 the way an enum "
        "membership test can, in the one procedure where getting it wrong is "
        "unrecoverable. The cost is that adding a third credential kind now "
        "requires a migration updating both, which is the correct amount of "
        "friction. The definition lives in one place — "
        "studium.models.portfolio.CREDENTIAL_KINDS — and "
        "studium.eval.credentials re-exports it rather than restating it."
    )

    doc.add_heading("3.5 What erasure deliberately does not preserve", 2)
    doc.add_paragraph(
        "The hash chain around the credential goes with the learner. The "
        "neighbouring items are their proofs and essays, and retaining them to "
        "keep a chain intact would retain precisely what §10 exists to remove. "
        "The retained credential’s signature.manifest.prev_sha256 therefore "
        "names a row that no longer exists."
    )
    doc.add_paragraph(
        "Verification is unaffected, and this is the load-bearing detail: "
        "§12.3 signs the credential payload in its own right "
        "(signature.credential_signature) and §12.4’s endpoint checks that "
        "signature against signing_keys, never the chain. The chain is "
        "tamper-evidence for a portfolio; the signature is the credential. A "
        "test asserts the credential still verifies after its learner is "
        "erased, rather than only that the row is still there — the row "
        "surviving is not the requirement."
    )
    doc.add_paragraph(
        "One limit worth stating plainly rather than burying: the signed "
        "payload contains learner_id, and it cannot be edited without breaking "
        "the signature that makes it verifiable. A retained credential is "
        "therefore a durable record that a particular user id passed a "
        "particular assessment, outliving the erasure of that account. That is "
        "inherent to what §3.1 asks for, not an implementation choice, and it "
        "is asserted by a test so nobody discovers it later by accident."
    )

    # --- 4 -----------------------------------------------------------------
    doc.add_heading("4. Task 2 — the erasure purge leaves a trail", 1)
    doc.add_paragraph(
        "Amendment §3.2: every ordinary retention policy writes a "
        "retention_actions row, and the one deletion carrying a statutory "
        "deadline wrote none. “Did the erasure request from user X on date Y "
        "complete by Y+30” was the single retention question the schema could "
        "not answer — every other disposal provably logged, and this one "
        "provably requested and unaccountably completed-or-not."
    )
    doc.add_paragraph(
        "The amendment gives the insert as policy_name / rows_affected / "
        "metadata / executed_at. None of those four columns exist. "
        "Infrastructure §12.2 wrote its DDL out in full and migration 0012 "
        "reproduced it verbatim, so the table is:"
    )
    code_line(doc, "retention_actions (id, ran_at, table_name, rows_deleted,")
    code_line(doc, "                   duration_ms, metadata)")
    doc.add_paragraph(
        "The row is written as table_name='users', rows_deleted=1, and "
        "metadata->>'policy_name' = 'erasure_purge' — the same convention "
        "ops.retention._record uses for the nightly worker, so one query reads "
        "both kinds of row and `studium ops retention log` shows them together."
    )
    doc.add_paragraph("Two departures from §3.2 beyond the column names, both deliberate:")
    bullets(
        doc,
        [
            "One row per account, not per pass. The question §3.2 exists for is "
            "asked about a person and a date; a single row saying “2” answers it "
            "for neither of them. A pass that purges nothing writes nothing — "
            "unlike the nightly worker’s per-policy zeros, where the zero is the "
            "evidence the worker ran. Here the worker’s own stage result is that "
            "evidence, and a zero row would assert a disposal that did not happen.",
            "Written in the same transaction as the delete. "
            "ops.retention._record deliberately swallows its own failures, "
            "because losing one line of the trail beats losing ten committed "
            "deletes and the nine policies still to run. That trade goes the "
            "other way when the deletion is the one with a regulator attached.",
        ],
    )
    doc.add_paragraph(
        "metadata.user_id_sha256 is a hash, per §3.2 — the identifier is the "
        "thing being disposed of. The plain id does survive in audit_log’s "
        "erase_user row, which §10 keeps deliberately as the accountability "
        "trail; metadata.audit_log_ref is the link to it. That reference is "
        "resolved before the users row is deleted, because the foreign key is "
        "ON DELETE SET NULL and afterwards the request no longer says whose it "
        "was. A test asserts the link, because getting the ordering wrong "
        "produces a row that looks complete and references nothing."
    )

    # --- 5 -----------------------------------------------------------------
    doc.add_heading("5. Task 3 — the operational calendar", 1)

    doc.add_heading("5.1 Why a file, and not the table SD10 imagined", 2)
    doc.add_paragraph(
        "SD10 offered three resolutions. This is the second — a `studium ops "
        "calendar` command — narrowed. The entry imagined reading each task’s "
        "last occurrence out of the database and noted that two obligations "
        "have no durable record anywhere, “which is a small table nobody has "
        "specified”. No table was specified. The record is a YAML file in the "
        "repository, and that choice is the design:"
    )
    bullets(
        doc,
        [
            "It survives the operator. A reminder in someone’s calendar leaves "
            "with them; a file in the repo is inherited by whoever clones it "
            "next, along with the runbook each row points at.",
            "It is reviewable. --complete changes two lines, so "
            "`git log -p content/operational-calendar.yml` is the audit trail: "
            "who said the drill was done, when, and in which commit. A table "
            "would need an actor column and a reason column to say as much.",
            "It cannot silently disagree with the deployment, because it is "
            "deployed with it. The Dockerfile’s COPY puts it in the image, so "
            "`fly ssh console` can answer “what is due”.",
        ],
    )

    doc.add_heading("5.2 The command", 2)
    code_line(doc, "studium ops calendar --list-all")
    code_line(doc, "studium ops calendar --due-this-week | --due-this-month")
    code_line(doc, "studium ops calendar --on-event on_source_add")
    code_line(doc, "studium ops calendar --complete <name>")
    code_line(doc, "studium ops calendar --add | --verify")
    doc.add_paragraph(
        "Two additions beyond the work order’s specification, both small and "
        "both arguable, so both are named here rather than left in the code:"
    )
    bullets(
        doc,
        [
            "The date queries exit non-zero when something is overdue — not "
            "merely due. “Exit codes are the interface” is already this CLI’s "
            "stated convention, and it means the last step SD10 is missing is a "
            "cron line rather than more code. A listing exits 0 whatever it "
            "contains: it was asked what exists, and it answered.",
            "--verify checks that every runbook still resolves to a file, not "
            "just that the schema is valid. A calendar of dead links is SD10’s "
            "failure wearing a different hat: the reminder fires, the operator "
            "opens the runbook, and there is nothing there. The check skips "
            "itself when docs/ is absent, which is the deployed image rather "
            "than a broken calendar. Two runbooks did not exist and were "
            "written: docs/ops/content-review.md and "
            "docs/ops/evaluation-regression.md.",
        ],
    )

    doc.add_heading("5.3 One correction to the work order’s table", 2)
    doc.add_paragraph(
        "The work order gives migration_downgrade_signoff the cadence "
        "on_source_add, which is the trigger for the entry above it in the same "
        "table. A sign-off on a schema downgrade has nothing to do with adding "
        "a source, and an obligation nothing can trigger is one nobody will "
        "ever perform — it would have sat in the file, inherited by every "
        "future operator, saying something false. It is "
        "on_migration_downgrade, and the reason is a comment in the file rather "
        "than a silent change."
    )
    doc.add_paragraph(
        "Everything else is as specified: nine obligations, the owners and "
        "cadences from the work order’s table, next_due dates anchored so the "
        "first backup drill falls weeks out rather than a quarter out (§9.3’s "
        "own argument is that the first drill is the one most likely to find "
        "something)."
    )

    doc.add_heading("5.4 SD10 is resolved in mechanism, not closed", 2)
    doc.add_paragraph(
        "Two things keep the entry open, and both are recorded in SPEC_DEBT "
        "rather than papered over."
    )
    doc.add_paragraph(
        "First, nothing runs the command. The exit code makes a cron line "
        "sufficient, and no cron line exists. Until one does, this is a report "
        "you have to remember to ask for — better than memory, short of an "
        "alert."
    )
    doc.add_paragraph(
        "Second, the work order’s nine are not SD10’s nine. The work order adds "
        "the credential audit from amendment v1.2.1 and the review-queue triage "
        "from ingestion §11.2, and drops three:"
    )
    table(
        doc,
        ["Missing obligation", "Why it matters"],
        [
            [
                "Monthly invoice reconciliation (§13.2)",
                "One of the two SD10 named as having no durable record at all",
            ],
            ["Quarterly alert-accuracy review (§16 Tier 3)", "The other one"],
            [
                "Destroy the retired private key, +90 days after a rotation "
                "(§11.2 step 6)",
                "SD10 calls this the single most forgettable item on the list, "
                "and the one whose omission leaves a usable signing key in a "
                "password manager indefinitely",
            ],
        ],
    )
    doc.add_paragraph(
        "The third is the one to add first, and it does not fit. It is not a "
        "recurring cadence — it is a one-off follow-up scheduled by an event a "
        "year earlier, which the vocabulary (daily…annually, plus named event "
        "cadences) cannot express. Either the vocabulary grows a “+N days after "
        "event X” form, or the 90-day destruction becomes a step of the "
        "rotation runbook so it is never a separate thing to remember. The "
        "second is smaller and probably right. Reconciling the two lists is a "
        "decision, not a build task, which is why the file ships with nine and "
        "SPEC_DEBT names the gap."
    )

    # --- 6 -----------------------------------------------------------------
    doc.add_heading("6. Three defects found while building", 1)

    doc.add_heading(
        "6.1 The migration would have failed the first production deploy", 2
    )
    doc.add_paragraph(
        "Migration 0013 as first drafted wrote its CHECK constraint with enum "
        "literals: kind IN ('assessment_pass', 'subject_completion'). Postgres "
        "refuses to use a new enum value in the same transaction that added it, "
        "migration 0011 adds both values, and env.py wraps `upgrade head` in "
        "one transaction. So:"
    )
    code_line(doc, "ERROR: unsafe use of new value \"assessment_pass\"")
    code_line(doc, "       of enum type portfolio_item_kind")
    code_line(doc, "HINT:  New enum values must be committed before they can be used.")
    doc.add_paragraph(
        "This is invisible during development. Applying 0013 to an "
        "already-migrated database succeeds every time, because 0011 committed "
        "months ago — which is what every local run, and every incremental "
        "deploy, does. It fails only when migrating a fresh database from base "
        "in one transaction, which is exactly what work order Task 7 step 4 "
        "describes: “applies migrations 0001-0013 as part of app startup”. The "
        "first production deploy would have aborted in the release command."
    )
    doc.add_paragraph(
        "One test in the project does that: test_full_migration_sequence. It "
        "caught it on the first run after the migration was written. The fix "
        "is to compare kind::text, which references no enum value at all and is "
        "the idiom credentials.verify_item already uses."
    )

    doc.add_heading("6.2 A missed backfill fails loudly, and this was checked", 2)
    doc.add_paragraph(
        "Every other migration test migrates to head before any row exists, so "
        "0013’s backfill UPDATE never touches anything and the CHECK is "
        "validated against an empty table. On a real deployment the backfill "
        "runs over whatever is already in portfolio_items, and a backfill that "
        "missed would leave existing credentials flagged FALSE — which is to "
        "say, deleted by the first erasure that reached them."
    )
    doc.add_paragraph(
        "A test now seeds three portfolio items at 0012 — one proof, one "
        "assessment_pass, one subject_completion — and migrates to 0013. "
        "Disabling the backfill does not produce a wrong flag; it aborts the "
        "migration, because ALTER TABLE ADD CONSTRAINT validates existing rows:"
    )
    code_line(
        doc,
        "CheckViolation: check constraint",
    )
    code_line(doc, "  \"ck_portfolio_items_is_credential_matches_kind\"")
    code_line(doc, "  of relation \"portfolio_items\" is violated by some row")
    doc.add_paragraph(
        "That is the stronger property and it was not designed in — it fell out "
        "of the CHECK, and was only established by breaking the backfill on "
        "purpose to see what happened. A first reading of the test output "
        "suggested the migration had succeeded with mis-flagged rows; it had "
        "not, and the difference matters enough to have been worth chasing."
    )

    doc.add_heading("6.3 The calendar’s central claim was false on Windows", 2)
    doc.add_paragraph(
        "The whole argument for a YAML file over a reminder service is that a "
        "completion is a readable diff. Path.read_text and Path.write_text "
        "translate newlines by default: read gives \\n on any platform, and "
        "write gives \\r\\n on Windows. Round-tripping the LF calendar through "
        "them rewrote all 125 lines."
    )
    doc.add_paragraph(
        "The first --complete run produced a 125-line diff for a two-value "
        "change. Reading and writing with newline=\"\" makes the round trip a "
        "no-op, and _set_field now captures each line’s own terminator rather "
        "than assuming one. Two tests pin it: one asserts exactly two lines "
        "differ and the file length is unchanged, one runs the same completion "
        "against LF and CRLF copies and asserts the endings survive."
    )
    doc.add_paragraph(
        "A related defect was found by the tests rather than by inspection: "
        "_entry_span could only locate an entry written as “- name: x”, and "
        "refused one written with the dash on its own line. Both are valid YAML "
        "and this file is edited by hand, so it now handles either, and matches "
        "the next sequence item at the same indentation so a markdown bullet "
        "inside a folded description cannot end the span early."
    )

    # --- 7 -----------------------------------------------------------------
    doc.add_heading("7. What was run", 1)
    doc.add_paragraph(
        "Against pgvector/pgvector:pg16 on port 5433, schema at head (0013), "
        "with STUDIUM_TEST_DATABASE_URL and "
        "STUDIUM_MIGRATION_TEST_DATABASE_URL both set. The paid tiers "
        "(-m anthropic) were deselected throughout and spent nothing."
    )
    table(
        doc,
        ["Run", "Before", "After"],
        [
            [
                "Tier 1 (-m \"not postgres\")",
                "2031 passed, 6 skipped",
                "included in the combined run below",
            ],
            ["Tier 2 (-m postgres)", "263 passed, 16 skipped", "—"],
            [
                "Combined (-m \"not anthropic\")",
                "≈2294 equivalent",
                "2367 passed, 1 skipped",
            ],
            ["Migration sequence", "4 passed, 1 skipped", "6 passed, 1 skipped"],
            ["tests/ops/test_calendar.py", "did not exist", "52 passed"],
            ["tests/integrity/test_erasure.py", "12 passed", "24 passed"],
            ["ruff check studium tests scripts", "clean", "clean"],
        ],
    )
    doc.add_paragraph(
        "The one skip is test_migration_round_trip: pg_dump is not on PATH on "
        "this machine, and the test skips rather than failing. It is "
        "pre-existing and unrelated to this work, but it means the "
        "schema-identity half of the downgrade check did not run. The downgrade "
        "was exercised by test_downgrade_to_base_leaves_no_tables and by the "
        "new refusal test instead."
    )
    doc.add_paragraph(
        "Migration 0013 was applied three ways: incrementally onto an existing "
        "0012 database, from base in a single transaction (the deploy path, "
        "§6.1), and onto a database with rows in portfolio_items (§6.2). The "
        "downgrade was run to base on an empty schema and refused, as designed, "
        "on a schema holding a detached credential."
    )

    # --- 8 -----------------------------------------------------------------
    doc.add_heading("8. Each new guard was shown failing", 1)
    doc.add_paragraph(
        "SPEC_DEBT already records a guard in this project that could not fail: "
        "the regression test protecting three orphaned schedulers searched the "
        "module’s source text for their names, and the docstring named all "
        "three. Deleting the erasure stage left it green. Every guard added "
        "here was therefore mutated and watched to go red before being trusted."
    )
    table(
        doc,
        ["Mutation", "Result"],
        [
            [
                "erase_user’s detach UPDATE matches nothing "
                "(AND is_credential → AND FALSE)",
                "5 failures, including “the credential was cascaded away with "
                "the enrollment” and the still-verifies test",
            ],
            [
                "purge_expired_soft_deletes writes no retention_actions rows "
                "(for row in due → for row in [])",
                "4 failures; the negative-control test — nothing written before "
                "the window elapses — correctly stayed green",
            ],
            [
                "Migration 0013’s backfill matches nothing",
                "The migration itself aborts with a CheckViolation (§6.2)",
            ],
        ],
    )

    # --- 9 -----------------------------------------------------------------
    doc.add_heading("9. What blocks Phase B and Phase C", 1)
    doc.add_paragraph(
        "Stated at the level of the specific missing artifact, because “needs "
        "Yannis” is not actionable and two of these are not what the work order "
        "expected."
    )

    doc.add_heading("9.1 Phase B", 2)
    table(
        doc,
        ["Task", "What is actually missing"],
        [
            [
                "4 — Michaelson classification",
                "There is no Michaelson source row in any database. The dev "
                "database holds one source, the Church paper, already "
                "public_domain. The source has never been ingested, so there is "
                "nothing to reclassify — this is a prerequisite the work order "
                "does not list. The permission terms are also not recorded "
                "anywhere in the repository.",
            ],
            [
                "5 — Content sync",
                "The package at /mnt/user-data/outputs/ is not on this machine. "
                "The repository holds one file of the 25, "
                "assessments/foundations.yaml, whose rubric_criterion_id values "
                "are documented placeholders. Separately, `studium content sync` "
                "does not exist: the commands are `studium ingest graph`, "
                "`ingest rubrics` and `ingest sources`.",
            ],
            [
                "6 — Activation",
                "`studium subject activate` and `studium assessment activate` do "
                "not exist. `studium publish subject <slug>` is the equivalent, "
                "and there is no per-assessment activation command. Nothing to "
                "activate until Task 5 lands regardless.",
            ],
        ],
    )

    doc.add_heading("9.2 Phase C", 2)
    doc.add_paragraph(
        "Tasks 7, 8 and 9 need a Fly.io account and deploy token, and Sentry, "
        "Langfuse and Uptime Robot accounts. Creating third-party accounts and "
        "deploying to production are not things to do on someone’s behalf "
        "without being asked directly, and §6.2 deliberately scopes the deploy "
        "token to authorised workstations. Task 9 additionally depends on a "
        "production snapshot, which requires Task 7 plus roughly 24 hours."
    )
    doc.add_paragraph(
        "One Phase C hazard was removed rather than merely noted: §6.1’s enum "
        "defect would have aborted the first deploy’s release command. Task 7 "
        "step 4 is now the path the migration-sequence test exercises on every "
        "CI run."
    )

    # --- 10 ----------------------------------------------------------------
    doc.add_heading("10. Acceptance criteria, item by item", 1)
    table(
        doc,
        ["Criterion", "Met", "Note"],
        [
            ["0013 applies cleanly (up)", "Yes", "Three ways; §7"],
            [
                "0013 reverses cleanly (down)",
                "Qualified",
                "Reverses on an empty schema. With a detached credential it "
                "refuses, by design: there is no enrollment left to re-attach "
                "to, so the deletion is a §12.4 sign-off decision rather than "
                "the migration’s to make.",
            ],
            [
                "Credentialed items survive with the anonymised owner",
                "Yes",
                "Plus learner_subject_id NULL, which Option B alone does not "
                "achieve",
            ],
            ["Non-credential items are gone", "Yes", "Same test, one erasure"],
            ["Tier 2 test verifies both paths", "Yes", "And both mutate to red"],
            [
                "v1.2.1 §3.1 open item marked closed",
                "No",
                "The amendment document is not in this repository. The code is "
                "done; the edit needs the document.",
            ],
            [
                "retention_actions row after the window elapses",
                "Yes",
                "Column names differ from §3.2’s pseudocode — see §4",
            ],
            ["No row before the window elapses", "Yes", "Negative control"],
            ["Tier 2 test verifies both timings", "Yes", ""],
            [
                "v1.2.1 §3.2 open item marked closed",
                "No",
                "Same reason as §3.1",
            ],
            ["--list-all returns the nine obligations", "Yes", ""],
            ["--due-this-week filters to +7 days", "Yes", "Overdue included"],
            ["--complete updates the YAML correctly", "Yes", "Two-line diff; §6.3"],
            [
                "--verify catches malformed entries",
                "Yes",
                "Missing fields, bad cadence, bad date, empty strings, bad name, "
                "duplicates, and event cadences carrying a date",
            ],
            [
                "Tier 1 test covers the four command paths",
                "Yes",
                "52 tests, no database",
            ],
            [
                "SD10 marked resolved",
                "Qualified",
                "Resolved in mechanism. Three obligations still absent and "
                "nothing runs the command on a schedule — §5.4",
            ],
        ],
    )

    # --- 11 ----------------------------------------------------------------
    doc.add_heading("11. Open", 1)
    bullets(
        doc,
        [
            "Nothing is committed. The work is in the working tree on branch "
            "yannis.",
            "The v1.2.1 amendment is not in this repository, so its §3.1 and "
            "§3.2 open items could not be marked closed. The divergences are "
            "recorded as V15, V16 and V17 in backend/DIVERGENCES.md instead.",
            "Three of SD10’s obligations are still not in the calendar, and one "
            "of them — destroying the retired private key 90 days after a "
            "rotation — needs a vocabulary decision or a runbook change before "
            "it can be added.",
            "Nothing runs `studium ops calendar --due-this-week` on a schedule. "
            "The exit code is ready for it.",
            "test_migration_round_trip is skipped on this machine (pg_dump not "
            "on PATH), so the pg_dump schema-identity check for 0013’s "
            "downgrade has not been run anywhere.",
            "The retained credential’s signed payload names the learner id "
            "permanently. Inherent to §3.1, asserted by a test, and worth a "
            "line in whatever privacy notice the deployment eventually carries.",
        ],
    )

    doc.add_paragraph()
    doc.add_paragraph(
        "Three tasks, and each had a second thing under it. The credential fix "
        "the amendment recommends would have destroyed credentials; the "
        "migration would have aborted the first production deploy; the "
        "calendar’s reason for existing was false on the platform it was "
        "written on. None of the three was found by reading the work order more "
        "carefully — they were found by applying the schema, running the "
        "deploy path, and diffing the file. What is verified here is what was "
        "executed, and section 1 says what that does not cover.",
        style="Intense Quote",
    )

    doc.core_properties.title = "Developer Work Order: Phase A"
    doc.core_properties.subject = (
        "Tasks 1-3 of the 4 September 2026 work order: credential retention, "
        "the erasure purge audit row, and the operational calendar"
    )
    doc.core_properties.author = "Studium build"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    return OUT


if __name__ == "__main__":
    print(f"wrote {build()}")
