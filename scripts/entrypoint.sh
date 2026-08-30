#!/usr/bin/env sh
# Container entrypoint: migrate, then serve (Phase 13, "migrate-on-deploy story").
#
# `alembic upgrade head` runs on every start of the *api* role and on no other. That is the whole
# story, and the two halves are deliberate:
#
#   * **On start, not in a separate job.** A deploy that migrates in one place and starts the app in
#     another has a window where the two disagree, and someone has to remember the order. Running it
#     here means the schema is at head before the first request is served, every time, including the
#     first deploy to an empty database.
#   * **Only the api role.** Alembic takes a lock, so a second migrator waits rather than corrupting
#     anything — but a worker blocked behind a migration is a worker that looks hung. One migrator,
#     named explicitly.
#
# Rolling more than one api replica means more than one migrator racing for that lock. That is safe
# but slow; a deployment at that size should run `alembic upgrade head` as a pre-deploy step and set
# RUN_MIGRATIONS=0 here.
set -eu

ROLE="${ROLE:-api}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-1}"

if [ "$ROLE" = "api" ] && [ "$RUN_MIGRATIONS" = "1" ]; then
    echo "entrypoint: migrating to head"
    alembic upgrade head
fi

case "$ROLE" in
    api)
        echo "entrypoint: starting the API"
        # --proxy-headers is what makes the client address and scheme real behind a load balancer.
        # Without it every request looks like it came from the proxy over plain http, which breaks
        # both the credential throttle and the HSTS header. --forwarded-allow-ips must name the
        # proxy: trusting every source would let a caller forge its own address.
        exec uvicorn main:app \
            --host 0.0.0.0 \
            --port "${PORT:-8000}" \
            --workers "${WEB_CONCURRENCY:-2}" \
            --proxy-headers \
            --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
        ;;
    worker)
        echo "entrypoint: starting a queue worker"
        exec celery -A worker.celery_app worker --loglevel="${LOG_LEVEL:-info}" --pool=solo
        ;;
    beat)
        # Exactly one of these, ever. Two schedulers means every due source is enqueued twice.
        echo "entrypoint: starting the scheduler"
        exec celery -A worker.celery_app beat --loglevel="${LOG_LEVEL:-info}"
        ;;
    *)
        echo "entrypoint: unknown ROLE '$ROLE' (expected api, worker or beat)" >&2
        exit 1
        ;;
esac
