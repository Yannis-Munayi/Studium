"""The license workflow, Tier 1 (ingestion §11, §16).

§16 asks for one thing here: "no code path in ingestion sets `license` to
anything other than `permission_granted` for a new upload." That is asserted
below by reading the source, because it is a property of the *code*, not of any
particular call -- a test that uploaded one PDF and checked the result would
pass while a second, unexercised branch set something else.

The rest of the module guards §11.4's three refusals. Each is written as a test
rather than a comment because each is a thing someone will eventually try to
add back as an improvement, and a failing test is a better argument than a
paragraph.
"""

from __future__ import annotations

import inspect
import re

import pytest

from studium.ingestion import licensing, pipeline
from studium.ingestion.licensing import (
    HONEST_DEFAULT,
    LICENSE_KINDS,
    PUBLISHABLE_LICENSES,
    LicenseState,
)


def test_the_honest_default_is_permission_granted() -> None:
    """§11.1. Not public_domain, which the system is in no position to claim."""
    assert HONEST_DEFAULT == "permission_granted"
    assert HONEST_DEFAULT in LICENSE_KINDS


def test_the_default_is_not_publishable() -> None:
    """The default is the *unclassified* state, so it cannot clear a publish.

    If ``permission_granted`` counted as a classification, every uploaded
    source would be publishable the moment it finished ingesting and §11 would
    be decoration.
    """
    assert HONEST_DEFAULT not in PUBLISHABLE_LICENSES


def test_upload_sets_only_the_honest_default() -> None:
    """§16: no code path in ingestion sets a new upload's license to anything else.

    Read from the source of ``pipeline.upload`` rather than from a call: this
    has to hold for every branch, including ones no fixture reaches. The
    failure it prevents is the one that already happened once -- a classifier
    reading the filename and writing a rights claim nobody made.
    """
    source = inspect.getsource(pipeline.upload)
    literals = set(re.findall(r"['\"](" + "|".join(LICENSE_KINDS) + r")['\"]", source))
    assert literals <= {HONEST_DEFAULT}, (
        f"pipeline.upload mentions license value(s) {literals - {HONEST_DEFAULT}}; "
        f"a new upload gets {HONEST_DEFAULT!r} and a reviewer decides the rest"
    )


def test_no_ingestion_module_infers_a_license_from_a_filename() -> None:
    """§11.4: filename-based classification is refused as a category.

    The original defect was a substring check for "turing". This looks for any
    code that reaches a license value from a filename or a path, so the next
    version of the same idea -- however many substrings it checks -- fails here
    rather than in the corpus.
    """
    import pkgutil

    import studium.ingestion as package

    # Reading the two columns in one SELECT is fine and normal -- `sources`
    # has both. What is forbidden is a license *value* being derived from a
    # name, so the check looks for a license-kind literal in the company of a
    # filename token, and for the substring tests the original defect was made
    # of. Matching merely on "both words appear" flagged an ordinary column
    # list and would have trained everyone to suppress it.
    name_tokens = ("filename", "storage_path", ".stem", "path.name", "basename")
    value_literals = tuple(f"'{kind}'" for kind in LICENSE_KINDS) + tuple(
        f'"{kind}"' for kind in LICENSE_KINDS
    )
    substring_tests = (".startswith(", ".endswith(", " in filename", " in name", ".lower() in")

    offenders: list[str] = []
    for info in pkgutil.iter_modules(package.__path__):
        module = __import__(f"{package.__name__}.{info.name}", fromlist=["_"])
        for number, line in enumerate(inspect.getsource(module).splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("#", "*", '"""', "'''")):
                continue  # prose about the refusal, not an implementation of it
            has_name = any(token in stripped for token in name_tokens)
            if not has_name:
                continue
            names_a_license_value = any(
                literal in stripped for literal in value_literals
            )
            tests_a_substring = "licens" in stripped.lower() and any(
                test in stripped for test in substring_tests
            )
            if names_a_license_value or tests_a_substring:
                offenders.append(f"{info.name}:{number}: {stripped}")

    assert not offenders, (
        "a license value is being reached from a filename or path:\n  "
        + "\n  ".join(offenders)
    )


def test_the_filename_detector_actually_fires() -> None:
    """A smoke test for the check above.

    The detector was already once too loose (it flagged an ordinary SELECT) and
    tightening it could just as easily have made it match nothing. This runs
    the same rule over the original defect and asserts it is caught, so the
    check cannot quietly become a no-op.
    """
    defect = 'if "turing" in filename.lower(): license = "public_domain"'
    name_tokens = ("filename", "storage_path", ".stem", "path.name", "basename")
    value_literals = tuple(f'"{kind}"' for kind in LICENSE_KINDS)

    assert any(token in defect for token in name_tokens)
    assert any(literal in defect for literal in value_literals)


def test_no_ingestion_module_asks_a_model_to_classify() -> None:
    """§11.4: no LLM-based classification.

    The most tempting of the three, because a model reading a title page
    usually can identify the work -- with the identical failure mode, now
    wearing a fluent justification.
    """
    import pkgutil

    import studium.ingestion as package

    offenders: list[str] = []
    for info in pkgutil.iter_modules(package.__path__):
        module = __import__(f"{package.__name__}.{info.name}", fromlist=["_"])
        source = inspect.getsource(module)
        for token in ("anthropic", "AsyncAnthropic", "messages.create", "studium.llm"):
            if token in source and "licens" in source.lower():
                offenders.append(f"{info.name} references {token}")

    assert not offenders, f"license classification must not reach a model: {offenders}"


def test_classification_requires_a_note() -> None:
    """§11.2 step 4. The note is the basis, and a conclusion without one is a guess.

    Not a style preference: the next reviewer has to be able to check the work,
    and "public_domain" with no reasoning is indistinguishable from the
    filename classifier's output.
    """
    signature = inspect.signature(licensing.classify)
    assert signature.parameters["note"].default is inspect.Parameter.empty
    assert signature.parameters["license"].default is inspect.Parameter.empty


@pytest.mark.parametrize(
    ("license", "notes", "expected"),
    [
        ("public_domain", None, True),
        ("cc_by", "hosted under CC-BY on the author's page", True),
        ("fair_use", None, True),
        # The default *with* a note means a reviewer looked and recorded why.
        ("permission_granted", "Michaelson confirmed by email 2026-08-20", True),
        # The default *without* one is the value the upload set. Nobody looked.
        ("permission_granted", None, False),
        ("permission_granted", "   ", False),
    ],
)
def test_is_classified_distinguishes_reviewed_from_untouched(
    license: str, notes: str | None, expected: bool
) -> None:
    """The distinction the publish gate turns on.

    ``permission_granted`` is both the untouched default and a legitimate
    outcome, so the license value alone cannot say whether a human was
    involved. The note is the evidence, which is why §11.2 requires it.
    """
    import uuid

    state = LicenseState(
        source_id=uuid.uuid4(),
        title="A Source",
        license=license,
        license_notes=notes,
        status="draft",
    )
    assert state.is_classified is expected


def test_classify_rejects_an_unknown_license() -> None:
    with pytest.raises(ValueError, match="unknown license"):
        licensing.classify(None, source_id=None, license="probably_fine", note="x")


def test_classify_rejects_an_empty_note() -> None:
    with pytest.raises(ValueError, match="note is required"):
        licensing.classify(None, source_id=None, license="public_domain", note="  ")


def test_publishable_licenses_are_all_real_kinds() -> None:
    assert set(LICENSE_KINDS) >= PUBLISHABLE_LICENSES


def test_user_uploaded_is_not_the_default() -> None:
    """§11.1 rejects it explicitly, and it carries an access restriction.

    ``user_uploaded`` reads as a close synonym for the honest default and is
    not one: it says something about how the file arrived rather than what may
    be done with it, and the query layer (§6.3) hides such sources from
    everyone but their uploader. Defaulting to it would make every ingested
    source invisible to learners for reasons no one wrote down.
    """
    assert HONEST_DEFAULT != "user_uploaded"
    assert "user_uploaded" not in PUBLISHABLE_LICENSES
