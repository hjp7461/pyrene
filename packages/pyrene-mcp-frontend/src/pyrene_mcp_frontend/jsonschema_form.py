"""Pure-function jsonschema → Streamlit widget mapping.

Q-7 default (PRD-040 §4.2): self-implemented, no `streamlit-pydantic`.
JSON Schema is a stable spec, so the mapping is testable in isolation.

Supported types (v1):
    - "string"          → text_input  (or selectbox if `enum`)
    - "integer"         → number_input(step=1)
    - "number"          → number_input(step=0.1)
    - "boolean"         → checkbox
    - "array of string" → text_area, comma-separated, parsed back

Out of scope (raises NotImplementedError):
    - nested "object"
    - "array" of non-string
    - "$ref" / oneOf / anyOf

The render result is a list of `WidgetCall` records — page modules iterate
them and call `st` widgets, so that this module stays Streamlit-free
and 100 % unit-testable (no Streamlit runtime).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

WidgetKind = Literal[
    "text_input",
    "number_input",
    "checkbox",
    "selectbox",
    "text_area_csv",
]


@dataclass(frozen=True)
class WidgetCall:
    """Description of one Streamlit widget for one schema field.

    `kind` chooses which `st.*` function to call; `kwargs` are the
    keyword arguments for it. `field_name` is the JSON Schema property
    name and is also the dict key to assign back to.

    `parser` (None for primitives, callable for `text_area_csv`) is used
    by the page module to convert the raw widget value back to the JSON
    type expected by the tool.
    """

    field_name: str
    label: str
    kind: WidgetKind
    kwargs: dict[str, Any]
    required: bool
    parser: Literal["csv_to_str_list", None]


def _csv_to_str_list(raw: str) -> list[str]:
    """Parse comma-separated text back into a list of stripped strings."""
    return [part.strip() for part in raw.split(",") if part.strip()]


# Re-exported so callers don't need to know the parser tag string.
PARSERS: dict[str, Any] = {
    "csv_to_str_list": _csv_to_str_list,
}


def _kind_for_string(prop: Mapping[str, Any]) -> WidgetKind:
    if "enum" in prop:
        return "selectbox"
    return "text_input"


def _build_one(
    field_name: str,
    prop: Mapping[str, Any],
    *,
    required: bool,
) -> WidgetCall:
    label = str(prop.get("title", field_name))
    description = prop.get("description")
    help_text = str(description) if description else None

    type_value = prop.get("type")

    if type_value == "string":
        kind = _kind_for_string(prop)
        if kind == "selectbox":
            return WidgetCall(
                field_name=field_name,
                label=label,
                kind="selectbox",
                kwargs={
                    "options": list(prop["enum"]),
                    "help": help_text,
                },
                required=required,
                parser=None,
            )
        return WidgetCall(
            field_name=field_name,
            label=label,
            kind="text_input",
            kwargs={"value": str(prop.get("default", "")), "help": help_text},
            required=required,
            parser=None,
        )

    if type_value == "integer":
        return WidgetCall(
            field_name=field_name,
            label=label,
            kind="number_input",
            kwargs={
                "value": int(prop.get("default", 0)),
                "step": 1,
                "help": help_text,
            },
            required=required,
            parser=None,
        )

    if type_value == "number":
        return WidgetCall(
            field_name=field_name,
            label=label,
            kind="number_input",
            kwargs={
                "value": float(prop.get("default", 0.0)),
                "step": 0.1,
                "help": help_text,
            },
            required=required,
            parser=None,
        )

    if type_value == "boolean":
        return WidgetCall(
            field_name=field_name,
            label=label,
            kind="checkbox",
            kwargs={
                "value": bool(prop.get("default", False)),
                "help": help_text,
            },
            required=required,
            parser=None,
        )

    if type_value == "array":
        items = prop.get("items", {})
        if items.get("type") == "string":
            return WidgetCall(
                field_name=field_name,
                label=f"{label} (쉼표로 구분)",
                kind="text_area_csv",
                kwargs={
                    "value": ",".join(prop.get("default", []) or []),
                    "help": help_text or "여러 값을 쉼표로 구분해서 입력하세요",
                },
                required=required,
                parser="csv_to_str_list",
            )
        raise NotImplementedError(
            f"array of {items.get('type')!r} is not supported in v1"
        )

    if type_value == "object":
        raise NotImplementedError(
            "nested object schemas are not supported in v1 — "
            "use the gateway invoke endpoint directly via curl/HTTP for these tools"
        )

    raise NotImplementedError(f"unsupported JSON Schema type: {type_value!r}")


def render(schema: Mapping[str, Any]) -> list[WidgetCall]:
    """Map a JSON Schema (`type=object` envelope) to a list of WidgetCall.

    Empty `properties` → empty list (caller should still allow "execute"
    button — some MCP tools take no arguments).
    """
    if schema.get("type") not in (None, "object"):
        raise NotImplementedError(
            f"top-level schema must be type=object, got {schema.get('type')!r}"
        )

    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    return [
        _build_one(name, prop, required=name in required)
        for name, prop in properties.items()
    ]


__all__ = [
    "PARSERS",
    "WidgetCall",
    "WidgetKind",
    "render",
]
