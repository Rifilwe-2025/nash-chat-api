"""What a tool is allowed to reach (spec §5.2.1, §5.7).

A tool call is an outbound HTTP request whose *arguments were written by a language model*, acting
on text a stranger typed into a chat box. That is the most hostile input in the system, and it is
why there are two independent checks here rather than one.

**The allowlist** is the tenant's decision: these hosts, nothing else. It is checked against the
agent's policy, and an empty list allows nothing — fail closed, because an agent whose owner never
thought about this should not be able to call anywhere. This is the check that survives a mistake in
any of the others: even a completely wrong URL cannot leave the set of hosts a human approved.

**The address check** is ours, and it is the SSRF guard shared with URL ingestion
(``src/shared/net``). It catches what an allowlist cannot: a host a tenant genuinely approved that
resolves — today, or after someone changes a DNS record — to ``169.254.169.254`` or to something on
the internal network.

Both run at two moments, and both times matter:

* **when a tool is saved**, so a bad endpoint is refused while a person is looking at the screen;
* **immediately before the request**, on the URL that path templating actually produced, because
  arguments interpolate into the path and an argument is not something a tenant approved.

:func:`resolve_url` is where that second point is enforced. It fills ``{placeholders}`` from the
model's arguments and then re-checks the result, so a tool defined as
``https://api.example.com/orders/{orderId}`` cannot be turned into a request to another host by an
``orderId`` of ``../../..`` or ``//evil.test/``.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from src import configs
from src.shared.net import UnsafeUrlError, assert_public_url

# `{name}` — the only templating a tool endpoint supports. Deliberately not a general expression
# language: every construct one of those adds is another way for an argument to change the meaning
# of a URL rather than fill in a blank.
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ToolSecurityError(Exception):
    """A call our own guards refused. Distinct from the tenant's API failing.

    The two must not be collapsed: this one is a configuration bug the tenant can fix, and the
    outcome it is recorded under (``REFUSED``) is the one that says nothing left the building.
    """


def hostname_of(url: str) -> str:
    """The host part of a URL, lowercased. Raises for anything that is not a usable http(s) URL."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolSecurityError("A tool endpoint must be an http or https URL.")
    if not parts.hostname:
        raise ToolSecurityError("The tool endpoint has no host.")
    return parts.hostname.lower()


def normalise_hosts(hosts: list[str]) -> list[str]:
    """Tidy a tenant-supplied allowlist into the form the check compares against.

    Accepts a bare hostname or a whole URL, because a tenant pasting their endpoint into the
    allowlist field is the obvious mistake and rejecting it teaches nothing.
    """
    cleaned: set[str] = set()
    for raw in hosts:
        candidate = str(raw).strip().lower()
        if not candidate:
            continue
        if "://" in candidate:
            candidate = urlsplit(candidate).hostname or ""
        # A host with a port or a path is a host someone half-pasted; keep the host.
        candidate = candidate.split("/")[0].split(":")[0]
        if candidate:
            cleaned.add(candidate)
    return sorted(cleaned)


def is_allowed_host(host: str, allowed: list[str]) -> bool:
    """Whether one host is on the list.

    A leading dot means "this domain and its subdomains" (``.example.com`` matches
    ``api.example.com``). Without it the match is exact — a bare ``example.com`` does **not** admit
    ``evil.example.com.attacker.test``, which is the trap a naive ``endswith`` falls into.
    """
    target = host.lower()
    for entry in allowed:
        pattern = str(entry).lower()
        if pattern.startswith("."):
            if target == pattern[1:] or target.endswith(pattern):
                return True
        elif target == pattern:
            return True
    return False


def assert_allowed(url: str, allowed: list[str]) -> str:
    """Check a URL against the agent's allowlist and the address guard. Returns its host.

    Order matters: the allowlist is checked first because it is cheap, has no side effects, and its
    refusal is the one a tenant can act on. The address check does DNS, so it runs only for a host
    somebody already approved.
    """
    host = hostname_of(url)

    if not allowed:
        raise ToolSecurityError(
            "This agent has no allowed tool hosts configured, so no tool can be called. "
            "Add the host to the agent's tool policy."
        )

    if not is_allowed_host(host, allowed):
        raise ToolSecurityError(f"The host {host!r} is not on this agent's allowed tool hosts.")

    try:
        assert_public_url(url, allow_private=configs.TOOLS_ALLOW_PRIVATE_URLS)
    except UnsafeUrlError as exc:
        raise ToolSecurityError(str(exc)) from exc

    return host


def resolve_url(template: str, arguments: dict[str, object], allowed: list[str]) -> str:
    """Fill a tool's path placeholders from the model's arguments, then re-check the result.

    Every substituted value is percent-encoded with ``safe=""``, so a slash in an argument stays a
    literal slash rather than becoming a path segment. That single choice is what stops
    ``{orderId}`` = ``../../admin`` from walking up the path, and ``//evil.test/x`` from being read
    as a new authority.

    A placeholder the arguments do not supply is an error rather than an empty string: a URL with a
    hole in it is not a URL anyone intended to call.
    """
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in arguments or arguments[key] is None:
            missing.append(key)
            return ""
        return quote(str(arguments[key]), safe="")

    resolved = PLACEHOLDER.sub(substitute, template)

    if missing:
        raise ToolSecurityError(
            f"The tool endpoint needs {', '.join(sorted(set(missing)))}, which the call did not "
            f"provide."
        )

    # Re-checked against the *resolved* URL. The template was approved; what the arguments made of
    # it was not, and this is the only place that difference is visible.
    assert_allowed(resolved, allowed)
    return resolved


def path_placeholders(template: str) -> list[str]:
    """The placeholder names in an endpoint, so the service can require them in the schema."""
    return sorted({match.group(1) for match in PLACEHOLDER.finditer(template)})
