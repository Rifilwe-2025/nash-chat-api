"""The guards around a tool call, tested directly (spec §5.2.1, §5.7).

Tool arguments are written by a language model from whatever a stranger typed, so these are the
checks that decide what that can and cannot reach. They are tested here without a database or a
provider, because each is a decision worth pinning down on its own — and because the address guard
must be exercised with the escape hatch **off**, which the end-to-end file cannot do.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import pytest

from src.modules.tools.internal import allowlist, response_mapper, schema
from src.modules.tools.internal.cache import ResponseCache

ALLOWED = ["api.example.com", ".partner.example"]


# -- the allowlist ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("api.example.com", True),
        ("API.EXAMPLE.COM", True),
        ("other.example.com", False),
        # The trap a naive `endswith` falls into: an attacker registering a domain that ends with
        # an allowed one.
        ("api.example.com.attacker.test", False),
        ("orders.partner.example", True),
        ("partner.example", True),
        ("partner.example.attacker.test", False),
        ("", False),
    ],
)
def test_host_matching_is_exact_unless_a_leading_dot_says_otherwise(
    host: str, expected: bool
) -> None:
    assert allowlist.is_allowed_host(host, ALLOWED) is expected


def test_an_empty_allowlist_allows_nothing() -> None:
    """Fail closed: an agent whose owner never configured this cannot call anywhere."""
    with pytest.raises(allowlist.ToolSecurityError) as raised:
        allowlist.assert_allowed("https://api.example.com/orders", [])

    assert "no allowed tool hosts" in str(raised.value)


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://api.example.com/x", "gopher://api.example.com/"]
)
def test_only_http_urls_are_usable(url: str) -> None:
    with pytest.raises(allowlist.ToolSecurityError):
        allowlist.hostname_of(url)


def test_hosts_are_normalised_from_whatever_a_tenant_pastes() -> None:
    """A tenant pasting their endpoint into the allowlist field is the obvious mistake."""
    assert allowlist.normalise_hosts(
        ["https://API.Example.com/orders/{id}", "  partner.example:8443 ", "", "api.example.com"]
    ) == ["api.example.com", "partner.example"]


# -- path templating, the part arguments control ---------------------------------------


@pytest.fixture
def templating(config_override: Callable[..., None]) -> None:
    """These are about what an argument can do to a URL, not about DNS.

    ``api.example.com`` does not resolve, so the address check would refuse every one of them
    before the templating under test was reached. It has its own tests in the next section, with
    the flag off.
    """
    config_override(TOOLS_ALLOW_PRIVATE_URLS="true")


def test_placeholders_are_filled_from_the_arguments(templating: None) -> None:
    resolved = allowlist.resolve_url(
        "https://api.example.com/orders/{orderId}", {"orderId": "A-10432"}, ALLOWED
    )

    assert resolved == "https://api.example.com/orders/A-10432"


@pytest.mark.parametrize(
    "value",
    [
        "../../admin",
        "//evil.test/x",
        "a/../../../etc/passwd",
        "?x=1#y",
    ],
    ids=["traversal", "authority", "deep-traversal", "query-injection"],
)
def test_an_argument_cannot_change_what_the_url_means(value: str, templating: None) -> None:
    """Percent-encoding with ``safe=""`` is the single line that makes this hold.

    Each of these would otherwise walk up the path, introduce a new authority, or append a query
    the tenant never configured. Encoded, every one stays a single literal path segment.
    """
    resolved = allowlist.resolve_url(
        "https://api.example.com/orders/{orderId}", {"orderId": value}, ALLOWED
    )

    # Asserted on the parsed URL, not on a substring: the argument may well still appear in the
    # result — percent-encoded — and that is the point. What must not change is what the URL
    # *means*, so the host, the path depth and the query are what get checked.
    parts = urlsplit(resolved)
    assert parts.hostname == "api.example.com"
    assert parts.path.count("/") == 2  # /orders/<one segment>
    assert parts.query == ""
    assert parts.fragment == ""


def test_a_missing_placeholder_is_an_error_not_a_hole(templating: None) -> None:
    with pytest.raises(allowlist.ToolSecurityError) as raised:
        allowlist.resolve_url("https://api.example.com/orders/{orderId}", {}, ALLOWED)

    assert "orderId" in str(raised.value)


def test_placeholders_are_discovered_for_schema_validation() -> None:
    found = allowlist.path_placeholders("https://api.example.com/{a}/thing/{b}?x={a}")

    assert found == ["a", "b"]


# -- the address guard, with the escape hatch off --------------------------------------


def test_a_loopback_host_is_refused(config_override: Callable[..., None]) -> None:
    """The SSRF case: an allowed host that points inside the network."""
    config_override(TOOLS_ALLOW_PRIVATE_URLS="false")

    with pytest.raises(allowlist.ToolSecurityError) as raised:
        allowlist.assert_allowed("http://localhost:6379/keys", ["localhost"])

    assert "private or internal address" in str(raised.value)


def test_the_cloud_metadata_address_is_refused(config_override: Callable[..., None]) -> None:
    config_override(TOOLS_ALLOW_PRIVATE_URLS="false")

    with pytest.raises(allowlist.ToolSecurityError):
        allowlist.assert_allowed("http://169.254.169.254/latest/meta-data/", ["169.254.169.254"])


def test_the_allowlist_is_checked_before_dns(config_override: Callable[..., None]) -> None:
    """A host nobody approved is refused without a lookup — cheap, and no side effects."""
    config_override(TOOLS_ALLOW_PRIVATE_URLS="false")

    with pytest.raises(allowlist.ToolSecurityError) as raised:
        allowlist.assert_allowed("http://127.0.0.1/x", ALLOWED)

    assert "not on this agent's allowed tool hosts" in str(raised.value)


# -- argument validation ----------------------------------------------------------------

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "orderId": {"type": "string"},
        "includeHistory": {"type": "boolean"},
        "limit": {"type": "integer"},
        "channel": {"type": "string", "enum": ["web", "store"]},
    },
    "required": ["orderId"],
}


def test_valid_arguments_pass_through() -> None:
    assert schema.validate(ORDER_SCHEMA, {"orderId": "A-1", "limit": 5}) == {
        "orderId": "A-1",
        "limit": 5,
    }


def test_a_missing_required_argument_is_refused() -> None:
    with pytest.raises(schema.SchemaError) as raised:
        schema.validate(ORDER_SCHEMA, {"limit": 5})

    assert "orderId" in str(raised.value)


def test_a_wrong_type_is_refused() -> None:
    with pytest.raises(schema.SchemaError):
        schema.validate(ORDER_SCHEMA, {"orderId": 12345})


def test_a_boolean_does_not_pass_as_a_number() -> None:
    """``True`` is an ``int`` in Python, and an order lookup for order number 1 is a real bug."""
    with pytest.raises(schema.SchemaError):
        schema.validate(ORDER_SCHEMA, {"orderId": "A-1", "limit": True})


def test_a_value_outside_an_enum_is_refused() -> None:
    with pytest.raises(schema.SchemaError):
        schema.validate(ORDER_SCHEMA, {"orderId": "A-1", "channel": "carrier-pigeon"})


def test_undeclared_arguments_are_dropped_not_refused() -> None:
    """Models add stray fields. Failing the call would make a working tool flaky."""
    cleaned = schema.validate(ORDER_SCHEMA, {"orderId": "A-1", "somethingInvented": "x"})

    assert cleaned == {"orderId": "A-1"}


def test_a_tool_with_no_schema_takes_no_arguments() -> None:
    assert schema.validate(None, {"anything": 1}) == {}
    assert schema.normalise(None) == {"type": "object", "properties": {}}


# -- response mapping -------------------------------------------------------------------

PAYLOAD: dict[str, Any] = {
    "data": {
        "status": "Shipped",
        "eta": "Friday",
        "internalCustomerId": 8891,
        "paymentToken": "tok_secret",
    }
}


def test_only_declared_fields_are_rendered() -> None:
    """`fields` is an allowlist — that is what keeps a payment token out of a prompt."""
    text = response_mapper.render(
        PAYLOAD, {"root": "data", "fields": {"status": "Status", "eta": "Arrives"}}, 4000
    )

    assert "Status: Shipped" in text
    assert "Arrives: Friday" in text
    assert "tok_secret" not in text
    assert "8891" not in text


def test_a_new_field_in_the_response_does_not_leak() -> None:
    """The allowlist has to hold when someone else's API adds a field next month."""
    payload = {"data": {"status": "Shipped", "newSensitiveField": "surprise"}}

    text = response_mapper.render(payload, {"root": "data", "fields": {"status": "S"}}, 4000)

    assert "surprise" not in text


def test_a_list_is_rendered_as_numbered_items_and_capped() -> None:
    payload = {"items": [{"name": f"Item {index}"} for index in range(10)]}

    text = response_mapper.render(
        payload, {"root": "items", "fields": {"name": "Name"}, "list_limit": 3}, 4000
    )

    assert "1. Name: Item 0" in text
    assert "Item 3" not in text
    assert "7 more not shown" in text


def test_a_missing_root_says_so_rather_than_returning_nothing() -> None:
    text = response_mapper.render(PAYLOAD, {"root": "nope.missing"}, 4000)

    assert "nothing at" in text


def test_with_no_mapping_the_whole_response_is_rendered_but_truncated() -> None:
    """The starting default: something works before it is tuned, and it is still bounded."""
    payload = {"blob": "x" * 5000}

    text = response_mapper.render(payload, None, 200)

    assert len(text) < 400
    assert "response truncated" in text


def test_a_dotted_path_indexes_into_lists() -> None:
    assert response_mapper.extract({"a": [{"b": "found"}]}, "a.0.b") == "found"
    assert response_mapper.extract({"a": []}, "a.5.b") is None
    assert response_mapper.extract({"a": 1}, "a.b.c") is None


# -- the cache ---------------------------------------------------------------------------


def test_the_cache_key_ignores_argument_order() -> None:
    """A model does not guarantee key order, and treating that as a different call misses always."""
    cache = ResponseCache()
    tool_id = __import__("uuid").uuid4()

    assert cache.key(tool_id, {"a": 1, "b": 2}) == cache.key(tool_id, {"b": 2, "a": 1})


def test_two_tools_never_share_a_cache_entry() -> None:
    """Isolation applied to a cache: the tool id is in the key for exactly this reason (§5.7)."""
    import uuid

    cache = ResponseCache()
    arguments = {"orderId": "A-1"}

    assert cache.key(uuid.uuid4(), arguments) != cache.key(uuid.uuid4(), arguments)


def test_a_zero_ttl_stores_nothing() -> None:
    """Caching is opt-in, because per-customer data cached is per-customer data wrong."""
    import uuid

    cache = ResponseCache()
    key = cache.key(uuid.uuid4(), {})
    cache.put(key, {"a": 1}, 200, ttl_seconds=0)

    assert cache.get(key) is None
    assert len(cache) == 0


def test_a_stored_entry_is_returned_within_its_ttl() -> None:
    import uuid

    cache = ResponseCache()
    key = cache.key(uuid.uuid4(), {})
    cache.put(key, {"status": "Shipped"}, 200, ttl_seconds=30)

    entry = cache.get(key)
    assert entry is not None
    assert entry.payload == {"status": "Shipped"}
