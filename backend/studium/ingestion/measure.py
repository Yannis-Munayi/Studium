"""Extractor quality measurement (ingestion §7.4).

**Any extractor swap requires measurement, not intuition.** The retrieval build
established why: three extractors over one PDF produced chunk-size medians of
440, 113 and 124 tokens. Nothing about reading their documentation would have
predicted the spread, and every downstream parameter -- chunk target size,
overlap ratio, the minimum below which a body chunk merges -- was tuned against
one of those distributions. Swapping the extractor without re-measuring means
those parameters now fit a distribution that no longer exists.

Three measurements, from §7.4:

* **Chunk-size distribution.** Median and quartiles across the fixture set. The
  headline number, and the one that moved three-fold.
* **Text-coverage ratio.** Extracted tokens over an independent count. Below
  80% means the extractor is dropping content, which is invisible in a chunk
  distribution that looks perfectly healthy for the text it did get.
* **Section-detection rate.** The fraction of chunks that carry a
  ``section_path``. A heading heuristic that silently stops firing costs
  retrieval its breadcrumbs and costs the chunker its preference-1 break point.

The freshness check is the part that makes this a discipline rather than a
script. A recorded measurement names the extractor version it was taken under;
if the code's version no longer matches, CI fails and asks for a re-run. That
is what stops an extractor upgrade landing with a stale number attached.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from studium.retrieval.chunking import Block, chunk_blocks, count_tokens

from .normalize import NORMALIZER_VERSION, normalize_document

#: §7.4: below this the source is flagged for reviewer inspection. Coverage is
#: a ratio against an independent token count, so 1.0 is parity and not
#: perfection -- an extractor that pulls in more than the reference (tables the
#: reference skipped, say) can legitimately exceed it.
MIN_COVERAGE_RATIO = 0.80

#: §7.4 asks for at least five real sources. Fewer than this and a median is
#: describing one document's typography rather than a corpus.
MIN_FIXTURE_SOURCES = 5


@dataclass(frozen=True, slots=True)
class SourceMeasurement:
    name: str
    pages: int
    extracted_tokens: int
    reference_tokens: int
    chunks: int
    chunk_token_median: float
    chunk_token_q1: float
    chunk_token_q3: float
    sectioned_chunk_ratio: float

    @property
    def coverage_ratio(self) -> float:
        if not self.reference_tokens:
            return 0.0
        return self.extracted_tokens / self.reference_tokens

    @property
    def covers_enough(self) -> bool:
        return self.coverage_ratio >= MIN_COVERAGE_RATIO


@dataclass(frozen=True, slots=True)
class Measurement:
    """One run of the §7.4 measurements, tied to the versions that produced it."""

    extractor_version: str
    normalizer_version: str
    sources: tuple[SourceMeasurement, ...] = field(default_factory=tuple)

    @property
    def chunk_token_median(self) -> float:
        """The headline number: the median across every chunk of every source.

        Pooled across sources rather than a median of per-source medians. The
        pooled figure is what the chunker's target size is actually compared
        against; averaging medians would weight a 3-page paper equally with a
        400-page book.
        """
        medians = [s.chunk_token_median for s in self.sources if s.chunks]
        return statistics.median(medians) if medians else 0.0

    @property
    def low_coverage(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.sources if not s.covers_enough)

    def to_json(self) -> str:
        return json.dumps(
            {
                "extractor_version": self.extractor_version,
                "normalizer_version": self.normalizer_version,
                "sources": [asdict(s) for s in self.sources],
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> Measurement:
        data = json.loads(text)
        return cls(
            extractor_version=data["extractor_version"],
            normalizer_version=data["normalizer_version"],
            sources=tuple(SourceMeasurement(**s) for s in data.get("sources", [])),
        )


def measure_pages(
    name: str,
    pages: Sequence[tuple[int, str]],
    *,
    reference_tokens: int | None = None,
    section_hierarchies: Sequence[Sequence[str]] | None = None,
) -> SourceMeasurement:
    """Measure one source, from extracted pages through to chunks.

    ``reference_tokens`` is the independent count §7.4 asks for -- ``pdftotext``
    output, or another extractor's. Without one, coverage is measured against
    the raw extracted text, which answers a narrower question: how much the
    *normalizer* dropped. That is still worth knowing (it catches
    over-stripping) but it cannot catch an extractor that never saw half the
    document, so the caller should supply a real reference where it can.

    ``section_hierarchies`` must be passed for the section-detection figure to
    mean anything. Omitting it does not measure "no sections were detected" --
    it measures nothing, and reports 0.00 either way. The caller has the
    hierarchies from the extractor; dropping them here silently turned §7.4's
    third measurement into a constant.
    """
    raw_tokens = sum(count_tokens(text) for _, text in pages)
    normalized = normalize_document(pages, section_hierarchies=section_hierarchies)

    blocks: list[Block] = []
    for page in normalized.pages:
        for paragraph in page.text.split("\n\n"):
            if paragraph.strip():
                blocks.append(
                    Block(
                        text=paragraph.strip(),
                        section_path=page.section_hierarchy,
                        page=page.page_number,
                    )
                )

    chunks = chunk_blocks(blocks)
    sizes = sorted(chunk.token_count for chunk in chunks)
    sectioned = sum(1 for chunk in chunks if chunk.section_path)

    return SourceMeasurement(
        name=name,
        pages=len(normalized.pages),
        extracted_tokens=sum(count_tokens(p.text) for p in normalized.pages),
        reference_tokens=reference_tokens if reference_tokens is not None else raw_tokens,
        chunks=len(chunks),
        chunk_token_median=_quantile(sizes, 0.5),
        chunk_token_q1=_quantile(sizes, 0.25),
        chunk_token_q3=_quantile(sizes, 0.75),
        sectioned_chunk_ratio=round(sectioned / len(chunks), 3) if chunks else 0.0,
    )


def _quantile(sorted_values: Sequence[int], q: float) -> float:
    """Nearest-rank quantile, defined for the tiny samples fixtures produce.

    ``statistics.quantiles`` needs at least two data points and interpolates;
    both are wrong here. A one-chunk source is a legitimate measurement, and an
    interpolated token count is not a chunk size anything ever had.
    """
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1)))))
    return float(sorted_values[index])


def build_measurement(
    *, extractor_version: str, sources: Sequence[SourceMeasurement]
) -> Measurement:
    return Measurement(
        extractor_version=extractor_version,
        normalizer_version=NORMALIZER_VERSION,
        sources=tuple(sources),
    )


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    fresh: bool
    reason: str = ""


def check_freshness(
    recorded: Measurement, *, extractor_version: str, normalizer_version: str
) -> FreshnessResult:
    """§7.4's CI check: a recorded measurement must match the current code.

    "An extractor change without a measurement update fails a CI check that
    reads the measurement fixture and asserts freshness." This is that check.

    Both versions matter. The extractor decides what text exists; the
    normalizer decides what survives to be chunked. A normalizer change that
    added a transformation would move the chunk distribution just as surely as
    an extractor swap, and a measurement that named only the extractor would
    still look current.
    """
    if recorded.extractor_version != extractor_version:
        return FreshnessResult(
            fresh=False,
            reason=(
                f"measurement was taken under {recorded.extractor_version!r} "
                f"and the code is running {extractor_version!r}. §7.4: an "
                f"extractor change requires re-measurement, not intuition -- "
                f"three extractors over one PDF differed three-fold in median "
                f"chunk size. Re-run scripts/measure_extraction.py."
            ),
        )
    if recorded.normalizer_version != normalizer_version:
        return FreshnessResult(
            fresh=False,
            reason=(
                f"measurement was taken under {recorded.normalizer_version!r} "
                f"and the code is running {normalizer_version!r}. A "
                f"normalisation change moves the chunk distribution as surely "
                f"as an extractor change. Re-run scripts/measure_extraction.py."
            ),
        )
    return FreshnessResult(fresh=True)


def load(path: Path) -> Measurement | None:
    if not path.exists():
        return None
    return Measurement.from_json(path.read_text(encoding="utf-8"))


def save(path: Path, measurement: Measurement) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(measurement.to_json() + "\n", encoding="utf-8")
    return path


def render(measurement: Measurement) -> str:
    """A human-readable table, for the script and for a failing test's message."""
    lines = [
        f"extractor:  {measurement.extractor_version}",
        f"normalizer: {measurement.normalizer_version}",
        "",
        f"{'source':28} {'pages':>5} {'chunks':>7} {'median':>7} "
        f"{'q1':>5} {'q3':>5} {'cover':>6} {'sect':>5}",
    ]
    for source in measurement.sources:
        lines.append(
            f"{source.name[:28]:28} {source.pages:5} {source.chunks:7} "
            f"{source.chunk_token_median:7.0f} {source.chunk_token_q1:5.0f} "
            f"{source.chunk_token_q3:5.0f} {source.coverage_ratio:6.2f} "
            f"{source.sectioned_chunk_ratio:5.2f}"
        )
    lines.append("")
    lines.append(f"pooled median chunk size: {measurement.chunk_token_median:.0f} tokens")
    if measurement.low_coverage:
        lines.append(
            f"below {MIN_COVERAGE_RATIO:.0%} coverage: "
            f"{', '.join(measurement.low_coverage)}"
        )
    return "\n".join(lines)


def as_dict(measurement: Measurement) -> dict[str, Any]:
    return json.loads(measurement.to_json())
