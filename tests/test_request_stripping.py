"""Every required request string is stripped before its length is checked
(stage 17, QA run 01 C2; final review I4).

`test_request_bounds.py` proves every field has an upper BOUND, read off the
OpenAPI schema — but a `maxLength` says nothing about whitespace, and a
bounded-but-unstripped field lets `"   "` slip past `min_length=1` as long as
it is short enough. Stripping happens in the TYPE
(`StringConstraints(strip_whitespace=True, ...)`), which is invisible to the
schema, so this walks the pydantic MODELS themselves instead of the schema.

Models are resolved from the OpenAPI component names a request body can
reach: FastAPI names a component by its class's bare `__name__`, unless two
classes share one (`AppealIn` in both `inspections.schemas` and
`public.schemas`), in which case it uses the qualified, `__`-joined dotted
path instead (`app__modules__public__schemas__AppealIn`) — both forms are
resolved back to a real class, by walking every already-imported `app.*`
module's `BaseModel` subclasses (`create_app()` imports the lot). A handful
of names never resolve: FastAPI's own synthetic `Body_*` multipart schemas
(`UploadFile` + `Form(...)` routes) are not source-level classes at all —
the same kind of blind spot `test_request_bounds.py` names for a hand-parsed
body.

`PasswordStr`/`BlobStr` are the only exemptions (R2): a password's spaces
are part of it, and a base64 blob is machine data no human edits."""

import sys
import typing
from typing import Any

from pydantic import BaseModel, StringConstraints
from pydantic.fields import FieldInfo

from app.core.schemas import BlobStr, PasswordStr

# The single `StringConstraints` instance each of PasswordStr/BlobStr carries
# (`typing.get_args` on `Annotated[str, StringConstraints(...)]` returns the
# origin type first, then its metadata) — every field typed `PasswordStr`
# reuses this exact alias, so an `==` match against a field's own metadata
# reliably recognises it regardless of where it is used.
_EXEMPT_METADATA = tuple(
    item for alias in (PasswordStr, BlobStr) for item in typing.get_args(alias)[1:]
)


def _openapi() -> dict[str, Any]:
    import os

    os.environ.setdefault("WORKERS_MODE", "off")
    from app.main import create_app

    return create_app().openapi()


def _model_registry() -> tuple[dict[str, type[BaseModel]], dict[str, list[type[BaseModel]]]]:
    """Every `BaseModel` subclass reachable from an already-imported `app.*`
    module, keyed both by its qualified dotted path (for a FastAPI-
    disambiguated, `__`-joined component name) and by its bare `__name__`
    (the common, non-colliding case) — the same `sys.modules` walk
    `test_code_conventions.py::_registry_sizes` uses for the same reason:
    the registry nobody has thought to name explicitly is exactly the one
    worth walking for."""
    by_qualname: dict[str, type[BaseModel]] = {}
    by_bare: dict[str, list[type[BaseModel]]] = {}
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("app."):
            continue
        for value in list(vars(module).values()):
            if not (
                isinstance(value, type) and issubclass(value, BaseModel) and value is not BaseModel
            ):
                continue
            qualname = f"{value.__module__}.{value.__qualname__}"
            by_qualname[qualname] = value
            by_bare.setdefault(value.__name__, []).append(value)
    return by_qualname, by_bare


def _resolve_model(
    component: str,
    by_qualname: dict[str, type[BaseModel]],
    by_bare: dict[str, list[type[BaseModel]]],
) -> type[BaseModel] | None:
    if "__" in component:
        return by_qualname.get(component.replace("__", "."))
    candidates = by_bare.get(component, [])
    unique = list({id(candidate): candidate for candidate in candidates}.values())
    return unique[0] if len(unique) == 1 else None


def _request_body_component_names(schema: dict[str, Any]) -> set[str]:
    """Every component name reachable from a request body, following `$ref`
    through `properties`/`items`/`anyOf`/`oneOf`/`allOf`/
    `additionalProperties` — the same shape `test_request_bounds.py`'s
    `_offenders` walks, but collecting names instead of judging bounds, so a
    nested type (`ChecklistQuestion` inside `ChecklistIn.items`) is checked
    too."""
    components = schema.get("components", {}).get("schemas", {})
    names: set[str] = set()

    def visit(node: dict[str, Any]) -> None:
        ref = node.get("$ref")
        if ref is not None:
            name = ref.rsplit("/", 1)[-1]
            if name not in names:
                names.add(name)
                visit(components.get(name, {}))
            return
        for key in ("anyOf", "oneOf", "allOf"):
            for member in node.get(key, []):
                visit(member)
        for sub in node.get("properties", {}).values():
            visit(sub)
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            visit(extra)
        if "items" in node:
            visit(node["items"])

    for operations in schema["paths"].values():
        for operation in operations.values():
            body = operation.get("requestBody")
            if body is None:
                continue
            for media in body.get("content", {}).values():
                visit(media.get("schema", {}))
    return names


def _min_length_and_stripped(field: FieldInfo) -> tuple[int | None, bool]:
    """`(min_length, stripped)` off a field's metadata. A `StringConstraints`
    instance (every shared bounded type in `app/core/schemas.py`) carries
    both attributes together; a bare `Field(min_length=..., pattern=...)`
    decomposes into separate `annotated_types.MinLen`/`MaxLen` objects with
    no stripping information at all — that shape can never be "stripped",
    which is exactly the gap this test closes."""
    min_length: int | None = None
    stripped = False
    for item in field.metadata:
        if isinstance(item, StringConstraints):
            if item.min_length is not None:
                min_length = item.min_length
            if item.strip_whitespace:
                stripped = True
        elif getattr(item, "min_length", None) is not None:
            min_length = item.min_length
    return min_length, stripped


def _accepts_str(annotation: Any) -> bool:
    if annotation is str:
        return True
    if typing.get_origin(annotation) is typing.Union:
        return str in typing.get_args(annotation)
    return False


def test_every_required_request_string_is_stripped() -> None:
    schema = _openapi()
    by_qualname, by_bare = _model_registry()
    names = _request_body_component_names(schema)
    checked = 0
    offenders: set[str] = set()
    unresolved: set[str] = set()
    for name in names:
        model = _resolve_model(name, by_qualname, by_bare)
        if model is None:
            unresolved.add(name)
            continue
        for field_name, field in model.model_fields.items():
            if not field.is_required() or not _accepts_str(field.annotation):
                continue
            min_length, stripped = _min_length_and_stripped(field)
            if min_length is None or min_length < 1:
                continue
            checked += 1
            if stripped:
                continue
            if any(item in _EXEMPT_METADATA for item in field.metadata):
                continue
            offenders.add(f"{name}.{field_name}")

    # A walk that resolves nothing cannot fail (the lesson
    # test_every_integer_query_parameter_carries_an_upper_bound paid for), so
    # both counts are asserted.
    assert checked >= 50, f"only {checked} required strings seen — the walk is wrong"
    # FastAPI's own synthetic multipart Body_* schemas (UploadFile + Form(...)
    # routes) are not source-level classes and cannot be resolved this way —
    # the only names allowed to stay unresolved.
    unexpected_unresolved = {name for name in unresolved if not name.startswith("Body_")}
    assert not unexpected_unresolved, (
        f"could not resolve to a model class: {sorted(unexpected_unresolved)}"
    )
    assert not offenders, (
        "a required string accepts leading/trailing whitespace as real content — use a "
        "stripped bounded type (CodeStr, NameStr, TextStr, LongTextStr) or add "
        "strip_whitespace=True to its own StringConstraints: " + ", ".join(sorted(offenders))
    )
