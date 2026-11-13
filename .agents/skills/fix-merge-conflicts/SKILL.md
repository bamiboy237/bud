---
name: fix-merge-conflicts
description: 'Resolve merge conflicts non-interactively, regenerate lockfiles, run the project checks, and finish in a buildable state. Use when a merge, rebase, or pull request reports conflicts.'
---
# Fix merge conflicts

## Workflow

1.  List the conflicting files.
    - `git --no-optional-locks status --short`
    - `grep -rn '^<<<<<<< ' <paths>` finds the remaining markers.
2.  Resolve each conflict with the smallest correctness-first edit. Preserve both sides
    when both are meaningful; otherwise keep the variant that builds and keeps public
    behavior stable.
3.  Regenerate lockfiles with the package manager, never by hand.
    - `uv lock` for `uv.lock`
    - `cargo update --manifest-path tui/Cargo.toml` for `tui/Cargo.lock`
4.  Run the `checks` skill.
5.  Stage the resolved files and summarize the decisions.

## Guardrails

- Leave no conflict markers in any file.
- Do not refactor while resolving a conflict.
- Do not push, tag, or merge during resolution. Report and let the user or the `land`
  skill continue.

## Output

- Files resolved
- Notable resolution choices and why
- Check outcome
