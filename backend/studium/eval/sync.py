"""Materialising datasets into the database (evaluation §4, §7.3 step 5).

``studium eval sync`` copies ``content/evaluation/`` into ``golden_datasets``
and ``golden_dataset_entries``. **The repo stays the source of truth** (§4);
this copy exists so a scheduled run, a dashboard, and ``evaluation_results``'
foreign key have something to point at. Nothing reads the database copy to
decide what to run -- the CLI parses the YAML every time -- so a stale sync
cannot silently change what a gate evaluates.

**Idempotent.** Running it twice over unchanged YAML changes nothing, which is
what lets it run on every merge (§7.3 step 5) without churning ``updated_at``
on rows nobody touched.

**Removals are refused, not silently applied.** An entry deleted from the YAML
that already has ``evaluation_results`` rows cannot be deleted:
``evaluation_results.entry_id`` is ``ON DELETE RESTRICT``, and that is correct
-- §7.3 is explicit that entries are versioned rather than deleted, and the old
results "remain queryable for historical comparison". Sync reports such entries
as orphans and leaves them. An entry with no results is deleted, because
nothing is lost and leaving it would keep it in future runs after the author
removed it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.eval.datasets import Dataset

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """What one sync changed, for the CLI to print before it commits."""

    datasets_created: list[str] = field(default_factory=list)
    datasets_updated: list[str] = field(default_factory=list)
    entries_created: list[str] = field(default_factory=list)
    entries_updated: list[str] = field(default_factory=list)
    entries_deleted: list[str] = field(default_factory=list)
    #: Entries removed from the YAML that could not be deleted because they
    #: carry results. Left in place; §7.3's retirement path is to deactivate
    #: the dataset and author a successor.
    orphans: list[str] = field(default_factory=list)
    #: Datasets in the database with no file. Never deleted here.
    unmatched_datasets: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.datasets_created
            or self.datasets_updated
            or self.entries_created
            or self.entries_updated
            or self.entries_deleted
        )

    def render(self) -> str:
        lines: list[str] = []
        for label, items in (
            ("+ dataset", self.datasets_created),
            ("~ dataset", self.datasets_updated),
            ("+ entry", self.entries_created),
            ("~ entry", self.entries_updated),
            ("- entry", self.entries_deleted),
        ):
            lines.extend(f"  {label} {item}" for item in items)
        for orphan in self.orphans:
            lines.append(
                f"  ! {orphan} was removed from the YAML but has evaluation "
                f"results; kept (§7.3 retires datasets, it does not delete "
                f"entries)"
            )
        for slug in self.unmatched_datasets:
            lines.append(
                f"  ! {slug} is in the database with no file; not touched. "
                f"Deactivate it with `studium eval retire {slug}` if it is "
                f"meant to be gone."
            )
        return "\n".join(lines) if lines else "  (no changes)"


def sync(session: Session, datasets: Sequence[Dataset]) -> SyncResult:
    """Materialise ``datasets``. Caller commits."""
    result = SyncResult()

    known = {
        row.slug
        for row in session.execute(sql("SELECT slug FROM golden_datasets")).all()
    }
    incoming = {d.slug for d in datasets}
    result.unmatched_datasets = sorted(known - incoming)

    for dataset in datasets:
        _sync_one(session, dataset, result)

    return result


def _sync_one(session: Session, dataset: Dataset, result: SyncResult) -> None:
    existing = session.execute(
        sql(
            """
            SELECT id, kind::text AS kind, agent::text AS agent, version,
                   description, active, regression_tolerance
              FROM golden_datasets WHERE slug = :slug
            """
        ),
        {"slug": dataset.slug},
    ).one_or_none()

    tolerance = json.dumps(dataset.regression_tolerance, sort_keys=True)

    if existing is None:
        dataset_id = session.execute(
            sql(
                """
                INSERT INTO golden_datasets
                    (slug, kind, agent, version, description, active,
                     entry_count, regression_tolerance)
                VALUES
                    (:slug, CAST(:kind AS golden_dataset_kind),
                     CAST(:agent AS agent_identity), :version, :description,
                     :active, 0, CAST(:tolerance AS jsonb))
                RETURNING id
                """
            ),
            {
                "slug": dataset.slug,
                "kind": dataset.kind,
                "agent": dataset.agent,
                "version": dataset.version,
                "description": dataset.description,
                "active": dataset.active,
                "tolerance": tolerance,
            },
        ).scalar_one()
        result.datasets_created.append(dataset.slug)
    else:
        dataset_id = existing.id
        changed = (
            existing.kind != dataset.kind
            or (existing.agent or None) != dataset.agent
            or existing.version != dataset.version
            or existing.description != dataset.description
            or bool(existing.active) != dataset.active
            or (existing.regression_tolerance or {}) != dataset.regression_tolerance
        )
        if changed:
            session.execute(
                sql(
                    """
                    UPDATE golden_datasets
                       SET kind = CAST(:kind AS golden_dataset_kind),
                           agent = CAST(:agent AS agent_identity),
                           version = :version,
                           description = :description,
                           active = :active,
                           regression_tolerance = CAST(:tolerance AS jsonb)
                     WHERE id = :id
                    """
                ),
                {
                    "id": dataset_id,
                    "kind": dataset.kind,
                    "agent": dataset.agent,
                    "version": dataset.version,
                    "description": dataset.description,
                    "active": dataset.active,
                    "tolerance": tolerance,
                },
            )
            result.datasets_updated.append(dataset.slug)

    _sync_entries(session, dataset, dataset_id, result)

    # E4: the spec gives entry_count a DEFAULT and never names a writer, so
    # left alone it reads 0 forever while the dataset has twenty entries --
    # and §14.3's queue-depth dashboards read exactly this sort of column.
    # Recomputed from the rows rather than incremented, so it cannot drift.
    session.execute(
        sql(
            """
            UPDATE golden_datasets
               SET entry_count = (
                     SELECT COUNT(*) FROM golden_dataset_entries
                      WHERE dataset_id = :id
                   )
             WHERE id = :id
            """
        ),
        {"id": dataset_id},
    )


def _sync_entries(
    session: Session, dataset: Dataset, dataset_id: object, result: SyncResult
) -> None:
    existing = {
        row.entry_key: row
        for row in session.execute(
            sql(
                """
                SELECT id, entry_key, entry_index, input, expected,
                       grading_kind, rubric, notes
                  FROM golden_dataset_entries WHERE dataset_id = :id
                """
            ),
            {"id": dataset_id},
        ).all()
    }

    for entry in dataset.entries:
        payload = {
            "dataset_id": dataset_id,
            "entry_key": entry.key,
            "entry_index": entry.index,
            "input": json.dumps(entry.input, sort_keys=True, default=str),
            "expected": json.dumps(entry.expected, sort_keys=True, default=str),
            "grading_kind": entry.grading_kind,
            "rubric": json.dumps(entry.rubric, sort_keys=True) if entry.rubric else None,
            "notes": entry.notes,
        }
        row = existing.pop(entry.key, None)

        if row is None:
            session.execute(
                sql(
                    """
                    INSERT INTO golden_dataset_entries
                        (dataset_id, entry_key, entry_index, input, expected,
                         grading_kind, rubric, notes)
                    VALUES
                        (:dataset_id, :entry_key, :entry_index,
                         CAST(:input AS jsonb), CAST(:expected AS jsonb),
                         :grading_kind, CAST(:rubric AS jsonb), :notes)
                    """
                ),
                payload,
            )
            result.entries_created.append(f"{dataset.slug}/{entry.key}")
            continue

        unchanged = (
            row.entry_index == entry.index
            and row.input == entry.input
            and row.expected == entry.expected
            and row.grading_kind == entry.grading_kind
            and (row.rubric or None) == (entry.rubric or None)
            and (row.notes or None) == (entry.notes or None)
        )
        if unchanged:
            continue

        session.execute(
            sql(
                """
                UPDATE golden_dataset_entries
                   SET entry_index = :entry_index,
                       input = CAST(:input AS jsonb),
                       expected = CAST(:expected AS jsonb),
                       grading_kind = :grading_kind,
                       rubric = CAST(:rubric AS jsonb),
                       notes = :notes
                 WHERE dataset_id = :dataset_id AND entry_key = :entry_key
                """
            ),
            payload,
        )
        result.entries_updated.append(f"{dataset.slug}/{entry.key}")

    # Whatever is left in `existing` was removed from the YAML.
    for key, row in existing.items():
        has_results = session.execute(
            sql("SELECT 1 FROM evaluation_results WHERE entry_id = :id LIMIT 1"),
            {"id": row.id},
        ).first()
        if has_results:
            result.orphans.append(f"{dataset.slug}/{key}")
            continue
        session.execute(
            sql("DELETE FROM golden_dataset_entries WHERE id = :id"), {"id": row.id}
        )
        result.entries_deleted.append(f"{dataset.slug}/{key}")


def retire(session: Session, slug: str, *, note: str) -> bool:
    """§7.3's retirement: deactivate a dataset, never delete it.

    "The old dataset's results remain queryable for historical comparison."
    Returns False when the dataset was already inactive, so the CLI can say so
    rather than reporting a change it did not make.
    """
    if not note.strip():
        raise ValueError(
            "a retirement note is required; §7.3 retires a dataset because the "
            "property it tested no longer applies, and that reason is the only "
            "record of why the coverage went away"
        )
    changed = session.execute(
        sql(
            """
            UPDATE golden_datasets
               SET active = FALSE,
                   description = description || E'\\n\\nRetired: ' || :note
             WHERE slug = :slug AND active
            """
        ),
        {"slug": slug, "note": note.strip()},
    )
    return bool(changed.rowcount)
