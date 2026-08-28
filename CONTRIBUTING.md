# Contributing

## Getting set up

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env            # then set DATABASE_URL for your local Postgres
git config core.hooksPath .githooks
python main.py                  # http://127.0.0.1:8000/health
```

`docker compose up -d postgres redis` starts the dependencies if you would rather not run them
natively. `docker compose --profile full up` also builds and runs the API container.

Apply migrations before first run:

```bash
alembic upgrade head
```

## Before you open a PR

All four must be green:

```bash
ruff check .
ruff format --check .
mypy
pytest
```

`pytest` needs a running Postgres. It creates and migrates its own database (`DATABASE_TEST_URL`,
`nashdb_test` by default) and rolls back every test in a transaction, so it never touches your
development data. Redis is not required — the tests that cover it use stubs.

If you changed `src/configs/application.yaml`, regenerate the type stub and commit it:

```bash
python -m src.configs.generate
```

If you added an endpoint, it must appear correctly in Swagger UI (`/docs`): a router `tags=[...]`
with the tag described in `src/core/openapi.py`, plus a `summary`, a `description`, an explicit
`ApiResponse[...]` response model, and a `responses={...}` entry per meaningful failure. CI fails on
any route missing a tag or summary.

## How changes land

`main` is protected: no direct pushes. Branch, PR, green CI, code-owner approval, squash-merge,
delete the branch.

```bash
git switch main && git pull origin main
git switch -c feat/short-description
# ...work, verify, commit...
git push -u origin feat/short-description
gh pr create --base main --fill
```

Only complete, working slices land on `main`. If a change needs plumbing before its user-facing
surface exists, land the inert plumbing first and keep the unfinished surface unreachable until it
works.

## Commit messages

```
type(scope): short summary of what was done
- path/to/file.py: what changed in this file
- path/to/other.py: what changed in this file
```

Conventional-commit type (`feat`, `fix`, `refactor`, `chore`, `style`, `docs`, `test`, `ci`), scope
is the module or domain. **Exactly one bullet per file in the commit** — the `commit-msg` hook
rejects anything else.

## Architecture

The layout and the layering rules are in `CLAUDE.md`; the build order is
`.docs/IMPLEMENTATION_PLAN.md`. In short: feature code lives in `src/modules/<name>/` split into
`domain/` (models, repositories, services), `internal/` (module-private helpers), and
`presentation/` (`dtos/` + `api/`). Routers stay thin, repositories never `commit()`, and no module
imports another module's `internal/`.
