# v1 acceptance — evidence for the §10 success criteria

> Written in Phase 13. Every criterion in spec §10 is listed with **what proves it**: a passing test
> by name, or a recorded manual check with what was done and what was seen. A criterion with neither
> is marked as such rather than assumed.
>
> Run everything referenced here with `pytest`. The commands under each criterion are the exact ones.

---

## 1. Sign-up to a working, WhatsApp-connected agent in under 15 minutes, without code

**Status: met, by tests for each step of the path.** No single test spans sign-up to a live WhatsApp
reply — that requires a real Meta number, which is recorded as a manual check below — but each hop is
covered end to end over HTTP.

| Step | Evidence |
|---|---|
| Sign up, get a token | `tests/modules/auth/test_auth_flow.py::test_signup_creates_tenant_user_and_tokens` |
| Create and configure an agent | `tests/modules/agents/test_agent_crud.py` |
| Upload an FAQ document | `tests/modules/knowledge_base/test_sources.py`, `::test_native_file_ingestion` |
| Attach it and test in preview | `tests/modules/knowledge_base/test_attachment.py`, `tests/modules/conversations/test_turn.py` |
| Publish | `tests/modules/agents/test_transitions.py` |
| Connect WhatsApp and receive a reply | `tests/modules/channels/whatsapp/test_whatsapp_channel.py` |

```bash
pytest tests/modules/auth tests/modules/agents tests/modules/knowledge_base tests/modules/channels
```

**Manual check still required before launch:** a real Meta Business number pointed at a deployed
`/v1/channels/whatsapp/webhook/{connectionId}`, one inbound message, one reply. The provider adapter
is exercised against a stubbed Meta API in tests; what tests cannot prove is that the callback URL
and verify token a tenant pastes into Meta are accepted by Meta itself.

---

## 2. Answers are grounded, and decline when the knowledge is not there

**Status: met.**

- Tier 1 (direct injection) and Tier 2 (Postgres full-text search), with the tier chosen per query:
  `tests/modules/knowledge_base/test_retrieval.py`, `::test_tier_routing.py`
- Below the relevance threshold retrieval reports *no context* rather than injecting noise:
  `tests/modules/knowledge_base/test_retrieval.py`
- The agent then says so instead of guessing, and the reply is marked `hasContext: false`:
  `tests/modules/conversations/test_turn.py`, `tests/modules/analytics/test_usage.py::test_quality_signals_count_the_markers_the_engine_wrote`
- The fallback rate is reportable, so grounding failures are visible rather than anecdotal:
  `GET /analytics/usage` → `quality.fallbackRate`

**No vector pipeline exists**, which is the constraint §5.2.2 sets and the thing most likely to be
violated by accident: there is no `kb_chunk` table, no embedding column, and no pgvector dependency.

---

## 3. Ingestion handles text, docx, PDF, images and CSV without an OCR pipeline

**Status: met.**

| Format | Path | Evidence |
|---|---|---|
| Plain text, markdown | direct | `tests/modules/knowledge_base/test_extractors.py` |
| `.docx` | `python-docx` | `test_extractors.py` |
| PDF, images | **native LLM file reading**, no OCR | `test_native_file_ingestion.py::test_a_pdf_is_stored_as_the_text_the_model_read`, `::test_an_image_takes_the_same_path_as_a_pdf` |
| CSV | rows rendered as sentences | `test_extractors.py` |
| HTML / URL | BeautifulSoup | `test_extractors.py`, `test_fetching.py` |

A file the model cannot read fails **readably**, with the reason on the source rather than a stack
trace: `test_native_file_ingestion.py::test_a_file_the_model_cannot_open_becomes_a_readable_failure`.

---

## 4. Switching provider is a config change, no code change

**Status: met.**

- One request runs unchanged through all three adapters:
  `tests/shared/llm/test_providers.py::test_the_same_request_works_through_every_adapter`
- Provider-specific quirks are absorbed by the adapter, not the caller — Claude's `max_tokens`, the
  models that reject `temperature`, Gemini's separate system field:
  `::test_anthropic_sends_the_system_prompt_separately`, `::test_anthropic_omits_temperature_on_models_that_reject_it`
- An agent's provider is a stored value, changed through `PATCH /agents/{id}`:
  `tests/modules/agents/test_agent_crud.py`
- Nothing outside `src/shared/llm/` imports a vendor SDK.

```bash
pytest tests/shared/llm
grep -rn "import anthropic\|import openai\|from google import genai" src/ | grep -v "src/shared/llm"
```

---

## 5. Two tenants' data is provably isolated

**Status: met, and enforced structurally rather than per endpoint.**

- The scoping lives in the shared repository base, so a cross-tenant read is not something an
  endpoint can forget: `tests/shared/test_tenant_scoping.py` — `::test_get_cannot_reach_another_tenants_row`,
  `::test_list_only_returns_the_callers_rows`, `::test_update_cannot_move_a_row_to_another_tenant`
- Per module, over HTTP with two real tenants: `tests/modules/tenants/test_isolation.py`,
  `tests/modules/agents/test_agent_isolation.py`, `tests/modules/knowledge_base/test_kb_isolation.py`,
  `tests/modules/channels/whatsapp/test_media_and_isolation.py`
- Analytics reads five other modules' tables and inherits the same base rather than reimplementing
  the filter: `tests/modules/analytics/test_failures.py::test_another_tenants_failures_are_invisible`,
  `tests/modules/analytics/test_trace_and_operations.py::test_another_tenants_conversation_is_a_404`
- A resource in another tenant is a **404, never a 403** — telling the two apart confirms existence.

```bash
pytest tests/shared/test_tenant_scoping.py tests/modules/tenants tests/modules/analytics -k isolation
```

---

## 6. A developer can integrate the web chat API from the docs alone

**Status: met, with the docs themselves under test.**

- Every route carries a tag, a summary, an explicit envelope response model and documented failure
  codes; CI fails otherwise: `tests/test_openapi.py::test_every_route_is_documented`,
  `tests/architecture/test_layering.py::test_every_successful_response_is_the_envelope`
- Every `error.code` the application raises is in the published catalogue, enforced by a source
  scan: `tests/test_error_catalogue.py::test_every_code_raised_in_the_source_is_documented`
- A per-tenant integration guide is generated from the same schema:
  `tests/modules/channels/test_webhooks_and_docs.py`
- Rate-limit headers are on every response, not only on a 429, so "why am I getting 429" is
  answerable from the response alone.

---

## Security pass (Phase 13, spec §5.7)

| Item | Status |
|---|---|
| Tenant credentials encrypted at rest | **Done.** AES-256-GCM through the column type (`shared/crypto`), applied to tool `authConfig`, channel credentials and webhook secrets. `tests/shared/test_crypto.py::test_a_tool_credential_is_unreadable_in_the_column`. Requires `SECURITY_ENCRYPTION_KEY`; without it the columns are clear and the app warns at startup. |
| PII redaction for uploaded documents | **Done, opt-in per knowledge base.** Applied at ingestion so the raw values never reach the database: `tests/modules/knowledge_base/test_redaction.py`. Pattern-based — a reduction in exposure, not a guarantee of anonymity, and the API says so. |
| Prompt-injection safeguards | **Done in Phase 7, re-verified.** Retrieved content and user messages are fenced and neutralised, instructions precede data: `tests/modules/conversations/test_injection_resistance.py`, `test_prompt_assembly.py::test_instructions_come_before_any_data`. |
| Per-key rate limits | **Done in Phase 8.** Reviewed here; Redis backend required for multi-worker deployments and the app warns when it is not. |
| Global / unauthenticated rate limits | **Done.** Per client address on signup, login and refresh — the endpoints reachable with no credential: `tests/core/test_security_headers.py::test_repeated_sign_in_attempts_are_throttled`. |
| Security headers | **Done.** `nosniff`, `DENY`, referrer policy, COOP/CORP everywhere; HSTS on HTTPS only; a CSP on the docs pages, which are the only HTML served. |
| CORS | **Reviewed.** Origins are an explicit allow-list from configuration (`CORS_ALLOW_ORIGINS`), never `*` with credentials. The public chat API is authenticated by key rather than by cookie, so a browser integration does not depend on credentialed CORS. |
| Dependency audit | **Done and wired into CI.** `pip-audit -r requirements.txt` runs on every PR. The advisories open at the start of this phase were resolved by upgrading FastAPI/Starlette, PyJWT, python-multipart and pytest; the audit is clean as of this phase. |
| SSRF on tenant-supplied URLs | **Done in Phases 5 and 11.** KB fetches and tool calls resolve the host and refuse private, loopback and link-local addresses unless explicitly allowed for local development. |
| Secrets in logs | **Reviewed.** Credentials are never logged; tool call logs store the *mapped* result text, never the raw response or the injected credential. |

### Deferred, with reasons

| Item | Why |
|---|---|
| Key rotation tooling | The envelope carries a `v1:` version prefix and `decrypt` dispatches on it, so rotation is a `v2` branch plus a re-encrypt pass. No rotation has been needed yet and building the tooling before the first key exists would be building it blind. |
| Object storage for uploaded originals | v1 stores extracted text, not files (§5.2.2), so the originals are only staged between the upload and the worker. A blob store is the right answer when originals must be retained, which is a v2 requirement. |
| PII detection beyond patterns | A model-based classifier catches names and addresses that regexes cannot, at the cost of a model call per document. Recorded in the API's own description so no tenant over-trusts what is there. |
| Team accounts / in-tenant roles | Out of scope for v1 (§9, §2). Every user is still the owner of their own tenant. A *platform* admin role now exists (see below), which is a different thing: it is about the deployment, not about a tenant. |

---

## Architecture audit (Phase 13)

Every rule in `CLAUDE.md` is now checked mechanically by `tests/architecture/test_layering.py`, so
the audit is repeated on every PR rather than being a snapshot of one afternoon:

- module shape (`domain` / `internal` / `presentation`), and every package importable
- no `commit()` in a repository
- no `select(...)` outside repositories and worker tasks
- routers import neither SQLAlchemy nor another layer's repositories, and are built with
  `create_router`
- no module imports another module's `internal/`
- no module imports another module's repositories
- every model inherits the shared base
- every successful response is the `ApiResponse` / `PaginatedResponse` envelope

**Deviations found and fixed in this phase:**

1. `auth/domain/services.py` reached into `tenants/domain/repositories.py`. Now goes service to
   service through `TenantService.register` / `find_by_email` / `find_user`.
2. `conversations/domain/services.py` imported `knowledge_base/internal/retrieval`. `RetrievalResult`
   is now re-exported from the knowledge base's domain surface.
3. `conversations/internal/history` and `conversations/internal/prompt` had no `__init__.py`.

**Deviations found and kept, with reasons:**

0. `admin/domain/repositories.py` reads **across tenants**, on `BaseRepository` rather than the
   tenant-scoped base. That is the one sanctioned unscoped path in the codebase and it is confined
   to that file: it reads `tenant` and counts of the rows hanging off one, and nothing else. An
   administrator reaching *into* an account does it by acting as that tenant through the ordinary
   endpoints, where the usual scoped repositories still do the work — so the cross-tenant surface is
   "the account list and how big each account is", not "everything".
1. `analytics/domain/repositories.py` reads other modules' models. That is what a read model is; the
   plan sanctions it, and every one of those repositories extends the tenant-scoped base so the
   isolation guarantee is inherited rather than restated.
2. `WhatsAppService` calls `session.commit()` before enqueueing claimed messages. A service owns its
   transaction boundary; a worker in another process cannot see a row this request has not committed.
3. `channels/` carries `web/` and `whatsapp/` sub-modules beside its own layers. They share one
   internal message format and one channel-settings table (§5.5); splitting them would duplicate both.

---

## Platform administration and account status (Phase 15)

Added after the v1 phases, replacing the plan limits that were built and removed:

| Item | Status |
|---|---|
| Cross-tenant admin | **Done, and deliberately narrow.** `/admin` covers the account list, one account's size and people, the platform totals, and the enable/disable lever. It exposes no tenant *content*. |
| Admin CRUD | **Done through the existing API.** An administrator sends `X-Tenant-Id` and every ordinary endpoint answers as though signed in to that account. One dependency decides it (`tenants/presentation/dependencies.py`), so there is one thing to audit rather than a parallel API per module — and every query stays tenant-scoped. Covered by `tests/modules/admin/test_platform_admin.py`. |
| The header cannot widen anyone else's scope | **Verified.** A non-admin sending `X-Tenant-Id` is silently scoped to their own tenant — ignored rather than refused, since refusing would confirm which tenant ids exist: `::test_a_non_admin_sending_the_header_stays_in_their_own_tenant`. |
| Granting admin | **Out of band only.** The first administrator is created at startup from `ADMIN_BOOTSTRAP_EMAIL` / `ADMIN_BOOTSTRAP_PASSWORD`; everyone after that is granted with `scripts/grant_platform_admin.py`. There is no endpoint, and a test asserts no route accepts the flag. |
| The handover password | **Forced to change.** The bootstrap account is created with `must_change_password`, so it can sign in and call `POST /auth/password` and nothing else — every other route answers `403 PASSWORD_CHANGE_REQUIRED`. Changing it revokes the sessions that used the old one. The bootstrap only ever *creates*: a restart never resets a live account's password back to the environment file. `tests/modules/admin/test_bootstrap_admin.py`. |
| Disabling an account | **Done, at every door**: sign-in, an already-issued access token on its next request, the account's API keys, and the WhatsApp webhook — the three paths that authenticate differently, plus the one that authenticates not at all. |
| Reversibility | Disabling deletes nothing; re-enabling restores service with no further steps. Deletion exists separately and requires typing the account's name back. |

## From an empty database to serving traffic

The documented path, and the one the acceptance run follows:

```bash
cp .env.example .env.production          # set DATABASE_URL, REDIS_URL, JWT_SECRET_KEY,
                                         # SECURITY_ENCRYPTION_KEY, provider keys, CORS origins
docker compose -f docker-compose.prod.yml --env-file .env.production up -d
curl -fsS http://127.0.0.1:8000/health/ready
```

`scripts/entrypoint.sh` runs `alembic upgrade head` before the API serves its first request, so an
empty database is migrated by the deploy itself. Operational failure modes — provider outage, queue
backlog, webhook storm — are in `.docs/RUNBOOK.md`.
