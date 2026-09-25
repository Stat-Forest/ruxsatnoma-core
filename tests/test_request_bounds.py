"""Every field of every REQUEST body carries an upper bound (stage 17, QA run 01).

The QA run found the class, not an instance: a citizen could add livestock rows
without limit, an anonymous estimate took 50 000 items and ten seconds of the
event loop, and ~220 more strings, lists and numbers had no bound because each
schema had to remember one. Read off the live OpenAPI schema, like
`test_every_integer_query_parameter_carries_an_upper_bound`, and for the same
reason: `app.routes` no longer lists the routes since FastAPI 0.141.

Bounded means: a string has maxLength, enum/const, a bounded format or an
anchored pattern without an unescaped * or +; an array has maxItems; an
integer or a Decimal has BOTH a maximum/exclusiveMaximum AND a
minimum/exclusiveMinimum; an object has declared properties only, or
declares its bound as an `x-max-*` extension, or is a dict (`additionalProperties`
is a schema) whose values are checked recursively and whose keys are capped
by BOTH maxProperties AND a bounded `propertyNames` (a string with
maxLength/enum/const, or an anchored fixed pattern) — `maxProperties` alone,
the way `json_schema_extra` can declare it, documents a bound pydantic does
not enforce (I1, final review) and does not count.
Every operation PARAMETER (query, path, header, cookie) is walked too, by the
same rules (stage 19, closing stage 17's own R8).

Known blind spot: a request body read by hand, outside a pydantic model, is
never walked — `notifications/webhooks_router.py`'s Eskiz webhook and
`payments/payme_router.py`'s Payme RPC both parse their own JSON and are not
covered here (M5, final review).

No baseline any more (task 8b closed the last three offenders): the rule
holds for every request body in the schema, with no exceptions carried
forward."""

import os
import re
from typing import Any

_BOUNDED_FORMATS = {"uuid", "date", "date-time", "time", "email", "binary"}


def _openapi() -> dict[str, Any]:
    os.environ.setdefault("WORKERS_MODE", "off")
    from app.main import create_app

    return create_app().openapi()


def _string_bounded(node: dict[str, Any]) -> bool:
    if any(key in node for key in ("maxLength", "enum", "const")):
        return True
    if node.get("format") in _BOUNDED_FORMATS:
        return True
    # A multipart file field renders as `{"type": "string", "contentMediaType":
    # "application/octet-stream"}` on this FastAPI/Pydantic version, never
    # `format: binary` — the upload itself is size-capped elsewhere
    # (`app/core/files.py`'s `max_upload_mb`, `POST /gis/imports`'s own cap).
    if "contentMediaType" in node:
        return True
    pattern = node.get("pattern")
    return (
        pattern is not None
        and pattern.startswith("^")
        and pattern.endswith("$")
        # An ESCAPED `\+`/`\*` (a literal character, e.g. `^\+998\d{9}$`'s
        # leading `+`) does not open the pattern — only an unescaped
        # quantifier does (M6, final review).
        and not re.search(r"(?<!\\)[*+]|,\}", pattern)
    )


def _number_bounded(node: dict[str, Any]) -> bool:
    has_max = "maximum" in node or "exclusiveMaximum" in node
    has_min = "minimum" in node or "exclusiveMinimum" in node
    return has_max and has_min


def _property_names_bounded(node: dict[str, Any]) -> bool:
    """A typed dict's keys (`propertyNames`) must be bounded too — a
    `maxProperties` alongside an open `additionalProperties: {"type":
    "string"}` caps the COUNT of entries but not the size of each key, so
    `{"x" * 200_000: "v"}` is still one property (I1, final review)."""
    names = node.get("propertyNames")
    return isinstance(names, dict) and _string_bounded(names)


def _offenders(schema: dict[str, Any]) -> tuple[int, set[str]]:
    components = schema.get("components", {}).get("schemas", {})
    checked = 0
    offenders: set[str] = set()
    visited: set[str] = set()

    def visit(node: dict[str, Any], where: str) -> None:
        nonlocal checked
        ref = node.get("$ref")
        if ref is not None:
            name = ref.rsplit("/", 1)[-1]
            if name not in visited:
                visited.add(name)
                visit(components[name], name)
            return
        # `allOf` is treated exactly like `anyOf`/`oneOf` here — every member is
        # walked, and EVERY one of them must be bounded on its own terms (a
        # shared `where` key, so one unbounded member adds the offense
        # regardless of the others — M6, final review: this used to say "any
        # one of them being bounded is enough", which described OR semantics
        # the code has never implemented). The live schema has no `allOf`
        # today (Pydantic emits `anyOf`/`oneOf` for unions and inlines
        # everything else), so this is an untested assumption, not an
        # observed shape.
        members = node.get("anyOf") or node.get("oneOf") or node.get("allOf")
        if members:
            kinds = {member.get("type") for member in members}
            if {"number", "string"} <= kinds:  # a Decimal: judged by its number side
                checked += 1
                if not any(_number_bounded(m) for m in members if m.get("type") == "number"):
                    offenders.add(where)
                return
            for member in members:
                visit(member, where)
            return
        kind = node.get("type")
        # An untyped `Any` (none of type/$ref/anyOf/oneOf/allOf/enum/const —
        # `$ref` and `anyOf`/`oneOf`/`allOf` are already handled and returned
        # above) is unbounded unless it declares its own cap as an `x-max-*`
        # extension (`JsonObject` and `GeoJsonGeometry` both do).
        if kind is None and not any(
            key in node for key in ("properties", "additionalProperties", "enum", "const")
        ):
            checked += 1
            if not any(key.startswith("x-max-") for key in node):
                offenders.add(where)
            return
        if kind == "object" or "properties" in node or "additionalProperties" in node:
            for prop, sub in node.get("properties", {}).items():
                visit(sub, f"{where}.{prop}")
            extra = node.get("additionalProperties")
            if "properties" not in node or extra not in (None, False):
                checked += 1
                declared = any(key.startswith("x-max-") for key in node)
                typed = (
                    isinstance(extra, dict)
                    and "maxProperties" in node
                    and _property_names_bounded(node)
                )
                if not (declared or typed):
                    offenders.add(where)
                if isinstance(extra, dict):
                    visit(extra, f"{where}.*")
            return
        if kind == "array":
            checked += 1
            if "maxItems" not in node:
                offenders.add(where)
            visit(node.get("items", {}), f"{where}[]")
            return
        if kind == "string":
            checked += 1
            if not _string_bounded(node):
                offenders.add(where)
        elif kind in ("integer", "number"):
            checked += 1
            if not _number_bounded(node):
                offenders.add(where)

    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            body = operation.get("requestBody")
            if body is None:
                continue
            for media in body.get("content", {}).values():
                visit(media.get("schema", {}), f"{method.upper()} {path}")
    return checked, offenders


def test_every_request_body_field_carries_an_upper_bound() -> None:
    checked, offenders = _offenders(_openapi())
    assert checked >= 300, f"only {checked} request fields seen — the walk is wrong"
    assert not offenders, (
        "unbounded request field(s) — use the bounded types in app/core/schemas.py "
        "(CodeStr, NameStr, TextStr, …, Field(max_length=…/le=…)): " + ", ".join(sorted(offenders))
    )


def _parameter_offenders(schema: dict[str, Any]) -> tuple[int, set[str]]:
    """Every operation parameter (query, path, header, cookie), judged by the
    same rules as a body field (stage 19, closing stage 17's R8). `null` in
    an optional parameter's `anyOf` is skipped; a boolean needs no bound."""
    checked = 0
    offenders: set[str] = set()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                param_schema = parameter.get("schema", {})
                members = param_schema.get("anyOf") or [param_schema]
                where = f"{method.upper()} {path} {parameter['in']}:{parameter['name']}"
                for member in members:
                    kind = member.get("type")
                    if kind in (None, "null", "boolean"):
                        continue
                    checked += 1
                    if kind == "string":
                        bounded = _string_bounded(member)
                    elif kind in ("integer", "number"):
                        bounded = _number_bounded(member)
                    elif kind == "array":
                        items = member.get("items", {})
                        bounded = "maxItems" in member and (
                            items.get("type") != "string" or _string_bounded(items)
                        )
                    else:
                        bounded = False
                    if not bounded:
                        offenders.add(where)
    return checked, offenders


def test_every_request_parameter_carries_an_upper_bound() -> None:
    checked, offenders = _parameter_offenders(_openapi())
    assert checked >= 500, f"only {checked} request parameters seen — the walk is wrong"
    assert not offenders, (
        "unbounded request parameter(s) — add Query(max_length=…)/Path(max_length=…) "
        "from app/core/schemas.py (CODE_MAX_LENGTH, SEARCH_MAX_LENGTH, …): "
        + ", ".join(sorted(offenders))
    )
