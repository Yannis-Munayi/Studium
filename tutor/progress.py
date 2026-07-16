"""Per-student progress persistence and the sequential mastery gate.

The gate is deterministic application code, not a model judgment: a unit is
accessible only if every earlier unit in course order has a passing attempt
(weighted rubric score >= PASS_THRESHOLD).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .catalog import Course, Unit

PASS_THRESHOLD = 0.75


class Progress:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    # -- persistence --------------------------------------------------------

    @classmethod
    def load(cls, progress_dir: Path, student: str) -> "Progress":
        path = progress_dir / f"{student}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {"student": student, "units": {}}
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    # -- attempts ------------------------------------------------------------

    def record_attempt(self, unit_key: str, score: float, passed: bool,
                       per_criterion: dict[str, int]) -> None:
        entry = self.data["units"].setdefault(unit_key, {"attempts": []})
        entry["attempts"].append({
            "when": datetime.now(timezone.utc).isoformat(),
            "score": round(score, 4),
            "passed": passed,
            "per_criterion": per_criterion,
        })
        self.save()

    def attempts(self, unit_key: str) -> list[dict]:
        return self.data["units"].get(unit_key, {}).get("attempts", [])

    def best_score(self, unit_key: str) -> float | None:
        scores = [a["score"] for a in self.attempts(unit_key)]
        return max(scores) if scores else None

    def has_passed(self, unit_key: str) -> bool:
        return any(a["passed"] for a in self.attempts(unit_key))

    # -- gating ---------------------------------------------------------------

    def is_unlocked(self, course: Course, unit: Unit) -> bool:
        for earlier in course.ordered_units():
            if earlier.key == unit.key:
                return True
            if not self.has_passed(earlier.key):
                return False
        return False

    def current_unit(self, course: Course) -> Unit | None:
        """First unit in course order without a passing attempt."""
        for unit in course.ordered_units():
            if not self.has_passed(unit.key):
                return unit
        return None
