# Skills & Tool Extensibility

Bud uses the open Agent Skills specification ([agentskills.io](https://agentskills.io/)) to load specialized workflows on demand.

## Progressive Disclosure
Instead of bloating the system prompt with every available instruction, Bud advertises only each skill's `name` and single-sentence `description`. When a task matches, Bud loads the skill's `SKILL.md` into context dynamically.

## Managing Skills
- `list_skills`: View currently installed skills and versions.
- `find_skills`: Search the public registry for reusable task methods.
- `prepare_skill`: Download and inspect a public skill package prior to installation.
- `install_skill`: Activate an inspected skill version (requires user approval).
