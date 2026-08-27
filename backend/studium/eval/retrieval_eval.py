"""Retrieval quality measurement (evaluation §9).

Retrieval is the one subsystem whose quality is a judgment call: "did it return
the right passages" needs someone to have said what right means. §9's datasets
encode that judgment as two labelled sets per query -- relevant, and misleading
-- and this module turns a retrieval result plus those sets into numbers.

Pure functions over ids. The retriever is called by :mod:`studium.eval.runner`;
nothing here touches a database or a provider, which is what lets §17 put the
metric arithmetic in Tier 1 and the A/B in Tier 3.

**Two departures from §9.2's formulas, both because the formula as written
measures something other than what it is named.** See DIVERGENCES-EVALUATION
(E8):

*Precision@k divides by the number retrieved, not by k.* §9.2 says
"|retrieved ∩ relevant| / k". Retrieval routinely returns fewer than k --
retrieval §13 makes thin grounding a normal, flagged state of an evolving
corpus, not an error -- and dividing by k charges a retriever for passages that
do not exist. A query where the corpus holds exactly two relevant chunks and
retrieval returns both scores 2/6 = 0.33 under the spec's formula and 1.0 under
this one. The second is what precision means, and the first would make
"improve the corpus" and "return more junk" look identical to the metric.

*Recall@k divides by the size of the relevant set*, which is what §9.2's
formula says and the opposite of what its prose says ("Of the k retrieved
chunks, how many are in the relevant list?"). The formula is right; the prose
describes precision. Following the formula.

**"Drop by more than 5%" (§9.4) is read as absolute**, matching the same
decision in :mod:`studium.eval.regression` for §13.2. Both specs write a
tolerance as a bare number against a metric that is already a proportion, and
one reading for both beats two readings that differ by which section you are
in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

#: §9.3's A/B runs at k=6, matching retrieval §6's default and the Lecturer's
#: actual request.
DEFAULT_K = 6

#: §9.4's provider-swap tolerance. "Arbitrary threshold, revisitable" -- the
#: spec's own words, so it lives here as one named constant.
PROVIDER_SWAP_MAX_DROP = 0.05


@dataclass(frozen=True, slots=True)
class Judgment:
    """One entry's labelled sets (§9.1's two-tier judgment).

    Chunks in neither set are "non-harmful adjacent material": they do not
    count for recall and do not count against the misleading rate. That third,
    unnamed tier is why precision and the misleading rate are separate numbers
    -- precision punishes an adjacent chunk exactly as hard as an off-topic
    one, and §9.2 wants the difference visible.
    """

    relevant: frozenset[str]
    misleading: frozenset[str] = frozenset()
    #: Chunk ids whose curated role matches the requested stance, for §9.2's
    #: "curated preference". Empty when the entry does not specify one.
    stance_appropriate: frozenset[str] = frozenset()

    @classmethod
    def from_expected(cls, expected: Mapping[str, object]) -> Judgment:
        return cls(
            relevant=frozenset(str(c) for c in (expected.get("relevant_chunks") or [])),
            misleading=frozenset(
                str(c) for c in (expected.get("misleading_chunks") or [])
            ),
            stance_appropriate=frozenset(
                str(c) for c in (expected.get("stance_appropriate_chunks") or [])
            ),
        )


@dataclass(frozen=True, slots=True)
class EntryMetrics:
    """§9.2's four metrics for one query."""

    recall: float
    precision: float
    misleading_rate: float
    #: Reciprocal rank of the first stance-appropriate chunk, or ``None`` when
    #: the entry specified no stance preference. ``None`` rather than 0.0 so an
    #: entry that expressed no preference does not drag the mean down as though
    #: retrieval had failed a test nobody set.
    curated_preference: float | None = None
    retrieved: int = 0

    @property
    def score(self) -> float:
        """A single 0-1 number for ``evaluation_results.score``.

        The mean of recall and precision, less the misleading rate. §6.2 is
        explicit that no single number summarises quality and that attempts to
        make one mislead -- so this is *not* offered as a quality score. It
        exists because ``evaluation_results.score`` is ``NOT NULL`` and §13.2's
        gate needs something to average. The four real metrics survive
        alongside it in ``grading_notes`` and in :func:`aggregate`, which is
        what the reviewer and the dashboard read.
        """
        base = (self.recall + self.precision) / 2
        return max(0.0, min(1.0, base - self.misleading_rate))

    def render(self) -> str:
        parts = [
            f"recall={self.recall:.2f}",
            f"precision={self.precision:.2f}",
            f"misleading={self.misleading_rate:.2f}",
        ]
        if self.curated_preference is not None:
            parts.append(f"stance_rr={self.curated_preference:.2f}")
        parts.append(f"n_retrieved={self.retrieved}")
        return " ".join(parts)


def evaluate_entry(
    retrieved: Sequence[str], judgment: Judgment, *, k: int = DEFAULT_K
) -> EntryMetrics:
    """Score one retrieval result against its judgment.

    ``retrieved`` is chunk ids **in rank order** -- which is not the order
    ``RetrievalResult.passages`` comes in. Retrieval §12 sorts passages by
    ``chunk_id`` so citation numbering is byte-stable for the prompt cache, and
    rank survives on ``relevance_score``. Ranking matters for exactly one of
    these four metrics (curated preference), so the caller re-sorts by score
    before calling this; :func:`ranked_ids` is that re-sort.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")

    top = [str(c) for c in retrieved[:k]]
    hits = [c for c in top if c in judgment.relevant]

    recall = len(hits) / len(judgment.relevant) if judgment.relevant else 0.0
    # Divided by what was actually returned, not by k -- see the module
    # docstring. An empty result is precision 0.0 rather than a ZeroDivision:
    # returning nothing is a real answer retrieval is allowed to give.
    precision = len(hits) / len(top) if top else 0.0
    misleading = (
        len([c for c in top if c in judgment.misleading]) / len(top) if top else 0.0
    )

    preference: float | None = None
    if judgment.stance_appropriate:
        preference = 0.0
        for rank, chunk in enumerate(top, start=1):
            if chunk in judgment.stance_appropriate:
                preference = 1.0 / rank
                break

    return EntryMetrics(
        recall=min(1.0, recall),
        precision=precision,
        misleading_rate=misleading,
        curated_preference=preference,
        retrieved=len(top),
    )


def ranked_ids(passages: Sequence[object]) -> list[str]:
    """Chunk ids in descending relevance, from a ``RetrievalResult``'s passages.

    ``RetrievalResult.passages`` is sorted by ``chunk_id`` (retrieval §12's
    citation-numbering contract), so reading it in order would score a
    reranker's output as though the reranker had never run -- and §9.3 exists
    specifically to measure whether the reranker changes the ordering. Passages
    with no score sort last, preserving their relative order.
    """
    scored = list(enumerate(passages))
    scored.sort(
        key=lambda pair: (
            -(getattr(pair[1], "relevance_score", None) or 0.0),
            pair[0],
        )
    )
    return [str(getattr(p, "chunk_id", "")) for _, p in scored]


@dataclass(frozen=True, slots=True)
class Aggregate:
    """§9.3 step 3: mean per metric across a dataset."""

    recall: float = 0.0
    precision: float = 0.0
    misleading_rate: float = 0.0
    curated_preference: float | None = None
    entries: int = 0

    def render(self) -> str:
        lines = [
            f"  mean recall@k       {self.recall:.4f}",
            f"  mean precision@k    {self.precision:.4f}",
            f"  mean misleading     {self.misleading_rate:.4f}",
        ]
        if self.curated_preference is not None:
            lines.append(f"  mean stance rank rr {self.curated_preference:.4f}")
        lines.append(f"  entries             {self.entries}")
        return "\n".join(lines)


def aggregate(results: Sequence[EntryMetrics]) -> Aggregate:
    if not results:
        return Aggregate()
    preferences = [r.curated_preference for r in results if r.curated_preference is not None]
    return Aggregate(
        recall=sum(r.recall for r in results) / len(results),
        precision=sum(r.precision for r in results) / len(results),
        misleading_rate=sum(r.misleading_rate for r in results) / len(results),
        curated_preference=(
            sum(preferences) / len(preferences) if preferences else None
        ),
        entries=len(results),
    )


# --- §9.3 reranker A/B -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ABResult:
    """§9.3's answer to retrieval §19 open question 3.

    Deliberately reports the deltas and refuses to draw the conclusion. §9.3
    step 4 gives that decision to the reviewer ("decides whether the improvement
    justifies the reranker's per-call cost"), and a per-call cost is not
    something this module can see.
    """

    with_rerank: Aggregate
    without_rerank: Aggregate
    #: Per-entry recall deltas, so a mean improvement that is really one entry
    #: moving a long way is visible as such.
    per_entry_recall_delta: tuple[float, ...] = ()

    @property
    def recall_delta(self) -> float:
        return self.with_rerank.recall - self.without_rerank.recall

    @property
    def precision_delta(self) -> float:
        return self.with_rerank.precision - self.without_rerank.precision

    @property
    def misleading_delta(self) -> float:
        return self.with_rerank.misleading_rate - self.without_rerank.misleading_rate

    @property
    def entries_improved(self) -> int:
        return sum(1 for d in self.per_entry_recall_delta if d > 0)

    @property
    def entries_worsened(self) -> int:
        return sum(1 for d in self.per_entry_recall_delta if d < 0)

    def render(self) -> str:
        return "\n".join(
            [
                "with rerank:",
                self.with_rerank.render(),
                "without rerank (raw fusion):",
                self.without_rerank.render(),
                "",
                f"  recall     {self.recall_delta:+.4f}",
                f"  precision  {self.precision_delta:+.4f}",
                f"  misleading {self.misleading_delta:+.4f} (negative is better)",
                f"  entries improved/worsened: "
                f"{self.entries_improved}/{self.entries_worsened} "
                f"of {self.with_rerank.entries}",
                "",
                "§9.3 step 4 leaves the call to the reviewer: is this worth the",
                "reranker's per-call cost? The measurement is repeatable as the",
                "corpus grows or a new reranker appears.",
            ]
        )


def compare_reranker(
    with_rerank: Sequence[EntryMetrics], without_rerank: Sequence[EntryMetrics]
) -> ABResult:
    """§9.3 steps 3-4. Both sequences must be the same entries in the same order."""
    if len(with_rerank) != len(without_rerank):
        raise ValueError(
            f"A/B needs the same entries on both arms: "
            f"{len(with_rerank)} vs {len(without_rerank)}"
        )
    return ABResult(
        with_rerank=aggregate(with_rerank),
        without_rerank=aggregate(without_rerank),
        per_entry_recall_delta=tuple(
            a.recall - b.recall for a, b in zip(with_rerank, without_rerank, strict=True)
        ),
    )


# --- §9.4 provider swap gate -----------------------------------------------


@dataclass(frozen=True, slots=True)
class SwapGate:
    """Whether a retrieval provider swap may proceed (§9.4)."""

    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    incumbent: Aggregate = field(default_factory=Aggregate)
    candidate: Aggregate = field(default_factory=Aggregate)

    def render(self) -> str:
        head = "swap allowed" if self.allowed else "swap BLOCKED"
        lines = [head, "incumbent:", self.incumbent.render(), "candidate:", self.candidate.render()]
        lines.extend(f"  - {reason}" for reason in self.reasons)
        return "\n".join(lines)


def provider_swap_gate(
    incumbent: Aggregate,
    candidate: Aggregate,
    *,
    max_drop: float = PROVIDER_SWAP_MAX_DROP,
) -> SwapGate:
    """§9.4: recall and precision may not drop past ``max_drop``; misleading
    may not rise at all.

    The asymmetry is the spec's and it is right: a small recall loss is a
    trade a reviewer can weigh against price or availability, while more
    nearby-but-wrong content reaching learners is the failure mode retrieval
    §13 exists to catch. There is no exchange rate between them.
    """
    reasons: list[str] = []

    for name, before, after in (
        ("recall@6", incumbent.recall, candidate.recall),
        ("precision@6", incumbent.precision, candidate.precision),
    ):
        drop = before - after
        if drop > max_drop:
            reasons.append(
                f"mean {name} dropped {drop:.4f} ({before:.4f} -> {after:.4f}), "
                f"past the {max_drop} tolerance"
            )

    if candidate.misleading_rate > incumbent.misleading_rate:
        reasons.append(
            f"misleading rate rose {incumbent.misleading_rate:.4f} -> "
            f"{candidate.misleading_rate:.4f}; §9.4 permits no increase"
        )

    if candidate.entries == 0 or candidate.entries != incumbent.entries:
        # A gate run over a different entry set is comparing two different
        # questions, and would pass or fail for reasons unrelated to the
        # provider.
        reasons.append(
            f"entry counts differ ({incumbent.entries} vs {candidate.entries}); "
            f"both arms must run the same dataset"
        )

    return SwapGate(
        allowed=not reasons,
        reasons=tuple(reasons),
        incumbent=incumbent,
        candidate=candidate,
    )
