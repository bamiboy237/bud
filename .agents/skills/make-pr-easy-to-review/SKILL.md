---
name: make-pr-easy-to-review
description: 'Prepare a pull request for review by cleaning noisy history, fixing the description, and adding reviewer guidance, without changing code behavior. Use for "make this easy to review", "tidy this PR", "clean up commits", or "annotate the diff".'
---
# Make the PR easy to review

The goal is reviewability with no behavior change.

## Workflow

1.  Resolve the PR from the URL the user gave, or from the current branch.
    - `gh pr view <PR> --json title,headRefName,baseRefName,state,commits,body`
2.  Inspect the change: commits, diff size, changed paths, generated files, description.
3.  Name the reviewability problems: noisy commits, stale description, unrelated changes,
    mechanical and logic changes mixed together, missing verification notes, unclear entry
    points.
4.  Propose a plan before any history rewrite or force-push. Wait for agreement.
5.  Apply the safe improvements, then confirm the tree still matches the intended code.

## History cleanup

Only rewrite history when the user asks or agrees to the plan. Base branch is `master`.

```sh
gh pr view <PR> --json title,headRefName,baseRefName,state,commits
git fetch origin <headRefName> <baseRefName>
ORIGINAL_TREE=$(git rev-parse origin/<headRefName>^{tree})
```

Group commits in dependency order: schema or storage, core logic, wiring, surface behavior,
then checks. After rewriting, prove content identity:

```sh
echo "Original tree: $ORIGINAL_TREE"
echo "Current tree:  $(git rev-parse HEAD^{tree})"
git diff origin/<headRefName> --stat
```

Do not push if the tree changed unintentionally.

## Reviewer guidance

When behavior must stay untouched, put the guidance in the description instead of the diff:

- A TL;DR that matches the actual diff.
- Core files separated from generated or mechanical ones.
- Risky behavior changes, migration order, and verification evidence called out.
- The `checks` and `verify-this` output quoted, not summarized away.

## Guardrails

- Never hide a behavior change inside "cleanup".
- If the PR is too large to make reviewable with notes, recommend splitting it instead of
  polishing around the problem.
