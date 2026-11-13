---
name: land
description: 'Land the thread changes on master through a pull request: run checks, commit, push the branch, open or reuse the PR, wait for CI, then merge. Use when the user asks to merge, land, or ship the current changes.'
metadata:
  delta-action: land
---
# Land changes on master

`master` takes changes through pull requests. Never push to `master` directly.

## Steps

1.  Inspect the work.
    - `git --no-optional-locks status --short --branch`
    - `git log --oneline origin/master..HEAD`

    With no commits and no changes, report that there is nothing to land and stop.

2.  Run the `checks` skill. Fix every failure before you continue.

3.  Start a branch. The thread checkout usually sits on `master`, and `master` takes
    changes only through pull requests.
    - `git rev-parse --abbrev-ref HEAD` reads the current branch.
    - On `master`, create one: `git switch -c <type>/<short-slug>`.

4.  Commit uncommitted work, one commit per logical change.
    - `git add -A`
    - `GIT_EDITOR=true git commit -m "<imperative subject>"`

    Do not amend or rebase a commit that is already pushed.

5.  Push the branch, never `master`.
    - `git push -u origin HEAD`

    Never force-push.

6.  Open or reuse the pull request.
    - `gh pr view --json number,url,state` reuses an open PR for this branch.
    - Otherwise `gh pr create --base master --fill`.

7.  Wait for CI.
    - `gh pr checks --watch --fail-fast`

    On failure, run the `loop-on-ci` skill. Do not merge a red pull request.

8.  Merge with a merge commit. The repository history uses merge commits, not squash.
    - `gh pr merge --merge --delete-branch`

9.  Report the result: target branch `master`, the merge commit SHA from
    `gh pr view --json mergeCommit`, and the CI conclusion.

## Rules

- Land only the changes that belong to this thread's task. Leave unrelated edits uncommitted
  and name them in the report.
- If CI fails, report the failing job and stop. Do not merge, and do not weaken a check to
  make it pass.
- If the merge conflicts, report the conflicting files and stop.
