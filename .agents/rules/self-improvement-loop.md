# Self-improvement loop

> Apply every session. Capture what was learned while working, then turn those
> notes into concrete improvements to the project, rules, and skills.

## 1. At session start — open a session note

Create `docs/notes/{session-slug}.md` where `{session-slug}` is
`{YYYY-MM-DD}-{short-kebab-topic}` (date first for ordering, then a few words
naming the session's main task — e.g. `2026-06-08-self-improvement-rule`).

If the topic is unclear at the start, use a placeholder and rename the file once
it is. Seed the file with the template at the bottom of this rule.

These notes are **ephemeral**: `docs/notes/` is git-ignored and the developer
purges it regularly. Treat a note as scratch input for the loop, never as a
source of truth. **Never link to or cite a session note from durable
documentation** (guides, ADRs, READMEs, skills, rules, code comments) — anything
worth keeping must be promoted into its proper home when the loop closes
(section 4).

## 2. During the session — record noteworthy facts

Append to the note whenever any of the following occurs. Be specific (quote the
exact command, file, error, or wording); a finding a future session can act on
beats a vague summary.

- **Lessons learned** — anything now understood that wasn't before.
- **Developer corrections** — what the developer pushed back on, and the rule
  it implies. Cross-check existing memory and rules; flag conflicts.
- **Non-trivial discovery** — how a non-obvious fact was found (the search path,
  not just the answer), so it need not be rediscovered.
- **Skill gaps** — a task that should have a skill/rule but doesn't.
- **Repeated attempts** — anything that took several tries to get right, and why.
- **Inconsistencies & conflicts** — contradictions between docs, rules, memory,
  code, or developer instructions.
- **Automatable repetition** — actions repeated often enough to become a skill,
  hook, or script.
- **Knowledge gaps** — missing information or insight that slowed the work.
- **Project-useful facts** — anything helpful for the project, or for evolving
  the agent rules and skills.

## 3. When to update the note

- **After every interaction with the developer.**
- **At the end of every agent action, before reporting back to the developer.**

If an action produced nothing noteworthy, the note may stay unchanged — do not
pad it.

## 4. Closing the loop

Trigger this when the developer says to **end** or **wrap up** the session, or
asks to **run the self-improvement loop**:

1. **Review** the full session note.
2. **Identify actionable findings** — only those that yield a tangible
   improvement to future work: new or updated skills, rules, docs, scripts,
   automations, or memory.
3. **Auto-apply every finding immediately** — make the edits/additions in this
   session.
4. **Then ask the developer to review** the applied changes. List what changed
   and why, so the review is quick.

Findings that are not actionable (one-offs, already-covered ground) produce no
change. Because the note is ephemeral, anything that must survive the next purge
has to be promoted out of the note in step 3 — don't rely on the note itself to
preserve it.

### Where promoted knowledge lives

Durable knowledge goes into **version-controlled repo homes**, so it is available
on every machine and to every contributor:

- `.agents/rules/` — agent conventions and working practices.
- `.agents/skills/` — procedures the agent runs.
- `docs/` — project facts and decisions (guides, and ADRs under `docs/decisions/`).

Do **not** rely on the agent's local memory as a durable home: it is per-machine
and not version-controlled, so it is invisible on other developers' machines.
When a finding currently lives only in local memory, promote it into one of the
homes above.

**Memory and repo are complementary, not either/or.** The repo is the
authoritative home; local memory is a per-session reminder that points at or
summarises what lives in the repo. Do not delete a memory entry just because the
underlying knowledge was promoted to the repo — and never delete memory entries
without being explicitly asked.

## Session note template

```markdown
# Session notes — {session-slug}

**Date:** {YYYY-MM-DD}
**Topic:** {one line}

## Lessons learned

## Developer corrections

## Non-trivial discovery

## Skill gaps

## Repeated attempts / struggles

## Inconsistencies & conflicts

## Automatable repetition

## Knowledge gaps

## Project / agent-improvement facts

## Actionable findings (filled at loop close)

- [ ] {finding} → {improvement to apply}
```
