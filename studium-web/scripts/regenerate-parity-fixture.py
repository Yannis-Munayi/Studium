"""Regenerate the sentence-boundary parity fixture from the runtime's own code.

`lib/stream/sentences.ts` is a port of
`backend/studium/orchestration/streaming.py::find_boundary`. This script runs
the Python original over a corpus and records what it returns, so
`tests/sentence-parity.test.ts` can assert the port still agrees rather than
merely claiming it does.

Run after touching either implementation:

    python scripts/regenerate-parity-fixture.py

A diff in the fixture is the review signal: it means the server's rule moved,
and the port has to move with it or the disagreement has to be justified.

Cases are grouped by what they exercise. Adding one is the cheapest way to pin
a boundary bug found in real Lecturer output -- paste the sentence, regenerate,
and the port is held to it forever.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from studium.orchestration.streaming import find_boundary  # noqa: E402

FIXTURE = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sentence-boundary-parity.json"

CASES: list[str] = [
    # Ordinary prose, complete and incomplete.
    "Beta reduction rewrites an application. The next step is",
    "Beta reduction rewrites an",
    "That is the whole rule.",
    "One complete sentence. A second one. And a frag",
    "Still writing the first",
    "Ends with no punctuation and no capital after",
    "No boundary here at all",
    "Multiple.  Spaces.  Between.",
    "Trailing whitespace after period.   ",
    # Abbreviations, which the naive rule gets wrong.
    "Consider e.g. the identity function is next.",
    "See i.e. this case. Then continue.",
    "Compare cf. Church. And then Curry.",
    "Dr. Michaelson wrote it. He was right.",
    "Fig. 3 shows the tree. Look closely.",
    "Ph.D. work on this. It continues.",
    "Thm. 4 is the key. Prop. 5 follows.",
    # Math, where a period is a decimal point or part of a lambda term.
    "We set $f(x) = 1.5$ and continue. Then we reduce.",
    "$$\\lambda x. x$$ is the identity. Next.",
    "The value is 3.14 exactly. Next sentence.",
    "Nested $a.b$ and $c.d$ here. Done.",
    "$$x = 1. y = 2$$ Continue here.",
    # The lookahead set: capital, digit, bracket, delimiter.
    "First. 2 is next",
    "First. (Second follows)",
    'He called it "reduction." The name stuck.',
    "A sentence ending in a bracket.] Next one.",
    # Other terminal punctuation.
    "Is it normalising? Yes it is.",
    "Stop! Then go.",
    # Degenerate input.
    "",
    ".",
    "...",
]


def main() -> None:
    payload = [{"text": case, "boundary": find_boundary(case)} for case in CASES]
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    found = sum(1 for row in payload if row["boundary"] is not None)
    print(f"wrote {len(payload)} cases to {FIXTURE} ({found} with a boundary)")


if __name__ == "__main__":
    main()
