"""Filesystem layout shared by the CLI and the web app."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATERIAL_DIR = ROOT / "material"
COURSEPACK_DIR = ROOT / "coursepack"
PROGRESS_DIR = ROOT / "progress"
WEB_DIR = Path(__file__).resolve().parent / "web"
