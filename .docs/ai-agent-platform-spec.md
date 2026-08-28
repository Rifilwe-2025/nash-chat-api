# AI Agent Platform — Project Specification

## 1. Objective

Build a platform that lets any user create, configure, and deploy custom AI chat agents (sales, support, general-purpose, or any other role) without writing code. Each agent can be connected to a knowledge base, given a personality and behavior rules, powered by a choice of LLM providers (Gemini, ChatGPT, Claude), and deployed to WhatsApp and/or any web client via a generated API key and integration docs.

In short: a **self-serve, multi-tenant "agent builder"** — think of it as infrastructure that turns "I want a chatbot for my business" into a working, embeddable, channel-connected agent in minutes.

## 2. Scope

### In scope (v1)
- Web-based builder UI (Next.js) for creating and configuring agents
- Backend platform (FastAPI, modular architecture) serving:
  - Agent CRUD and configuration
  - Knowledge base ingestion and storage (direct context injection + keyword search — see Section 5.2; **no vector/embedding pipeline in v1**)
  - Multi-LLM abstraction (Gemini, OpenAI/ChatGPT, Claude)
  - Conversation/session management
  - Channel integrations: WhatsApp Business API + generic web widget/API
  - API key generation and per-integration authentication
  - Auto-generated integration documentation
- Multi-tenant data isolation (each user's agents, knowledge, and conversations are fully separated)
- Basic analytics/usage tracking (messages sent, tokens used, cost per agent)

### In scope (v2)
- Vector-based retrieval (RAG): chunking, embeddings, pgvector, hybrid/semantic search — for knowledge bases too large to fit in context (see Section 5.2.2)
- Multimodal embeddings for image-as-knowledge use cases (e.g. product catalogs)
- Cross-encoder re-ranking for retrieval quality

### Out of scope (v1) — candidates for later phases
- Voice channel support (calls, voice notes beyond WhatsApp's native voice messages)
- Advanced tool-calling / agent "actions" marketplace (e.g., booking, payments) — architecture should allow it later
- Fine-tuning custom models
- Team/role-based permissions within an organization (multi-user accounts)
- Marketplace of pre-built agent templates
- Advanced analytics/BI dashboards (conversation quality scoring, sentiment trends)

## 3. Core User Journey

1. User signs up on the web platform.
2. User creates a new agent:
   - Names it, defines its **persona/character** and **engagement rules**
   - Chooses the **LLM brain** (Gemini / ChatGPT / Claude) and model settings
   - Attaches or creates a **knowledge base** (upload files, add URLs, or enter FAQ text)
   - Sets **guardrails** (topics to avoid, escalation rules, tone)
3. User tests the agent in an in-browser preview chat.
4. User publishes the agent and generates an **integration API key**.
5. Platform auto-generates **API docs** specific to that agent/key.
6. User integrates the agent into WhatsApp (via provided setup steps) and/or their own website using the API/widget.
7. User monitors conversations and usage from a dashboard.

## 4. System Architecture (high level)

```
┌─────────────────┐        ┌──────────────────────────────────────────┐
│   Next.js Web    │◄──────►│              FastAPI Backend              │
│  (Builder + Docs │        │  (modular: agents, kb, channels, chat,    │
│   + Dashboard)   │        │   auth, billing, analytics)               │
└─────────────────┘        └──────────────┬─────────────────────────────┘
                                           │
              ┌────────────────────────────┼────────────────────────────┐
              │                            │                            │
      ┌───────▼───────┐          ┌─────────▼─────────┐        ┌─────────▼─────────┐
      │  LLM Provider  │          │  Knowledge Base     │        │  Channel Adapters  │
      │   Abstraction  │          │  Pipeline (v1:       │        │ (WhatsApp, Web,    │
      │ (Gemini/GPT/   │          │  direct injection +  │        │  future: Telegram, │
      │  Claude)       │          │  keyword search)     │        │  Messenger, etc.)  │
      └────────────────┘          └─────────────────────┘        └────────────────────┘
                                           │
                                  ┌─────────▼─────────┐
                                  │      Postgres        │
                                  │  (agents, kb docs,   │
                                  │  conversations, keys)│
                                  │  + pgvector added in │
                                  │  v2 for embeddings   │
                                  └──────────────────────┘
```

Async/background processing (task queue e.g. Celery/RQ + Redis) sits behind ingestion, embedding, and webhook handling so request/response paths stay fast.

## 5. Feature List

### 5.1 Agent Builder (Web UI)
- [ ] Agent creation wizard: name, avatar, persona/character description
- [ ] Behavior configuration: tone, engagement style, do's/don'ts, escalation triggers
- [ ] LLM brain selection (provider + model + temperature/max tokens)
- [ ] Knowledge base attachment (create new or link existing)
- [ ] Guardrails configuration (restricted topics, moderation level, fallback responses)
- [ ] Live test/preview chat window before publishing
- [ ] Versioning: edit agent config, roll back to previous version
- [ ] Agent status: draft / published / paused

### 5.2 Knowledge Base Management
- [ ] Multiple source types: file upload (PDF/docx/txt/csv), URL crawl, manual FAQ entry, **scheduled API pull (indexed)**, **live API call (tool-calling)**
- [ ] Ingestion pipeline: parse → chunk → embed → store
- [ ] Per-KB embedding model consistency
- [ ] Re-sync/refresh controls (manual + scheduled)
- [ ] Source status tracking (last synced, chunk count, errors)
- [ ] Knowledge base reusability (one KB attachable to multiple agents)
- [ ] Similarity threshold / "no relevant answer found" fallback
- [ ] Source citation tracking (internal logging at minimum)
- [ ] Clear separation of two API-based patterns (see 5.2.1 below): indexed API content vs. real-time API tool calls

#### 5.2.1 API as a Knowledge Source — Two Distinct Patterns

**Pattern A — Live lookup (tool-calling / function-calling)**
Used for real-time, user-specific, or fast-changing data that cannot or should not be pre-indexed: order status, account balance, live inventory, booking availability, pricing lookups.
- [ ] "Actions/Tools" config per agent: name, description (for the LLM to know when to use it), endpoint URL, HTTP method, auth type (API key / OAuth / bearer), request parameter schema, response-to-text mapping
- [ ] At query time, the LLM (via native function-calling) decides whether a tool is needed; backend executes the HTTP call server-side and injects the result into the conversation
- [ ] Tenant API credentials are stored and injected server-side — never exposed to the LLM or client
- [ ] Endpoint allowlisting per agent to prevent SSRF / arbitrary outbound calls
- [ ] Timeout, retry, and graceful-failure handling ("I couldn't check that right now, let me connect you to someone")
- [ ] Optional short-lived response caching for identical repeated calls

**Pattern B — Scheduled pull & index (API as an ingestion source)**
Used for semi-static content exposed via an API instead of a file: product catalogs, CMS articles, Zendesk/Notion/Confluence knowledge articles.
- [ ] Connector config: endpoint, auth, pagination handling, field mapping (which JSON fields become chunk content vs. metadata)
- [ ] Runs through the same chunk → embed → store pipeline as files/URLs
- [ ] Scheduled sync (interval configurable per source) plus manual "sync now"
- [ ] Delta/incremental sync using source `last_modified`/version fields where available, to avoid re-embedding unchanged records
- [ ] Failure alerting if a scheduled sync breaks (auth expired, endpoint changed, schema changed)

Agents should be able to combine both patterns simultaneously — e.g. a support agent with indexed policy docs (Pattern B) *and* a live "check my order" tool (Pattern A).

#### 5.2.2 Tiered Ingestion — Vectors Are a v2 Feature, Not a v1 Requirement

Full RAG (chunk + embed + vector search) adds real operational complexity: an embedding model dependency, a vector index to maintain, re-embedding on every content change, and fuzzy tuning (chunk size, thresholds). It is **not required for v1** — most small business knowledge bases (FAQs, a few policy docs, product info) fit comfortably within a modern LLM's context window, and modern LLMs can read PDFs and images natively without a separate OCR step.

| Tier | When it applies | Approach | Version |
|---|---|---|---|
| 1 — Direct injection | KB content is small (fits comfortably in the model's context window — e.g. a short FAQ or a handful of policy docs) | Pass the extracted text (or the file itself, for models that accept PDFs/images natively) straight into the prompt. No chunking, no embeddings, no retrieval step. | **v1** |
| 2 — Keyword search | Medium-sized KB, mostly structured FAQ-style content, too large to inject in full every turn | Postgres full-text search (`tsvector`/`tsquery`) to pull the relevant sections before injecting. No embedding model dependency. | **v1** |
| 3 — Vector / hybrid search | Large KB, many long documents, or users phrase questions very differently from how docs are written | Full chunk → embed → pgvector pipeline (see 5.2.4). | **v2** |

**v1 plan:** ship Tiers 1 and 2 only. No embedding provider, no pgvector, no chunking pipeline. Knowledge is stored as plain extracted text (with source metadata) and either injected directly or pulled via keyword search depending on size. This keeps the entire knowledge base subsystem simple to build, debug, and reason about for launch.

**v2 plan:** add Tier 3 as an upgrade path for tenants whose knowledge bases outgrow Tiers 1–2 (see 5.2.4 for the full vector/hybrid retrieval design). Because knowledge is already stored as structured text with metadata in v1, adding vector search in v2 means layering a new retrieval path on top of existing data — not re-architecting ingestion from scratch.

Tier selection can be automatic (based on content size at ingestion time), with an option for the user to force a tier manually once Tier 3 exists.

#### 5.2.3 Ingestion & Extraction Pipeline (per source format) — v1

For v1 (Tiers 1–2, no vectors), the goal is simply to get clean, structured text out of whatever the user provides. Where possible, lean on the LLM's native ability to read files directly rather than building separate extraction libraries for every format:

| Source type | v1 approach | Notes |
|---|---|---|
| Plain text (.txt, .md) | Use as-is | No processing needed |
| Word docs (.docx) | `python-docx` to extract text | Preserve headings for readability |
| PDF (text or scanned) | Pass directly to the LLM (Claude/GPT-4o/Gemini can read PDFs natively, including scanned/image-based ones) | Removes the need for a separate OCR library in v1 |
| CSV / spreadsheets | Parse rows → convert to natural-language lines, not raw rows | e.g. "Product SKU123 is priced at $45.99 and comes in Blue" reads far better in a prompt than a raw CSV row |
| HTML / web pages | Strip boilerplate, keep semantic structure | `BeautifulSoup` or `trafilatura` |
| Images (screenshots, photos, scanned forms) | Pass directly to the LLM for reading/description | No separate OCR pipeline needed in v1 — the model reads the image at ingestion or query time |

Pipeline shape for v1 (uniform regardless of format):

```
File/content submitted (any type)
   │
   ▼
Type detection
   │
   ▼
Extract text (library-based for docx/csv/html) OR pass file directly
   to the LLM for reading (PDFs, images) — whichever is simpler/cheaper
   │
   ▼
Store as structured plain text + metadata (source name, section,
uploaded_at) — no embeddings
   │
   ▼
Tier routing (5.2.2): direct injection into prompt, or keyword search
   if the KB is too large to inject in full every turn
```

**v2 note:** once vectors are introduced, this same extracted text feeds into chunking + embedding (5.2.4) instead of (or in addition to) direct injection. Multimodal embeddings (treating images themselves as searchable vectors, not just OCR'd text) are a further v2+ feature for use cases like product catalogs, where the visual content itself — not just text in the image — is the knowledge being searched.

#### 5.2.4 Retrieval Architecture — v2 (Vector / Hybrid Search)

> **This entire section is a v2 feature.** It only becomes relevant once a knowledge base outgrows Tiers 1–2 (direct injection / keyword search) in Section 5.2.2. Not required for v1 launch.

The goal is precision: only the most relevant chunks reach the model, scoped correctly per tenant/agent, with no cross-tenant leakage.

```
User message arrives
   │
   ▼
1. Query rewriting (optional) — resolve pronouns/context using conversation
   history (e.g. "what about the pro plan?" → "what does the pro plan cost?")
   │
   ▼
2. Hard filter — WHERE tenant_id = ? AND agent_id = ? AND kb_id = ?
   (this is both tenant isolation AND the biggest relevance boost — apply
   it before any similarity ranking)
   │
   ▼
3. Parallel search within that scope
   ├─ Vector search: embed query → cosine similarity top-k (pgvector)
   └─ Keyword search: Postgres full-text search top-k
   │
   ▼
4. Merge + re-rank
   - Combine both result sets (Reciprocal Rank Fusion is the standard method)
   - Optional: re-rank top ~20 with a cross-encoder or fast LLM call → best 3-5
   │
   ▼
5. Relevance threshold — drop anything below a similarity/score cutoff;
   if nothing survives, explicitly tell the model "no relevant context found"
   rather than injecting noise
   │
   ▼
6. Inject final 3-6 chunks into the prompt, clearly delimited from
   instructions, with source metadata attached for citation/logging
```

**Why hybrid (vector + keyword), not vector-only:** vector search captures meaning but can miss exact tokens (order numbers, SKUs, error codes); keyword search catches those instantly. Combined, they cover both failure modes. This is standard practice in production RAG systems at this scale.

**Category/topic pre-filtering:** if a KB has sub-categories (billing, technical, shipping), an optional lightweight classification step (cheap LLM call or keyword rule) can tag the incoming query's topic and filter to that category before similarity search — this cuts noise significantly for larger knowledge bases.

**Conflict resolution:** when two chunks disagree, prefer the more recently updated source — store `source_updated_at` on each chunk/source and factor it into ranking.

**Suggested starting defaults (v2):**

| Setting | Default |
|---|---|
| Chunk size | 400–600 tokens |
| Chunk overlap | 10–15% |
| Top-k per search method (pre-merge) | 10 |
| Final chunks sent to LLM | 3–5 |
| Similarity threshold | ~0.75 cosine, tune empirically per KB |
| Embedding model | one fixed model per KB, never mixed |

**Embedding model choice (v2):** start with one hosted API model as the default (e.g. OpenAI `text-embedding-3-small` — cheap, solid quality). Design it behind an `EmbeddingProvider` interface (same pattern as the LLM provider abstraction in 5.3) so a self-hosted open-source model (BGE, E5) can be swapped in later for cost savings or data-residency requirements.

**Scaling path:** Postgres + pgvector with hybrid search is sufficient for the vast majority of tenants at this stage. Add a cross-encoder re-ranker if retrieval quality complaints arise (highest-leverage upgrade). Only move to a dedicated vector database (e.g. Qdrant) if truly high scale or sub-100ms latency requirements are hit — don't build for that prematurely.

### 5.3 LLM Provider Abstraction
- [ ] Unified internal interface across Gemini, OpenAI, Claude
- [ ] Per-agent model/provider selection stored in config
- [ ] Provider-specific adapters (auth, request/response formatting, streaming, tool-calling syntax)
- [ ] Token usage tracking per provider for cost calculation
- [ ] Fallback/retry handling on provider errors or rate limits

### 5.4 Conversation Engine
- [ ] Session/state management keyed by (agent, channel, user)
- [ ] Conversation history storage with context-window trimming/summarization
- [ ] Message queueing to handle rapid/concurrent user messages
- [ ] Human handoff / escalation mechanism
- [ ] Prompt assembly: persona + guardrails + retrieved KB context + conversation history

### 5.5 Channel Integrations
- [ ] Generic Chat API (REST, provider-agnostic) for any web client
- [ ] Embeddable web widget (drop-in script/iframe option)
- [ ] WhatsApp Business API integration (via Meta Cloud API/Twilio/360dialog)
  - [ ] Webhook receipt + verification
  - [ ] 24-hour session window handling + template message support
  - [ ] Media message support (images, documents)
- [ ] Channel-agnostic internal message format (Incoming/Outgoing message abstraction)
- [ ] Extensible design for future channels (Telegram, Messenger, Instagram)

### 5.6 API Key & Integration Management
- [ ] API key generation per agent/integration
- [ ] Key scoping and revocation
- [ ] Rate limiting per key
- [ ] Auto-generated, agent-specific API documentation
- [ ] Webhook configuration for outbound events (e.g., new conversation, escalation triggered)

### 5.7 Multi-Tenancy & Security
- [ ] Full data isolation per tenant/user across agents, knowledge bases, and conversations
- [ ] Tenant-scoped filtering enforced at the query layer, not just the application layer
- [ ] Prompt injection safeguards (KB/retrieved content treated as data, not instructions)
- [ ] PII handling/redaction option for uploaded documents
- [ ] Secure storage of tenant-provided provider API keys (if users bring their own LLM keys)

### 5.8 Analytics & Monitoring
- [ ] Per-agent usage dashboard: messages/day, active conversations, tokens used, cost estimate
- [ ] Conversation logs viewer (with citation/source trace for debugging)
- [ ] Error/failure tracking (failed ingestions, provider errors, webhook failures)
- [ ] Basic quality signals (e.g., "I don't know" response rate, escalation rate)

### 5.9 Account & Billing (can be phased)
- [ ] User authentication and account management
- [ ] Plan/usage limits per account
- [ ] Usage-based billing hooks (token/message metering)

## 6. Key Focus Areas (where to invest design time)

| Area | Why it matters | Priority | Version |
|---|---|---|---|
| Knowledge base ingestion (direct injection + keyword search) | Core to answer quality and trust; simplest viable approach that still covers most small-business use cases | High | v1 |
| Ingestion/extraction per source format (docs, PDFs, images, CSV, APIs) — leaning on native LLM file reading | Where most format-specific bugs live; v1 avoids building a separate OCR pipeline by using LLMs that read PDFs/images natively | High | v1 |
| LLM provider abstraction | Determines whether "choose your brain" is actually maintainable | High | v1 |
| Multi-tenant data isolation | A leak here is a serious breach; must be designed in from day one | High | v1 |
| WhatsApp integration nuances | 24-hour window, template messages, webhook idempotency are easy to get wrong | High | v1 |
| Conversation/session state | Chat is inherently stateful; context management affects cost and coherence | Medium-High | v1 |
| Agent configuration schema | The actual product surface — needs to be flexible and versioned | Medium-High | v1 |
| Human handoff / escalation | Nearly every real deployment needs this eventually | Medium | v1 |
| Async architecture (queues) | Keeps webhook and ingestion paths fast and reliable | Medium | v1 |
| Analytics/observability | Needed for debugging and for customers to trust the product | Medium | v1 |
| Prompt injection / guardrails | Security concern once external content (docs, URLs, user messages) feeds the prompt — more relevant in v1 since full documents are injected raw | Medium | v1 |
| Billing/usage metering | Needed for monetization but can follow after core product works | Lower | v1.1+ |
| Vector/hybrid retrieval pipeline (chunking, embeddings, pgvector) | Needed once knowledge bases outgrow context-window limits | High (when needed) | **v2** |
| Multimodal embeddings (image-as-knowledge) | Useful for product catalogs / e-commerce agents specifically | Medium | **v2** |
| Cross-encoder re-ranking | Retrieval quality upgrade once basic search is live | Low-Medium | **v2** |

## 7. Suggested Data Model (starting point)

```
tenant (id, name, plan, created_at)
user (id, tenant_id, email, role)

agent (id, tenant_id, name, persona, engagement_rules, model_provider,
       model_config_json, status, version, created_at)

knowledge_base (id, tenant_id, name, retrieval_tier[direct/keyword/vector],
                embedding_model, created_at)
                -- retrieval_tier is 'direct' or 'keyword' in v1;
                -- 'vector' becomes available in v2
kb_source (id, kb_id, type[file/url/manual/api_indexed], config_json,
           extracted_text, last_synced_at, status, source_updated_at)
           -- extracted_text populated in v1 (Tiers 1-2); no embeddings yet
kb_chunk (id, kb_id, source_id, content, embedding vector, metadata jsonb)
           -- v2 table: populated only once Tier 3 (vector) is introduced

agent_kb_link (agent_id, kb_id)   -- many-to-many

agent_tool (id, agent_id, name, description, endpoint_url, http_method,
            auth_type, auth_config_json, request_schema_json,
            response_mapping_json, status)   -- Pattern A: live API tool-calling

conversation (id, agent_id, channel, external_user_id, started_at, status)
message (id, conversation_id, role, content, tokens_used, created_at)

api_key (id, agent_id, key_hash, scopes, rate_limit, created_at, revoked_at)
channel_config (id, agent_id, channel_type, credentials_json, status)
```

## 8. FastAPI Modular Structure (proposed)

```
/app
  /agents          # agent CRUD, config, versioning
  /knowledge_base   # ingestion, chunking, embedding, retrieval
  /llm_providers    # adapters for Gemini, OpenAI, Claude
  /channels
    /whatsapp       # webhook handling, message formatting
    /web            # generic chat API + widget support
  /conversations    # session state, history, context management
  /auth             # user auth, tenant management
  /api_keys         # key generation, scoping, rate limiting
  /analytics        # usage tracking, dashboards
  /billing          # (phase 2) usage metering
  /workers          # background tasks (ingestion, re-sync, embedding)
  /core             # shared config, DB session, security utils
```

## 9. Open Questions to Resolve Early

- Do users bring their own LLM provider API keys, or does the platform provide access under one account and bill for usage?
- What's the pricing model — per agent, per message, per token, per seat?
- How much white-labeling do we want to support (custom domains, branded widget)?
- Do we support team accounts (multiple users managing the same tenant's agents) in v1 or later?
- What's the minimum viable "tool-calling" story for v1 — do sales/support agents need live data lookups on day one, or can that be phase 2?

## 10. Success Criteria for v1

- A user can go from sign-up to a working, WhatsApp-connected support agent (backed by an uploaded FAQ doc) in under 15 minutes without touching code.
- Agent answers are grounded in the attached knowledge base (via direct injection or keyword search — no vector pipeline required) and clearly decline to answer when information isn't available.
- Knowledge base ingestion handles common formats (text, docx, PDF, images, CSV) without requiring a separate OCR pipeline, by relying on native LLM file reading where applicable.
- Switching an agent's LLM provider requires only a config change, no code change.
- Two different tenants' data (agents, knowledge, conversations) are provably isolated from each other.
- Integration docs are accurate enough that a developer unfamiliar with the platform can integrate the web chat API without support assistance.

## 11. v1 → v2 Upgrade Path (Knowledge Base)

Because v1 stores knowledge as structured extracted text (not raw files) with source metadata, v2's vector pipeline can be layered on top without re-architecting ingestion:

1. Add `embedding_model` selection per knowledge base
2. Backfill: chunk + embed existing `kb_source.extracted_text` for knowledge bases that exceed the Tier 1/2 size ceiling
3. Add pgvector column/index and the `kb_chunk` table
4. Introduce the hybrid retrieval flow (Section 5.2.4) as a new code path, selected automatically when `retrieval_tier = 'vector'`
5. No changes required to the agent builder UI's knowledge base upload flow — the tiering is an internal implementation detail, not something the end user needs to configure manually (though an "advanced" override could be exposed later)
