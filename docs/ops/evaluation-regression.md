# The weekly evaluation regression

Evaluation §13.2. One obligation from the operational calendar:
`evaluation_regression_audit`, weekly, owned by dev.

**This spends real money.** §7.4 puts a full suite at $5–15. `eval gate` prints
its own estimate and runs a budget preflight before asking, so the number is
always in front of you before the spend, not after.

## Why weekly, and why it is not the CI gate

`.github/workflows/prompt-regression.yml` already runs the gate on prompt
changes — that answers "did *this diff* make it worse". The weekly run answers
a different question: did anything drift without a diff. Model behaviour
changes under you, retrieval changes as content is ingested, and a rubric that
was calibrated in August grades differently in November against the same
answers. Nothing in the repository moved, so nothing triggered the CI gate.

## Running it

```sh
cd backend
studium eval list                                    # what is active
make eval-gate d="lecturer_formal_stance_grounding retrieval_lambda_calculus"
```

`eval gate` runs each dataset against the current prompts, compares against the
stored baseline, and exits non-zero when either gate blocks:

- **§8 thresholds** — "is this good enough to ship at all";
- **§13.2 tolerance** — "did this get worse than the baseline".

A run can pass one and fail the other, and the two failures mean different
things. Read `report.render()`'s output rather than only the exit code.

## When it blocks

§13.1 step 7 gives three endings and they are the only three: approve the
regression with a written reason, request changes to the prompt, or reject.
"Re-run it and see" is not one of them — a gate that passes on the second
attempt was measuring noise, and the thing to fix is the dataset's variance.

Every run is persisted (`trigger_kind='ci'` for the CI path; a manual weekly
run records itself the same way), so the comparison the next week makes is
against what actually ran, not against a baseline someone remembered to update.

## Recording it

```sh
studium ops calendar --complete evaluation_regression_audit
```

Complete it whether or not it blocked. The calendar records that the audit
happened; the run record and the review thread carry what it found.
