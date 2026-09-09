# Knowledge Base

Knowledge Base gives an AW Workspace searchable memory over project documentation, skills, notes, and selected repository content. It helps users and agents find relevant context before making decisions or editing code.

## What It Does

- Indexes workspace knowledge so it can be searched semantically.
- Supports text search for exact names, terms, and paths.
- Provides a browser for reading and updating knowledge entries.
- Offers agent tools for searching, adding, updating, deleting, and loading skill guidance.
- Stores its index and data so knowledge survives restarts.

## Why Use It

Use this app when a workspace has more context than one person or one agent can keep in memory. It is useful for architecture notes, lessons learned, project decisions, skill instructions, and documentation that should influence future work.

## How To Use It

Install the app and open Knowledge Base from the workspace navigation. Search for a project, feature, decision, or file topic before starting work. Agents can also search it directly when they need project context.

## What It Delivers

The app gives AW Workspace a shared memory layer. It reduces repeated discovery work and helps users and agents act with the project’s existing context in view.

## Testing, coverage and lint

Pilot implementation of `docs/standards/pipeline-testing.md` (see that doc for the
full design) — the first real-repo rollout of the estate-wide standard.

- **Coverage gate**: `pytest --cov=kb_app`, gated by `[tool.coverage.report] fail_under`
  in `pyproject.toml`. **One-way ratchet — this number may only go up, never down**
  (lowering it needs a written reason in the commit message). Baseline measured
  2026-09-09: 48% before this card's own tests, 53% after (`self_register.py` and
  `main.py` went from 0% covered to 100%/89%).
- **Lint gate**: `pylint kb_app --disable=all --enable=E --disable=E0401,E1101`,
  blocking in CI (`.github/workflows/build.yml`). `E0401` (import-error) is
  disabled per the standard — it fires on this repo's lazy third-party imports.
  `E1101` (no-member) is also disabled here: astroid cannot infer psycopg3's
  generically-typed `Connection` through `kb_pg.py`/`exec_pg.py`'s
  `_conn = None` + lazily-imported `psycopg.connect()` pattern, and flags every
  real method call on the connection as a false positive. Verified by reading
  the source, not assumed — every flagged member (`.closed`, `.execute`,
  `.commit`, `.close`) exists on the real `psycopg.Connection`.
- **pre-commit**: `pip install -r kb_app/requirements-dev.txt && pre-commit install`,
  then hooks run on `git commit` (ruff, the same blocking pylint E-class check,
  manifest validation, whitespace/EOF hygiene). Bypassable with `--no-verify`;
  CI is the real gate, this is the fast local subset.
