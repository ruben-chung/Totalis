---
name: brief
description: Write a delegation brief for a peer Claude Code session in this repo's standard shape (why, verified facts, deliverables, ownership, report back), save it under docs/, and send it. Use when handing a component to another session.
---

# Delegation brief

Write `docs/<topic>-brief.md` with exactly these sections, then send the essentials by SendMessage
to the target session and ask it to read the file in full.

1. **Header**: date, who wrote it (the planning session's current name), owner (Ruben), where to
   report back. Name any sibling brief it builds on.
2. **Why**: two to five sentences on what this unblocks and the decision it implements. Link the
   decision's evidence (a notes file, an analysis, a conversation outcome).
3. **Verified facts**: only things checked against live data or the repo today, each with how it
   was checked. Mark inferences as inferences. Put API quirks, units, date conventions, rate
   limits and sign conventions here; this section saves the most time downstream.
4. **Deliverables**: numbered, each with the module path, the function or CLI signature other
   sessions will code against, and the tests expected. Say what is out of scope.
5. **Ownership and coordination**: which files the session owns, which it must not edit and who
   owns them, and whom to message for interface changes. Remind it to run
   `scripts/claim-owner <label>` once so the ownership hook knows it. If the task needs a shared
   file changed, say who makes that change.
6. **Report back**: the specific numbers and surprises to send, and the instruction to say
   plainly if anything in the brief turned out wrong.

Style: short paragraphs, concrete numbers, no speculation dressed as fact. Length is whatever the
facts need; most briefs run 60 to 130 lines. After sending, record the delegation in
`docs/future-work.md` only if something was deliberately scoped out, and in memory.
