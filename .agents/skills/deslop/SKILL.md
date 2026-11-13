---
name: deslop
description: 'Remove AI-generated slop from the branch diff against master: redundant comments, defensive code that hides errors, unnecessary casts, and nesting that early returns simplify. Use after a large agent-written change, or when asked to clean up or de-slop the diff.'
---
# Remove AI code slop

Work on the diff against `master`, not the whole repository.

```sh
git --no-optional-locks diff origin/master...HEAD --stat
git --no-optional-locks diff origin/master...HEAD
```

## What to remove

- Comments that restate the code, narrate the edit, or are inconsistent with the file.
- `try`/`except` blocks that swallow errors on trusted paths, and defensive checks the
  surrounding code does not use.
- `# type: ignore`, `cast(Any, ...)`, and `Any` used only to silence `ty`.
- Nesting that an early return or guard clause flattens.
- Helpers and abstractions used once, added for a single call site.
- Patterns the surrounding file does not use: a different error style, a new dependency
  where the standard library suffices, a parallel code path.

## Guardrails

- Keep behavior unchanged unless you are fixing a clear bug. Name any bug you fix.
- Prefer small focused edits over a rewrite.
- Run the `checks` skill after the edits.
- Keep the summary to one to three sentences, with the files touched.
