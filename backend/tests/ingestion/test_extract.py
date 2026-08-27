"""Extraction against real PDFs (ingestion §7, §16).

§16 asks for real PDF fixtures from Tier 1, and these are real PDFs -- built by
reportlab at test time rather than committed as binaries, so each one isolates
exactly one property of a document and the repository stays free of blobs.

What these tests cannot do on their own is settle §18 open question 1. A
synthetic PDF built by the same library on both sides of the test proves the
plumbing works, not that the extractor is good at academic typesetting.

That question has now been answered separately, by running
``scripts/measure_extraction.py --corpus`` against the real material — and the
answer is worth knowing before reading anything below. pdfplumber's *sizes*
were fine from the start (median 409 tokens against a 400 target on the
Michaelson book) while its *text* was unusable: the default word-split
tolerance ran whole paragraphs together into single tokens. No fixture here
could have caught that, because reportlab spaces its output normally. It took
the real corpus, which is the lesson SD7 records.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from studium.ingestion.extract import (
    MIN_DOCUMENT_TOKENS,
    ExtractionError,
    ExtractorUnavailable,
    PdfPlumberExtractor,
    available_extractors,
    get_extractor,
)
from studium.ingestion.normalize import normalize_document
from studium.retrieval.chunking import count_tokens
from tests.fixtures import pdfs

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _extract(path: Path):
    return asyncio.run(get_extractor().extract(path))


# --- the registry ----------------------------------------------------------


def test_pdfplumber_is_the_default(tmp_path: Path) -> None:
    """§7.3: pdfplumber for every source unless a reviewer says otherwise."""
    assert get_extractor().name == "pdfplumber"
    assert "pdfplumber" in available_extractors()


def test_an_unknown_extractor_is_refused_by_name() -> None:
    """Naming what is available beats "KeyError: 'marker'"."""
    with pytest.raises(ExtractorUnavailable, match="unknown extractor"):
        get_extractor("marker")


def test_unimplemented_alternatives_are_absent_not_stubbed() -> None:
    """§7.2's alternatives are named in the spec and not registered here.

    A registry entry that raises on use is worse than a missing key: it reads
    as supported at the call site and fails at ingestion time, after the
    upload, in a worker. A missing key fails at selection, immediately.
    """
    assert "marker" not in available_extractors()
    assert "unstructured" not in available_extractors()


def test_the_version_string_carries_the_library_version() -> None:
    """§5 addition 2's format: "pdfplumber/0.11.10".

    Both halves matter. The name says which extractor, the version says which
    build of it -- and a corpus-wide re-extraction has to distinguish "not
    processed by marker" from "processed by an older pdfplumber".
    """
    version = PdfPlumberExtractor().version
    assert version.startswith("pdfplumber/")
    assert version.split("/", 1)[1] not in {"", "unavailable"}


def test_a_missing_file_raises_extraction_error(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="no file at"):
        _extract(tmp_path / "absent.pdf")


# --- extraction over the fixture set ---------------------------------------


def test_a_simple_document_extracts(tmp_path: Path) -> None:
    content = _extract(pdfs.simple(tmp_path / "simple.pdf", pages=3))
    assert len(content.pages) == 3
    assert all(page.text.strip() for page in content.pages)
    assert count_tokens(content.text) > MIN_DOCUMENT_TOKENS


def test_headings_become_a_section_hierarchy(tmp_path: Path) -> None:
    """§7.1's font-size heuristic, and the reason section_path means anything.

    Without it every chunk files at the document root, and retrieval's
    citations lose the "Chapter 3 > 3.2" breadcrumb the hover card shows.
    """
    content = _extract(pdfs.simple(tmp_path / "simple.pdf", pages=3))
    assert content.pages[0].section_hierarchy == ("1. Section 1",)
    assert content.pages[2].section_hierarchy == ("3. Section 3",)


def test_the_hierarchy_carries_across_pages_without_headings(tmp_path: Path) -> None:
    """A page mid-section has no heading of its own and still belongs to it."""
    content = _extract(pdfs.long_document(tmp_path / "long.pdf", pages=12))
    # Headings land on pages 1 and 11 (every tenth). Page 5 is mid-chapter.
    assert content.pages[4].section_hierarchy, "page 5 lost its section"
    assert content.pages[4].section_hierarchy == content.pages[0].section_hierarchy


def test_no_ligature_reaches_a_chunk(tmp_path: Path) -> None:
    """The end-state invariant: whatever extraction yields, chunks are clean.

    A word is asserted rather than an absence alone, because "no ligature
    present" is also satisfied by a page that extracted to nothing.

    **What this cannot test, and why.** The Michaelson artefact is U+FB01
    arriving in *extracted* text, which reportlab-built PDFs cannot reproduce:
    reportlab writes a ToUnicode CMap that decomposes the ligature glyph, so
    the extractor sees "fi" no matter which font is embedded -- verified across
    Calibri, Times and Arial. The artefact comes from PDFs whose producer maps
    the glyph to the ligature codepoint itself, which is typical of TeX output
    and is what the Michaelson book is.

    So this asserts the invariant end-to-end on the path it can reach, the
    character-level behaviour is covered directly in
    ``tests/ingestion/test_normalize.py``, and the real-corpus case stays open
    under §18 question 1 until the §7.4 fixtures run against actual sources.
    """
    content = _extract(pdfs.ligature_heavy(tmp_path / "lig.pdf"))
    normalized = normalize_document([(p.page_number, p.text) for p in content.pages])
    joined = "\n".join(page.text for page in normalized.pages)

    assert "definition" in joined, "the fixture's ligature words did not survive"
    for ligature in pdfs.LIGATURE_BLOCK:
        assert ligature not in joined, f"{ligature!r} (U+{ord(ligature):04X}) reached a chunk"


def test_extraction_splits_words(tmp_path: Path) -> None:
    """Guards the word-split tolerance against a revert to pdfplumber's default.

    At the default tolerance of 3 the Michaelson book extracted with no word
    breaks at all -- "Itispossible,however,forbound..." -- which is invisible
    to every size and coverage metric and fatal to keyword search. This asserts
    the extracted text has ordinary word spacing, so a change back to the
    default fails here rather than in the corpus.

    The reportlab fixtures space normally at any tolerance, so this cannot
    reproduce the original defect; what it pins is that the tuned tolerance is
    still being applied and has not started splitting *within* words.
    """
    from studium.ingestion.extract import WORD_SPLIT_TOLERANCE

    assert WORD_SPLIT_TOLERANCE < 3, (
        "the word-split tolerance is back at or above pdfplumber's default, "
        "which ran whole paragraphs together on the real corpus"
    )

    content = _extract(pdfs.simple(tmp_path / "simple.pdf", pages=2))
    text = content.text
    ratio = text.count(" ") / len(text)
    assert 0.10 < ratio < 0.30, f"implausible space ratio {ratio:.3f}"
    # A tolerance set too low splits *inside* words rather than between them,
    # which the space ratio alone cannot distinguish from correct spacing. Long
    # words are where that shows first, so they are what is asserted.
    for word in ("substituting", "application", "abstraction", "confluence"):
        assert word in text, f"{word!r} did not survive extraction intact"


def test_a_scanned_pdf_extracts_to_nothing(tmp_path: Path) -> None:
    """§7.1: pdfplumber does not do OCR, and §14 rejects the source cleanly.

    The distinction that matters is between "empty" and "error". A scan is a
    valid PDF that this extractor cannot read, so extraction succeeds and
    returns nothing -- and the *pipeline* is what turns that into an
    extractor_failure a reviewer sees, rather than a source that is nominally
    ingested and retrieves nothing forever.
    """
    content = _extract(pdfs.scanned(tmp_path / "scan.pdf"))
    assert count_tokens(content.text) < MIN_DOCUMENT_TOKENS


def test_a_tiny_document_is_below_the_floor(tmp_path: Path) -> None:
    content = _extract(pdfs.tiny(tmp_path / "tiny.pdf"))
    assert count_tokens(content.text) < MIN_DOCUMENT_TOKENS


def test_a_single_page_document_extracts(tmp_path: Path) -> None:
    content = _extract(pdfs.single_page(tmp_path / "one.pdf"))
    assert len(content.pages) == 1
    assert count_tokens(content.text) > MIN_DOCUMENT_TOKENS


def test_a_two_column_layout_keeps_its_text(tmp_path: Path) -> None:
    """Column detection is §7.1's "works on most layouts".

    Asserting that the content is all present rather than that the reading
    order is perfect: interleaved columns are a known extractor weakness, and a
    test demanding perfect order would be asserting something pdfplumber does
    not promise. What must not happen is text going missing.
    """
    content = _extract(pdfs.two_column(tmp_path / "cols.pdf", pages=2))
    text = content.text
    assert "Left" in text and "Right" in text
    assert text.count("Left") >= 18


def test_extraction_is_deterministic(tmp_path: Path) -> None:
    """Same bytes, same output.

    The whole retry policy in §6.3 rests on this: a retry is only safe if it
    cannot produce a different corpus than the attempt it replaces.
    """
    path = pdfs.simple(tmp_path / "simple.pdf", pages=2)
    assert _extract(path).text == _extract(path).text


def test_page_numbers_are_one_based_and_contiguous(tmp_path: Path) -> None:
    """Off-by-one here silently corrupts every citation's page reference."""
    content = _extract(pdfs.simple(tmp_path / "simple.pdf", pages=5))
    assert [p.page_number for p in content.pages] == [1, 2, 3, 4, 5]


def test_confidence_defaults_to_one_for_pdfplumber(tmp_path: Path) -> None:
    """§5 addition 3: 1.0 means "no opinion", not "verified good"."""
    content = _extract(pdfs.simple(tmp_path / "simple.pdf", pages=2))
    assert all(page.confidence == 1.0 for page in content.pages)


def test_document_metadata_is_advisory_only(tmp_path: Path) -> None:
    """§11.4: metadata is a hint for the reviewer, never a classification input.

    Read and stored so a reviewer can use it. Nothing consumes it as a
    decision, which is what the licensing tests assert from the other side.
    """
    content = _extract(pdfs.simple(tmp_path / "simple.pdf", pages=1))
    assert isinstance(content.document_metadata, dict)


# --- the pieces normalisation depends on ----------------------------------


def test_running_heads_extract_then_strip(tmp_path: Path) -> None:
    """The two stages together, on the case §8.1's heuristic exists for."""
    content = _extract(pdfs.running_heads(tmp_path / "heads.pdf", pages=8))
    header = "Michaelson - An Introduction to Functional Programming"
    assert header in content.text, "the fixture did not carry a running head"

    normalized = normalize_document([(p.page_number, p.text) for p in content.pages])
    joined = "\n".join(page.text for page in normalized.pages)
    assert header not in joined
    assert "beta-reduction" in joined, "stripping took the body with it"


def test_hyphenated_line_breaks_extract_then_repair(tmp_path: Path) -> None:
    content = _extract(pdfs.hyphenated(tmp_path / "hyphen.pdf"))
    normalized = normalize_document([(p.page_number, p.text) for p in content.pages])
    joined = "\n".join(page.text for page in normalized.pages)

    assert "definition" in joined, "the typesetter's hyphen was not repaired"
    # And the semantic one survives: "ChurchRosser" appears in no index.
    assert "ChurchRosser" not in joined
