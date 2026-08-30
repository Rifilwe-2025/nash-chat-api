# Runbook

> What to do when the platform misbehaves. Written in Phase 13 for the failure modes the spec calls
> out (§5.2.1, §5.3, §5.5, §6) plus the ones the architecture makes possible.
>
> Every section is the same shape: **what it looks like**, **how to confirm it**, **what to do**, and
> what *not* to do. The last part is there because most of these have an obvious wrong move.

## Before anything else

Three questions answer most incidents:

```bash
curl -fsS https://<host>/health/ready        # is the API up, and can it reach its dependencies?
curl -fsS -H "X-Operator-Token: $OPERATOR_TOKEN" https://<host>/analytics/operations   # this process
docker compose -f docker-compose.prod.yml logs --tail=200 api worker beat
```

`/health/ready` checks the database and Redis. `/analytics/operations` reports request counts and
latency by route, and provider call counts, latency and errors — **for one process**, in memory,
since it started. With more than one API replica you are looking at one of them; ask each.

Every response carries `X-Request-ID`, and every request log line carries the same id. A tenant
reporting a problem should be asked for it first: it turns "the chat was broken this morning" into
one log line.

---

## Provider outage — the model cannot be reached

**Looks like:** chat calls returning `409 PROVIDER_UNAVAILABLE`. WhatsApp customers getting no
reply. `GET /analytics/failures` showing a rising `provider` count.

**Confirm:**

```bash
curl -fsS -H "X-Operator-Token: $OPERATOR_TOKEN" https://<host>/analytics/operations \
  | jq '.value.counters[] | select(.name == "llm_provider_errors_total")'
```

The `error` label separates the two cases that need different responses: `LLMRateLimitError` is us
being throttled, `LLMUnavailableError` is the vendor being down.

**Do:**

1. Check the provider's own status page before anything else. Most of these are not ours.
2. If one provider is down and another is configured, set `LLM_FALLBACK_PROVIDER` and restart the
   API. Calls that fail with a rate limit or an unavailable error then retry on the fallback.
3. If it is a rate limit against our own account, reduce load rather than retry into it: lower the
   per-key limits of the noisiest tenants (`PATCH /agents/{id}/api-keys/{keyId}`) until the quota
   window resets.
4. WhatsApp messages that failed during the outage are in `GET /analytics/failures` under `channel`,
   with the contact each one belongs to. There is no bulk retry — a customer who asked something an
   hour ago does not want an answer now — so tell the tenant which conversations went unanswered.

**Do not** raise `LLM_MAX_ATTEMPTS`. Retries against a provider that is down turn one slow request
into three, and the connection pool fills with requests nobody is still waiting for.

---

## Queue backlog — work is enqueued faster than it is done

**Looks like:** uploads stuck at `processing`. WhatsApp replies arriving minutes late. Redis memory
climbing.

**Confirm:**

```bash
docker compose -f docker-compose.prod.yml exec redis redis-cli llen celery
docker compose -f docker-compose.prod.yml logs --tail=100 worker
```

**Do:**

1. **Add workers**, do not add concurrency inside one. `--pool=solo` is deliberate: the work is I/O
   bound and solo behaves identically on every platform.
   ```bash
   docker compose -f docker-compose.prod.yml up -d --scale worker=4
   ```
2. If the backlog is one tenant's bulk upload, it will drain. If it is the scheduled sync sweep,
   check for a source re-syncing far too often — `sync_interval_minutes` has a floor
   (`SYNC_MIN_INTERVAL_MINUTES`) but a tenant with hundreds of sources can still saturate a worker.
3. If a *single* task is wedged, the soft time limit (`QUEUE_TASK_SOFT_TIME_LIMIT_SECONDS`) raises
   inside it so it records its own failure; the hard limit kills it. A task that survives both is a
   bug — capture the worker log and the task id before restarting.

**Do not** purge the queue to clear a backlog. It holds customers' unanswered WhatsApp messages and
tenants' ingestion work, and neither is re-created by anything.

**Check exactly one `beat` is running.** Two schedulers enqueue every due source twice, which looks
exactly like a worker shortage.

---

## Webhook storm — a channel is redelivering

**Looks like:** the same conversation answered repeatedly; a spike of `POST
/v1/channels/whatsapp/webhook/...` in the request counters.

**Confirm:** the `whatsapp_message` ledger. A duplicate `wamid` loses the unique-constraint insert
and stops there, so a storm should produce **one** reply per message no matter how many deliveries
arrive. If a customer received two replies, that is a real bug — capture the two message rows.

**Do:**

1. Confirm the webhook is returning `200` quickly. Meta redelivers whenever it does not see a prompt
   acknowledgement, so a slow webhook *causes* the storm. The route claims the message and enqueues;
   if it has become slow, something has been added to the request path that belongs on the queue.
2. If a tenant's own outbound webhooks are failing, `GET /analytics/failures` lists the endpoints by
   consecutive failure count. Deliveries are best effort and never block a turn, so this degrades
   the tenant's integration and nothing else. Disable the endpoint if it is generating load:
   `PATCH /webhooks/{id}` with `status: disabled`.

**Do not** disable signature verification to "get messages flowing". An unverified webhook endpoint
is an open door to anyone who knows the URL.

---

## Ingestion failures — sources stuck or failing

**Looks like:** `GET /analytics/failures` with a rising `ingestion` count, or sources sitting at
`processing`.

**Do:**

1. Read `errorDetail` on the source — it carries the actual reason: an unreadable file, an expired
   credential on an API source, a changed response shape.
2. A source at `processing` with no worker running is waiting, not broken. Start a worker.
3. A connector failing repeatedly raises its `consecutiveFailures`; past
   `SYNC_FAILURE_ALERT_THRESHOLD` it is reported to the tenant rather than retried silently. That is
   almost always expired credentials.
4. Re-run one source with `POST /knowledge-bases/{kbId}/sources/{sourceId}/sync`.

---

## Rate limiting is not holding

**Looks like:** a tenant exceeding their limit; or every request refused after a Redis blip.

**Confirm:** `RATE_LIMIT_BACKEND`. With `memory`, counters are **per process** — a four-worker
deployment enforces four times the limit that was sold. The app logs a warning at startup when this
is the case outside local environments.

**Do:** set `RATE_LIMIT_BACKEND=redis` and restart. The limiter **fails open** on a backend outage
by design: a Redis blip must not reject every customer's traffic.

---

## Credentials cannot be decrypted

**Looks like:** `EncryptionError` in the logs when a tool runs or a WhatsApp message is sent, right
after a deploy.

**Cause, almost always:** `SECURITY_ENCRYPTION_KEY` changed or was lost. Rows written under a key
cannot be read without it — that is the point of them.

**Do:** restore the previous key. If it is genuinely gone, the affected credentials must be
re-entered by each tenant; there is no recovery path, and there is not meant to be. A deployment that
starts with **no** key set does not fail — it warns at startup and writes new credentials in clear,
which is worth catching in the boot log rather than in a database dump later.

---

## Database

**Migrations run on API start** (`scripts/entrypoint.sh`), so a deploy migrates itself. With more
than one API replica they race for Alembic's lock: safe, but slow. At that size run
`alembic upgrade head` as a pre-deploy step and set `RUN_MIGRATIONS=0`.

**A migration that fails leaves the API not serving.** Roll the image back; the previous version's
schema is still there because migrations are additive within a phase.

**Slow queries:** the reads that grew indexes in Phase 13 are the analytics aggregates, the source
listing and the API-key listing. If a new report is slow, check it against
`ix_message_conversation_created` and `ix_conversation_tenant_created` before adding anything.

---

## Load check before a launch

```bash
python scripts/smoke_load.py --url https://<host> --key <staging agent key> \
    --concurrency 10 --requests 200
```

Reports p50/p95/p99, the status mix and the lowest rate-limit allowance it saw. Two things to know
before running it: every request costs a real model call on that key's tenant, and 429s mean the
limiter worked, not that the API is slow.
