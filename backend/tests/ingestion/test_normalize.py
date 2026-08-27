"""Normalisation, Tier 1 (ingestion §16).

Every transformation gets a fixture, and every fixture that could be satisfied
by a transformation that is too aggressive gets its adversarial twin. That
pairing is the point of this module: "ligatures are expanded" is trivially
passed by ``unicodedata.normalize('NFKC', text)``, which also destroys the
mathematical notation this corpus is made of. The negative cases are what pin
the behaviour down.
"""

from __future__ import annotations

import pytest

from studium.ingestion.normalize import (
    FURNITURE_MASK_CHARS,
    MAX_FURNITURE_CHARS,
    MIN_PAGES_FOR_FURNITURE,
    NORMALIZER_VERSION,
    ligature_coverage,
    normalize_document,
    normalize_page,
)

# --- determinism -----------------------------------------------------------


def test_normalisation_is_deterministic() -> None:
    """§16: the same input returns identical output.

    Load-bearing beyond tidiness: chunk text feeds the Lecturer's cached prefix
    through ``grounding_version``, so text that varies between runs invalidates
    a cache the runtime pays real money to keep warm.
    """
    text = "The deﬁnition of ‘beta’ — see Church–Rosser, pages 12–18."
    assert normalize_page(text) == normalize_page(text)


def test_normalisation_is_idempotent() -> None:
    """Normalising twice is normalising once.

    The pipeline relies on this: retrieval's chunker folds ligatures again on
    every block it is handed, so normalised text passes through a second
    ligature pass before it is ever stored.
    """
    once = normalize_page("The deﬁnition of a ﬁxed point—see 12–18.")
    assert normalize_page(once) == once


# --- 1: ligatures ----------------------------------------------------------


def test_every_fb_ligature_is_mapped() -> None:
    """§16: every codepoint in the U+FB00-FB06 block has an ASCII expansion."""
    coverage = ligature_coverage()
    assert coverage.unmapped == (), (
        f"unmapped ligatures {coverage.unmapped}; a chunk carrying one is "
        f"invisible to keyword search for the word it contains"
    )
    assert len(coverage.mapped) == 7


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("deﬁnition", "definition"),
        ("ﬀ", "ff"),
        ("ﬂow", "flow"),
        ("eﬃcient", "efficient"),
        ("ﬄ", "ffl"),
    ],
)
def test_ligatures_expand(raw: str, expected: str) -> None:
    assert normalize_page(raw) == expected


def test_ligature_only_text_survives() -> None:
    """§16 adversarial case: text that is nothing but ligatures."""
    assert normalize_page("ﬀﬁﬂﬃﬄ") == "fffiflffiffl"


def test_mathematical_notation_is_not_folded() -> None:
    """The reason full NFKC is refused.

    NFKC expands superscripts, fractions and several operators. In a lambda
    calculus corpus the notation *is* the content: folding λ² to λ2 or ½ to 1/2
    changes what the passage says, and it does so silently on text that already
    extracted correctly.
    """
    notation = "λx.x ⇒ β-reduction, x² ≠ ½, ∀x ∈ ℕ"
    assert normalize_page(notation) == notation


# --- 2: quotes -------------------------------------------------------------


def test_curly_quotes_become_straight() -> None:
    assert normalize_page("“beta” and ‘alpha’") == '"beta" and \'alpha\''


def test_apostrophes_are_preserved_not_dropped() -> None:
    """§8.1: "apostrophes preserved" means as an apostrophe, not deleted.

    "Church's theorem" and "Churchs theorem" are different words to the search
    index, so dropping it loses the match it was meant to keep.
    """
    assert normalize_page("Church’s theorem") == "Church's theorem"


# --- 3: dashes -------------------------------------------------------------


def test_em_dashes_are_semantic_and_survive() -> None:
    """§16 adversarial case: an intentional em dash must not become a hyphen."""
    assert "—" in normalize_page("The result — surprisingly — holds.")


def test_en_dash_in_a_numeric_range_survives() -> None:
    assert normalize_page("pages 12–18") == "pages 12–18"


def test_en_dash_between_words_becomes_a_hyphen() -> None:
    """A compound indexes as one term only if it carries an ASCII hyphen."""
    assert normalize_page("Church–Rosser") == "Church-Rosser"


# --- 4: whitespace ---------------------------------------------------------


def test_horizontal_whitespace_collapses() -> None:
    assert normalize_page("lots   of \t  space") == "lots of space"


def test_paragraph_breaks_survive_whitespace_collapsing() -> None:
    """The blank line is what the chunker splits paragraphs on.

    Folding newlines into the horizontal-whitespace run would hand the chunker
    one continuous wall of text, and every §7 preference-2 break point would
    disappear at once.
    """
    assert normalize_page("one\n\npara two") == "one\n\npara two"
    assert normalize_page("one\n\n\n\n\npara two") == "one\n\npara two"


@pytest.mark.parametrize(
    "codepoint",
    ["", "", " ", " ", " ", "　"],
    ids=lambda c: hex(ord(c)),
)
def test_exotic_whitespace_becomes_a_plain_space(codepoint: str) -> None:
    """§8.1 step 4 names form feed and vertical tab explicitly.

    They become spaces rather than being deleted as control characters. A form
    feed is where a column or a page ended, so deleting one joins the word
    before it to the word after and produces a token that matches no query --
    "the endBeginning" is a worse artefact than the character it replaced.

    Escapes rather than literals because several of these survive a copy-paste
    as an ordinary space, which would silently make the assertion vacuous.
    """
    assert normalize_page(f"a{codepoint}b") == "a b"


# --- 5: hyphenated line breaks --------------------------------------------


def test_hyphenated_line_break_is_repaired() -> None:
    assert normalize_page("a defi-\nnition here") == "a definition here"


def test_semantic_hyphen_before_a_capital_is_not_joined() -> None:
    """§8.1: a hyphen before an uppercase letter is the author's, not the typesetter's.

    Joining it produces "ChurchRosser", which appears in no index and matches
    no query -- a repair that is strictly worse than the artefact.
    """
    assert normalize_page("Church-\nRosser theorem") == "Church-\nRosser theorem"


def test_hyphen_before_a_digit_is_not_joined() -> None:
    assert normalize_page("figure-\n3 shows") == "figure-\n3 shows"


# --- 6: header/footer stripping -------------------------------------------


def _paged(pages: int, *, header: str | None = None, footer: str | None = None) -> list:
    out = []
    for page in range(1, pages + 1):
        lines = [f"Body line {n} of page {page} says something different." for n in range(8)]
        if header:
            lines.insert(0, header)
        if footer:
            lines.append(footer.format(page=page))
        out.append((page, "\n".join(lines)))
    return out


def test_a_constant_running_head_is_stripped() -> None:
    result = normalize_document(_paged(8, header="A Book About Reduction"))
    assert "A Book About Reduction" in result.stripped_furniture
    assert all("A Book About Reduction" not in page.text for page in result.pages)


def test_a_page_number_footer_is_stripped() -> None:
    """The case an exact-match heuristic cannot see.

    Every footer differs, so no two pages share a line -- and the literal §8.1
    rule strips nothing. Masking digit runs is what makes it fire, which is the
    whole of divergence I2.
    """
    result = normalize_document(_paged(8, footer="{page}"))
    assert result.stripped_furniture, "page-number footers were not detected"
    assert all(page.text.strip().split("\n")[-1] != str(index + 1)
               for index, page in enumerate(result.pages))


def test_body_prose_differing_only_in_a_number_is_not_stripped() -> None:
    """The over-stripping hazard digit masking creates.

    Eight pages of "Body line 3 of page N ..." mask to one signature and clear
    the 50% threshold. Confining masking to short lines is what stops the
    body of every page being deleted as a running head -- and this is the test
    that would fail if that guard were removed.
    """
    result = normalize_document(_paged(8))
    for index, page in enumerate(result.pages, start=1):
        assert f"of page {index}" in page.text, (
            f"page {index} lost its body text to furniture stripping"
        )


def test_a_long_repeated_line_is_never_furniture() -> None:
    """§8.1 errs toward keeping content; a paragraph-length line is content."""
    boilerplate = "x" * (MAX_FURNITURE_CHARS + 10)
    result = normalize_document(_paged(8, header=boilerplate))
    assert boilerplate not in result.stripped_furniture
    assert all(boilerplate in page.text for page in result.pages)


def test_short_documents_are_left_alone() -> None:
    """Below §8.1's page floor the ratio means nothing.

    On a three-page paper one repeated section heading clears 50%, which is not
    evidence of anything.
    """
    result = normalize_document(_paged(MIN_PAGES_FOR_FURNITURE - 1, header="Heading"))
    assert result.stripped_furniture == ()


def test_a_page_is_never_stripped_to_nothing() -> None:
    """Whatever the document-wide vote said, a page that is all candidates keeps them."""
    pages = [(n, f"Repeated Head\n{n}") for n in range(1, 9)]
    result = normalize_document(pages)
    assert all(page.text.strip() for page in result.pages)


def test_a_repeated_line_in_the_page_middle_is_not_furniture() -> None:
    """A line is a header because it repeats *at the top*.

    The same string in the middle of a page is body text, and counting per
    edge is what keeps the two apart. Without the edge split, a phrase an
    author happens to repeat -- a refrain, a recurring definition, a table
    caption -- would be deleted from every page it appears on.
    """
    pages = [
        (
            n,
            "\n".join(
                [
                    f"First line, page {n}, distinct prose of some length here.",
                    f"Second line, page {n}, also distinct and reasonably long.",
                    "AN EXACTLY REPEATED MIDDLE LINE",
                    f"Fourth line, page {n}, more distinct prose to pad this out.",
                    f"Fifth line, page {n}, and the last of the distinct ones.",
                ]
            ),
        )
        for n in range(1, 9)
    ]
    result = normalize_document(pages)
    for page in result.pages:
        assert "AN EXACTLY REPEATED MIDDLE LINE" in page.text


def test_short_edge_lines_differing_only_in_digits_are_treated_as_furniture() -> None:
    """A known limitation of I2's digit masking, asserted rather than left to be found.

    A *short* line at a page edge that differs only in a numeral is
    indistinguishable from a running head by any signature-based rule -- "Page
    3" and "Figure 3" and "Opening line 3" all mask identically. The design
    accepts this for short lines and refuses it for long ones, because the
    short case is overwhelmingly furniture in real documents and the long case
    overwhelmingly is not.

    Written as a test so the trade is visible: if a real corpus turns up short
    edge content being eaten, this is the assertion to change and
    ``FURNITURE_MASK_CHARS`` is the knob.
    """
    pages = [
        (
            n,
            "\n".join(
                [
                    f"Note {n}",
                    *[f"Body line {i} of page {n} with genuinely distinct prose." for i in range(6)],
                ]
            ),
        )
        for n in range(1, 9)
    ]
    result = normalize_document(pages)
    assert any(item.startswith("Note") for item in result.stripped_furniture)
    # The body is untouched either way, which is the part that matters.
    for index, page in enumerate(result.pages, start=1):
        assert f"of page {index}" in page.text


def test_masking_threshold_is_below_the_furniture_cap() -> None:
    """A guard on the two constants that make I2 safe.

    If masking ever applied to every line up to the furniture cap, the
    over-stripping case above comes back. The relationship is the invariant,
    so it is asserted rather than left implied by two numbers in a module.
    """
    assert FURNITURE_MASK_CHARS < MAX_FURNITURE_CHARS


# --- 7: non-printables -----------------------------------------------------


def test_zero_width_characters_are_stripped() -> None:
    """Invisible, and each one splits an index term in half with no visible cause."""
    assert normalize_page("defi​nition") == "definition"
    assert normalize_page("soft­hyphen") == "softhyphen"


def test_control_characters_go_but_tabs_and_newlines_stay() -> None:
    assert normalize_page("a\x00b") == "ab"
    assert normalize_page("a\tb") == "a b"
    assert normalize_page("a\nb") == "a\nb"


# --- §8.3 warnings ---------------------------------------------------------


def test_long_lines_raise_a_warning() -> None:
    pages = [(n, "\n".join(["y" * 600] * 25)) for n in range(1, 3)]
    kinds = {w.kind for w in normalize_document(pages).warnings}
    assert "long_lines" in kinds


def test_high_non_ascii_raises_a_warning() -> None:
    """Suggests the extractor missed an encoding conversion."""
    pages = [(n, "мама мыла раму " * 40) for n in range(1, 3)]
    kinds = {w.kind for w in normalize_document(pages).warnings}
    assert "non_ascii" in kinds


def test_words_run_together_raise_a_warning() -> None:
    """The defect the real corpus had, and the surface that now catches it.

    The Michaelson book extracted as
    "Itispossible,however,forboundvariablesindifferent..." under pdfplumber's
    default word-split tolerance. Every existing signal read healthy -- median
    chunk size 409 against a 400 target, coverage 0.99, section detection 1.00
    -- because a token count cannot tell a paragraph from a paragraph-shaped
    word. Only a space-density check can.
    """
    run_together = (
        "Itispossible,however,forboundvariablesindifferentfunctions"
        "tohavethesamename. "
    ) * 8
    kinds = {w.kind for w in normalize_document([(1, run_together)]).warnings}
    assert "words_run_together" in kinds


def test_ordinary_prose_is_not_accused_of_running_together() -> None:
    """The false positive that would make the warning useless.

    A threshold set for English averages would fire on every maths-heavy or
    code-heavy page, and a queue full of those trains reviewers to ignore it.
    """
    prose = (
        "It is possible, however, for bound variables in different functions "
        "to have the same name. "
    ) * 8
    kinds = {w.kind for w in normalize_document([(1, prose)]).warnings}
    assert "words_run_together" not in kinds


def test_dense_notation_is_not_accused_of_running_together() -> None:
    """Symbols are excluded from the ratio, so notation does not trip it."""
    notation = "(λx.λy.x) (λz.z) → λy.(λz.z) ∀x∈ℕ f(x)=x²+1 " * 12
    kinds = {w.kind for w in normalize_document([(1, notation)]).warnings}
    assert "words_run_together" not in kinds


def test_clean_prose_raises_no_warnings() -> None:
    """A warning surface nobody trusts is worse than none.

    If ordinary text triggered warnings, the queue would fill with noise and
    the real signals in it would be ignored.
    """
    pages = [
        (n, "\n\n".join([f"A clean paragraph {n}.{i} of ordinary English prose "
                         f"about reduction strategies and normal forms." for i in range(6)]))
        for n in range(1, 6)
    ]
    assert normalize_document(pages).warnings == ()


def test_warnings_are_reportable() -> None:
    pages = [(n, "\n".join(["y" * 600] * 25)) for n in range(1, 3)]
    warning = normalize_document(pages).warnings[0]
    payload = warning.as_payload()
    assert set(payload) == {"kind", "detail", "page"}


def test_version_is_stamped_on_the_result() -> None:
    """§8.4: the version travels with the output, to reach sources.normalizer_version."""
    assert normalize_document([(1, "text")]).version == NORMALIZER_VERSION
    assert NORMALIZER_VERSION.startswith("studium.normalize/")


# --- empties ---------------------------------------------------------------


def test_empty_input_does_not_crash() -> None:
    assert normalize_page("") == ""
    assert normalize_document([]).pages == ()


def test_an_all_whitespace_document_is_empty() -> None:
    assert normalize_document([(1, "   \n\n  \t ")]).is_empty
