"""YAML authoring and graph validation, Tier 1 (ingestion §9, §10, §16).

Parsing and validation only -- everything here runs without a database. The
import paths that write rows are Tier 2, in ``tests/online/test_ingestion_db``.

The split matters for what these tests are for. Validation is the gate between
a domain expert's afternoon of YAML and a graph the Curator has to sequence, so
the interesting cases are the malformed ones: what is rejected, what is merely
warned about, and whether the message tells the author which line to fix.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from studium.ingestion.authoring import (
    AuthoringError,
    _find_cycle,
    _overlaps_pages,
    graph_warnings,
    load_yaml,
    parse_concept_sources,
    parse_graph,
    parse_rubrics,
    yaml_files,
)

VALID_GRAPH = """
subject:
  slug: lambda-calculus
  title: Lambda Calculus
  version: 1
  short_description: An introduction to the untyped lambda calculus.

concepts:
  - slug: syntax
    title: "Lambda terms"
    depth: 1
    is_load_bearing: true
    estimated_minutes: 40
    module: foundations
  - slug: alpha-equivalence
    title: Alpha-equivalence
    depth: 2
    is_load_bearing: true
  - slug: beta-reduction
    title: Beta-reduction
    depth: 2

edges:
  - from: syntax
    to: alpha-equivalence
    kind: prerequisite
  - from: alpha-equivalence
    to: beta-reduction
    kind: prerequisite
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --- parsing ---------------------------------------------------------------


def test_a_valid_graph_parses(tmp_path: Path) -> None:
    graph = parse_graph([_write(tmp_path, "graph.yaml", VALID_GRAPH)])
    assert graph.slug == "lambda-calculus"
    assert len(graph.concepts) == 3
    assert len(graph.edges) == 2


def test_malformed_yaml_names_the_file(tmp_path: Path) -> None:
    """§16: malformed files raise with specific error messages.

    "invalid YAML" with no file and no line is what turns a typo into ten
    minutes of bisecting a content directory.
    """
    path = _write(tmp_path, "broken.yaml", "subject:\n  slug: [unclosed\n")
    with pytest.raises(AuthoringError) as excinfo:
        load_yaml(path)
    assert "broken.yaml" in str(excinfo.value)


def test_an_empty_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AuthoringError, match="empty"):
        load_yaml(_write(tmp_path, "empty.yaml", ""))


def test_a_top_level_list_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AuthoringError, match="mapping"):
        load_yaml(_write(tmp_path, "list.yaml", "- one\n- two\n"))


def test_yaml_files_are_ordered_stably(tmp_path: Path) -> None:
    """An import diff is read in a terminal and compared against the last run.

    Filesystem order varies between machines; a diff that reorders itself
    cannot be reviewed.
    """
    for name in ("c.yaml", "a.yaml", "b.yml"):
        _write(tmp_path, name, "subject: {}\n")
    assert [p.name for p in yaml_files(tmp_path)] == ["a.yaml", "b.yml", "c.yaml"]


def test_a_missing_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(AuthoringError, match="no such directory"):
        yaml_files(tmp_path / "nope")


def test_a_graph_split_across_files_is_assembled(tmp_path: Path) -> None:
    """§9.1 allows splitting a large graph by module."""
    first = _write(
        tmp_path,
        "01-subject.yaml",
        """
        subject:
          slug: lambda-calculus
          title: Lambda Calculus
        concepts:
          - slug: syntax
            title: Syntax
        """,
    )
    second = _write(
        tmp_path,
        "02-reductions.yaml",
        """
        concepts:
          - slug: beta-reduction
            title: Beta-reduction
        edges:
          - from: syntax
            to: beta-reduction
            kind: prerequisite
        """,
    )
    graph = parse_graph([first, second])
    assert {c["slug"] for c in graph.concepts} == {"syntax", "beta-reduction"}


def test_two_subject_blocks_are_rejected(tmp_path: Path) -> None:
    """Split files must not each define the subject, or they can disagree."""
    files = [
        _write(tmp_path, "a.yaml", VALID_GRAPH),
        _write(tmp_path, "b.yaml", VALID_GRAPH),
    ]
    with pytest.raises(AuthoringError, match="second 'subject:'"):
        parse_graph(files)


def test_no_subject_block_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AuthoringError, match="no 'subject:'"):
        parse_graph([_write(tmp_path, "a.yaml", "concepts:\n  - slug: x\n    title: X\n")])


# --- §9.2 validation: hard errors ------------------------------------------


def test_a_cycle_is_a_hard_error(tmp_path: Path) -> None:
    """§9.2 step 3, §14. The Curator's sequencing walk would not terminate."""
    body = """
    subject:
      slug: demo
      title: Demo
    concepts:
      - {slug: alpha, title: Alpha}
      - {slug: beta, title: Beta}
      - {slug: gamma, title: Gamma}
    edges:
      - {from: alpha, to: beta, kind: prerequisite}
      - {from: beta, to: gamma, kind: prerequisite}
      - {from: gamma, to: alpha, kind: prerequisite}
    """
    with pytest.raises(AuthoringError) as excinfo:
        parse_graph([_write(tmp_path, "cycle.yaml", body)])
    message = str(excinfo.value)
    assert "cycle" in message
    # §14: "reviewer sees the cycle path". Naming the members is the difference
    # between a fixable report and "there is a cycle somewhere".
    assert "alpha" in message and "beta" in message and "gamma" in message


def test_a_self_edge_is_rejected(tmp_path: Path) -> None:
    body = """
    subject: {slug: demo, title: Demo}
    concepts:
      - {slug: alpha, title: Alpha}
    edges:
      - {from: alpha, to: alpha, kind: prerequisite}
    """
    with pytest.raises(AuthoringError, match="self-edge"):
        parse_graph([_write(tmp_path, "self.yaml", body)])


def test_a_duplicate_slug_is_rejected(tmp_path: Path) -> None:
    body = """
    subject: {slug: demo, title: Demo}
    concepts:
      - {slug: alpha, title: Alpha}
      - {slug: alpha, title: Also Alpha}
    """
    with pytest.raises(AuthoringError, match="duplicate concept slug"):
        parse_graph([_write(tmp_path, "dup.yaml", body)])


def test_a_duplicate_edge_is_rejected(tmp_path: Path) -> None:
    body = """
    subject: {slug: demo, title: Demo}
    concepts:
      - {slug: alpha, title: Alpha}
      - {slug: beta, title: Beta}
    edges:
      - {from: alpha, to: beta, kind: prerequisite}
      - {from: alpha, to: beta, kind: prerequisite}
    """
    with pytest.raises(AuthoringError, match="duplicate edge"):
        parse_graph([_write(tmp_path, "dup.yaml", body)])


def test_an_edge_to_a_missing_concept_is_rejected(tmp_path: Path) -> None:
    body = """
    subject: {slug: demo, title: Demo}
    concepts:
      - {slug: alpha, title: Alpha}
    edges:
      - {from: alpha, to: ghost, kind: prerequisite}
    """
    with pytest.raises(AuthoringError, match="not a concept in this graph"):
        parse_graph([_write(tmp_path, "ghost.yaml", body)])


def test_an_out_of_range_depth_is_rejected(tmp_path: Path) -> None:
    body = """
    subject: {slug: demo, title: Demo}
    concepts:
      - {slug: alpha, title: Alpha, depth: 9}
    """
    with pytest.raises(AuthoringError, match="depth"):
        parse_graph([_write(tmp_path, "depth.yaml", body)])


def test_every_problem_is_reported_at_once(tmp_path: Path) -> None:
    """One error per run turns a 60-concept graph into 60 edit-run cycles."""
    body = """
    subject: {slug: demo, title: Demo}
    concepts:
      - {slug: alpha, title: Alpha, depth: 9}
      - {slug: alpha, title: Dup}
      - {slug: gamma}
    """
    with pytest.raises(AuthoringError) as excinfo:
        parse_graph([_write(tmp_path, "many.yaml", body)])
    assert len(excinfo.value.problems) >= 3


# --- §9.2 validation: warnings, not errors --------------------------------


def test_an_orphan_concept_warns_but_does_not_block(tmp_path: Path) -> None:
    """§9.2 step 4, §14: a warning, so a graph can be built incrementally.

    An orphan is usually a concept the author has not connected *yet*.
    Blocking would mean the graph could only ever be imported complete.
    """
    body = """
    subject: {slug: demo, title: Demo}
    concepts:
      - {slug: alpha, title: Alpha}
      - {slug: beta, title: Beta}
      - {slug: lonely, title: Lonely}
    edges:
      - {from: alpha, to: beta, kind: prerequisite}
    """
    graph = parse_graph([_write(tmp_path, "orphan.yaml", body)])
    warnings = graph_warnings(graph)
    assert any("lonely" in w for w in warnings)


def test_a_deep_concept_without_groundwork_warns(tmp_path: Path) -> None:
    """§9.2 step 5: a learner could reach it without the prerequisites."""
    body = """
    subject: {slug: demo, title: Demo}
    concepts:
      - {slug: advanced, title: Advanced, depth: 4}
      - {slug: also-advanced, title: Also, depth: 4}
    edges:
      - {from: also-advanced, to: advanced, kind: prerequisite}
    """
    graph = parse_graph([_write(tmp_path, "deep.yaml", body)])
    assert any("advanced" in w and "shallower" in w for w in graph_warnings(graph))


def test_a_well_formed_graph_warns_about_nothing(tmp_path: Path) -> None:
    graph = parse_graph([_write(tmp_path, "graph.yaml", VALID_GRAPH)])
    assert graph_warnings(graph) == []


# --- the cycle finder itself -----------------------------------------------


def test_cycle_finder_returns_none_on_a_dag() -> None:
    assert _find_cycle({"a": ["b"], "b": ["c"]}) is None


def test_cycle_finder_finds_a_self_loop() -> None:
    assert _find_cycle({"a": ["a"]}) == ["a", "a"]


def test_cycle_finder_handles_a_long_chain() -> None:
    """Iterative, not recursive.

    A recursive walk hits Python's limit somewhere past a thousand, and
    "maximum recursion depth exceeded" is a terrible way to learn that a
    subject has a long prerequisite chain.
    """
    chain = {str(n): [str(n + 1)] for n in range(3000)}
    assert _find_cycle(chain) is None
    chain["3000"] = ["0"]
    cycle = _find_cycle(chain)
    assert cycle is not None and cycle[0] == cycle[-1]


def test_cycle_finder_is_deterministic() -> None:
    """Two runs over one graph report the same cycle.

    Iterating the adjacency in sorted order rather than insertion order: an
    author fixing a reported cycle should not be handed a different one on the
    next run because a dict happened to enumerate differently.
    """
    graph = {"b": ["c"], "c": ["b"], "a": ["b"]}
    assert _find_cycle(graph) == _find_cycle(dict(reversed(list(graph.items()))))


# --- §10 concept-sources ---------------------------------------------------


def test_concept_source_yaml_parses(tmp_path: Path) -> None:
    body = """
    source: 1_TuringMachines.pdf
    subject: lambda-calculus
    links:
      - concept: syntax
        role: canonical_definition
        chunks:
          - by_page_range: [3, 5]
      - concept: beta-reduction
        role: worked_example
        chunks:
          - by_text_match: "a redex is an application"
        note: unusually clear
    """
    documents = parse_concept_sources([_write(tmp_path, "s.yaml", body)])
    assert len(documents) == 1
    assert len(documents[0]["links"]) == 2
    assert documents[0]["links"][1]["note"] == "unusually clear"


def test_an_unknown_role_is_rejected(tmp_path: Path) -> None:
    body = """
    source: x.pdf
    subject: demo
    links:
      - concept: alpha
        role: vibes
        chunks: [{by_page_range: [1, 2]}]
    """
    with pytest.raises(AuthoringError, match="role"):
        parse_concept_sources([_write(tmp_path, "s.yaml", body)])


def test_a_link_with_no_selectors_is_rejected(tmp_path: Path) -> None:
    body = """
    source: x.pdf
    subject: demo
    links:
      - concept: alpha
        role: worked_example
        chunks: []
    """
    with pytest.raises(AuthoringError, match="no chunk selectors"):
        parse_concept_sources([_write(tmp_path, "s.yaml", body)])


class _Chunk:
    def __init__(self, page_start: int | None, page_end: int | None) -> None:
        self.page_start = page_start
        self.page_end = page_end


@pytest.mark.parametrize(
    ("start", "end", "chunk", "expected"),
    [
        (3, 5, _Chunk(3, 3), True),
        (3, 5, _Chunk(4, 6), True),   # straddles the far edge
        (3, 5, _Chunk(1, 3), True),   # straddles the near edge
        (3, 5, _Chunk(6, 8), False),
        (3, 5, _Chunk(1, 2), False),
        (3, 5, _Chunk(None, None), False),
    ],
)
def test_page_range_overlap(start: int, end: int, chunk: _Chunk, expected: bool) -> None:
    """§10.1: either endpoint inside the range, not containment.

    A chunk spanning pages 4-6 against a range of [3, 5] is material the author
    pointed at. Requiring containment would silently drop exactly the chunks
    that straddle the boundary the author drew -- the ones at the edges of the
    passage they meant.
    """
    assert _overlaps_pages(chunk, start, end) is expected


# --- rubrics ---------------------------------------------------------------


def test_rubric_yaml_parses(tmp_path: Path) -> None:
    body = """
    subject: lambda-calculus
    criteria:
      - concept: beta-reduction
        slug: identifies-redex
        prompt: Can the learner identify a redex?
        key_points: [redex, operator, abstraction]
        weight: 3
    """
    criteria = parse_rubrics([_write(tmp_path, "r.yaml", body)])
    assert criteria[0]["slug"] == "identifies-redex"
    assert criteria[0]["weight"] == 3


def test_a_rubric_without_a_prompt_is_rejected(tmp_path: Path) -> None:
    body = """
    subject: demo
    criteria:
      - {concept: alpha, slug: crit}
    """
    with pytest.raises(AuthoringError, match="no prompt"):
        parse_rubrics([_write(tmp_path, "r.yaml", body)])


def test_an_invalid_rubric_weight_is_rejected(tmp_path: Path) -> None:
    """The schema's CHECK allows 1, 2 or 3; catching it here names the criterion."""
    body = """
    subject: demo
    criteria:
      - {concept: alpha, slug: crit, prompt: P, weight: 7}
    """
    with pytest.raises(AuthoringError, match="weight"):
        parse_rubrics([_write(tmp_path, "r.yaml", body)])


def test_duplicate_rubric_slugs_on_one_concept_are_rejected(tmp_path: Path) -> None:
    body = """
    subject: demo
    criteria:
      - {concept: alpha, slug: crit, prompt: P}
      - {concept: alpha, slug: crit, prompt: Q}
    """
    with pytest.raises(AuthoringError, match="duplicate rubric"):
        parse_rubrics([_write(tmp_path, "r.yaml", body)])
