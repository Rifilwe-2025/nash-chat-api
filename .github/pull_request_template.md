## Phase / scope

<!-- Which phase of .docs/IMPLEMENTATION_PLAN.md does this land, or what standalone slice is it? -->

## What this delivers

-

## Verification

- [ ] `ruff check .`
- [ ] `ruff format --check .`
- [ ] `mypy`
- [ ] `pytest`
- [ ] Manual check (describe below)

<!-- Paste the relevant output or describe what you exercised by hand. -->

## Architecture check

- [ ] Routers stay thin — no SQLAlchemy or business rules in `presentation/api`
- [ ] Repositories `flush()`, never `commit()`
- [ ] No cross-module imports of another module's `internal/`
- [ ] Every endpoint returns `ApiResponse` / `PaginatedResponse`
- [ ] Config read through `src.configs`, not the environment
- [ ] New endpoints documented in Swagger: tag (described in `src/core/openapi.py`), summary,
      description, response model, and `responses` for each meaningful failure

## Notes / follow-ups

<!-- Anything deliberately deferred, and why. -->
