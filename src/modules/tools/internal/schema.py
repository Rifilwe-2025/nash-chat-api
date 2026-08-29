"""The argument schema: what a tenant declares, and what the model is held to (spec §5.2.1).

``request_schema_json`` does two jobs from one definition, which is why it is JSON Schema rather
than something of our own. It is handed to the provider verbatim as the function's parameters — so
the model knows what to send — and it is checked here against what the model actually sent.

Both halves are needed. The schema tells the model the shape; it does not guarantee it. Models omit
required fields, invent extra ones, and pass ``"12"`` where a number was asked for, and every one of
those becomes part of a URL or a request body if nothing looks. This validation is what turns "the
model sent something odd" into a refused call with a readable reason instead of a malformed request
to a tenant's API.

**A deliberately small subset of JSON Schema**: object with typed properties, required, and enum.
Not because more could not be supported, but because every construct added here is one the tenant
has to get right and the model has to honour — and a tool that takes an order number does not need
``oneOf``. Anything unrecognised is passed through to the provider and simply not enforced, so a
richer schema degrades to "the model is told, but we do not check" rather than being rejected.

Extra properties the schema does not mention are **dropped, not refused**. A model adding a stray
field is common and harmless; failing the call over it would turn a working tool into a flaky one,
while sending it on would put unvetted keys into someone's API request.
"""

from __future__ import annotations

from typing import Any

TYPE = "type"
PROPERTIES = "properties"
REQUIRED = "required"
ENUM = "enum"

# JSON Schema type -> what Python types satisfy it. `bool` is excluded from `integer`/`number`
# explicitly below: in Python `True` is an int, and a boolean silently passing as a quantity is the
# kind of thing that produces an order lookup for order number 1.
_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class SchemaError(Exception):
    """The arguments do not satisfy the tool's schema. The message is shown to the tenant's log."""


def empty_schema() -> dict[str, Any]:
    """The schema for a tool that takes no arguments.

    Every provider requires *some* parameters object on a function declaration, so a tool with no
    inputs still needs this rather than nothing.
    """
    return {"type": "object", "properties": {}}


def normalise(schema: dict[str, Any] | None) -> dict[str, Any]:
    """What is sent to the provider as the function's parameters."""
    if not schema:
        return empty_schema()
    if schema.get(TYPE) != "object":
        # Every provider expects the top level of a function's parameters to be an object. A tenant
        # who wrote a bare string schema gets it wrapped rather than a rejection.
        return {"type": "object", "properties": {}, **schema, TYPE: "object"}
    return schema


def validate(schema: dict[str, Any] | None, arguments: dict[str, Any]) -> dict[str, Any]:
    """Check the model's arguments and return only the declared ones.

    Raises :class:`SchemaError` for a missing required field or a wrong type — the two failures that
    would otherwise reach a tenant's API as a broken request.
    """
    declared = normalise(schema)
    properties = declared.get(PROPERTIES)
    if not isinstance(properties, dict):
        return {}

    required = declared.get(REQUIRED)
    required_names = [str(name) for name in required] if isinstance(required, list) else []

    missing = [name for name in required_names if arguments.get(name) is None]
    if missing:
        raise SchemaError(f"The call is missing required arguments: {', '.join(sorted(missing))}.")

    cleaned: dict[str, Any] = {}
    for name, definition in properties.items():
        if name not in arguments or arguments[name] is None:
            continue
        value = arguments[name]
        spec = definition if isinstance(definition, dict) else {}
        _check(str(name), value, spec)
        cleaned[str(name)] = value

    return cleaned


def _check(name: str, value: Any, spec: dict[str, Any]) -> None:
    expected = spec.get(TYPE)
    if isinstance(expected, str) and expected in _TYPES:
        allowed = _TYPES[expected]
        # `True` is an `int` in Python, so a boolean would satisfy "integer" without this.
        if expected in ("integer", "number") and isinstance(value, bool):
            raise SchemaError(f"{name!r} must be a {expected}, not a boolean.")
        if not isinstance(value, allowed):
            raise SchemaError(f"{name!r} must be a {expected}.")

    choices = spec.get(ENUM)
    if isinstance(choices, list) and choices and value not in choices:
        allowed_text = ", ".join(repr(choice) for choice in choices)
        raise SchemaError(f"{name!r} must be one of: {allowed_text}.")
