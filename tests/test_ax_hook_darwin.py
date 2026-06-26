"""Tests for macOS AX hook helpers."""

from types import SimpleNamespace

import pytest

from capture.native import ax_hook_darwin as darwin


class FakeAX:
    kAXErrorSuccess = 0
    kAXRoleAttribute = "AXRole"
    kAXTitleAttribute = "AXTitle"
    kAXDescriptionAttribute = "AXDescription"
    kAXValueAttribute = "AXValue"
    kAXPlaceholderValueAttribute = "AXPlaceholder"
    kAXHelpAttribute = "AXHelp"
    kAXIdentifierAttribute = "AXIdentifier"
    kAXRoleDescriptionAttribute = "AXRoleDescription"
    kAXPositionAttribute = "AXPosition"
    kAXSizeAttribute = "AXSize"
    kAXValueCGPointType = 1
    kAXValueCGSizeType = 2

    @staticmethod
    def AXValueGetValue(value, _value_type, _value_ptr):
        return True, value


def test_normalize_ax_result_accepts_error_value_tuple():
    assert darwin._normalize_ax_result((0, "value")) == (0, "value")


def test_normalize_ax_result_accepts_value_error_tuple():
    assert darwin._normalize_ax_result(("value", 0)) == (0, "value")


def test_ax_element_to_info_maps_role_label_and_frame(monkeypatch):
    values = {
        "AXRole": "AXTextField",
        "AXTitle": "",
        "AXDescription": "Search",
        "AXValue": "",
        "AXPlaceholder": "",
        "AXHelp": "",
        "AXIdentifier": "",
        "AXRoleDescription": "",
        "AXPosition": SimpleNamespace(x=10, y=20),
        "AXSize": SimpleNamespace(width=300, height=40),
    }

    monkeypatch.setattr(darwin, "_get_ax_module", lambda: FakeAX)
    monkeypatch.setattr(darwin, "_copy_attribute", lambda _element, attr: values[attr])

    result = darwin._ax_element_to_info(object(), 1, 2)

    assert result == {
        "x": 10,
        "y": 20,
        "width": 300,
        "height": 40,
        "label": "Search",
        "type": "AXTextField",
    }


def test_ax_element_to_info_uses_placeholder_label(monkeypatch):
    values = {
        "AXRole": "AXTextArea",
        "AXTitle": "",
        "AXDescription": "",
        "AXValue": "",
        "AXPlaceholder": "Message #testing",
        "AXHelp": "",
        "AXIdentifier": "",
        "AXRoleDescription": "",
        "AXPosition": SimpleNamespace(x=10, y=20),
        "AXSize": SimpleNamespace(width=300, height=40),
    }

    monkeypatch.setattr(darwin, "_get_ax_module", lambda: FakeAX)
    monkeypatch.setattr(darwin, "_copy_attribute", lambda _element, attr: values[attr])

    result = darwin._ax_element_to_info(object(), 1, 2)

    assert result["label"] == "Message #testing"
    assert result["type"] == "AXTextField"


def test_to_number_decodes_ax_value(monkeypatch):
    monkeypatch.setattr(darwin, "_get_ax_module", lambda: FakeAX)

    assert darwin._to_number(SimpleNamespace(x=42, y=84), "x") == 42
    assert darwin._to_number(SimpleNamespace(width=320, height=200), "height") == 200


def test_role_to_ax_type_uses_configurable_default():
    assert darwin._role_to_ax_type("AXDefinitelyMissing", default="AXUnknown") == "AXUnknown"


def test_copy_children_accepts_iterable_objective_c_arrays(monkeypatch):
    class ChildArray:
        def __iter__(self):
            return iter(["a", "b"])

    monkeypatch.setattr(darwin, "_copy_attribute", lambda _element, _attr: ChildArray())

    assert darwin._copy_children(object()) == ["a", "b"]


def test_find_elements_filters_by_type_and_prefers_label(monkeypatch):
    infos = {
        "root": {"type": "AXWindow", "label": "App", "x": 0, "y": 0, "width": 10, "height": 10},
        "save": {"type": "AXButton", "label": "Save", "x": 10, "y": 10, "width": 40, "height": 20},
        "save_as": {"type": "AXButton", "label": "Save As", "x": 20, "y": 20, "width": 80, "height": 20},
        "amount": {"type": "AXTextField", "label": "Amount", "x": 30, "y": 30, "width": 80, "height": 20},
    }

    monkeypatch.setattr(darwin, "_visible_application_roots", lambda: ["root"])
    monkeypatch.setattr(darwin, "_walk_ax_elements", lambda _root: iter(infos.keys()))
    monkeypatch.setattr(darwin, "_ax_element_to_info", lambda element, _x, _y: dict(infos[element]))

    results = darwin._find_elements("AXButton", "Save", limit=10)

    assert [item["label"] for item in results] == ["Save", "Save As"]
    assert all(item["found"] for item in results)


def test_public_info_removes_internal_element():
    result = darwin._public_info({
        "label": "Save",
        "type": "AXButton",
        "_element": object(),
    })

    assert result == {"label": "Save", "type": "AXButton"}


def test_handle_message_find_element_stores_internal_element(monkeypatch):
    element = object()
    responses = []

    monkeypatch.setattr(darwin, "write_response", responses.append)
    monkeypatch.setattr(darwin, "_find_elements", lambda *_args, **_kwargs: [{
        "found": True,
        "label": "Save",
        "type": "AXButton",
        "_element": element,
    }])

    darwin.handle_message({
        "id": "req-1",
        "method": "find_element",
        "params": {"type": "AXButton", "label": "Save"},
    })

    assert darwin._last_element is element
    assert responses[0]["result"] == {"found": True, "label": "Save", "type": "AXButton"}


def test_handle_message_execute_type_uses_last_element(monkeypatch):
    element = object()
    responses = []
    calls = []

    monkeypatch.setattr(darwin, "write_response", responses.append)
    monkeypatch.setattr(darwin, "_last_element", element)
    monkeypatch.setattr(darwin, "_last_element_info", {"label": "Search", "type": "AXTextField"})
    monkeypatch.setattr(
        darwin,
        "_execute_type",
        lambda received_element, info, value: calls.append((received_element, info, value)) or True,
    )

    darwin.handle_message({
        "id": "req-2",
        "method": "execute_action",
        "params": {"action": "type", "value": "hello"},
    })

    assert calls == [(element, {"label": "Search", "type": "AXTextField"}, "hello")]
    assert responses[0]["result"] == {"typed": "hello", "performed": True}


def test_on_click_emits_click_event(monkeypatch):
    events = []

    monkeypatch.setattr(darwin, "is_recording", True)
    monkeypatch.setattr(darwin, "_pending_type_target", None)
    monkeypatch.setattr(darwin, "_pending_value", "")
    monkeypatch.setattr(darwin, "_pending_action_type", "")
    monkeypatch.setattr(darwin, "_find_element_at_point", lambda x, y: {
        "type": "AXButton",
        "label": "Save",
    })
    monkeypatch.setattr(darwin, "write_event", events.append)

    darwin._on_click(10, 20, darwin.mouse.Button.left, True)

    assert events[0]["action"] == "click"
    assert events[0]["target"] == {"type": "AXButton", "label": "Save", "role": "AXButton"}
    assert events[0]["position"] == {"x": 10, "y": 20}


def test_on_press_accumulates_pending_text(monkeypatch):
    events = []

    monkeypatch.setattr(darwin, "is_recording", True)
    monkeypatch.setattr(darwin, "_pending_type_target", {
        "type": "AXTextField",
        "label": "Search",
    })
    monkeypatch.setattr(darwin, "_pending_value", "")
    monkeypatch.setattr(darwin, "_pending_action_type", "type")
    monkeypatch.setattr(darwin, "write_event", events.append)

    darwin._on_press(SimpleNamespace(char="h"))
    darwin._on_press(SimpleNamespace(char="i"))
    darwin._on_press(darwin.keyboard.Key.enter)

    assert events[0]["action"] == "type"
    assert events[0]["target"] == {"type": "AXTextField", "label": "Search", "role": "AXTextField"}
    assert events[0]["value"] == "hi"


def test_on_press_falls_back_to_last_unknown_click(monkeypatch):
    events = []

    monkeypatch.setattr(darwin, "is_recording", True)
    monkeypatch.setattr(darwin, "_pending_type_target", None)
    monkeypatch.setattr(darwin, "_pending_value", "")
    monkeypatch.setattr(darwin, "_pending_action_type", "")
    monkeypatch.setattr(darwin, "_last_click_target", {
        "type": "AXUnknown",
        "label": "Message #testing",
    })
    monkeypatch.setattr(darwin, "write_event", events.append)

    darwin._on_press(SimpleNamespace(char="h"))
    darwin._on_press(SimpleNamespace(char="i"))
    darwin._on_press(darwin.keyboard.Key.enter)

    assert events[0]["action"] == "type"
    assert events[0]["target"] == {
        "type": "AXTextField",
        "label": "Message #testing",
        "role": "AXTextField",
    }
    assert events[0]["value"] == "hi"


def test_handle_message_check_permissions(monkeypatch):
    responses = []

    monkeypatch.setattr(darwin, "_is_accessibility_trusted", lambda prompt=False: prompt)
    monkeypatch.setattr(darwin, "HAS_PYNPUT", True)
    monkeypatch.setattr(darwin, "write_response", responses.append)

    darwin.handle_message({
        "id": "req-1",
        "method": "check_permissions",
        "params": {"prompt": True},
    })

    assert responses[0]["id"] == "req-1"
    assert responses[0]["status"] == "ok"
    assert responses[0]["result"]["ready_for_event_recording"] is True
