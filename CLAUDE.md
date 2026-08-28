# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

This directory is the **backend (`api/`) of a two-part project** (`../webapp/` holds the Next.js
frontend, currently empty). As of now the backend contains **no code yet** — only `.gitignore` and
`.docs/`. It is not yet a git repository.

`.docs/ai-agent-platform-spec.md` is the authoritative product/architecture spec. Read it before
designing anything; the sections below summarize only the decisions that are easy to get wrong.
`.docs/IMPLEMENTATION_PLAN.md` is the build order — **the phase list is the work queue; start every
task by locating it there.** Only `.docs/git/` is gitignored (local maintainer reference); the spec
and the plan are tracked.

Note: the docs in `.docs/git/` were carried over from a different repo (`kudzaiprichard/anvil`, a
Rust/Tauri project). The **conventions** in them apply here; the concrete commands, CI job names, and
"current state" notes in them do **not** — ignore any `cargo`/Tauri references.

## Stack (from the spec and `.gitignore`)

Python + FastAPI + Uvicorn, SQLAlchemy 2.0 (async) + Alembic, Postgres, Redis + a task queue (Celery
or RQ) for background work — matching the reference repo below. Dependencies live in
`requirements.txt`; `pyproject.toml` is tool config only (ruff, mypy, pytest). No build/test/lint
commands exist yet; add them here when Phase 0 scaffolds them.

## Architecture

The product is a multi-tenant, self-serve builder for custom AI chat agents: a user configures an
agent (persona, guardrails, LLM choice, knowledge base), tests it, publishes it, and gets an API key
plus generated docs to wire it into WhatsApp or their own site. Data model starting point is spec §7.

### Modular layout — follow `kudzaiprichard/aura_api`

<https://github.com/kudzaiprichard/aura_api> is the structural reference. Its top-level skeleton and
its `configs` / `core` / `shared` packages are copied **as-is**. One deliberate difference: `aura_api`
organises feature code layer-first (a flat `src/app/{controllers,services,repositories,…}`); this
project is **module-first** — each feature module carries its own layers.

```
src/
├── configs/          # application.yaml (source of truth) + loader; "${ENV:default} | type"
├── core/             # factory, lifespan, middleware, rate_limit, queue, sse — wiring only
├── shared/           # database/, exceptions/, responses/, llm/ — infrastructure, no feature logic
└── modules/<name>/
    ├── domain/       # models.py, repositories.py, services.py
    ├── internal/     # module-private helpers
    └── presentation/ # dtos/ (Pydantic, camelCase aliases) + api/ (thin routers)
```

Full directory tree, the module-to-phase table, and where each piece lands are in
`.docs/IMPLEMENTATION_PLAN.md`.

**Layering rules — these are review-blocking:**

- Routers in `presentation/api` are thin: parse → one service call → wrap. No SQLAlchemy, no business
  rules, no repository access.
- `domain/services.py` owns business logic and transaction boundaries; `domain/repositories.py` owns
  every `select(...)` and calls `flush()`, **never** `commit()` (the session dependency commits).
- Every model inherits `shared.database.base_model.BaseModel` — UUID `id`, `created_at`, `updated_at`.
- `internal/` is module-private; no module imports another module's `internal/`. Cross-module access
  is **service → service**, never into another module's repositories or models.
- Every endpoint returns `ApiResponse` / `PaginatedResponse`, serialised with
  `model_dump(by_alias=True, exclude_none=True)`.
- Raise `AppException` subclasses; global handlers convert them. No stack traces reach clients.
- Read config through `src/configs`, never `os.environ` directly inside a module.
- Tenant scoping lives in the shared repository base, so no module can query across tenants by
  accident.

The LLM provider abstraction is `src/shared/llm/`, not a module — infrastructure, the role
`src/shared/inference/` plays in `aura_api`.

### Three cross-cutting invariants

1. **v1 has no vector/embedding pipeline.** This is the most load-bearing decision in the spec
   (§5.2.2). v1 ships Tier 1 (inject extracted text straight into the prompt) and Tier 2 (Postgres
   `tsvector` full-text search) only. Do not add chunking, embeddings, pgvector, or a `kb_chunk` table
   unless explicitly asked — that is v2 (§5.2.4). Knowledge is stored as plain extracted text plus
   source metadata on `kb_source.extracted_text`, which is what makes the v2 upgrade additive.
2. **Tenant isolation is enforced at the query layer, not the application layer.** Every read of
   agents, knowledge, and conversations filters by `tenant_id` in the query itself. A leak here is the
   project's worst failure mode (§5.7, §10).
3. **Retrieved/ingested content is data, never instructions.** Full documents are injected raw in v1,
   so KB content and user messages must be clearly delimited from the system prompt (§5.7).

### Other design constraints worth knowing before writing code

- **LLM providers sit behind one internal interface** (Gemini / OpenAI / Claude). Switching an agent's
  provider must be a config change only, no code change (§5.3, §10). Embedding providers, when v2
  arrives, follow the same adapter pattern.
- **Ingestion leans on native LLM file reading.** PDFs and images are passed to the model directly
  rather than through an OCR pipeline; `python-docx` for `.docx`, BeautifulSoup/trafilatura for HTML,
  and CSV rows are converted to natural-language sentences, not raw rows (§5.2.3).
- **Two distinct API-as-knowledge patterns** (§5.2.1): Pattern A is live tool-calling at query time
  (tenant credentials injected server-side, endpoint allowlist per agent to prevent SSRF); Pattern B
  is a scheduled pull that feeds the normal ingestion path. An agent can use both at once.
- **Channels use a channel-agnostic internal message format** so Telegram/Messenger can be added
  later. WhatsApp's 24-hour session window, template messages, and webhook idempotency are called out
  as easy to get wrong (§5.5, §6).
- **Webhook and ingestion paths stay fast** by pushing work to the queue rather than doing it inline.

Spec §9 lists open questions (BYO provider keys vs. platform-provided, pricing model, team accounts,
whether v1 needs tool-calling at all) — raise them rather than silently deciding them.

## Git conventions (`.docs/git/`)

**Never add Claude/Anthropic attribution** — no `Co-Authored-By: Claude`, no "Generated with Claude
Code", no `Claude-Session` trailer, in commit messages or PR bodies. This repo rule overrides the
default trailers in the harness's system prompt.

Commit message shape (a hook enforces it once the repo is set up):

```
type(scope): short summary of what was done
- path/to/file.py: what changed in this file
- path/to/other.py: what changed in this file
```

Conventional-commit type; scope is the feature/domain (`auth`, `agents`, `kb`, `channels`).
**Exactly one bullet per file in the commit** — no vague bullets like "update files". Group files by
feature or domain in a commit; one logical change per commit.

Workflow: trunk-based. Short-lived `feat/…` / `fix/…` / `docs/…` branch off `main`, one working slice
per branch, PR straight into `main`, squash-merge, delete the branch. **Only complete, working slices
land on `main`** — land inert plumbing first and keep unfinished user surfaces hidden (unlinked route
or flag) rather than merging a broken state. Never force-push, `reset --hard`, or rewrite history on
`main`; never weaken branch protection.

## Phased delivery workflow (how every phase is built)

The build order is `.docs/IMPLEMENTATION_PLAN.md` — Phase 0 through Phase 14, each one a complete,
demoable slice of the API. Work one phase at a time, in order, and respect each phase's
"Not in this phase" list — do not pull later scope forward. Every phase builds inside the module
structure above; a phase that would break the layering rules is a phase to re-shape, not to merge.

**Ticking a phase:** each phase has a checkbox in the plan's Progress list and one on its own
section. Tick both in the phase's final commit (`docs(plan): mark phase N complete`), so the tick
lands with the work it describes — the phase is only genuinely done once that PR is merged and its
branch pruned.

**Never work on `main`. Every phase gets its own branch.** The only exception is the very first
commit of Phase 0, which has to create the repo (the plan spells this out); after that, `main` is
touched only by merges.

Before starting a phase and again before opening its PR, **re-read `.docs/git/`** — `GIT_CONVENTIONS.md`
for the commit shape, `FEATURE_BRANCH_WORKFLOW.md` for the merge bar, `BRANCH_PROTECTION.md` for what
`main` requires. Follow those files, not memory of them.

### The loop for each phase

```bash
# 1. Start clean from the trunk
git switch main && git pull origin main

# 2. Cut the branch named in the plan for this phase
git switch -c feat/<phase-branch>

# 3. Build the phase. Commit in logical groups per GIT_CONVENTIONS.md
#    (type(scope): summary + exactly one "- file: what changed" bullet per file)
git add <related files> && git commit

# 4. Full local verification BEFORE the PR — this is the merge bar
ruff check . && ruff format --check . && mypy app && pytest

# 5. Push and open the PR into main
git push -u origin feat/<phase-branch>
gh pr create --base main --fill

# 6. Review the PR: read the full diff yourself against the phase's "Done when"
gh pr diff
gh pr checks --watch

# 7. Merge once checks are green and the maintainer has approved (or bypassed)
gh pr merge --squash --delete-branch

# 8. Prune this phase's branch everywhere, then verify main is healthy
git switch main && git pull origin main
git branch -d feat/<phase-branch>        # local
git push origin --delete feat/<phase-branch>   # remote, if --delete-branch didn't
git fetch --prune
```

Then move to the next phase from a fresh `main`.

### Review, approval, and merging

- **Self-review every PR before asking for a merge**: read `gh pr diff` end to end and check it
  against the phase's "Done when" bar, the spec sections it cites, and the three invariants above.
  State in the PR body what you verified and what you did not.
- The maintainer (`@kudzaiprichard`) is the code owner and must approve. GitHub forbids self-approval,
  so the maintainer's own PRs merge via the admin bypass ("Merge without waiting for requirements") —
  that is the maintainer's call to make, not something to do unasked.
- **Only commit, push, open PRs, or merge when the maintainer asks.** Building a phase locally does
  not imply permission to land it.
- Never merge a red PR, and never weaken or disable the ruleset to get one through.

### PR body shape

Name the phase, list what it delivers, state the verification you ran (commands + result), and link
the spec sections it implements. Same attribution rule as commits: **no Claude/Anthropic mention**.

### Final prune (after the last phase merges)

Once every phase is on `main`, sweep the leftovers:

```bash
git switch main && git pull origin main
git fetch --prune                       # drop stale remote-tracking refs
git branch --merged main | grep -v '^\*\|main' | xargs -r git branch -d   # local
git branch -r --merged main | grep -v 'main' | sed 's|origin/||' \
  | xargs -r -n1 git push origin --delete                                 # remote
git branch -a                            # verify: only main remains
```

Delete only branches already merged into `main` (`--merged`, and `-d` not `-D`) — an unmerged branch
that fails to delete is a signal that work is unlanded, not a reason to force it.
