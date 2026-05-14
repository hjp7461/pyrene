"""Unit tests for `jsonschema_form.render` — pure function, no Streamlit.

PRD-040 AC-5 — coverage ≥ 80% for the matrix of supported types.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pyrene_mcp_frontend import jsonschema_form
from pyrene_mcp_frontend.jsonschema_form import WidgetCall


def test_empty_object_yields_no_widgets() -> None:
    assert jsonschema_form.render({"type": "object", "properties": {}}) == []


def test_string_default_widget_is_text_input() -> None:
    widgets = jsonschema_form.render(
        {
            "type": "object",
            "properties": {"name": {"type": "string", "default": "alice"}},
            "required": ["name"],
        }
    )
    assert len(widgets) == 1
    w = widgets[0]
    assert w.field_name == "name"
    assert w.kind == "text_input"
    assert w.required is True
    assert w.kwargs["value"] == "alice"


def test_string_with_enum_yields_selectbox() -> None:
    widgets = jsonschema_form.render(
        {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["info", "warn", "error"]}
            },
        }
    )
    assert widgets[0].kind == "selectbox"
    assert widgets[0].kwargs["options"] == ["info", "warn", "error"]


def test_integer_yields_number_input_step1() -> None:
    widgets = jsonschema_form.render(
        {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 5}},
        }
    )
    assert widgets[0].kind == "number_input"
    assert widgets[0].kwargs["value"] == 5
    assert widgets[0].kwargs["step"] == 1


def test_number_yields_number_input_step_decimal() -> None:
    widgets = jsonschema_form.render(
        {
            "type": "object",
            "properties": {"ratio": {"type": "number", "default": 0.5}},
        }
    )
    assert widgets[0].kind == "number_input"
    assert widgets[0].kwargs["step"] == 0.1


def test_boolean_yields_checkbox() -> None:
    widgets = jsonschema_form.render(
        {
            "type": "object",
            "properties": {"force": {"type": "boolean", "default": True}},
        }
    )
    assert widgets[0].kind == "checkbox"
    assert widgets[0].kwargs["value"] is True


def test_array_of_string_yields_text_area_csv_with_parser() -> None:
    widgets = jsonschema_form.render(
        {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["a", "b"],
                }
            },
        }
    )
    w = widgets[0]
    assert w.kind == "text_area_csv"
    assert w.parser == "csv_to_str_list"
    assert w.kwargs["value"] == "a,b"


def test_csv_parser_strips_and_drops_empty() -> None:
    parser = jsonschema_form.PARSERS["csv_to_str_list"]
    assert parser("a, b ,, c") == ["a", "b", "c"]


def test_unsupported_array_item_type_raises() -> None:
    with pytest.raises(NotImplementedError, match="array of"):
        jsonschema_form.render(
            {
                "type": "object",
                "properties": {
                    "nested": {"type": "array", "items": {"type": "integer"}}
                },
            }
        )


def test_nested_object_raises() -> None:
    with pytest.raises(NotImplementedError, match="nested object"):
        jsonschema_form.render(
            {
                "type": "object",
                "properties": {"sub": {"type": "object", "properties": {}}},
            }
        )


def test_unsupported_top_level_type_raises() -> None:
    with pytest.raises(NotImplementedError, match="must be type=object"):
        jsonschema_form.render({"type": "string"})


def test_unsupported_property_type_raises() -> None:
    with pytest.raises(NotImplementedError, match="unsupported"):
        jsonschema_form.render(
            {"type": "object", "properties": {"x": {"type": "null"}}}
        )


def test_required_marker_propagates() -> None:
    widgets = jsonschema_form.render(
        {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "string"},
            },
            "required": ["a"],
        }
    )
    by_name = {w.field_name: w for w in widgets}
    assert by_name["a"].required is True
    assert by_name["b"].required is False


def test_title_overrides_field_name_as_label() -> None:
    widgets = jsonschema_form.render(
        {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "title": "사용자 ID"},
            },
        }
    )
    assert widgets[0].label == "사용자 ID"


def test_widget_call_is_frozen_dataclass() -> None:
    """Defensive — ensures WidgetCall stays immutable."""
    w = WidgetCall(
        field_name="x",
        label="x",
        kind="text_input",
        kwargs={},
        required=False,
        parser=None,
    )
    with pytest.raises(FrozenInstanceError):
        w.field_name = "y"  # type: ignore[misc]
