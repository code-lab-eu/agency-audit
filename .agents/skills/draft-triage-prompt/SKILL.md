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
into a graph of child tasks that AI workers pick up and implement.

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

It assigns each child to the **profile whose description best fits** that unit of
work, falling back to the default assignee when nothing fits well. It reads the
body as prose and decides the split from the content, so clear structure and
explicit dependencies in the body are what shape a good graph.

**You set the granularity by how explicitly you split the work.** Enumerate
discrete units in the body — each with its own location and acceptance bar — and
the decomposer emits one child per unit. State a goal in broader strokes and it
groups the work into cohesive chunks and finds the seams itself. Write at the
level you want the cards to land.

**Decomposition is iterative.** The original triage task stays alive as the parent
of every child. When the first wave of children completes, the orchestrator
profile that owns the root wakes back up, re-reads the full plan, and adds more
tasks if the work isn't finished. So a large plan is worked in waves — a first
batch of children, then more as each batch lands — and never has to emerge from a
single pass. Order your body so the units that should run first come first.

Two facts about the workers downstream shape how you write:

- **Each worker starts cold.** A worker reads only its own child `body` (and the
  task's comment thread) — not your other children, not the original triage card.
  Whatever a unit needs to be done correctly must live in that unit's description,
  so the orientation and constraints you write need to be propagatable into every
  child.
- **Keep the body tight and front-load it.** A single decompose pass reads only
  the front of a long body, while the orchestrator picks up the rest across later
  waves. Lead with the highest-priority units and keep each unit's spec concise.

## How to write the body

Write a body where each unit of work has a clear goal and a checkable bar, kept
independent of the other units wherever possible.

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

5. **Write at the granularity you want cards to land.** Enumerate discrete,
   fully-specified units for a card each; state a goal in broader strokes to let
   the decomposer find the seams. Keep every unit independent and self-contained,
   since a large plan is worked in waves.

6. **Match work to a describable specialty.** Phrase each unit so its nature is
   obvious — "Python + pytest implementation", "SQL migration", "FastAPI route
   tests" — so the decomposer can route it by the kind of work involved.

## Example

A triage task whose body enumerates discrete units, names a dependency, and gives
each unit a checkable bar.

> **Title:** Trustworthy test gate + audit-pipeline coverage
>
> **Body:**
>
> `agency-audit` ("Real Estate Radar") discovers and audits real estate agency
> sites: Python 3.12, asyncpg/PostgreSQL, a Typer CLI, a FastMCP server, a
> FastAPI/Jinja2 web app, and an orchestration `loop`. Checkout is the repo root;
> see `AGENTS.md` for layout and conventions. Every change is one small,
> single-pass PR on its own `fix/<slug>` or `feat/<slug>` branch off `master`,
> with unit tests riding along. The gate each PR must pass: `uv run --extra dev
> pytest` and `uvx ruff check .` both clean, and tests never require a live
> database — mock the pool as `tests/test_loop.py` does.
>
> Do the gate fix first; the coverage work depends on it:
>
> - **Green pytest with no Postgres.** `tests/test_mcp_server.py` opens a real
>   `asyncpg.connect(...)` in setup/teardown, so its cases error when no database
>   is up. Rebuild it on the mocked-pool pattern, or gate it behind a fixture that
>   skips when the DB is unreachable. Done when `uv run --extra dev pytest` is
>   fully green with no Postgres running. (Python + pytest.)
>
> Once the gate is green, these run in parallel:
>
> - **Cover `audit/robots.py`.** Fixture-driven unit tests for fetch/parse,
>   crawl-delay, sitemap extraction, and default-allow-when-absent; mock httpx, no
>   live network. Done when each branch is asserted and `ruff` is clean for the
>   file. (Python + pytest.)
> - **Cover `audit/scoring.py`.** Unit tests for `compute_score` and its breakdown
>   arithmetic against fixture inputs. Done when the score and each breakdown
>   component are asserted. (Python + pytest.)

## Checklist before you submit

- [ ] Body is self-orienting: stack, checkout, and the pytest + ruff gate are stated.
- [ ] Standing constraints (one small change, own branch, tests ride along) appear once.
- [ ] Each unit has a concrete location and a single-unit-testable acceptance bar.
- [ ] Dependencies are stated in prose; independent units read as independent.
- [ ] Each unit is genuinely independent and self-contained.
- [ ] Highest-priority units come first, so they decompose in the first wave.
- [ ] Each unit is described by the specialty it needs.
