---
name: plan-kanban-tasks
description: >-
  Decompose a body of work on the agency-audit project into a ready-to-create
  Kanban task plan — a markdown file with one self-contained section per task,
  already split so each section becomes exactly one card. Use whenever turning a
  goal into Kanban work: feature development, refactors, bug-fix batches,
  test-coverage drives, config/packaging cleanup, ops, or documentation. You do
  the decomposition here; the plan is created as individual tasks, never fed to
  the auto-decomposer.
---

# Plan Kanban tasks for agency-audit

## What you are producing

A **task plan**: one markdown file that already enumerates the work as discrete,
fully-specified tasks — one `## [task]` section each. Each section is written to
become **exactly one Kanban card**, created directly. You are doing the
decomposition yourself, in this file. The plan is finished work, not raw material
for further splitting.

Save it to `docs/prompts/<slug>.md`, where `<slug>` is a short kebab-case name for
the work (e.g. `docs/prompts/remove-bbox-fallback.md`). Put the plan title on the
first line as a top-level `# heading`. One file per body of work; when work needs
several independent plans, write one file each and number the slugs
(`...-1-*.md`, `...-2-*.md`). Create `docs/prompts/` if it does not exist. Keep the
file to just the plan — no commentary about the drafting session.

Read `AGENTS.md` at the repo root first — it is the source of truth for
agency-audit's commands, layout, conventions, and the merge gate. The orientation
you bake into the plan comes from there.

## Why you decompose it yourself — do NOT hand a whole plan to the auto-decomposer

There is an auto-decomposer that takes a single rough "triage" card and splits it
into child tasks. **Never route a finished plan through it.** It re-plans
generically from the **title**: it caps at ~2–6 tasks, rewrites each body for a
context-free worker, and ignores your unit markers and "this prerequisite is
already on master" constraints. A hand-made 8-unit plan comes back collapsed into
a 5-task generic pipeline that rebuilds things the plan said already existed.
Worse, it **hallucinates work from the title alone** — a real "discovery tiling"
plan got decomposed into a Postgres `tsvector` full-text search and a
`viewport_presets` CRUD table, neither anywhere in the plan, and workers built and
opened PRs for both before anyone noticed.

So the split is your job, done here. A plan that already enumerates discrete tasks
is **done being decomposed**: each `## [task]` section is handed straight to task
creation as its own card. The auto-decomposer is only for a genuinely rough idea
that *wants* fleshing out — which is not what this skill produces.

## How the plan is turned into cards (what the sections must support)

Whoever creates the board tasks reads this file and creates **one card per
`## [task]` section**, copying that section's body verbatim, on the Kanban board
named **`agency-audit`**. Concretely:

- **All cards land on the `agency-audit` board.** State this in the plan so nobody
  has to guess the target board when creating the tasks.
- **Each worker starts cold.** A worker sees only its own card body — not the other
  sections, not this file's shared preamble unless it was copied in. So each task's
  body must carry everything the worker needs on its own.
- **Every task is assigned to the `agency-audit-developer` worker and runs in
  worktree mode.** State this in the plan; it is constant for this project.
- **Dependencies are `blocked-by`.** A task with a blocker is not started until the
  blocker is `done`. Express each dependency in prose ("Blocked by: <task title>")
  so it can be wired directly. Tasks with no blocker run in parallel.
- **Verify stated prerequisites before relying on them.** If a task assumes
  something is "already on master", confirm it actually is — a symbol name or a
  gate file existing is not proof the change landed. Only then write the dependent
  task as unblocked.

## How to write the plan

1. **Open with a shared preamble to prepend to every task body.** A few sentences
   of orientation the cold worker needs: what agency-audit is (Python 3.14,
   asyncpg/PostgreSQL, Typer CLI, FastMCP server, FastAPI/Jinja2 web app,
   orchestration `loop`), that the checkout is at `/opt/data/workspace/agency-audit`,
   that `AGENTS.md` is the source of truth, and the merge gate every change must
   pass (`scripts/qa.sh` exits green: lint, format check, mypy, tests). Mark this
   block clearly as "prepend to every task body" so each card carries it. Keep it
   tight. State once, up front, that every task is a card on the `agency-audit`
   Kanban board.

2. **State the standing constraints once, in that preamble.** One small change per
   task, on its own branch, reviewable in a single pass, with tests riding along;
   assigned to `agency-audit-developer`; runs in worktree mode; pushed as a PR
   against `master` (the worker does not merge). These carry into every card.

3. **Write one `## [task]` section per unit of work**, each with: a one-line goal,
   the concrete location (`file.py:lines`, a migration, a route), the approach, an
   explicit **acceptance bar** ("done when a test asserts …"), and the specialty it
   needs ("Python + pytest", "SQL migration", "FastAPI route test"). Precise
   locations and a testable bar let a cold worker act without rediscovering the
   problem. If a task cannot be proven by a single focused test, it is too big —
   split it.

4. **Make dependencies explicit.** End each dependent section with `Blocked by:
   <the task it waits on>`. Describe genuinely independent tasks as independent so
   they run in parallel. Order the sections so the ones that must go first appear
   first.

5. **Split to the granularity you want cards to land at.** Each `## [task]` becomes
   one card, so enumerate exactly the units you want. Do not leave seams for
   someone else to find — that is the mistake this skill exists to prevent.

## Output format

```markdown
# <Plan title>

## Shared context — prepend to every task body

Each `## [task]` below is one card on the **`agency-audit`** Kanban board.
`agency-audit` ("Real Estate Radar") … <stack, checkout path, AGENTS.md pointer>.
Merge gate: `scripts/qa.sh` exits green (lint, format check, mypy, tests). Every
task is one small PR on its own branch off `master`, tests riding along, assigned
to the `agency-audit-developer` worker in worktree mode; push a PR and let a human
merge.

## [task] <first task title>

Goal: …
Location: `path/to/file.py:120-160`, migration `00N_*.sql`, …
Approach: …
Acceptance: done when a test asserts …
Specialty: Python + pytest.

## [task] <second task title>

Goal: …
…
Blocked by: <first task title>.
```

## Checklist before you submit

- [ ] Saved to `docs/prompts/<slug>.md` with the plan title as the `# heading` — not left in chat.
- [ ] The work is fully decomposed here: one `## [task]` section per card, no seams left to split.
- [ ] A shared preamble carries stack, checkout path, the `scripts/qa.sh` gate, and the standing constraints — and says to prepend it to every task body.
- [ ] The plan states the target Kanban board is `agency-audit`.
- [ ] Every task names `agency-audit-developer` + worktree mode (via the preamble).
- [ ] Each task has a concrete location and a single-test acceptance bar.
- [ ] Dependencies are stated as `Blocked by: …`; independent tasks read as independent.
- [ ] Any "already on master" prerequisite has been verified, not assumed.
- [ ] Highest-priority / unblocking tasks come first.
- [ ] Nothing in the file assumes it will be auto-decomposed.
