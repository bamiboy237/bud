---
name: attention-briefing
description: Synthesize what deserves attention over a requested day, week or period across connected messages, email, calendars, documents and tasks. Use for briefings, priorities and cross-app updates.
metadata:
  version: "1"
---
# Attention briefing

Resolve the requested period in the person's timezone. Read relevant saved preferences
(`inspect_preferences` or the current `<memory>` block), priorities and unfinished work.
Apply `skill:attention-briefing` preferences first, then `global` preferences where they do
not conflict. Keep preferences distinct from historical facts, and never restore a
superseded or cleared preference from an older conversation summary or recalled turn. Use
the connected sources that matter for this request.

Gather source-linked findings: requests directed at the person, decisions, deadlines,
meetings needing preparation, conflicts and meaningful updates. Read relevant threads
and documents, not only titles or unread counts. Track which sources and time windows
were covered. Distinguish an empty result from an unavailable or incomplete source.

Connect findings by topic across apps. Check whether an apparent action is already
resolved, belongs to somebody else, or duplicates another finding. Investigate an open
question further when it could change the priority. Do not infer urgency solely from
unread status, sender seniority or an arbitrary response window.

Explain what matters, why, and the useful next action. Follow the person's saved priorities,
length and format preferences; do not force a fixed number of items or sections. Let
task-specific instructions in the current request refine or override saved preferences for
this briefing only, without altering saved preferences unless the person asks to retain the
change. Keep references so "why this?" and "what happened with that?" can be answered.

Producing a briefing does not star emails, create tasks, send messages or schedule work.
Use the ordinary approval flow for requested follow-up actions. Offer to save the result
when useful. When the person explicitly asks to inspect, save, correct or forget a briefing
preference for future runs, use `inspect_preferences`, `set_preference` (with scope
`skill:attention-briefing`), or `forget_preference`.
