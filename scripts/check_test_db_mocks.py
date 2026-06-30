#!/usr/bin/env python3
"""Reject database-mock patterns in tests; steer authors to the real database.

Why this exists
---------------
Mocking an ``asyncpg`` connection proves only that our Python wrapper passes a
string and some parameters to ``conn.fetch``/``execute``/``executemany`` — it
never sends that SQL to Postgres. So mock-only tests cannot catch invalid SQL,
broken migrations, wrong JOIN/GROUP BY/HAVING semantics, or the classic
``executemany() returns None`` bug. CI already provisions a real Postgres
database (``.github/workflows/agency-audit-ci.yml``) and seeds it via
``scripts/seed-test-db.py``; new database tests must use it.

Ruff can't express this rule (it has no custom-rule API, and its only relevant
feature — banned imports — would flag the legitimate HTTP/Playwright mocks too),
so this standalone check runs alongside ruff in ``scripts/qa.sh`` and CI.

How to satisfy it
-----------------
Write an integration test against the live database instead of a mock. Follow
the established pattern in ``tests/test_mcp_server.py``: a ``db_conn`` fixture
that connects with ``asyncpg.connect(dsn=settings.dsn)`` and ``pytest.skip``s
when no database is reachable, plus per-test cleanup of the rows you insert.

Escape hatch
------------
For the rare case where a flagged ``.execute(...)``/``.fetch(...)`` mock is
genuinely *not* a database call (e.g. a subprocess or socket), add the inline
comment ``# db-mock-check: ignore`` on that line.

The ratchet
-----------
``KNOWN_DEBT`` grandfathers the files that already mock the database so this
check does not turn CI red on the existing backlog. As each file is migrated to
the real database, remove it from ``KNOWN_DEBT`` — the check fails if a listed
file no longer contains any mocks (so the list can't silently re-open the door).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1] / "tests"

# Files where mocking is correct *by design* — they test connection/runner
# plumbing, not SQL semantics, so they are permanently exempt.
ALLOWED: dict[str, str] = {
    "test_db.py": "tests pool lifecycle (get_pool/close_pool), not SQL",
    "test_migrations.py": (
        "tests the migration runner's version-tracking logic; the migration SQL "
        "itself is exercised against real Postgres by scripts/seed-test-db.py"
    ),
}

# Files that already mock the database and are scheduled for migration to the
# real DB. Grandfathered so the check passes today. Remove an entry once its
# file no longer mocks the database (the check enforces that — see below).
KNOWN_DEBT: set[str] = {
    "test_discovery_pipeline.py",
    "test_loop.py",
    "test_loop_coverage.py",
    "test_mcp_server.py",  # has real-DB tests too; the mocked unit tests remain
    "test_orchestrator_errors.py",
    "test_web_app.py",
}

IGNORE_MARKER = "db-mock-check: ignore"

# Unambiguous signals that a test is mocking the asyncpg database layer.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "patching the connection pool",
        re.compile(r"""patch\(\s*["'][^"']*\.(get_pool|create_pool)["']"""),
    ),
    (
        "stubbing an asyncpg cursor method return value",
        re.compile(r"\.(fetch|fetchrow|fetchval|execute|executemany)\.(return_value|side_effect)"),
    ),
]

GUIDANCE = """\
Database mocks are not allowed in new tests.

A mocked asyncpg connection only checks that your wrapper forwarded a SQL string
and some parameters — it never runs the SQL, so it cannot catch invalid SQL, a
broken migration, wrong JOIN/GROUP BY/HAVING results, or an executemany() that
silently updates nothing.

Write a real-database integration test instead. CI provisions Postgres and seeds
it (scripts/seed-test-db.py); follow the db_conn fixture pattern in
tests/test_mcp_server.py (connect via settings.dsn, pytest.skip when no DB is
reachable, clean up the rows you insert).

If a flagged line is genuinely not a database call, append the comment
'# db-mock-check: ignore' to that line.
"""


def _scan(path: Path) -> list[tuple[int, str, str]]:
    """Return (line_no, label, source) for each DB-mock signal in *path*."""
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if IGNORE_MARKER in line:
            continue
        for label, pattern in PATTERNS:
            if pattern.search(line):
                hits.append((lineno, label, line.strip()))
    return hits


def main() -> int:
    violations: list[str] = []
    notices: list[str] = []

    test_files = {p.name: p for p in TESTS_DIR.rglob("test_*.py")}

    # Listed files must exist, so the lists can't quietly rot.
    for name in (*ALLOWED, *KNOWN_DEBT):
        if name not in test_files:
            violations.append(
                f"{name}: listed in check_test_db_mocks.py but no such test file exists. "
                "Remove the stale entry."
            )

    for name, path in sorted(test_files.items()):
        if name in ALLOWED:
            continue

        hits = _scan(path)

        if name in KNOWN_DEBT:
            # Ratchet: once a debt file is clean, force its removal from the list
            # so it can never silently regress back to mocking the database.
            if not hits:
                violations.append(
                    f"{name}: no longer mocks the database — remove it from KNOWN_DEBT "
                    "in scripts/check_test_db_mocks.py to lock in the migration."
                )
            else:
                notices.append(f"  - tests/{name} ({len(hits)} mock signal(s))")
            continue

        for lineno, label, source in hits:
            violations.append(f"tests/{name}:{lineno}: {label}\n      {source}")

    if notices:
        print("Known database-mock debt (grandfathered, pending migration to real DB):")
        print("\n".join(sorted(notices)))
        print()

    if violations:
        print(GUIDANCE)
        print("Violations:")
        for v in violations:
            print(f"  {v}")
        print(f"\n{len(violations)} violation(s).")
        return 1

    print("check-test-db-mocks: OK (no new database mocks).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
