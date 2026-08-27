"""Synthetic PDF fixtures for the ingestion tests (ingestion §16 "Fixture data").

Real PDFs, generated rather than committed. The spec asks for real PDF fixtures
from Tier 1, and generating them with reportlab gets that without a binary blob
in the repository -- and, more usefully, lets each fixture isolate exactly one
property of a document.

The edge cases here are the ones §16 names, and each exists because it broke
something or plausibly would:

* ``ligature_heavy`` -- the Michaelson defect. U+FB01 survives extraction as
  one codepoint, and a chunk carrying it is invisible to keyword search for the
  word it plainly contains.
* ``running_heads`` -- a document with a header and a page number on every
  page, which is what §8.1's furniture stripping exists for and what an
  exact-match heuristic silently fails to strip.
* ``two_column`` -- column detection, where a failure interleaves the columns
  and produces text that is grammatical nowhere.
* ``hyphenated`` -- words broken across lines by the typesetter, which index as
  two terms unless repaired.
* ``scanned`` -- a page with no text layer at all. Must be *rejected cleanly*
  rather than ingested as an empty source, which is the difference between a
  reviewer seeing "this needs OCR" and a learner seeing a subject that
  retrieves nothing.
* ``single_page`` / ``long`` -- the size extremes, where per-document
  heuristics that need several pages either fire on too little evidence or get
  slow.

These are not a substitute for the real corpus. §18 open question 1 stands:
pdfplumber's quality against Michaelson, Church and Selinger is unmeasured
until the §7.4 fixtures run against them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

reportlab = pytest.importorskip(
    "reportlab", reason="reportlab generates the ingestion PDF fixtures"
)

from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

BODY_FONT = "Helvetica"
HEADING_FONT = "Helvetica-Bold"
BODY_SIZE = 10
HEADING_SIZE = 16
LINE_HEIGHT = 14
TOP = 720
LEFT = 72


def _prose(page: int, line: int) -> str:
    """A line of plausible, page-distinct prose.

    Distinct per page on purpose: §8.1's furniture heuristic votes on lines
    that repeat, and body text that happened to be identical across pages
    would be stripped as a running head -- which is a property of the fixture,
    not of the normalizer, and would make the test assert the wrong thing.
    """
    topics = (
        "beta-reduction proceeds by substituting the argument into the body",
        "alpha-equivalence lets bound variables be renamed without changing meaning",
        "the Church-Rosser theorem guarantees confluence of reduction",
        "a redex is an application whose operator is an abstraction",
        "normal order reduction always finds a normal form when one exists",
    )
    return f"On page {page}, line {line}: {topics[line % len(topics)]}."


def _write_page(
    pdf: canvas.Canvas,
    *,
    heading: str | None = None,
    lines: list[str],
    header: str | None = None,
    footer: str | None = None,
) -> None:
    y = TOP
    if header:
        pdf.setFont(BODY_FONT, 9)
        pdf.drawString(LEFT, 760, header)
    if heading:
        pdf.setFont(HEADING_FONT, HEADING_SIZE)
        pdf.drawString(LEFT, y, heading)
        y -= LINE_HEIGHT * 2
    pdf.setFont(BODY_FONT, BODY_SIZE)
    for line in lines:
        pdf.drawString(LEFT, y, line)
        y -= LINE_HEIGHT
    if footer:
        pdf.setFont(BODY_FONT, 9)
        pdf.drawString(300, 40, footer)
    pdf.showPage()


def simple(path: Path, *, pages: int = 3, lines_per_page: int = 24) -> Path:
    """A well-behaved document: numbered headings, ordinary prose, no traps."""
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for page in range(1, pages + 1):
        _write_page(
            pdf,
            heading=f"{page}. Section {page}",
            lines=[_prose(page, line) for line in range(lines_per_page)],
        )
    pdf.save()
    return path


#: Fonts that carry glyphs for the whole U+FB00-FB06 block, by platform. The
#: base-14 fonts reportlab uses by default do *not*: asking Helvetica for
#: U+FB01 silently draws a different glyph, and the resulting PDF extracts to
#: "dennition" -- garbage that looks like an extractor bug and is a fixture
#: bug. A ligature fixture has to embed a TrueType font that actually has them.
_LIGATURE_FONT_CANDIDATES = (
    "C:/Windows/Fonts/calibri.ttf",
    "C:/Windows/Fonts/times.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Times New Roman.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
)

LIGATURE_BLOCK = tuple(chr(cp) for cp in range(0xFB00, 0xFB07))


def _ligature_font() -> str:
    """Register and return a font with real ligature glyphs, or skip.

    Coverage is *verified*, not assumed from the filename. A font that is
    present but lacks the block would produce the same silent garbage as the
    base-14 default, and a fixture that quietly stops testing what it claims to
    is worse than one that is skipped out loud.
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont as RLTTFont

    for candidate in _LIGATURE_FONT_CANDIDATES:
        if not Path(candidate).exists():
            continue
        try:
            font = RLTTFont("LigatureFont", candidate)
        except Exception:  # noqa: BLE001 -- an unreadable font is just not a candidate
            continue
        if not all(ord(ch) in font.face.charToGlyph for ch in LIGATURE_BLOCK):
            continue
        pdfmetrics.registerFont(font)
        return "LigatureFont"

    pytest.skip(
        "no installed font carries U+FB00-FB06; the ligature PDF fixture "
        "cannot be built. The character-level ligature behaviour is covered "
        "by tests/ingestion/test_normalize.py regardless."
    )


def ligature_heavy(path: Path, *, pages: int = 2) -> Path:
    """Source text carrying U+FB01 and friends.

    A caveat worth knowing before writing a test against this. The *source*
    text has real ligatures and the embedded font draws them, but reportlab
    writes a ToUnicode CMap that decomposes the glyph -- so an extractor reads
    back "fi", not U+FB01. Verified across Calibri, Times and Arial; it is a
    property of reportlab's PDF output, not of the font or the extractor.

    The Michaelson artefact, where the ligature codepoint survives into
    extracted text, comes from producers that map the glyph to the ligature
    itself (TeX, typically). This fixture therefore proves that ligature words
    round-trip to clean chunks; it does not reproduce the raw artefact. That
    lives in the character-level tests in ``test_normalize.py``.
    """
    font = _ligature_font()
    ligature_lines = [
        "The deﬁnition of a ﬁxed point is the ﬁrst thing to ﬁx.",
        "Eﬃcient aﬃxes suﬃce for the oﬃcial ﬂow.",
        "The ﬂag ﬂoats above the ﬁeld of ﬁnite ﬁgures.",
    ]
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for page in range(1, pages + 1):
        pdf.setFont(font, HEADING_SIZE)
        pdf.drawString(LEFT, TOP, f"{page}. Ligatures")
        pdf.setFont(font, BODY_SIZE)
        y = TOP - LINE_HEIGHT * 2
        for line in [*ligature_lines, *[_prose(page, n) for n in range(18)]]:
            pdf.drawString(LEFT, y, line)
            y -= LINE_HEIGHT
        pdf.showPage()
    pdf.save()
    return path


def running_heads(path: Path, *, pages: int = 8) -> Path:
    """Every page carries the same header and a changing page number."""
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for page in range(1, pages + 1):
        _write_page(
            pdf,
            heading=None,
            lines=[_prose(page, line) for line in range(24)],
            header="Michaelson - An Introduction to Functional Programming",
            footer=str(40 + page),
        )
    pdf.save()
    return path


def hyphenated(path: Path) -> Path:
    """Words broken across lines by hyphenation, plus one semantic hyphen.

    The semantic hyphen is the discriminator. A repair that joins every
    ``word-``/newline pair turns "Church-Rosser" into "ChurchRosser", a term
    that appears in no index and matches no query.
    """
    pdf = canvas.Canvas(str(path), pagesize=letter)
    _write_page(
        pdf,
        heading="1. Reduction",
        lines=[
            "The following is a defi-",
            "nition of normal order reduc-",
            "tion, which differs from the Church-",
            "Rosser property discussed earlier.",
            *[_prose(1, n) for n in range(16)],
        ],
    )
    pdf.save()
    return path


#: Column geometry. The gutter has to be wide enough that no line in the left
#: column reaches the right column's origin: two columns that physically
#: overlap are not a two-column document, they are one column of interleaved
#: characters, and a fixture built that way tests the extractor against a PDF
#: no typesetter would produce. Text is measured against this, not eyeballed.
_COLUMN_WIDTH = 200
_RIGHT_COLUMN_X = LEFT + _COLUMN_WIDTH + 40


def _fit(text: str, width: int = _COLUMN_WIDTH) -> str:
    """Truncate ``text`` to fit the column at the body size."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    while text and stringWidth(text, BODY_FONT, BODY_SIZE) > width:
        text = text[:-1]
    return text


def two_column(path: Path, *, pages: int = 2) -> Path:
    """A two-column layout, the common academic-paper case."""
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for page in range(1, pages + 1):
        pdf.setFont(HEADING_FONT, HEADING_SIZE)
        pdf.drawString(LEFT, TOP, f"{page}. Two Columns")
        pdf.setFont(BODY_FONT, BODY_SIZE)
        y = TOP - LINE_HEIGHT * 2
        for line in range(18):
            pdf.drawString(
                LEFT, y - line * LINE_HEIGHT, _fit(f"Left {_prose(page, line)}")
            )
            pdf.drawString(
                _RIGHT_COLUMN_X,
                y - line * LINE_HEIGHT,
                _fit(f"Right {_prose(page, line + 1)}"),
            )
        pdf.showPage()
    pdf.save()
    return path


def scanned(path: Path, *, pages: int = 2) -> Path:
    """A PDF with no text layer -- what a scan looks like to an extractor.

    Drawn as vector rectangles rather than an embedded raster, which produces
    the same thing that matters here (a page whose ``extract_text`` returns
    nothing) without a megabyte of image data per fixture.
    """
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for _ in range(pages):
        for row in range(20):
            pdf.rect(LEFT, TOP - row * 16, 400, 8, stroke=0, fill=1)
        pdf.showPage()
    pdf.save()
    return path


def single_page(path: Path) -> Path:
    """One page. Below the threshold at which cross-page heuristics mean anything."""
    pdf = canvas.Canvas(str(path), pagesize=letter)
    _write_page(
        pdf,
        heading="1. A Short Note",
        lines=[_prose(1, line) for line in range(20)],
    )
    pdf.save()
    return path


def tiny(path: Path) -> Path:
    """Under §6.3's 100-token floor. Must be rejected, not ingested empty."""
    pdf = canvas.Canvas(str(path), pagesize=letter)
    _write_page(pdf, lines=["A note."])
    pdf.save()
    return path


def long_document(path: Path, *, pages: int = 120) -> Path:
    """Long enough to exercise the multi-page paths without a slow test.

    §16 names ">500 pages" as the extreme. 120 is the same code path at a fifth
    of the runtime; the 500-page case belongs in the §7.4 measurement run
    against the real corpus, not in a suite that runs on every push.
    """
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for page in range(1, pages + 1):
        _write_page(
            pdf,
            heading=f"{page}. Chapter {page}" if page % 10 == 1 else None,
            lines=[_prose(page, line) for line in range(20)],
            header="A Long Document",
            footer=str(page),
        )
    pdf.save()
    return path


def not_a_pdf(path: Path) -> Path:
    """Bytes that are not a PDF, for the §14 415 case."""
    path.write_bytes(b"This is plainly not a PDF file.\n")
    return path


#: Name -> builder, for tests that sweep the whole set.
BUILDERS = {
    "simple": simple,
    "ligature_heavy": ligature_heavy,
    "running_heads": running_heads,
    "hyphenated": hyphenated,
    "two_column": two_column,
    "single_page": single_page,
}
