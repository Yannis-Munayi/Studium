"""Measure extractor quality (ingestion §7.4).

    python scripts/measure_extraction.py                 # print the table
    python scripts/measure_extraction.py --write         # update the baseline
    python scripts/measure_extraction.py --corpus DIR    # measure real PDFs

Run this after any change to the extractor, the normalizer, or the chunker, and
*look at the table* before committing the result. The freshness test will tell
you the baseline is stale; only you can tell whether the numbers moving is what
you intended.

Why the looking matters: the retrieval build measured three extractors over one
PDF and got chunk-size medians of 440, 113 and 124 tokens. Every downstream
parameter -- target size, overlap, the minimum below which a body chunk merges
-- was tuned against one of those distributions. A number that moves by a
factor of three is not a detail to accept because the test asked you to.

``--corpus`` is the mode that answers §18 open question 1. The default fixtures
are reportlab-generated and prove the harness works; they say nothing about
pdfplumber's quality on real academic typesetting. Point this at the actual
corpus to learn that.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from studium.ingestion.extract import get_extractor  # noqa: E402
from studium.ingestion.measure import (  # noqa: E402
    MIN_FIXTURE_SOURCES,
    SourceMeasurement,
    build_measurement,
    measure_pages,
    render,
    save,
)

BASELINE = BACKEND / "tests" / "ingestion" / "extraction_baseline.json"


async def _measure_pdf(
    path: Path, *, extractor_name: str | None
) -> SourceMeasurement:
    content = await get_extractor(extractor_name).extract(path)
    return measure_pages(
        path.stem,
        [(page.page_number, page.text) for page in content.pages],
        section_hierarchies=[page.section_hierarchy for page in content.pages],
    )


async def _measure_fixtures(extractor_name: str | None) -> list[SourceMeasurement]:
    """Build the synthetic fixture set into a temporary directory and measure it."""
    import tempfile

    from tests.fixtures import pdfs

    out: list[SourceMeasurement] = []
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        for name in sorted(pdfs.BUILDERS):
            try:
                path = pdfs.BUILDERS[name](directory / f"{name}.pdf")
            except Exception as exc:  # noqa: BLE001 -- a skipped fixture is not fatal
                print(f"  (skipping {name}: {exc})", file=sys.stderr)
                continue
            out.append(await _measure_pdf(path, extractor_name=extractor_name))
    return out


async def _measure_corpus(
    directory: Path, extractor_name: str | None
) -> list[SourceMeasurement]:
    pdf_paths = sorted(directory.glob("**/*.pdf"))
    if not pdf_paths:
        raise SystemExit(f"no PDFs under {directory}")
    return [await _measure_pdf(path, extractor_name=extractor_name) for path in pdf_paths]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="update the baseline file")
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="measure real PDFs under this directory instead of the fixtures",
    )
    parser.add_argument("--extractor", default=None)
    args = parser.parse_args(argv)

    if args.corpus:
        sources = asyncio.run(_measure_corpus(args.corpus, args.extractor))
    else:
        sources = asyncio.run(_measure_fixtures(args.extractor))

    measurement = build_measurement(
        extractor_version=get_extractor(args.extractor).version, sources=sources
    )
    print(render(measurement))

    if len(sources) < MIN_FIXTURE_SOURCES:
        print(
            f"\nwarning: {len(sources)} sources measured; §7.4 asks for at "
            f"least {MIN_FIXTURE_SOURCES}. A median over fewer describes one "
            f"document's typography, not the extractor.",
            file=sys.stderr,
        )

    if args.corpus and args.write:
        # The baseline is the synthetic set, deliberately. Writing real-corpus
        # numbers into it would make the freshness test depend on PDFs that are
        # not in the repository and, for most of them, cannot be.
        print(
            "\nrefusing to write a corpus measurement into the fixture "
            "baseline: the baseline must be reproducible from the repository. "
            "Record corpus numbers in the build report instead.",
            file=sys.stderr,
        )
        return 1

    if args.write:
        save(BASELINE, measurement)
        print(f"\nwrote {BASELINE.relative_to(BACKEND)}")

    if measurement.low_coverage:
        print(
            f"\n{len(measurement.low_coverage)} source(s) below the coverage "
            f"floor; §7.4 flags these for reviewer inspection.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
