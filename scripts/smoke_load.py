"""A load smoke test for the chat path (Phase 13).

Not a benchmark, and it does not pretend to be one: it drives the one endpoint that matters —
``POST /chat/messages``, the public chat API a tenant's own product calls — with N concurrent
clients for a fixed number of requests, and reports latency percentiles, the status mix, and the
rate-limit headers it saw. What it is for is the question a deploy has to answer before it takes
traffic: *does this fall over, and where does it slow down?*

It talks to a **running deployment over HTTP** with a real API key, rather than driving the app
in-process. In-process numbers would leave out the parts most likely to be the problem — the
connection pool, the proxy, the provider call, and the rate limiter — which is to say the numbers
would be excellent and meaningless.

Usage::

    python scripts/smoke_load.py --url https://api.example.com --key nsk_live_… \\
        --concurrency 10 --requests 200

Two warnings worth reading before the first run:

* **Every request costs a model call**, on the tenant whose key you use. Point it at a staging
  agent, not at a production one someone is paying for.
* **The key's own rate limit applies.** A run that reports 429s has not found a slow API; it has
  found the limiter working. Raise the key's limit for the run, or lower the concurrency.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx


@dataclass(slots=True)
class Results:
    latencies_ms: list[float] = field(default_factory=list)
    statuses: Counter[int] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    remaining_seen: list[int] = field(default_factory=list)

    def record(self, status: int, duration_ms: float, remaining: str | None) -> None:
        self.statuses[status] += 1
        self.latencies_ms.append(duration_ms)
        if remaining is not None and remaining.isdigit():
            self.remaining_seen.append(int(remaining))

    def percentile(self, share: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(int(len(ordered) * share), len(ordered) - 1)
        return ordered[index]

    def report(self, elapsed: float) -> str:
        total = sum(self.statuses.values())
        lines = [
            "",
            f"requests      {total} in {elapsed:.1f}s  ({total / elapsed:.1f}/s)"
            if elapsed
            else "",
            f"status mix    {dict(sorted(self.statuses.items()))}",
            f"latency ms    p50 {self.percentile(0.50):.0f}   "
            f"p95 {self.percentile(0.95):.0f}   p99 {self.percentile(0.99):.0f}   "
            f"max {max(self.latencies_ms, default=0):.0f}",
        ]
        if self.latencies_ms:
            lines.append(f"mean ms       {statistics.fmean(self.latencies_ms):.0f}")
        if self.remaining_seen:
            lines.append(
                f"rate limit    lowest remaining allowance seen: {min(self.remaining_seen)}"
            )
        if self.errors:
            lines.append(f"transport     {dict(self.errors)}")
        if self.statuses.get(429):
            lines.append(
                "note          429s mean the key's rate limit was reached, not that the API is "
                "slow. Raise the key's limit for the run or lower --concurrency."
            )
        return "\n".join(line for line in lines if line)


async def one_client(
    client: httpx.AsyncClient,
    url: str,
    key: str,
    message: str,
    budget: asyncio.Queue[int],
    results: Results,
) -> None:
    """Take requests from the shared budget until it is empty."""
    while True:
        try:
            budget.get_nowait()
        except asyncio.QueueEmpty:
            return

        started = time.perf_counter()
        try:
            response = await client.post(
                f"{url.rstrip('/')}/chat/messages",
                json={"message": message},
                headers={"Authorization": f"Bearer {key}"},
            )
        except httpx.HTTPError as exc:
            results.errors[type(exc).__name__] += 1
        else:
            results.record(
                response.status_code,
                (time.perf_counter() - started) * 1000,
                response.headers.get("x-ratelimit-remaining"),
            )
        finally:
            budget.task_done()


async def run(args: argparse.Namespace) -> Results:
    results = Results()
    budget: asyncio.Queue[int] = asyncio.Queue()
    for index in range(args.requests):
        budget.put_nowait(index)

    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        await asyncio.gather(
            *(
                one_client(client, args.url, args.key, args.message, budget, results)
                for _ in range(args.concurrency)
            )
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", required=True, help="Base URL of the deployment, e.g. https://api…"
    )
    parser.add_argument("--key", required=True, help="An agent API key with the chat:write scope.")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--message", default="What are your opening hours?")
    args = parser.parse_args()

    started = time.perf_counter()
    results = asyncio.run(run(args))
    print(results.report(time.perf_counter() - started))


if __name__ == "__main__":
    main()
