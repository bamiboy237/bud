---
name: verify-this
description: 'Prove or disprove a specific claim with fresh local evidence: restate it falsifiably, capture baseline and treatment, compare artifacts, and return VERIFIED, NOT VERIFIED, or INCONCLUSIVE. Use for "verify this", "prove it works", "did this fix it", or any behavior, performance, or API claim.'
---
# Verify this

Verification is not a recap. It proves or disproves one claim with repeatable evidence.

## Workflow

1.  Restate the claim in falsifiable form: condition, metric, threshold.
2.  Pick the smallest local surface that can disprove it.
3.  Capture a baseline from the old state: merge base, parent commit, or the current broken
    repro.
4.  Capture the treatment from the changed state with the same command, input, and
    environment.
5.  Compare raw artifacts: command output, response bodies, timings, screenshots.
6.  Return exactly one verdict: `VERIFIED`, `NOT VERIFIED`, or `INCONCLUSIVE`.

## Local surfaces in this repository

There is no test suite and no pytest dependency. Do not report tests that do not exist.

- Behavior on the simulated world: `uv run bud eval --task <id> --trials 1`. Needs model
  access. `--probe` checks the approval reviewer on hand-written calls.
- One prompt end to end: `uv run bud ask "<prompt>"`.
- Interactive flow: `uv run bud serve --simulate` in one terminal, `uv run bud chat` in
  another.
- Static behavior: the `checks` skill covers ruff and ty.
- Pure functions: a short `uv run python - <<'PY'` script that imports the module and
  prints both states.

## Verdict rules

- `VERIFIED`: baseline and treatment differ in the predicted direction, by the claimed
  threshold, with no obvious confound.
- `NOT VERIFIED`: the behavior is unchanged, moves the wrong way, or misses the threshold.
- `INCONCLUSIVE`: no valid baseline, noisy signal, failed measurement, or an environment
  difference invalidates the comparison.

## Output

```text
VERIFIED | NOT VERIFIED | INCONCLUSIVE
Claim: <falsifiable claim>

Evidence:
<metric or artifact>: baseline=<...>, treatment=<...>, delta=<...>, threshold=<...>

Reasoning:
<one paragraph naming the evidence and any confounds>
```

Do not soften a negative result. A clear `NOT VERIFIED` is useful.
