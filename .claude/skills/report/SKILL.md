---
name: report
description: Send a completion report for a delegated task in this repo's standard shape (shipped, numbers, corrections to the brief, settlement or data surprises, not done) and write the matching claude-briefs/<topic>-notes.md. Use when finishing a briefed task.
---

# Completion report

Two outputs: `claude-briefs/<topic>-notes.md` (the durable record, written for a reviewer who did not
watch the work) and a SendMessage to the session that briefed you, whose first line states the
task is done.

Sections, in both:

1. **Shipped**: files created or changed (your own only), tests added, the full-suite count.
   CLI or function signatures other sessions will call.
2. **Numbers**: coverage, counts, rates, latencies, calibration results; whatever the brief's
   "report back" section asked for, in a short table where there are more than three.
3. **Corrections to the brief**: every fact in the brief that turned out wrong or different,
   with the evidence. This section is never empty by default; say "none" only after checking.
4. **Findings**: data quirks, settlement-rule differences, API behaviour, anything a later
   reader would otherwise rediscover the hard way.
5. **Not done / v2**: deliberate omissions and their reasons, one line each. Send these to the
   planning session so they land in the "Next Steps" section of `handin-summary.md`; do not edit that file yourself.

Keep it factual and specific: numbers over adjectives, paths over descriptions, and nothing
about files you do not own. If the suite is not green, say which tests fail and whose they are.
