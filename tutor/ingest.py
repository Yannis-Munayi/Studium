"""Ingestion pipeline: unit PDFs -> structured unit pack JSON.

One Claude call per unit. The PDF(s) go in as native document blocks; the
response is parsed and validated against the UnitPack schema, then written to
coursepack/<module>/<unit-key>.json. Runs once per unit — lessons and
assessments afterwards read the stored pack.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .catalog import Course, Unit
from .client import MODEL, get_client, pdf_block, usage_cost
from .models import UnitPack

INGEST_SYSTEM = """\
You are a curriculum designer converting textbook material into a structured,
self-paced lesson. Base everything STRICTLY on the attached document(s) — do
not import outside material beyond what is needed to explain the document's
own content clearly.

Produce a unit pack with:

- overview: 2-4 sentences on what this unit covers and why it matters.
- learning_objectives: 4-8 concrete "the student will be able to..." items.
- key_definitions: every term a student must be able to define precisely.
- segments: the lesson itself, in teaching order, like a patient lecture.
  Each segment is one idea, 150-350 words of markdown teaching text that
  explains, shows a worked example where the material allows, and connects to
  the previous segment. Cover the whole document — typically 6-12 segments.
  Attach a practice question to roughly every second segment: answerable from
  that segment alone, with a hint and a model answer.
- rubric: 5-8 criteria for a mastery test where the student explains concepts
  in their own words. Each criterion names one concept, lists the key_points a
  correct explanation must contain (specific facts/definitions from the
  document, not vague themes), and a weight (1 = supporting idea, 2 = core
  idea, 3 = the central concept of the unit).
"""


def pack_path(coursepack_dir: Path, unit: Unit) -> Path:
    return coursepack_dir / unit.module_key / f"{unit.key}.json"


def load_pack(coursepack_dir: Path, unit: Unit) -> UnitPack | None:
    path = pack_path(coursepack_dir, unit)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return UnitPack.model_validate(data["pack"])


def ingest_unit(client, unit: Unit, coursepack_dir: Path) -> tuple[UnitPack, str, float]:
    content = [pdf_block(p) for p in unit.pdf_paths]
    content.append({
        "type": "text",
        "text": (
            f"Create the unit pack for the unit titled '{unit.title}' "
            f"from the attached document(s)."
        ),
    })

    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=INGEST_SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_format=UnitPack,
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Unit '{unit.title}' output was truncated (max_tokens). "
            "Re-run ingest for this unit."
        )
    pack = response.parsed_output
    if pack is None:
        raise RuntimeError(f"Could not parse a unit pack for '{unit.title}'.")

    out = pack_path(coursepack_dir, unit)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "unit_key": unit.key,
                "title": unit.title,
                "module_key": unit.module_key,
                "model": MODEL,
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                "pack": pack.model_dump(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    summary, cost = usage_cost(response.usage)
    return pack, summary, cost


def ingest(course: Course, coursepack_dir: Path, *, module_key: str | None = None,
           unit_key: str | None = None, force: bool = False, log=print) -> None:
    client = get_client()
    targets = [
        u for u in course.ordered_units()
        if (module_key is None or u.module_key == module_key)
        and (unit_key is None or u.key == unit_key)
    ]
    if not targets:
        log("No units match that filter. Run `python -m tutor units` to list keys.")
        return

    total_cost = 0.0
    for unit in targets:
        if not force and pack_path(coursepack_dir, unit).exists():
            log(f"  skip (already ingested): {unit.key}")
            continue
        log(f"  ingesting: {unit.key} ...")
        pack, summary, cost = ingest_unit(client, unit, coursepack_dir)
        total_cost += cost
        log(
            f"    done — {len(pack.segments)} segments, "
            f"{len(pack.rubric)} rubric criteria | {summary} | ~${cost:.2f}"
        )
    log(f"Ingestion finished. Estimated API cost this run: ~${total_cost:.2f}")
