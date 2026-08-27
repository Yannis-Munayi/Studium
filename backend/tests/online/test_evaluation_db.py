"""Evaluation harness against Postgres (evaluation §17 Tier 2).

§17 names four:

* golden dataset sync produces the expected rows;
* an evaluation run round-trips with the aggregate calculated correctly;
* content review CLI actions update the queue and produce an audit trail;
* a portfolio item is created, signed, retrievable and verifies.

Plus the constraints the migration added, asserted against the real database
rather than against the ORM metadata. Several of them -- the credential-kind
enum values, the one-current-key index, the run's counts-agree CHECK -- exist
precisely because the ORM would happily construct the row they forbid.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from studium.eval import credentials, review, runner, sync
from studium.eval.checks import CheckOutcome
from studium.eval.credentials import SigningIdentity
from studium.eval.datasets import parse_dataset, parse_directory
from studium.eval.grading import EntryGrade, PropertyGrade
from studium.eval.runner import EntryResult, RunReport

pytestmark = pytest.mark.postgres

CONTENT_ROOT = Path(__file__).resolve().parents[2] / "content" / "evaluation"


def make_dataset(tmp_path: Path, slug: str = "tier2_dataset", entries: int = 2):
    lines = [
        "dataset:",
        f"  slug: {slug}",
        "  kind: agent_output",
        "  agent: lecturer",
        "  description: A Tier 2 fixture dataset.",
        "  regression_tolerance:",
        "    aggregate_score_drop_max: 0.05",
        "    per_entry_failure_max: 2",
        "entries:",
    ]
    for index in range(entries):
        lines += [
            f"  - id: entry_{index:03d}",
            "    input:",
            "      concept: {title: Beta-reduction}",
            "      retrieved_passages:",
            "        - {id: chunk_a, text: first}",
            "    expected:",
            "      properties:",
            "        - name: citations_resolve",
            "          check: citation_targets_valid",
        ]
    path = tmp_path / f"{slug}.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return parse_dataset(path)


def report_for(dataset, *, scores: list[float]) -> RunReport:
    """A RunReport with hand-set outcomes, so persistence is tested without
    needing a model call."""
    results = []
    for entry, score in zip(dataset.entries, scores, strict=True):
        results.append(
            EntryResult(
                entry=entry,
                grade=EntryGrade(
                    entry.key,
                    properties=(
                        PropertyGrade(
                            name="citations_resolve",
                            check="citation_targets_valid",
                            outcome=CheckOutcome(score >= 1.0, score, "fixture"),
                        ),
                    ),
                ),
                actual_output={"text": "fixture"},
                latency_ms=12,
                cost_usd=0.001,
            )
        )
    return RunReport(
        dataset=dataset,
        prompt_hash="a" * 64,
        model="claude-opus-5",
        results=results,
        completed_at=dt.datetime.now(dt.UTC),
    )


# --- §17: golden dataset sync ---------------------------------------------


class TestSync:
    def test_sync_creates_dataset_and_entry_rows(self, db: Session, tmp_path) -> None:
        dataset = make_dataset(tmp_path)
        result = sync.sync(db, [dataset])
        db.flush()

        assert result.datasets_created == [dataset.slug]
        assert len(result.entries_created) == 2

        row = db.execute(
            sql(
                "SELECT kind::text AS kind, agent::text AS agent, entry_count,"
                " regression_tolerance FROM golden_datasets WHERE slug = :s"
            ),
            {"s": dataset.slug},
        ).one()
        assert row.kind == "agent_output"
        assert row.agent == "lecturer"
        assert row.regression_tolerance["aggregate_score_drop_max"] == 0.05

    def test_entry_count_is_maintained(self, db: Session, tmp_path) -> None:
        """E4: the spec gives the column a DEFAULT and names no writer, so left
        alone it reads 0 forever while the dataset has entries -- and §14.3's
        dashboards read exactly this sort of column."""
        dataset = make_dataset(tmp_path, entries=3)
        sync.sync(db, [dataset])
        db.flush()

        count = db.execute(
            sql("SELECT entry_count FROM golden_datasets WHERE slug = :s"),
            {"s": dataset.slug},
        ).scalar_one()
        actual = db.execute(
            sql(
                "SELECT COUNT(*) FROM golden_dataset_entries e"
                " JOIN golden_datasets d ON d.id = e.dataset_id WHERE d.slug = :s"
            ),
            {"s": dataset.slug},
        ).scalar_one()
        assert count == actual == 3

    def test_sync_is_idempotent(self, db: Session, tmp_path) -> None:
        """It runs on every merge (§7.3 step 5); churning updated_at on rows
        nobody touched would make "when did this entry last change" useless."""
        dataset = make_dataset(tmp_path)
        sync.sync(db, [dataset])
        db.flush()

        second = sync.sync(db, [dataset])
        assert second.is_empty, second.render()

    def test_an_edited_entry_is_updated_in_place(self, db: Session, tmp_path) -> None:
        dataset = make_dataset(tmp_path)
        sync.sync(db, [dataset])
        db.flush()

        edited = make_dataset(tmp_path, entries=2)
        object.__setattr__(edited.entries[0], "notes", "a new note")
        result = sync.sync(db, [edited])
        db.flush()
        assert result.entries_updated

    def test_an_entry_removed_from_the_yaml_is_deleted_when_unused(
        self, db: Session, tmp_path
    ) -> None:
        sync.sync(db, [make_dataset(tmp_path, entries=3)])
        db.flush()
        result = sync.sync(db, [make_dataset(tmp_path, entries=1)])
        db.flush()
        assert len(result.entries_deleted) == 2
        assert not result.orphans

    def test_an_entry_with_results_is_kept_as_an_orphan(
        self, db: Session, tmp_path
    ) -> None:
        """evaluation_results.entry_id is ON DELETE RESTRICT and §7.3 is
        explicit that old results "remain queryable for historical
        comparison". Deleting would fail; sync reports instead."""
        dataset = make_dataset(tmp_path, entries=2)
        sync.sync(db, [dataset])
        db.flush()
        runner.persist(db, report_for(dataset, scores=[1.0, 1.0]))
        db.flush()

        result = sync.sync(db, [make_dataset(tmp_path, entries=1)])
        db.flush()
        assert result.orphans
        assert not result.entries_deleted

    def test_a_database_dataset_with_no_file_is_reported_not_deleted(
        self, db: Session, tmp_path
    ) -> None:
        sync.sync(db, [make_dataset(tmp_path, slug="tier2_first")])
        db.flush()
        result = sync.sync(db, [make_dataset(tmp_path, slug="tier2_second")])
        assert "tier2_first" in result.unmatched_datasets

    def test_retire_deactivates_and_keeps_the_rows(
        self, db: Session, tmp_path
    ) -> None:
        dataset = make_dataset(tmp_path)
        sync.sync(db, [dataset])
        db.flush()

        assert sync.retire(db, dataset.slug, note="the property no longer applies")
        db.flush()
        row = db.execute(
            sql("SELECT active, description FROM golden_datasets WHERE slug = :s"),
            {"s": dataset.slug},
        ).one()
        assert row.active is False
        assert "Retired:" in row.description
        assert not sync.retire(db, dataset.slug, note="again")

    def test_retire_requires_a_note(self, db: Session, tmp_path) -> None:
        with pytest.raises(ValueError, match="note is required"):
            sync.retire(db, "anything", note="  ")

    def test_the_shipped_datasets_sync(self, db: Session) -> None:
        """The real content tree, not a fixture: a dataset that parses and
        cannot be materialised would be discovered on a merge."""
        result = sync.sync(db, parse_directory(CONTENT_ROOT))
        db.flush()
        assert result.datasets_created


# --- §17: evaluation run round-trip ---------------------------------------


class TestRunPersistence:
    def test_a_run_round_trips_with_its_aggregate(
        self, db: Session, tmp_path
    ) -> None:
        dataset = make_dataset(tmp_path)
        sync.sync(db, [dataset])
        db.flush()

        run_id = runner.persist(db, report_for(dataset, scores=[1.0, 0.0]))
        db.flush()

        row = db.execute(
            sql(
                "SELECT status, aggregate_score, entries_run, entries_passed,"
                " entries_failed, cost_usd FROM evaluation_runs WHERE id = :id"
            ),
            {"id": run_id},
        ).one()
        assert row.status == "complete"
        assert float(row.aggregate_score) == pytest.approx(0.5)
        assert (row.entries_run, row.entries_passed, row.entries_failed) == (2, 1, 1)
        assert float(row.cost_usd) == pytest.approx(0.002)

    def test_one_result_row_per_entry(self, db: Session, tmp_path) -> None:
        dataset = make_dataset(tmp_path)
        sync.sync(db, [dataset])
        db.flush()
        run_id = runner.persist(db, report_for(dataset, scores=[1.0, 1.0]))
        db.flush()

        count = db.execute(
            sql("SELECT COUNT(*) FROM evaluation_results WHERE run_id = :id"),
            {"id": run_id},
        ).scalar_one()
        assert count == 2

    def test_persisting_an_unsynced_dataset_names_the_fix(
        self, db: Session, tmp_path
    ) -> None:
        with pytest.raises(runner.RunAborted, match="eval sync"):
            runner.persist(db, report_for(make_dataset(tmp_path), scores=[1.0, 1.0]))

    def test_baseline_reads_the_last_complete_run(
        self, db: Session, tmp_path
    ) -> None:
        """§13.1 step 4 compares against "the last passing run for the same
        dataset"."""
        dataset = make_dataset(tmp_path)
        sync.sync(db, [dataset])
        db.flush()

        runner.persist(db, report_for(dataset, scores=[1.0, 1.0]))
        db.flush()
        baseline = runner.baseline(db, dataset.slug)
        assert baseline is not None
        assert baseline.aggregate_score == pytest.approx(1.0)
        assert {e.key for e in baseline.entries} == {"entry_000", "entry_001"}

    def test_no_baseline_for_a_dataset_that_has_never_run(
        self, db: Session, tmp_path
    ) -> None:
        dataset = make_dataset(tmp_path)
        sync.sync(db, [dataset])
        db.flush()
        assert runner.baseline(db, dataset.slug) is None

    def test_the_gate_blocks_on_a_regression_against_the_baseline(
        self, db: Session, tmp_path
    ) -> None:
        dataset = make_dataset(tmp_path)
        sync.sync(db, [dataset])
        db.flush()

        runner.persist(db, report_for(dataset, scores=[1.0, 1.0]))
        db.flush()

        regressed = report_for(dataset, scores=[0.0, 0.0])
        result = runner.gate(db, regressed)
        assert result.blocked
        assert set(result.newly_failing) == {"entry_000", "entry_001"}


class TestRunConstraints:
    """The CHECKs the migration added, against the real database."""

    def _dataset_id(self, db: Session, tmp_path) -> uuid.UUID:
        dataset = make_dataset(tmp_path)
        sync.sync(db, [dataset])
        db.flush()
        return db.execute(
            sql("SELECT id FROM golden_datasets WHERE slug = :s"), {"s": dataset.slug}
        ).scalar_one()

    def _insert(self, db: Session, dataset_id, **overrides) -> None:
        payload = {
            "dataset_id": dataset_id,
            "prompt_hash": "a" * 64,
            "model": "m",
            "trigger_kind": "manual",
            "status": "complete",
            "aggregate": 0.5,
            "run": 2,
            "passed": 1,
            "failed": 1,
            **overrides,
        }
        db.execute(
            sql(
                """
                INSERT INTO evaluation_runs
                    (dataset_id, prompt_hash, model, trigger_kind, status,
                     aggregate_score, entries_run, entries_passed, entries_failed)
                VALUES (:dataset_id, :prompt_hash, :model, :trigger_kind, :status,
                        :aggregate, :run, :passed, :failed)
                """
            ),
            payload,
        )
        db.flush()

    def test_a_short_prompt_hash_is_refused(self, db: Session, tmp_path) -> None:
        with pytest.raises(IntegrityError, match="prompt_hash_len"):
            self._insert(db, self._dataset_id(db, tmp_path), prompt_hash="abc")

    def test_an_unknown_trigger_kind_is_refused(self, db: Session, tmp_path) -> None:
        """§15.1 attributes cost differently per trigger; a typo'd value would
        book a scheduled system run to a real user's ledger."""
        with pytest.raises(IntegrityError, match="trigger_kind_known"):
            self._insert(db, self._dataset_id(db, tmp_path), trigger_kind="whenever")

    def test_a_complete_run_must_carry_a_score(self, db: Session, tmp_path) -> None:
        """A complete run with no score is one whose gate compares against NULL
        and silently passes."""
        with pytest.raises(IntegrityError, match="complete_has_score"):
            self._insert(db, self._dataset_id(db, tmp_path), aggregate=None)

    def test_entry_counts_must_agree(self, db: Session, tmp_path) -> None:
        with pytest.raises(IntegrityError, match="entry_counts_agree"):
            self._insert(db, self._dataset_id(db, tmp_path), run=5)

    def test_one_result_per_entry_per_run(self, db: Session, tmp_path) -> None:
        """Without the constraint a retried entry writes a second row and the
        aggregate double-counts it, with entries_run none the wiser."""
        dataset = make_dataset(tmp_path)
        sync.sync(db, [dataset])
        db.flush()
        run_id = runner.persist(db, report_for(dataset, scores=[1.0, 1.0]))
        db.flush()

        entry_id = db.execute(
            sql(
                "SELECT e.id FROM golden_dataset_entries e"
                " JOIN golden_datasets d ON d.id = e.dataset_id"
                " WHERE d.slug = :s LIMIT 1"
            ),
            {"s": dataset.slug},
        ).scalar_one()

        with pytest.raises(IntegrityError, match="uq_eval_results_run_entry"):
            db.execute(
                sql(
                    """
                    INSERT INTO evaluation_results
                        (run_id, entry_id, actual_output, score, passed)
                    VALUES (:run_id, :entry_id, '{}'::jsonb, 1.0, TRUE)
                    """
                ),
                {"run_id": run_id, "entry_id": entry_id},
            )
            db.flush()


class TestDatasetConstraints:
    def test_an_agent_output_dataset_needs_an_agent(self, db: Session) -> None:
        with pytest.raises(IntegrityError, match="agent_matches_kind"):
            db.execute(
                sql(
                    """
                    INSERT INTO golden_datasets (slug, kind, description)
                    VALUES ('no_agent', 'agent_output', 'x')
                    """
                )
            )
            db.flush()

    def test_a_retrieval_dataset_must_not_name_an_agent(self, db: Session) -> None:
        with pytest.raises(IntegrityError, match="agent_matches_kind"):
            db.execute(
                sql(
                    """
                    INSERT INTO golden_datasets (slug, kind, agent, description)
                    VALUES ('r_with_agent', 'retrieval_quality', 'lecturer', 'x')
                    """
                )
            )
            db.flush()

    def _dataset(self, db: Session, slug: str) -> uuid.UUID:
        return db.execute(
            sql(
                """
                INSERT INTO golden_datasets (slug, kind, agent, description)
                VALUES (:slug, 'agent_output', 'lecturer', 'x')
                RETURNING id
                """
            ),
            {"slug": slug},
        ).scalar_one()

    def _entry(
        self, db: Session, dataset_id: uuid.UUID, expected: str, rubric: str | None
    ) -> None:
        db.execute(
            sql(
                """
                INSERT INTO golden_dataset_entries
                    (dataset_id, entry_key, entry_index, input, expected,
                     grading_kind, rubric)
                VALUES (:id, 'e', 0, '{}'::jsonb, CAST(:expected AS jsonb),
                        'meta_graded', CAST(:rubric AS jsonb))
                """
            ),
            {"id": dataset_id, "expected": expected, "rubric": rubric},
        )
        db.flush()

    PROPERTY_WITH_RUBRIC = (
        '{"properties":[{"name":"s","check":"meta_graded","rubric":"be formal"}]}'
    )
    PROPERTY_WITHOUT_RUBRIC = '{"properties":[{"name":"s","check":"meta_graded"}]}'

    def test_a_meta_graded_property_without_a_rubric_is_refused(
        self, db: Session
    ) -> None:
        """§7.2: the check "invokes the Evaluator ... with the rubric provided
        in the YAML". With none it judges against nothing and returns a
        confident number anyway."""
        dataset_id = self._dataset(db, "rubric_missing")
        with pytest.raises(IntegrityError, match="meta_graded_needs_rubric"):
            self._entry(db, dataset_id, self.PROPERTY_WITHOUT_RUBRIC, None)

    def test_a_rubric_on_the_property_satisfies_the_constraint(
        self, db: Session
    ) -> None:
        """§7.2's own worked example puts it here, not on the entry. The first
        draft of this constraint only accepted an entry-level rubric and
        rejected the shipped Lecturer dataset."""
        self._entry(db, self._dataset(db, "rubric_on_property"), self.PROPERTY_WITH_RUBRIC, None)

    def test_an_entry_level_rubric_satisfies_the_constraint(
        self, db: Session
    ) -> None:
        """The other placement: a rubric that governs the whole entry."""
        self._entry(
            db,
            self._dataset(db, "rubric_on_entry"),
            self.PROPERTY_WITHOUT_RUBRIC,
            '{"text": "be formal"}',
        )

    def test_an_empty_rubric_string_does_not_count(self, db: Session) -> None:
        """`rubric: ""` is a rubric-shaped absence; the Evaluator would be
        handed an empty string and grade against it."""
        dataset_id = self._dataset(db, "rubric_empty")
        with pytest.raises(IntegrityError, match="meta_graded_needs_rubric"):
            self._entry(
                db,
                dataset_id,
                '{"properties":[{"name":"s","check":"meta_graded","rubric":""}]}',
                None,
            )


# --- §17: content review queue processing ---------------------------------


@pytest.fixture
def queued(db: Session):
    """A content_review_queue row attached to a real artifact."""
    from tests.fixtures import lambda_calculus

    fixture = lambda_calculus.build(db, email=f"rev-{uuid.uuid4().hex[:8]}@example.com")
    db.flush()

    artifact_id = db.execute(
        sql(
            """
            INSERT INTO content_artifacts
                (concept_id, kind, stance, body, generated_by, model, prompt_hash,
                 status)
            VALUES (:concept_id, 'lecture_segment', 'formal', 'A segment [P1].',
                    'lecturer', 'claude-opus-5', :hash, 'draft')
            RETURNING id
            """
        ),
        {"concept_id": fixture.concept_id("beta-reduction"), "hash": "b" * 64},
    ).scalar_one()

    queue_id = db.execute(
        sql(
            """
            INSERT INTO content_review_queue (artifact_id, source, reason, severity)
            VALUES (:artifact_id, 'system_confidence', 'thin grounding', 3)
            RETURNING id
            """
        ),
        {"artifact_id": artifact_id},
    ).scalar_one()
    db.flush()
    return {"queue_id": queue_id, "artifact_id": artifact_id, "fixture": fixture}


class TestContentReview:
    def test_pending_lists_the_item_with_its_agent(self, db: Session, queued) -> None:
        items = review.pending(db)
        found = next(i for i in items if i.id == queued["queue_id"])
        assert found.agent == "lecturer"
        assert found.severity == 3

    def test_filtering_by_agent(self, db: Session, queued) -> None:
        assert any(i.id == queued["queue_id"] for i in review.pending(db, agent="lecturer"))
        assert not any(i.id == queued["queue_id"] for i in review.pending(db, agent="tutor"))

    def test_detail_returns_the_artifact(self, db: Session, queued) -> None:
        found = review.detail(db, queued["queue_id"])
        assert found is not None
        assert found["artifact"]["body"].startswith("A segment")

    def test_approve_dismisses_rather_than_resolves(self, db: Session, queued) -> None:
        """`resolved` means "there was a problem and it has been dealt with",
        which an approved item is not. Getting this the other way round makes
        "how many real defects did the queue catch" unanswerable -- and that
        number is what §14.3 tunes the severity thresholds on."""
        assert review.close(db, queued["queue_id"], action="approve", note="fine")
        db.flush()
        status = db.execute(
            sql("SELECT status::text FROM content_review_queue WHERE id = :id"),
            {"id": queued["queue_id"]},
        ).scalar_one()
        assert status == "dismissed"

    def test_reject_resolves(self, db: Session, queued) -> None:
        assert review.close(db, queued["queue_id"], action="reject", note="a defect")
        db.flush()
        status = db.execute(
            sql("SELECT status::text FROM content_review_queue WHERE id = :id"),
            {"id": queued["queue_id"]},
        ).scalar_one()
        assert status == "resolved"

    def test_closing_writes_an_audit_row(self, db: Session, queued) -> None:
        """Closing a review item is a privileged action taken on content a
        learner saw."""
        review.close(db, queued["queue_id"], action="approve", note="fine")
        db.flush()
        row = db.execute(
            sql(
                "SELECT action, target_type, reason FROM audit_log"
                " WHERE target_id = :id"
            ),
            {"id": queued["queue_id"]},
        ).one()
        assert row.action == "content_review.approve"
        assert row.target_type == "content_review_queue"
        assert row.reason == "fine"

    def test_closing_twice_is_a_no_op(self, db: Session, queued) -> None:
        review.close(db, queued["queue_id"], action="approve", note="fine")
        db.flush()
        assert not review.close(db, queued["queue_id"], action="reject", note="no")

    def test_a_note_is_required(self, db: Session, queued) -> None:
        with pytest.raises(ValueError, match="note is required"):
            review.close(db, queued["queue_id"], action="approve", note="   ")

    def test_escalation_only_raises(self, db: Session, queued) -> None:
        assert not review.escalate(db, queued["queue_id"], severity=2)
        db.flush()
        db.execute(
            sql("UPDATE content_review_queue SET severity = 1 WHERE id = :id"),
            {"id": queued["queue_id"]},
        )
        assert review.escalate(db, queued["queue_id"], severity=3)

    def test_depth_counts_by_source(self, db: Session, queued) -> None:
        """§16's last row: when the reviewer is unavailable, items accumulate
        and the queue-depth dashboard reflects it."""
        assert review.depth(db)["system_confidence"] >= 1

    def test_flag_to_dataset_writes_a_stub_and_closes_the_item(
        self, db: Session, queued, tmp_path
    ) -> None:
        """§10.2: the mechanism by which production defects strengthen the
        regression suite."""
        stub = review.flag_to_dataset(
            db,
            queued["queue_id"],
            dataset_slug="lecturer_grounding",
            note="under-grounded on beta-reduction",
            content_root=tmp_path,
        )
        db.flush()

        assert stub.path.exists()
        text = stub.path.read_text(encoding="utf-8")
        assert "STUB" in text
        assert "stub: true" in text
        assert str(queued["queue_id"]) in text

        status = db.execute(
            sql("SELECT status::text FROM content_review_queue WHERE id = :id"),
            {"id": queued["queue_id"]},
        ).scalar_one()
        assert status == "resolved"

    def test_the_stub_parses_and_is_not_runnable(
        self, db: Session, queued, tmp_path
    ) -> None:
        """A stub that did not parse would break `studium eval validate` for
        the whole tree; one that ran would score the agent against nothing and
        report a pass for the defect it came from."""
        stub = review.flag_to_dataset(
            db,
            queued["queue_id"],
            dataset_slug="lecturer_grounding",
            note="under-grounded",
            content_root=tmp_path,
        )
        db.flush()
        dataset = parse_dataset(stub.path)
        assert dataset.stub_count == 1
        assert not dataset.runnable_entries

    def test_upstream_escalation_creates_an_ingestion_queue_row(
        self, db: Session, queued
    ) -> None:
        """§10.4: the fix is upstream, so the row moves to the queue whose
        reviewer can act on it."""
        ingestion_id = review.escalate_upstream(
            db,
            queued["queue_id"],
            reason="concept_sources authoring is missing for this concept",
            concept_id=queued["fixture"].concept_id("beta-reduction"),
        )
        db.flush()

        row = db.execute(
            sql(
                "SELECT flag_source::text AS flag_source, severity, payload"
                " FROM ingestion_review_queue WHERE id = :id"
            ),
            {"id": ingestion_id},
        ).one()
        assert row.flag_source == "concept_source_conflict"
        assert row.severity == 3
        assert row.payload["content_review_queue_id"] == str(queued["queue_id"])

    def test_upstream_escalation_needs_a_target(self, db: Session, queued) -> None:
        with pytest.raises(ValueError, match="needs a concept or a subject"):
            review.escalate_upstream(db, queued["queue_id"], reason="x")


# --- §17: portfolio item creation and verification ------------------------


@pytest.fixture
def identity(db: Session) -> SigningIdentity:
    ident = SigningIdentity(
        key_id=f"test-{uuid.uuid4().hex[:8]}",
        seed=base64.b64decode(credentials.generate_seed()),
    )
    credentials.register_public_key(db, ident)
    db.flush()
    return ident


class TestCredentials:
    def test_end_to_end_issue_and_verify(
        self, db: Session, identity: SigningIdentity
    ) -> None:
        """§17: "portfolio item is created, signed, retrievable via verify
        endpoint, and verifies against public key"."""
        from tests.fixtures import lambda_calculus

        fixture = lambda_calculus.build(
            db, email=f"cred-{uuid.uuid4().hex[:8]}@example.com"
        )
        db.flush()

        item_id = credentials.issue_credential(
            db,
            user_id=fixture.user.id,
            learner_subject_id=fixture.enrollment.id,
            subject_slug="lambda-calculus",
            kind="assessment_pass",
            score=0.87,
            passing_threshold=0.70,
            criteria_results=[{"criterion": "beta", "weight": 2, "grade": 2}],
            assessment_id=uuid.uuid4(),
            identity=identity,
        )
        db.flush()

        result = credentials.verify_item(db, item_id)
        assert result.found and result.valid
        assert result.item["score"] == 0.87
        assert result.key_id == identity.key_id
        assert not result.key_retired

    def test_a_tampered_body_stops_verifying(
        self, db: Session, identity: SigningIdentity
    ) -> None:
        """The whole point of signing: an edited credential is detectable
        without trusting whoever is holding it."""
        from tests.fixtures import lambda_calculus

        fixture = lambda_calculus.build(
            db, email=f"tamper-{uuid.uuid4().hex[:8]}@example.com"
        )
        db.flush()
        item_id = credentials.issue_credential(
            db,
            user_id=fixture.user.id,
            learner_subject_id=fixture.enrollment.id,
            subject_slug="lambda-calculus",
            kind="subject_completion",
            score=0.9,
            passing_threshold=0.70,
            criteria_results=[],
            identity=identity,
        )
        db.flush()

        # The replacement strings are bound, not inlined: the canonical body
        # contains `"score":0.9`, and `:0` inside a text() literal is parsed as
        # a bind parameter -- so an inline version failed with "a value is
        # required for bind parameter '0'" and never reached the database.
        db.execute(
            sql(
                "UPDATE portfolio_items SET body = replace(body, :old, :new)"
                " WHERE id = :id"
            ),
            {"id": item_id, "old": '"score":0.9', "new": '"score":1.0'},
        )
        db.flush()

        result = credentials.verify_item(db, item_id)
        assert result.found and not result.valid

    def test_a_work_item_is_not_served_by_the_verifier(self, db: Session) -> None:
        """The endpoint is unauthenticated (§12.4). portfolio_items also holds
        the learner's proofs and essays, so an id-lookup that served them would
        publish coursework to anyone who could guess a UUID.
        DIVERGENCES-EVALUATION (E1)."""
        from tests.fixtures import lambda_calculus

        fixture = lambda_calculus.build(
            db, email=f"work-{uuid.uuid4().hex[:8]}@example.com"
        )
        db.flush()
        item_id = db.execute(
            sql(
                """
                INSERT INTO portfolio_items
                    (user_id, learner_subject_id, chain_index, kind, title, body,
                     content_sha256, signature)
                VALUES (:user_id, :lsid, 0, 'prose', 'My essay',
                        'private coursework', :digest, '{}'::jsonb)
                RETURNING id
                """
            ),
            {
                "user_id": fixture.user.id,
                "lsid": fixture.enrollment.id,
                "digest": "c" * 64,
            },
        ).scalar_one()
        db.flush()

        result = credentials.verify_item(db, item_id)
        assert not result.found, "the public verifier served a learner's work"

    def test_an_unknown_id_and_a_work_item_are_indistinguishable(
        self, db: Session
    ) -> None:
        """So the endpoint cannot be used to probe which ids exist."""
        unknown = credentials.verify_item(db, uuid.uuid4())
        assert not unknown.found
        assert unknown.reason == "no such credential"

    def test_credentials_join_the_learners_hash_chain(
        self, db: Session, identity: SigningIdentity
    ) -> None:
        from tests.fixtures import lambda_calculus

        fixture = lambda_calculus.build(
            db, email=f"chain-{uuid.uuid4().hex[:8]}@example.com"
        )
        db.flush()

        first = credentials.issue_credential(
            db,
            user_id=fixture.user.id,
            learner_subject_id=fixture.enrollment.id,
            subject_slug="lambda-calculus",
            kind="assessment_pass",
            score=0.8,
            passing_threshold=0.7,
            criteria_results=[],
            assessment_id=uuid.uuid4(),
            identity=identity,
        )
        second = credentials.issue_credential(
            db,
            user_id=fixture.user.id,
            learner_subject_id=fixture.enrollment.id,
            subject_slug="lambda-calculus",
            kind="subject_completion",
            score=0.9,
            passing_threshold=0.7,
            criteria_results=[],
            identity=identity,
        )
        db.flush()

        rows = db.execute(
            sql(
                "SELECT chain_index, signature FROM portfolio_items"
                " WHERE id IN (:a, :b) ORDER BY chain_index"
            ),
            {"a": first, "b": second},
        ).all()
        assert [r.chain_index for r in rows] == [0, 1]
        # The second links to the first: that is what makes it a chain rather
        # than a list of independent hashes.
        assert rows[1].signature["manifest"]["prev_sha256"]

    def test_rotation_retires_the_incumbent_and_keeps_it_published(
        self, db: Session, identity: SigningIdentity
    ) -> None:
        """§12.3: "Old public keys remain published so historical portfolio
        items stay verifiable.\""""
        successor = SigningIdentity(
            key_id=f"test-{uuid.uuid4().hex[:8]}",
            seed=base64.b64decode(credentials.generate_seed()),
            issuer=identity.issuer,
        )
        credentials.register_public_key(db, successor)
        db.flush()

        published = {k["key_id"]: k for k in credentials.published_keys(db)}
        assert published[identity.key_id]["current"] is False
        assert published[successor.key_id]["current"] is True

    def test_a_credential_signed_by_a_retired_key_still_verifies(
        self, db: Session, identity: SigningIdentity
    ) -> None:
        """A retired key is still a valid signer for what it already signed.
        Rejecting would invalidate every credential ever issued."""
        from tests.fixtures import lambda_calculus

        fixture = lambda_calculus.build(
            db, email=f"rot-{uuid.uuid4().hex[:8]}@example.com"
        )
        db.flush()
        item_id = credentials.issue_credential(
            db,
            user_id=fixture.user.id,
            learner_subject_id=fixture.enrollment.id,
            subject_slug="lambda-calculus",
            kind="subject_completion",
            score=0.9,
            passing_threshold=0.7,
            criteria_results=[],
            identity=identity,
        )
        credentials.register_public_key(
            db,
            SigningIdentity(
                key_id=f"test-{uuid.uuid4().hex[:8]}",
                seed=base64.b64decode(credentials.generate_seed()),
                issuer=identity.issuer,
            ),
        )
        db.flush()

        result = credentials.verify_item(db, item_id)
        assert result.valid
        assert result.key_retired, "the verifier should report the rotation"

    def test_only_one_current_key_per_issuer(self, db: Session) -> None:
        """Two current keys make "which key signed this" a question the issuer
        itself cannot answer."""
        issuer = f"test-{uuid.uuid4().hex[:8]}.app"
        db.execute(
            sql(
                "INSERT INTO signing_keys (key_id, public_key, issuer)"
                " VALUES ('k1', :pk, :issuer)"
            ),
            {"pk": "A" * 44, "issuer": issuer},
        )
        db.flush()
        with pytest.raises(IntegrityError, match="idx_signing_keys_current"):
            db.execute(
                sql(
                    "INSERT INTO signing_keys (key_id, public_key, issuer)"
                    " VALUES ('k2', :pk, :issuer)"
                ),
                {"pk": "B" * 44, "issuer": issuer},
            )
            db.flush()


class TestPortfolioEffectHandler:
    def test_a_work_item_is_written_with_a_signature_manifest(
        self, db: Session
    ) -> None:
        """E9: portfolio_items.signature is NOT NULL with no default and the
        effect handler never built one, so every record_portfolio_item effect
        raised NotNullViolation -- taking the turn's mastery evidence and
        journal update down with it, because _apply_all rolls the batch back.
        Nothing covered the path."""
        from studium.orchestration.effects import _record_portfolio_item
        from tests.fixtures import lambda_calculus

        fixture = lambda_calculus.build(
            db, email=f"eff-{uuid.uuid4().hex[:8]}@example.com"
        )
        db.flush()

        item_id = _record_portfolio_item(
            db,
            {
                "user_id": str(fixture.user.id),
                "learner_subject_id": str(fixture.enrollment.id),
                "body": "A proof the learner wrote.",
                "kind": "proof",
                "title": "Beta-reduction is confluent",
            },
            None,
        )
        db.flush()

        row = db.execute(
            sql("SELECT signature, content_sha256 FROM portfolio_items WHERE id = :id"),
            {"id": item_id},
        ).one()
        assert row.signature["manifest"]["content_sha256"] == row.content_sha256
        assert "signed" in row.signature

    def test_an_unsigned_work_item_says_so(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The chain digest is the tamper-evidence and the signature is
        additional, so a missing key degrades rather than failing a learner's
        turn. It records *that it is unsigned* rather than looking like a
        signed item whose value happens to be absent."""
        from studium.orchestration.effects import _record_portfolio_item
        from tests.fixtures import lambda_calculus

        monkeypatch.delenv(credentials.SIGNING_KEY_ENV, raising=False)
        fixture = lambda_calculus.build(
            db, email=f"uns-{uuid.uuid4().hex[:8]}@example.com"
        )
        db.flush()

        item_id = _record_portfolio_item(
            db,
            {
                "user_id": str(fixture.user.id),
                "learner_subject_id": str(fixture.enrollment.id),
                "body": "Another proof.",
            },
            None,
        )
        db.flush()

        signature = db.execute(
            sql("SELECT signature FROM portfolio_items WHERE id = :id"),
            {"id": item_id},
        ).scalar_one()
        assert signature["signed"] is False
        assert signature["unsigned_reason"]
