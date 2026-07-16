"""Parse the sample-curriculum material/ directory into a course catalog.

Two manifest shapes exist in the material tree:

  Shape A (1_turing_machine/_module.yaml):
      units:
        - title: "..."
          references: ["a.pdf", ...]

  Shape B (2_lambda_calculus, 3_clojure):
      units:
        - "2_lambda_calculus.pdf"     # filenames living under parts/

For shape B, unit titles come from parts/_parts.yaml chunk entries when
available, otherwise they are prettified from the filename.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Unit:
    key: str            # stable id, e.g. "2_lambda_calculus--2_lambda_calculus"
    title: str
    module_key: str
    pdf_paths: list[Path] = field(default_factory=list)


@dataclass
class Module:
    key: str            # directory name, e.g. "2_lambda_calculus"
    title: str
    units: list[Unit] = field(default_factory=list)


@dataclass
class Course:
    name: str
    title: str
    note: str
    modules: list[Module] = field(default_factory=list)

    def ordered_units(self) -> list[Unit]:
        return [u for m in self.modules for u in m.units]

    def find_unit(self, key: str) -> Unit | None:
        for unit in self.ordered_units():
            if unit.key == key:
                return unit
        return None


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "unit"


def _prettify(stem: str) -> str:
    return re.sub(r"^\d+_?", "", stem).replace("_", " ").strip().title()


def _load_part_titles(module_dir: Path) -> dict[str, str]:
    """Map part PDF filename -> chunk title from parts/_parts.yaml."""
    parts_yaml = module_dir / "parts" / "_parts.yaml"
    if not parts_yaml.exists():
        return {}
    data = yaml.safe_load(parts_yaml.read_text(encoding="utf-8"))
    titles: dict[str, str] = {}
    for chunk in data.get("chunks", []):
        if chunk.get("pdf") and chunk.get("title"):
            titles[chunk["pdf"]] = str(chunk["title"]).strip().title()
    return titles


def load_course(material_dir: Path) -> Course:
    manifest = yaml.safe_load(
        (material_dir / "_curriculum.yaml").read_text(encoding="utf-8")
    )
    course = Course(
        name=manifest.get("name", "course"),
        title=manifest.get("title", "Course"),
        note=(manifest.get("note") or "").strip(),
    )

    module_dirs = sorted(
        d for d in material_dir.iterdir()
        if d.is_dir() and (d / "_module.yaml").exists()
    )
    for module_dir in module_dirs:
        mod_manifest = yaml.safe_load(
            (module_dir / "_module.yaml").read_text(encoding="utf-8")
        )
        module = Module(key=module_dir.name, title=mod_manifest.get("title", module_dir.name))
        part_titles = _load_part_titles(module_dir)

        for entry in mod_manifest.get("units", []):
            if isinstance(entry, dict):  # shape A: title + references
                title = entry["title"]
                pdfs = [module_dir / ref for ref in entry.get("references", [])]
                key = f"{module.key}--{_slugify(title)}"
            else:  # shape B: bare filename under parts/
                filename = str(entry)
                pdf = module_dir / "parts" / filename
                if not pdf.exists():
                    pdf = module_dir / filename
                title = part_titles.get(filename) or _prettify(Path(filename).stem)
                pdfs = [pdf]
                key = f"{module.key}--{Path(filename).stem}"

            missing = [p for p in pdfs if not p.exists()]
            if missing:
                raise FileNotFoundError(
                    f"Unit '{title}' references missing PDF(s): "
                    + ", ".join(str(p) for p in missing)
                )
            module.units.append(
                Unit(key=key, title=title, module_key=module.key, pdf_paths=pdfs)
            )
        course.modules.append(module)

    return course
