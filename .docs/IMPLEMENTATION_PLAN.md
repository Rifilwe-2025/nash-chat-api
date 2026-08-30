# API Implementation Plan (v1)

> The phase-by-phase build order for the **backend only** (`api/`). The frontend (`../webapp/`) is out
> of scope for every phase here — each phase must be verifiable through HTTP, tests, and the OpenAPI
> docs alone, with no UI.
>
> Source of truth for *what* to build: `.docs/ai-agent-platform-spec.md` (section refs below, e.g. §5.3).
> Source of truth for *how* work lands: `.docs/git/` + the "Phased delivery workflow" section of
> `CLAUDE.md`. **Every phase is done on its own branch and merged via PR. Never work on `main`.**

---

## Progress

Tick a phase only when its PR is **merged into `main` and its branch pruned locally and remotely**.
The tick goes in the phase's own final commit (`docs(plan): …`), so it lands with the work.

- [x] **Phase 0** — Repository & toolchain bootstrap · `chore/bootstrap-repo`
- [x] **Phase 1** — Core foundations: config, database, migrations, test harness · `feat/core-foundations`
- [x] **Phase 2** — Auth & multi-tenant isolation · `feat/auth-and-tenancy`
- [x] **Phase 3** — Agent CRUD, configuration & versioning · `feat/agent-crud`
- [x] **Phase 4** — LLM provider abstraction · `feat/llm-provider-abstraction`
- [x] **Phase 5** — Knowledge base: model, sources & extraction · `feat/kb-ingestion`
- [x] **Phase 6** — Retrieval: Tier 1 injection + Tier 2 keyword search · `feat/kb-retrieval-tiers`
- [x] **Phase 7** — Conversation engine & prompt assembly · `feat/conversation-engine`
- [x] **Phase 8** — Web chat API, API keys & integration docs · `feat/web-channel-and-api-keys`
- [x] **Phase 9** — Async workers, scheduled sync & API-as-source · `feat/workers-and-scheduled-sync`
- [x] **Phase 10** — WhatsApp channel · `feat/whatsapp-channel`
- [x] **Phase 11** — Agent tools: live API calls · `feat/agent-tools`
- [x] **Phase 12** — Analytics, logs & observability · `feat/analytics-and-observability`
- [x] **Phase 13** — Hardening & v1 release readiness · `chore/v1-hardening`
- [ ] **Phase 14** *(v1.1)* — Plan limits & usage metering · `feat/usage-metering`

---

## Architecture — modular, layered (modelled on `kudzaiprichard/aura_api`)

The reference repo is <https://github.com/kudzaiprichard/aura_api>. Its top-level skeleton,
`configs` / `core` / `shared` modules, response envelope, and layering rules are adopted **as-is**.
The one deliberate difference: `aura_api` organises the feature code layer-first (a flat
`src/app/{controllers,services,repositories,models,dtos,helpers}`); this project organises it
**module-first**, with each feature module carrying its own `domain` / `internal` / `presentation`
layers. Everything outside `src/modules/` should look like `aura_api`.

```
api/
├── main.py                     # uvicorn entrypoint
├── worker.py                   # queue worker entrypoint (Phase 9)
├── requirements.txt
├── alembic.ini
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
├── .env.example
└── src/
    ├── configs/                # application.yaml is source of truth; env vars override
    │   ├── application.yaml
    │   ├── loader.py
    │   └── generate.py
    ├── core/                   # app wiring — no business logic
    │   ├── factory.py          # create_app(): routers, handlers, middleware
    │   ├── lifespan.py         # startup/shutdown, pinned singletons on app.state
    │   ├── middleware.py       # raw ASGI request logging + X-Request-ID
    │   ├── rate_limit.py
    │   ├── queue.py            # Celery/RQ app + scheduler (Phase 9)
    │   └── sse.py              # streaming broker
    ├── shared/                 # cross-module infrastructure, no feature knowledge
    │   ├── database/           # base_model, engine, dependencies, repository, pagination
    │   ├── exceptions/         # AppException hierarchy + global error handlers
    │   ├── responses/          # ApiResponse / PaginatedResponse envelope
    │   └── llm/                # provider interface + adapters (Phase 4)
    └── modules/                # one package per feature domain
        └── <module>/
            ├── domain/         # models.py, repositories.py, services.py
            ├── internal/       # module-private helpers and machinery
            └── presentation/
                ├── dtos/       # Pydantic request/response models, camelCase aliases
                └── api/        # thin routers: HTTP → service call
```

### Modules and the phase that creates each

| Module | Phase | Owns |
|---|---|---|
| `system` | 0 | health / liveness / readiness |
| `tenants` | 1–2 | `tenant`, `user`, tenant context |
| `auth` | 2 | signup, login, JWT rotation, token records |
| `agents` | 3 | agent config, status, versioning |
| `knowledge_base` | 5–6 | KBs, sources, extraction, tiered retrieval |
| `conversations` | 7 | sessions, history, prompt assembly |
| `api_keys` | 8 | key issue/scope/revoke, per-key rate limits |
| `channels` | 8, 10 | `channels/web/`, `channels/whatsapp/` as sub-modules sharing one message format |
| `tools` | 11 | agent tool definitions + server-side execution |
| `analytics` | 12 | usage, cost, logs, quality signals |
| `billing` | 14 | plan quotas, metering hooks |

The LLM provider abstraction lives in `src/shared/llm/`, not in a module — it is infrastructure used
by several modules, the same role `src/shared/inference/` plays in `aura_api`.

### Layering rules (enforced in review, every phase)

- **`presentation/api`** routers are thin: parse, call one service, wrap the result. No SQLAlchemy, no
  business rules, no direct repository access.
- **`domain/services.py`** owns business logic and transaction boundaries.
- **`domain/repositories.py`** owns every `select(...)`; repositories call `flush()`, **never**
  `commit()` — the session dependency commits.
- **`domain/models.py`** inherits `shared.database.base_model.BaseModel` — UUID `id`, `created_at`,
  `updated_at` on every table.
- **`internal/`** is module-private. No other module may import from another module's `internal/`.
- **Cross-module calls go service → service.** Never reach into another module's repositories or
  models.
- **`presentation/dtos`** are Pydantic with camelCase aliases; every endpoint returns `ApiResponse` or
  `PaginatedResponse`, serialised with `model_dump(by_alias=True, exclude_none=True)`.
- **Errors** are raised as `AppException` subclasses and converted by global handlers. No stack traces
  reach clients.
- **Config** comes from `application.yaml` with the `"${ENV:default} | type"` override format — never
  read `os.environ` directly inside a module.
- **Tenant scoping** lives in the shared repository base (Phase 2), so no module can accidentally
  query across tenants.

New code that does not fit this shape does not merge — restructure it, or raise the mismatch before
building on it.

---

## How to read a phase

Each phase is one **complete, working slice** (per `.docs/git/FEATURE_BRANCH_WORKFLOW.md`): after it
merges, `main` builds, tests pass, and the API still runs. A phase is not "a folder of files" — it is a
capability you can demonstrate with `curl` or a test.

| Field | Meaning |
|---|---|
| **Branch** | The exact branch name to cut off `main` for this phase. |
| **Depends on** | Phases that must already be on `main`. |
| **Delivers** | The scope of the phase, by path. |
| **Not in this phase** | Deliberately deferred — do not pull it forward. |
| **Done when** | The verification bar for opening the PR. |

Phases are sequential unless "Depends on" says otherwise. If a phase turns out to be too big once
started, split it into `…-part-1` / `…-part-2` branches — each still a working slice, each its own PR.

Every phase ends the same way (full loop in `CLAUDE.md`): local verification green → PR into `main` →
self-review against "Done when" → merge → **prune the branch local and remote** → tick the box above.

---

## Phase 0 — Repository & toolchain bootstrap

- [x] **Complete** · **Branch:** `chore/bootstrap-repo` (see the bootstrap exception below)
**Depends on:** nothing

**Delivers**
- `git init`, initial commit on `main`, GitHub remote created, `main` pushed.
- The full directory skeleton above, with `__init__.py` in every package and `.gitkeep` where a
  directory is still empty — so later phases only fill in, never restructure.
- `requirements.txt` (FastAPI, Uvicorn, SQLAlchemy 2.0 async, Alembic, asyncpg, PyYAML, slowapi,
  httpx, pytest, pytest-asyncio, ruff, mypy) and `.env.example`, matching `aura_api`'s dependency
  style. `pyproject.toml` holds **tool config only** (ruff, mypy, pytest) — dependencies stay in
  `requirements.txt`.
- `src/configs/`: `application.yaml` + `loader.py` implementing the `"${ENV:default} | type"` override
  format; a typed settings accessor.
- `src/core/factory.py` (`create_app()`), `lifespan.py`, `middleware.py` (raw ASGI request logging +
  `X-Request-ID`); `main.py` entrypoint.
- `src/shared/responses/api_response.py` — the `ApiResponse` / `PaginatedResponse` envelope, plus
  `router.py`'s `create_router()` so every module's routes serialise with `exclude_none`/`by_alias`.
- `src/core/openapi.py` + Swagger UI (`/docs`), ReDoc (`/redoc`) and the schema (`/openapi.json`),
  paths configurable under the `docs` section — the documentation standard every later phase follows
  is in `CLAUDE.md`.
- `src/modules/system/` — the first module in the canonical shape, serving `GET /health`.
- `docker-compose.yml` (Postgres + Redis) and a `Dockerfile` for the API.
- `.github/workflows/ci.yml` — `ruff check`, `ruff format --check`, `mypy src`, `pytest` against a
  Postgres service container.
- `.github/CODEOWNERS`, PR template, `CONTRIBUTING.md` (contributor-facing summary of `.docs/git/`).
- `commit-msg` hook enforcing the `.docs/git/GIT_CONVENTIONS.md` message shape.
- Branch protection ruleset on `main` per `.docs/git/BRANCH_PROTECTION.md`, with required check names
  matching **this repo's** CI jobs (the names in that doc come from another project — copying them
  verbatim leaves every PR permanently unmergeable).

**Not in this phase:** any domain model, any endpoint beyond `/health`.

**Done when:** `docker compose up` starts the stack, `GET /health` returns a correct `ApiResponse`
envelope, `ruff`/`mypy`/`pytest` are green locally and in CI on a real PR, and a PR with a failing
check cannot be merged.

> **Bootstrap exception:** the repo does not exist yet, so the initial commit necessarily lands on
> `main` directly. Do that first, push, then cut `chore/bootstrap-repo` for the tooling, CI, and
> protection work and PR it. Enable the ruleset as the last step of this phase. From Phase 1 onward,
> **no commit is ever made on `main`.**

> **Deferred from this phase — branch protection is not enabled.** `Rifilwe-2025/nash-chat-api` is
> private on a GitHub Free plan, and GitHub restricts both rulesets and classic branch protection to
> public repositories on that plan (`403: Upgrade to GitHub Pro or make this repository public`).
> The ruleset JSON is ready to apply — PR required, 1 code-owner approval, stale approvals dismissed,
> squash-only, required check `Lint, types & tests`, no force-push, no deletion, admin bypass — the
> moment the repo goes public or the account upgrades. Until then the workflow in `CLAUDE.md` is
> followed by convention, not enforced by the server: **`main` still gets nothing but merged PRs.**
> What *is* enforced meanwhile: squash-only merges and automatic branch deletion (repo settings), CI
> on every PR and push, and the local `commit-msg` hook.

---

## Phase 1 — Core foundations: database, migrations, test harness

- [x] **Complete** · **Branch:** `feat/core-foundations`
**Depends on:** Phase 0

**Delivers**
- `src/shared/database/`: `engine.py` (async engine + session factory), `base_model.py` (UUID `id`,
  `created_at`, `updated_at`), `dependencies.py` (session dependency that owns commit/rollback),
  `repository.py` (generic base repository — `flush()`, never `commit()`), `pagination.py`.
- `src/shared/exceptions/`: `AppException` hierarchy + global handlers wired into `core/factory.py`,
  mapping to the error envelope with no stack-trace leakage.
- Alembic wired up (`alembic.ini`, `env.py` reading config from `src/configs`); migration `0001` for
  `tenant` and `user` (§7).
- `src/modules/tenants/domain/` — models and repositories for `tenant` and `user` (no endpoints yet).
- Test harness: pytest fixtures for an isolated test database, per-test transactional rollback, an
  `httpx.AsyncClient` app fixture, and factory helpers for seeding tenants and users.
- `system` module extended: liveness + readiness (readiness checks Postgres and Redis).

**Not in this phase:** auth endpoints, any other module.

**Done when:** migrations apply from empty to head and downgrade cleanly, the suite runs against a
real Postgres in CI, readiness reports DB/Redis status accurately, and an unhandled error returns the
envelope rather than a traceback.

---

## Phase 2 — Auth & multi-tenant isolation

- [x] **Complete** · **Branch:** `feat/auth-and-tenancy`
**Depends on:** Phase 1

**Delivers**
- `src/modules/auth/`: signup (creates tenant + first user), login, refresh, logout.
  - `internal/password_hasher.py`, `internal/token_provider.py`, `internal/token_cleanup.py`
  - `domain/models.py` — `token` records stored as SHA-256 hashes; login/refresh revoke prior tokens
    (the rotation pattern from `aura_api`)
  - `presentation/api/` — auth routes; `presentation/dtos/` — camelCase request/response DTOs
- `src/modules/tenants/`: `get_current_user` / `get_current_tenant` dependencies, `/me`, account
  endpoints.
- **The tenant-scoping layer** (§5.7) added to `shared/database/repository.py`: a tenant-scoped
  repository base that requires a `tenant_id` filter, so isolation is enforced in the query rather
  than in each endpoint. A helper that can return cross-tenant rows must be impossible to call by
  accident.

**Not in this phase:** roles/permissions within a tenant (out of scope for v1, §2), billing plans.

**Done when:** tests prove tenant B cannot read, update, or delete tenant A's rows through any exposed
route; a revoked or rotated token is rejected; unauthenticated requests fail consistently through the
shared envelope.

---

## Phase 3 — Agent CRUD, configuration & versioning

- [x] **Complete** · **Branch:** `feat/agent-crud`
**Depends on:** Phase 2

**Delivers**
- `src/modules/agents/` in the full module shape.
- `agent` table + migration (§7): name, persona, engagement rules, guardrails, `model_provider`,
  `model_config_json`, `status` (draft/published/paused), `version`.
- Typed configuration DTO — persona, tone, do's and don'ts, escalation triggers, restricted topics,
  fallback responses, model + temperature + max tokens (§5.1).
- Full CRUD on the tenant-scoped repository base; publish / pause / unpublish transitions with
  validation (e.g. cannot publish without a provider configured).
- Config versioning in `domain/services.py`: every update snapshots the previous config; list versions
  and roll back.

**Not in this phase:** knowledge base attachment, actually calling an LLM.

**Done when:** an agent can be created, edited, published, paused, and rolled back to an earlier
config version through the API, with isolation tests on every route.

---

## Phase 4 — LLM provider abstraction

- [x] **Complete** · **Branch:** `feat/llm-provider-abstraction`
**Depends on:** Phase 3

**Delivers**
- `src/shared/llm/`: one `LLMProvider` interface plus adapters for **Gemini, OpenAI, Claude** (§5.3),
  each normalising auth, request/response shape, streaming, and tool-calling syntax; a registry that
  resolves a provider from `agent.model_provider` + `model_config_json` — so switching providers is a
  config change with no code change (§10).
- Uniform result object carrying content plus token usage (prompt/completion/total).
- Retry with backoff on transient errors and a configurable fallback provider on rate limits (§5.3).
- Provider credentials via `src/configs` (env-overridden), with the tenant bring-your-own-key path
  stubbed behind the same interface (§9 open question — do not decide it unilaterally).
- Long-running/streamed responses use `core/sse.py`.
- An integration test proving all three adapters work end to end.

**Not in this phase:** prompt assembly, conversation history, tool execution (Phase 11).

**Done when:** the same request works through all three adapters — mocked HTTP in unit tests, real
credentials in a manual smoke test — and token counts are recorded on every call.

---

## Phase 5 — Knowledge base: model, sources & extraction

- [x] **Complete** · **Branch:** `feat/kb-ingestion`
**Depends on:** Phase 3

**Delivers**
- `src/modules/knowledge_base/` in the full module shape, with extractors under
  `internal/extractors/` (one per format).
- `knowledge_base`, `kb_source`, `agent_kb_link` tables + migration (§7). `retrieval_tier` is
  `direct` or `keyword` only.
- KB CRUD; attach/detach a KB to multiple agents — KBs are reusable (§5.2). Cross-module access to
  agents goes through the agents **service**, not its repositories.
- v1 source types: file upload (txt/md, docx, csv, pdf, images), URL, manual FAQ entry.
- Extraction per format (§5.2.3): `.txt`/`.md` as-is; `.docx` via `python-docx` preserving headings;
  CSV rows converted to natural-language sentences (not raw rows); HTML stripped of boilerplate;
  **PDFs and images passed to the LLM for native reading — no OCR library**.
- Results stored as structured plain text + metadata on `kb_source.extracted_text` (§5.2.2). No
  chunking, no embeddings.
- Source status tracking: `status`, `last_synced_at`, `source_updated_at`, error detail on failure.
- File size, type, and per-tenant storage limits enforced at upload.

**Not in this phase:** retrieval/injection (Phase 6), scheduled API sync (Phase 9), anything vector.

**Done when:** every supported format can be uploaded and its extracted text inspected through the
API, a failed extraction surfaces as a readable source error rather than a 500, and one KB can serve
two agents.

---

## Phase 6 — Retrieval: Tier 1 direct injection + Tier 2 keyword search

- [x] **Complete** · **Branch:** `feat/kb-retrieval-tiers`
**Depends on:** Phase 5

**Delivers**
- `knowledge_base/internal/retrieval/` — tier router plus one strategy per tier, behind a single
  retrieval service method so callers never branch on tier themselves.
- Tier routing (§5.2.2): direct injection vs. keyword search chosen automatically from KB content size
  against the target model's context budget, with a manual override on the KB.
- **Tier 1** — assemble the full extracted text, with source metadata, for injection.
- **Tier 2** — Postgres full-text search: `tsvector` column + GIN index migration, `tsquery` search,
  ranked results, top-N section selection.
- Relevance threshold plus an explicit "no relevant context found" signal instead of injecting noise.
- Source citation metadata on every retrieval result, for logging and debugging (§5.2).
- A retrieval "explain" endpoint — what a given query would pull, and via which tier.

**Not in this phase:** embeddings, pgvector, chunking, hybrid search, re-ranking — **all v2** (§5.2.4).

**Done when:** a small KB round-trips through Tier 1 and a large one through Tier 2, tier selection is
tested at the size boundary, and an off-topic query returns the no-context signal.

---

## Phase 7 — Conversation engine & prompt assembly

- [x] **Complete** · **Branch:** `feat/conversation-engine`
**Depends on:** Phases 4 and 6

**Delivers**
- `src/modules/conversations/`, with `internal/prompt/` (assembly + delimiting) and
  `internal/history/` (trimming + rolling summarisation).
- `conversation` and `message` tables + migration (§7); sessions keyed by
  (agent, channel, external_user_id) (§5.4).
- History storage with context-window management: trimming plus rolling summarisation once history
  exceeds the model budget.
- **Prompt assembly** (§5.4): persona + guardrails + retrieved KB context + trimmed history, with
  retrieved content and user input **clearly delimited as data, never instructions** (§5.7).
- Guardrail enforcement: restricted topics, fallback response when the KB has no answer, escalation
  triggers that flip a conversation to `escalated` and emit a handoff event.
- Per-message token and cost recording, from the Phase 4 usage numbers.
- Message queueing so rapid or concurrent messages in one session are processed in order.

**Not in this phase:** any channel transport — this phase is exercised through tests and a direct
service call.

**Done when:** a full turn (user message → retrieval → prompt → provider → stored reply) works for an
agent with a KB attached, a long conversation stays inside the context budget, and injection-style
text in the KB or the user message does not override the system instructions.

---

## Phase 8 — Web chat API, API keys & integration docs

- [x] **Complete** · **Branch:** `feat/web-channel-and-api-keys`
**Depends on:** Phase 7

**Delivers**
- `src/modules/api_keys/`: `api_key` table + migration (§7); generation (shown once, stored hashed),
  scoping, revocation, per-key rate limiting via `core/rate_limit.py` (§5.6).
- `src/modules/channels/` with the **channel-agnostic internal message format** (Incoming/Outgoing) in
  `channels/domain/`, plus `channel_config` + migration — every channel adapter maps to this (§5.5).
- `src/modules/channels/web/`: public chat API authenticated by API key — send message, fetch history,
  streaming (SSE) responses via `core/sse.py`.
- Builder-side preview/test chat for the authenticated owner, so an agent can be tested before it is
  published (§5.1, §3 step 3).
- Auto-generated, agent-specific integration documentation derived from the live schema (§5.6).
- Outbound webhook configuration for platform events (new conversation, escalation).

**Not in this phase:** the embeddable JS widget bundle (a frontend artifact), WhatsApp.

**Done when:** an external caller holding only an API key can hold a conversation with a published
agent, a revoked key is rejected immediately, rate limits return 429 with correct headers, and the
generated docs are accurate enough to integrate against without reading the source (§10).

---

## Phase 9 — Async workers, scheduled sync & API-as-source (Pattern B)

- [x] **Complete** · **Branch:** `feat/workers-and-scheduled-sync`
**Depends on:** Phases 5 and 8

**Delivers**
- `src/core/queue.py` (Celery or RQ on Redis) + `worker.py` entrypoint + compose and CI wiring. Tasks
  themselves live in the owning module's `internal/tasks.py` — `core` stays free of business logic.
- Ingestion moved off the request path: upload returns immediately with a `processing` source status
  that the worker advances to `ready` or `failed`.
- **Pattern B connectors** (§5.2.1) in `knowledge_base/internal/connectors/`: endpoint, auth,
  pagination, field mapping (which JSON fields become content vs. metadata), feeding the same
  extraction path as files.
- Scheduled re-sync per source (configurable interval) plus manual "sync now"; incremental sync using
  the source's `last_modified`/version fields to skip unchanged records.
- Retries, dead-letter handling, and failure alerting when a scheduled sync breaks — expired auth,
  changed schema (§5.2.1).

**Not in this phase:** Pattern A live tool-calling (Phase 11).

**Done when:** a large upload no longer blocks the request, a connector pulls and indexes a paginated
API source on a schedule, an unchanged record is skipped on re-sync, and a broken source raises an
alert instead of failing silently.

---

## Phase 10 — WhatsApp channel

- [x] **Complete** · **Branch:** `feat/whatsapp-channel`
**Depends on:** Phases 8 and 9

**Delivers**
- `src/modules/channels/whatsapp/` — a sibling of `channels/web/`, mapping to the same internal
  message format. Provider integration (Meta Cloud API, or Twilio/360dialog behind the same adapter
  interface in `internal/`) (§5.5).
- Webhook verification handshake, signature validation, and **idempotent** delivery handling — a
  duplicate webhook ID must not produce a second reply.
- Outbound send with delivery status tracking.
- **24-hour session window** tracking and template-message support outside the window.
- Media messages: inbound images/documents routed into the same extraction path; outbound media send.
- Per-agent channel connection flow and credential storage, with setup steps exposed in the generated
  integration docs.

**Done when:** a real WhatsApp number reaches a published agent end to end, a replayed webhook yields
exactly one reply, and a message outside the 24-hour window correctly falls back to a template.

---

## Phase 11 — Agent tools: live API calls (Pattern A)

- [x] **Complete** · **Branch:** `feat/agent-tools`
**Depends on:** Phases 7 and 9

**Delivers**
- `src/modules/tools/`, with execution and guards in `internal/` (`http_executor.py`,
  `allowlist.py`, `response_mapper.py`).
- `agent_tool` table + migration (§7): name, description (what the LLM uses to decide), endpoint,
  method, auth type, auth config, request schema, response mapping, status.
- Tool definitions translated into each provider's native function-calling format through the
  `shared/llm` abstraction; the chosen tool executed server-side and its result injected back into
  the turn by the conversations service.
- **Security (§5.2.1):** tenant credentials injected server-side, never exposed to the LLM or the
  client; a per-agent endpoint allowlist to prevent SSRF; outbound request timeouts.
- Retry, graceful failure copy ("I couldn't check that right now…"), and optional short-lived caching
  of identical repeated calls.
- Tool-call logging — arguments, latency, outcome — for debugging.

**Done when:** an agent with both an indexed KB and a live tool answers a policy question from the KB
and an "order status" question through the tool in the same conversation, a non-allowlisted host is
refused, and a timing-out tool degrades to the fallback message instead of erroring the turn.

---

## Phase 12 — Analytics, logs & observability

- [x] **Complete** · **Branch:** `feat/analytics-and-observability`
**Depends on:** Phases 8 and 10

**Delivers**
- `src/modules/analytics/` — read-model services over the other modules' data, reached through their
  services or through dedicated read repositories in this module; no writes into other modules.
- Usage aggregation per agent and per tenant: messages/day, active conversations, tokens, estimated
  cost by provider (§5.8).
- Conversation log endpoints carrying the source/citation trace captured in Phases 6–7.
- Failure tracking exposed through the API: failed ingestions, provider errors, webhook failures.
- Quality signals: fallback ("I don't know") rate and escalation rate.
- Operational instrumentation: structured request logs, metrics counters, provider latency timing.

**Done when:** a dashboard-shaped payload for one agent returns counts and costs that reconcile with
the stored message rows, and every failure class above is queryable through the API.

---

## Phase 13 — Hardening & v1 release readiness

- [x] **Complete** · **Branch:** `chore/v1-hardening`
**Depends on:** all previous phases

**Delivers**
- Security pass: encryption at rest for tenant-provided credentials, an opt-in PII redaction path for
  uploaded documents (§5.7), global and per-key rate limits reviewed, CORS, security headers,
  dependency audit.
- **Architecture audit**: every module conforms to the `domain` / `internal` / `presentation` shape,
  no cross-module `internal/` imports, no repository calls from routers, no `commit()` in
  repositories, every endpoint returning the envelope. Fix or file what deviates.
- Written re-verification of the §10 success criteria, each mapped to a passing test or a recorded
  manual check — especially tenant isolation and the provider swap.
- OpenAPI polish: descriptions, examples, an error catalogue; generated integration docs reviewed.
- Load smoke test on the chat path; slow-query review and any missing indexes added.
- Deployment: production container/compose config, migrate-on-deploy story, and a runbook for the
  operational failure modes (provider outage, queue backlog, webhook storm).

**Done when:** every §10 criterion has evidence, each security and architecture item is done or
explicitly deferred with a reason, and a clean environment can go from empty database to serving
traffic using only the documented steps.

---

## Phase 14 (v1.1) — Plan limits & usage metering

- [ ] **Complete** · **Branch:** `feat/usage-metering`
**Depends on:** Phase 12

`src/modules/billing/`: per-plan quotas (agents, messages, storage), enforcement on the request path,
metering hooks for usage-based billing, and quota-exceeded responses. §5.9 and §6 place billing after
the core product — **do not pull this forward** unless the maintainer asks.

---

## Explicitly out of scope for this plan (v2+)

From §2 and §5.2.4 — do not build these, and do not let a v1 phase quietly grow into one: vector and
embedding pipelines, chunking, pgvector, hybrid search, cross-encoder re-ranking, multimodal
embeddings, voice channels, fine-tuning, an actions marketplace, in-tenant team roles, an agent
template marketplace, and advanced BI dashboards.

## Open questions to settle with the maintainer

Carried from §9 — raise each at the phase where it first bites rather than deciding it silently:
bring-your-own provider keys vs. platform-provided (Phase 4), pricing model (Phase 14),
white-labeling depth (Phase 8), team accounts (Phase 2), and whether v1 truly needs tool-calling on
day one (Phase 11).
