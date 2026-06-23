---
name: draft-triage-prompt
description: >-
  Write a triage-task prompt for the Hermes Kanban decomposer so a body of work
  on the agency-audit project fans out into a good graph of child tasks for AI
  workers. Use whenever turning a goal into Hermes work: feature development,
  refactors, bug-fix batches, test-coverage drives, config/packaging cleanup,
  ops, or documentation. Produces the title + body of a single Triage card.
---

# Draft a Hermes triage prompt

## What you are producing

A **triage task** for the Hermes Kanban board: a short **title** and a **body**.
You drop it into the board's `triage` column (via `hermes kanban create`, the
dashboard's inline-create, or `/kanban`), and the Hermes **decomposer** turns it
into a small graph of child tasks that AI workers pick up and implement.

The body is the prompt. This skill is about writing a body the decomposer can
split well. Read `AGENTS.md` at the repo root first — it is the source of truth
for agency-audit's commands, layout, conventions, and the test/lint gate, and the
orientation you bake into the body comes from there.

## How the decomposer reads your task

The decomposer is an auxiliary LLM. When a task sits in `triage` (automatically
when `kanban.auto_decompose` is on, or when you run `hermes kanban decompose <id>`
or click **⚗ Decompose**), Hermes sends it:

- the task **title** and **body**,
- the roster of **profiles** installed on the machine, each with its description,
- a **default assignee** for work that matches no profile.

It returns one of two outcomes:

- **Fan-out** — a list of child tasks, each with its own `title`, `body`,
  `assignee`, and `parents` (dependencies). The children are created, linked under
  your original task, and dispatched to workers. Children with no parents run in
  **parallel**; a child with parents waits until every parent is `done`.
- **Single task** — when the work is genuinely one unit, it tightens your title
  and body and assigns it, with no fan-out.

The decomposer aims for a **small graph, roughly 2–6 child tasks** — cohesive
units, neither one giant task nor a swarm of tiny ones. It assigns each child to
the **profile whose description best fits** that unit of work, falling back to the
default assignee when nothing fits well. It reads the body as prose and decides
the split from the content, so clear structure and explicit dependencies in the
body are what shape a good graph.

Two facts about the workers downstream shape how you write:

- **Each worker starts cold.** A worker reads only its own child `body` (and the
  task's comment thread) — not your other children, not the original triage card.
  Whatever a unit needs to be done correctly must live in that unit's description,
  so the orientation and constraints you write need to be propagatable into every
  child.
- **The body is truncated to about 4000 characters** before the decomposer sees
  it (title to about 400). Be complete but tight.

## How to write the body

Aim for a body a careful reader could split into 2–6 units, each independent where
possible, each with a clear goal and a checkable bar.

1. **Lead with orientation (a few sentences).** What agency-audit is, the stack
   (Python 3.12, asyncpg/PostgreSQL, Typer CLI, FastMCP server, FastAPI/Jinja2
   web app, orchestration `loop`), where the checkout is, and the gate every
   change must pass: `uv run --extra dev pytest` and `uvx ruff check .` both
   clean, with tests never requiring a live database (mock the pool as
   `tests/test_loop.py` does). This is what lets each child stand on its own.

2. **State the standing constraints once.** One small change per unit, on its own
   `fix/<slug>` or `feat/<slug>` branch off `master`, reviewable in a single pass,
   with unit tests riding along. These carry into each child's description.

3. **Describe each unit of work** with: the goal, the concrete location
   (`file.py:lines`, a migration, a route), the approach, and an explicit
   **acceptance bar** ("done when a test asserts …"). Precise locations and a
   testable bar let a cold worker act without rediscovering the problem. If a unit
   cannot be proven by a single unit test, it is too big — split it.

4. **Make dependencies explicit in prose.** Say plainly what must land before what
   ("the test-gate fix lands before the coverage work"); the decomposer turns that
   into `parents`. Describe genuinely independent units as independent so they fan
   out in parallel.

5. **Right-size to the 2–6 graph.** If the work has more atomic pieces than that,
   group them into cohesive child-sized chunks (e.g. "cover the audit pipeline
   modules" as one unit rather than one per module), or split the effort across
   several triage tasks and let each decompose on its own.

6. **Match work to a describable specialty.** Phrase each unit so its nature is
   obvious — "Python + pytest implementation", "SQL migration", "FastAPI route
   tests" — so the decomposer can route it by the kind of work involved.

## Checklist before you submit

- [ ] Body is self-orienting: stack, checkout, and the pytest + ruff gate are stated.
- [ ] Standing constraints (one small change, own branch, tests ride along) appear once.
- [ ] Each unit has a concrete location and a single-unit-testable acceptance bar.
- [ ] Dependencies are stated in prose; independent units read as independent.
- [ ] The work groups into roughly 2–6 cohesive units (or is split across triage tasks).
- [ ] Each unit is described by the specialty it needs.
- [ ] Body stays well under ~4000 characters.
