"""Generate the subsystem 6 build report as a .docx.

Written as a script rather than by hand so the numbers in it come from one
place and can be regenerated. Run from the backend directory:

    python scripts/build_evaluation_report.py
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
    / "Sub System 6 - Assessment and evaluation harness"
    / "evaluation-build-report.docx"
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


def code(doc: Document, text: str) -> None:
    """A monospace run for identifiers and commands, per the report style."""
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.name = "Consolas"
    run.font.size = Pt(9.5)


def build() -> Path:
    doc = Document()
    doc.styles["Normal"].font.size = Pt(10.5)

    doc.core_properties.title = "Studium Evaluation and Assessment Harness — Build Report"
    doc.core_properties.author = "Yannis Munayi"
    doc.core_properties.subject = "Subsystem 6 of 7, specification v1.0"

    doc.add_heading("Studium Evaluation and Assessment Harness — Build Report", 0)
    sub = doc.add_paragraph(
        "Subsystem 6 of 7  •  Specification v1.0  •  Built 26 August 2026  •  "
        "Report 27 August 2026"
    )
    sub.alignment = WD_ALIGN_PARAGRAPH.LEFT

    doc.add_paragraph(
        "The evaluation harness is built and runs on all three tiers, including "
        "the paid one. Golden datasets in YAML, seven deterministic checks, the "
        "Evaluator's new meta-grading mode, per-agent thresholds that can "
        "actually block, retrieval quality measurement, the closed-book "
        "assessment flow, and signed credentials a stranger can verify without "
        "us. Six defects were found in code that already had passing tests — "
        "two of them in subsystems built weeks earlier, three in this build's "
        "own code, and every one of the six invisible to the tier below the one "
        "that caught it.",
        style="Intense Quote",
    )

    # --- 1 -----------------------------------------------------------------
    doc.add_heading("1. What “verified” means in this report", 1)
    doc.add_paragraph(
        "Three tiers, each establishing something the others cannot. The "
        "distinction carries more weight in this subsystem than in any prior "
        "one, because this subsystem's product is testing infrastructure — a "
        "harness that reports green while measuring nothing is the specific "
        "failure it exists to prevent, and it is a failure it can commit "
        "against itself."
    )
    bullets(
        doc,
        [
            "Tier 1 — 339 tests, offline, no database and no API key. The check "
            "registry and its coverage guard, dataset parsing and validation, "
            "§13.2's tolerance arithmetic on its boundary cases, §8's twenty-two "
            "metrics and ten blocking thresholds asserted on both sides of each "
            "line, §9's retrieval metrics, Ed25519 signing and canonicalisation, "
            "and §11.2's closed-book conditions.",
            "Tier 2 — 51 tests, real Postgres 16 with pgvector. Dataset sync and "
            "its idempotence, run persistence and aggregate arithmetic, the review "
            "queue with its audit trail, credential issue-and-verify end to end, "
            "and every constraint migration 0011 added asserted against the "
            "database rather than against ORM metadata.",
            "Tier 3 — 5 tests, real Anthropic calls, RUN AND PASSING. A real "
            "Lecturer regression run, the meta-grader round trip in both "
            "directions, and a real assessment graded by a real Evaluator into a "
            "real signed credential. 46.7 seconds, well under a dollar.",
            "Migration 0011 was applied, downgraded and re-applied against real "
            "Postgres, including the portfolio_item_kind enum rebuild that the "
            "downgrade needs because Postgres cannot drop an enum value.",
        ],
    )
    doc.add_paragraph(
        "So “verified” here means exercised against a real database and real "
        "models, end to end, including the credential path that is the "
        "product's actual deliverable. The whole backend suite stands at 2,030 "
        "offline tests passing."
    )

    doc.add_heading("What is not verified", 2)
    bullets(
        doc,
        [
            "Whether the agents are any good. This subsystem measures that; it does "
            "not assert it. The Tier 3 assertions are deliberately about the "
            "harness — that a run completed, produced one result per entry, and "
            "produced numbers in range. A test that failed because a prompt got "
            "worse would be a regression gate hiding inside a test suite, firing "
            "on the wrong signal at the wrong time.",
            "Meta-grader stability across runs (§19 open question 2). One round "
            "trip proves meta-grading works; six months of grading_calibration runs "
            "prove it is stable enough to regress against. The dataset kind exists "
            "and is empty.",
            "The blocking thresholds themselves (§19 open question 1). They are the "
            "spec's educated guesses, transcribed. They are in one place so they "
            "can be recalibrated when real runs give them something to be "
            "calibrated against.",
            "Retrieval quality against the real corpus. The retrieval_quality "
            "dataset has three hand-labelled entries and no embeddings behind them; "
            "answering §9.3's reranker A/B needs Voyage and a populated corpus.",
        ],
    )

    # --- 2 -----------------------------------------------------------------
    doc.add_heading("2. The headline finding: six defects in code with passing tests", 1)
    doc.add_paragraph(
        "This is the part of the build worth reading. Six defects, none of "
        "which any existing test could have caught, in three distinct "
        "categories. They are grouped by who wrote the broken code and which "
        "tier found it, because the pattern differs by group."
    )

    doc.add_heading("2.1 Two in subsystems built weeks earlier", 2)
    doc.add_paragraph(
        "Both were dead paths: a symbol present at every declaration site and "
        "absent from every execution site. Both had thorough-looking wiring. "
        "Neither had a caller."
    )
    table(
        doc,
        ["Defect", "What was actually wrong"],
        [
            [
                "E9 — every portfolio write raised",
                "orchestration.effects._record_portfolio_item never built the "
                "NOT NULL signature manifest data layer §6.10 specifies, so every "
                "record_portfolio_item effect raised NotNullViolation at insert. "
                "Worse than one failed effect: _apply_all rolls the whole batch "
                "back on a handler exception, so the turn lost its mastery "
                "evidence and journal update too. The learner does the work, the "
                "system records none of it, and it surfaces as a degraded turn "
                "with no obvious cause. The kind appeared in the effect dispatch "
                "table, the ToolEffect allow-list and the end chunk's id fields — "
                "and in no test.",
            ],
            [
                "E10 — a state a session could enter and never leave",
                "SUMMATIVE_ASSESSMENT was in State, in PERSISTED_MODE, in "
                "MODE_ENTRY_STATE and in budget_gate's per-mode multipliers — and "
                "in no transition row as a source. A session opened in that mode "
                "entered the state, and the only event that matched was the "
                "wildcard END_SESSION. A learner who submitted an answer got "
                "IllegalTransition. Four test tiers passed over it because no test "
                "ever opened a session in that mode.",
            ],
        ],
    )
    doc.add_paragraph(
        "E10 came with a compensating discovery: two of §11.2's six closed-book "
        "conditions were already enforced, by accident. No PrimitiveRule lists "
        "SUMMATIVE_ASSESSMENT in its valid_from, so the command palette raises "
        "before dispatch; and LEARNER_INTERRUPT has rows only from LECTURING and "
        "TUTORIAL, so “raise your hand” is unreachable. Both were true before "
        "this subsystem and neither was written down. They are asserted now, "
        "which is what stops a later widening of the primitive matrix from "
        "silently handing a learner the command palette mid-examination."
    )

    doc.add_heading("2.2 Three in this build's own code, found by Tier 3", 2)
    doc.add_paragraph(
        "All three sit in the gap between a fake agent and a real one. The "
        "third is the sharpest instance of that pattern encountered on this "
        "project so far."
    )
    table(
        doc,
        ["Defect", "Why no lower tier could see it"],
        [
            [
                "E14 — real calls are foreign-key bound to rows a fixture does not create",
                "Fixtures minted every id by uuid5 derivation — correct for Tier 1, "
                "wrong for anything real. Every billable call writes a trace; "
                "traces._write_sync writes a session_turns row first; that takes "
                "next_turn_index, which locks a learning_sessions row and requires "
                "it to exist, and the turn also carries a concept_id foreign key. "
                "The first real Lecturer call raised NoResultFound and the first "
                "real Evaluator call raised ForeignKeyViolation — both AFTER the "
                "model had been paid for.",
            ],
            [
                "E15 — a streaming agent's cost was recorded as zero",
                "_consume_stream summed cost from trace-kind chunks. No streaming "
                "agent emits one: §20's stream carries text, tool_effect, end and "
                "degraded, and the cost is known only after the stream closes. So "
                "every Lecturer and Tutor entry recorded $0, and a full regression "
                "would have written evaluation_runs.cost_usd = 0 and told §15.1's "
                "attribution that a fifteen-dollar run was free. Invisible below "
                "Tier 3 by construction: a fake agent's cost legitimately IS zero, "
                "so the bug satisfied every assertion.",
            ],
            [
                "E16 — the first paid run looked like a hang",
                "Fifteen minutes of no output. Three plausible causes were checked "
                "and eliminated — the API (0.7s), the model routing at effort high "
                "(2.8s), a dead default database URL (connection refused, which is "
                "fast). The real cause was E14, surfacing as a hang because pytest "
                "-q buffers and five tests each failing after a real model call is "
                "slow.",
            ],
        ],
    )
    doc.add_paragraph(
        "E15 is the one to carry forward. A fake whose correct behaviour happens "
        "to equal the buggy behaviour makes the test agree with the bug. When a "
        "fake returns a default — zero, empty, None — that is exactly where to "
        "check what the real thing returns, because that is where the two "
        "silently agree. And one process note that cost fifteen minutes: run a "
        "paid tier verbose from the start. A tier that costs money is the one "
        "where you least want to be guessing which of five tests you are in."
    )

    doc.add_heading("2.3 One in this report's own author: SD9 was wrong", 2)
    doc.add_paragraph(
        "This build filed SPEC_DEBT SD9, holding that a $5–15 regression run "
        "drew on the reviewer's $8 daily hard cap, so a scheduled run would lock "
        "them out of their own learning sessions. The subsystem 7 build measured "
        "it against the code and found the premise false: every trace of a live "
        "run books to the system account, which migration 0008 seeded with "
        "$1,000 daily caps."
    )
    doc.add_paragraph(
        "SD9's own “option 2” — attribute runs to the system user — was already "
        "the implementation. It arrived while fixing E14, because a run's traces "
        "need a session and the session had to belong to someone. The two were "
        "never connected. The entry was wrong for eleven days and nothing was at "
        "risk in the meantime."
    )
    doc.add_paragraph(
        "It is recorded rather than quietly deleted because the mistake is "
        "repeatable and the shape of it is instructive: SD9 reasoned from a "
        "spec's text about what the code does, instead of reading the code. "
        "Every other finding in this report went the other way — the code was "
        "read and the spec was found wanting — and that asymmetry is not a "
        "coincidence.",
        style="Intense Quote",
    )
    doc.add_paragraph(
        "The half of SD9 that was real is the half it stated least clearly: the "
        "caps were a number nobody read. budget_gate.pre_flight_check is called "
        "only from the Orchestrator's session path, and an evaluation run "
        "invokes agents directly, so no cap of any kind constrained a run. A "
        "human typing yes to a printed estimate was the only limit — precisely "
        "what a CI job removes. Subsystem 7 added runner.budget_preflight as a "
        "gate in front of both eval run and eval gate, called before the "
        "confirmation prompt because --yes is how CI drives that path."
    )

    # --- 3 -----------------------------------------------------------------
    doc.add_heading("3. What was built", 1)
    doc.add_paragraph(
        "About 11,900 lines: 7,001 across 13 source files in studium/eval, "
        "2,427 of Tier 1 tests, 1,015 of Tier 2, 477 of Tier 3, a 503-line "
        "migration, 461 lines of models, and a 487-line divergences record."
    )

    doc.add_heading("3.1 The modules", 2)
    table(
        doc,
        ["Module", "What it owns"],
        [
            ["datasets.py", "§7.2/§7.3: the authoring format, validation, and the citation ordering both the fixtures and the checks depend on"],
            ["checks.py", "§7.2's seven deterministic checks as a registry, each scored rather than boolean"],
            ["fixtures.py", "§3's seeded runtime context — derived ids, the fixture retriever, and the real session and concept rows a live run needs"],
            ["grading.py", "§4/§7.2: deterministic and meta-graded, and the Evaluator's new meta-grading mode"],
            ["runner.py", "§4/§16: run execution, per-entry failure isolation, persistence, and the §13.2 gate"],
            ["metrics.py", "§8's twenty-two metrics, ten blocking thresholds, and the coverage guard that makes them enforceable"],
            ["retrieval_eval.py", "§9: recall@k, precision@k, misleading rate, curated preference, the reranker A/B, the provider swap gate"],
            ["regression.py", "§13.2's tolerance arithmetic and §13.4's affected-dataset detection"],
            ["sync.py", "§7.3 step 5: YAML to database, idempotent, refusing deletions that would lose history"],
            ["review.py", "§10: the content review surface, the flag --to-dataset stub, §10.4's upstream escalation"],
            ["summative.py", "§11: the closed-book flow, nine classified capabilities, the retake policy"],
            ["credentials.py", "§12: Ed25519 signing, canonical JSON, key rotation, public verification"],
            ["cli.py", "§10.2/§13.1: studium eval and studium review"],
        ],
    )

    doc.add_heading("3.2 Schema: seven additions, not the four §5 names", 2)
    doc.add_paragraph(
        "Migration 0011 adds five tables and two enum values. §5 names four "
        "tables and says “All are non-breaking”; the other three are E1 and E4 "
        "in the divergences record."
    )
    table(
        doc,
        ["Addition", "Source"],
        [
            ["golden_datasets, golden_dataset_entries", "§5 additions 1 and 2"],
            ["evaluation_runs, evaluation_results", "§5 additions 3 and 4"],
            ["signing_keys", "Not in any spec. §12 says credentials are “backed by data layer §6.10's portfolio_items and signing_keys tables”. §6.10 defines only the first."],
            ["portfolio_item_kind += assessment_pass, subject_completion", "§12.1 credentials both. The existing six values are learner work and neither could be written."],
            ["golden_datasets.regression_tolerance", "§13.2 puts the tolerance in the YAML and §5 gives it no column, so a scheduled run had nothing to read."],
        ],
    )

    # --- 4 -----------------------------------------------------------------
    doc.add_heading("4. Decisions worth reading twice", 1)

    doc.add_heading("A gate whose input is absent must fail, not pass", 2)
    doc.add_paragraph(
        "§8 names metrics; §7.2 names properties; nothing in the spec joins "
        "them. So “citation validity < 100% blocks deploy” had no input. Left "
        "alone, a Lecturer dataset with no citations_resolve property makes "
        "compute skip the metric, violations find nothing to violate, and the "
        "gate report green — having measured nothing. That is strictly worse "
        "than having no threshold, because the green gets read as evidence that "
        "citations were checked. metrics.METRICS pins the join and "
        "coverage_gaps reports the holes; the CI job fails on one."
    )

    doc.add_heading("An ordering decided twice is an ordering decided by luck", 2)
    doc.add_paragraph(
        "[P1] means “the first passage the agent was given”, and two modules "
        "need to agree which that is: the fixtures that build the context and "
        "the checks that resolve a from-list of chunk ids back to ordinals. If "
        "they sort differently, every from-restricted citation check measures a "
        "different passage than the agent cited — and passes, because the counts "
        "still line up. Nothing in the output would look wrong. "
        "datasets.order_passages decides it once, at parse time, and both sides "
        "read list order afterwards."
    )

    doc.add_heading("The public verifier serves credentials only", 2)
    doc.add_paragraph(
        "§12.4's endpoint is unauthenticated by design — a credential whose "
        "check requires the issuer's permission is one nobody outside can rely "
        "on. But portfolio_items also holds the learner's proofs and essays, "
        "because §12 assumes the table holds credentials alone and in this "
        "schema it does not. An id lookup that served them would publish "
        "coursework to anyone who could guess a UUID. verify_item filters by "
        "kind, and a work item returns the same “not found” as an unknown id, so "
        "the endpoint cannot be used to probe which ids exist."
    )

    doc.add_heading("Credentials must be signed; a learner's proof need not be", 2)
    doc.add_paragraph(
        "Two signing paths on purpose. A credential with no key is not issued — "
        "SigningKeyUnavailable propagates and §16's row applies, because signing "
        "is what a credential is for. A work item with no key is written "
        "unsigned, recording that it is unsigned in a field a query can find: "
        "the hash chain is its tamper-evidence and the signature is additional, "
        "so refusing to record a learner's proof because a development box has "
        "no key would trade something real for something marginal."
    )

    doc.add_heading("The affected-dataset check fails loudly when it finds nothing", 2)
    doc.add_paragraph(
        "eval affected exits non-zero when a changed prompt resolves to no "
        "dataset, rather than reporting “nothing affected”. That silence is "
        "exactly how a prompt change ships unevaluated, and §13.4 makes the "
        "reviewer the fallback for the cases automation cannot settle."
    )

    doc.add_heading("A summative submission does not branch on the verdict", 2)
    doc.add_paragraph(
        "LAB branches three ways on ANSWER_SUBMITTED — correct, incorrect with "
        "attempts remaining, incorrect and exhausted. The two rows added for "
        "SUMMATIVE_ASSESSMENT branch only on whether criteria remain, because "
        "§11.2 is single-submission and the retry branch IS the multi-attempt "
        "cycle that closed-book assessment replaces."
    )

    # --- 5 -----------------------------------------------------------------
    doc.add_heading("5. Where the spec was followed, and where it was not", 1)
    doc.add_paragraph(
        "Sixteen divergences, E1–E16, in DIVERGENCES-EVALUATION.md. Four are "
        "load-bearing — following the spec literally produces something that "
        "cannot run or is quietly wrong."
    )
    table(
        doc,
        ["Divergence", "Summary"],
        [
            ["E1 (load-bearing)", "signing_keys does not exist, portfolio_item_kind has neither credential value, and the table already means learner work"],
            ["E2 (load-bearing)", "grading_kind needs three values; §5's comment names two and §7.2's own example declares the third"],
            ["E9 (load-bearing)", "Every record_portfolio_item effect raised, taking its whole batch with it"],
            ["E10 (load-bearing)", "SUMMATIVE_ASSESSMENT was reachable and inescapable"],
            ["E3", "Entries need a stable key; index-only naming silently repoints every reviewer note on insertion"],
            ["E4", "entry_count has no writer and regression_tolerance has no column"],
            ["E5", "Money is NUMERIC, not the REAL §5 specifies; caught by the existing §14 convention test"],
            ["E6", "The CASCADE/RESTRICT pair on datasets is decorative; left as specified, with the reason recorded"],
            ["E7", "§8's metrics and §7.2's properties are not joined, and the gap fails green"],
            ["E8", "precision@k divides by what was returned, not by k; §9.2's prose for recall describes precision"],
            ["E11", "The reviewer role was both too permissive (no delete concept at all) and too restrictive (audit_log) against §14.1"],
            ["E12", "The CI workflow gates on measurement rather than on scores, and says so at length"],
            ["E13", "§15.3's revert left the budget question open — superseded by SD9's closure, see §2.3"],
            ["E14–E16", "Found by Tier 3 in this build's own code; see §2.2"],
        ],
    )

    # --- 6 -----------------------------------------------------------------
    doc.add_heading("6. What is not done", 1)
    bullets(
        doc,
        [
            "Golden datasets are two, not the ~150–250 entries §7.4 budgets for "
            "MVP. What exists is a Lecturer dataset with three entries covering the "
            "core case and both adversarial shapes §8.1 asks for, and a retrieval "
            "dataset with three hand-labelled queries. The other six agents have no "
            "dataset, which the CI job reports rather than hides.",
            "The grading_calibration dataset kind is implemented and empty. It is "
            "what §19 open question 2 turns on, and it needs ground-truth grades "
            "from a domain expert.",
            "The web reviewer surface is §10.3's v1.1 candidate and is not built; "
            "the CLI is complete.",
            "The assessment definition ships with three problems — one attempt's "
            "worth — so check_eligibility will refuse the first retake until the "
            "pool is expanded. That is the intended behaviour and the file says so.",
            "rubric_criterion_id values in that definition are placeholders, exactly "
            "as §11.3's own example leaves them. parse_definition accepts the file "
            "and start_attempt refuses to assess a learner against criteria that do "
            "not exist, so the failure lands at the right moment.",
            "The frontend has no assessment surface. §19 files it as a frontend v1.1 "
            "revision and it is not in this build.",
        ],
    )

    # --- 7 -----------------------------------------------------------------
    doc.add_heading("7. Spec debt", 1)
    table(
        doc,
        ["Item", "Status"],
        [
            [
                "SD9 — evaluation runs share the learner's budget cap",
                "Opened by this build, CLOSED by subsystem 7 — and the premise was "
                "false. See §2.3. The enforcement half was real and is now "
                "runner.budget_preflight.",
            ],
        ],
    )
    doc.add_paragraph(
        "No other spec debt was opened. Two counts in the evaluation spec itself "
        "are wrong and are recorded rather than silently corrected: §5 and §19 "
        "both say four v1.2 additions from this subsystem, and the real number "
        "is seven. §15.2 and §15.3 contain a visible train of thought that "
        "revises the count to five, then six, then reverts to four."
    )

    # --- 8 -----------------------------------------------------------------
    doc.add_heading("8. What to do first", 1)
    bullets(
        doc,
        [
            "Author the six missing agent datasets. The harness is the cheap half; "
            "§3 is categorical that the entries are written by a human with intent "
            "about what each tests, and §7.4 is explicit that twenty good entries "
            "beat two hundred mediocre ones.",
            "Start the grading_calibration dataset. It is the only thing that "
            "answers whether meta-graded metrics can ever be trusted enough to "
            "gate on, and it needs six months of runs to say anything — so the "
            "clock starts when the first entry lands.",
            "Recalibrate §8's thresholds once there are real runs behind them. They "
            "are the spec's guesses and §19 expects them to move within three "
            "months of production use.",
            "Run the reranker A/B once the corpus has embeddings. It answers "
            "retrieval §19 open question 3, which has been open since subsystem 3, "
            "and the dataset for it is written.",
        ],
    )

    doc.add_page_break()
    doc.add_heading("Appendix: running it", 1)
    doc.add_paragraph("Validate and materialise the datasets:")
    code(doc, "make eval-validate\nmake eval-sync")
    doc.add_paragraph("Before merging a prompt change (§13.1):")
    code(
        doc,
        "python -m studium.eval.cli eval affected --changed $(git diff --name-only main)\n"
        'make eval-gate d="lecturer_formal_stance_grounding"',
    )
    doc.add_paragraph("The reviewer's queue (§10.2):")
    code(
        doc,
        "studium review list --agent lecturer\n"
        "studium review show <queue_id>\n"
        'studium review flag <queue_id> --to-dataset lecturer_grounding --note "..."',
    )
    doc.add_paragraph("Issuer keys (§12.3):")
    code(doc, "studium eval keys generate\nstudium eval keys publish\nstudium eval keys list")
    doc.add_paragraph("The paid tier — verbose, per E16:")
    code(
        doc,
        'ANTHROPIC_API_KEY=... STUDIUM_RUN_PAID_TESTS=1 \\\n'
        "  python -m pytest tests/online/test_evaluation_paid.py -v -m anthropic",
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    return OUT


if __name__ == "__main__":
    print(f"wrote {build()}")
