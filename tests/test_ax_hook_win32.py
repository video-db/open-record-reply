"""Tests for _find_element_at_point label fallback chain in ax_hook_win32."""

from unittest.mock import MagicMock, patch

import pytest


def _make_mock_ctrl(name="", automation_id="", class_name="", help_text="", parent=None, children=None):
    ctrl = MagicMock()
    ctrl.Name = name
    ctrl.AutomationId = automation_id
    ctrl.ClassName = class_name
    ctrl.HelpText = help_text
    ctrl.BoundingRectangle = MagicMock()
    ctrl.BoundingRectangle.left = 100
    ctrl.BoundingRectangle.top = 200
    ctrl.BoundingRectangle.width.return_value = 150
    ctrl.BoundingRectangle.height.return_value = 40
    if parent:
        ctrl.GetParentControl.return_value = parent
        if children is not None:
            parent.GetChildren.return_value = children
        elif not hasattr(parent, "GetChildren"):
            parent.GetChildren.return_value = []
    else:
        ctrl.GetParentControl.return_value = None
    return ctrl


def _make_mock_text_child(name, x, y, w=80, h=20):
    child = MagicMock()
    child.Name = name
    child.BoundingRectangle = MagicMock()
    child.BoundingRectangle.left = x
    child.BoundingRectangle.top = y
    child.BoundingRectangle.width.return_value = w
    child.BoundingRectangle.height.return_value = h
    return child


def _mock_fg_window(title=""):
    fg = MagicMock()
    fg.Name = title
    return fg


class TestFindElementAtPoint:
    def test_uses_name_when_present(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        ctrl = _make_mock_ctrl(name="Submit", automation_id="btn-submit", class_name="Button")
        with patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("")):
            result = _find_element_at_point(100, 200)
        assert result["label"] == "Submit"
        assert result["automation_id"] == "btn-submit"
        assert result["class_name"] == "Button"
        assert "foreground_window" in result

    def test_falls_back_to_automation_id_when_name_empty(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        ctrl = _make_mock_ctrl(name="", automation_id="search-box", class_name="Edit")
        with patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("")):
            result = _find_element_at_point(100, 200)
        assert result["label"] == "search-box"
        assert result["automation_id"] == "search-box"

    def test_falls_back_to_class_name_when_name_and_automation_id_empty(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        ctrl = _make_mock_ctrl(name="", automation_id="", class_name="Edit")
        with patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("")):
            result = _find_element_at_point(100, 200)
        assert result["label"] == "Edit"
        assert result["class_name"] == "Edit"

    def test_falls_back_to_help_text_when_previous_empty(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        ctrl = _make_mock_ctrl(
            name="", automation_id="", class_name="",
            help_text="Enter your username here"
        )
        with patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("")):
            result = _find_element_at_point(100, 200)
        assert result["label"] == "Enter your username here"
        assert result["help_text"] == "Enter your username here"

    def test_falls_back_to_parent_name_when_all_child_props_empty(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        parent = MagicMock()
        parent.Name = "Search Form"
        ctrl = _make_mock_ctrl(
            name="", automation_id="", class_name="",
            help_text="", parent=parent
        )
        with patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("")):
            result = _find_element_at_point(100, 200)
        assert result["label"] == "Search Form"

    def test_falls_back_to_nearby_text_label(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        ctrl = _make_mock_ctrl(
            name="", automation_id="", class_name="",
            help_text="", parent=None
        )
        with patch("capture.native.ax_hook_win32._find_nearby_label", return_value="Username:"), \
             patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("")):
            result = _find_element_at_point(100, 200)
        assert result["label"] == "Username:"

    def test_nearby_text_empty_when_no_match(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        ctrl = _make_mock_ctrl(
            name="", automation_id="", class_name="",
            help_text="", parent=None
        )
        with patch("capture.native.ax_hook_win32._find_nearby_label", return_value=""), \
             patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("")):
            result = _find_element_at_point(100, 200)
        assert result["label"] == ""

    def test_foreground_window_captured(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        ctrl = _make_mock_ctrl(name="Submit")
        with patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("Chrome")):
            result = _find_element_at_point(100, 200)
        assert result["foreground_window"] == "Chrome"

    def test_returns_empty_label_when_nothing_available(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        ctrl = _make_mock_ctrl(
            name="", automation_id="", class_name="",
            help_text="", parent=None
        )
        with patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("")):
            result = _find_element_at_point(100, 200)
        assert result["label"] == ""
        assert result["automation_id"] == ""
        assert result["class_name"] == ""

    def test_skips_failed_property_access(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        ctrl = _make_mock_ctrl(name="", automation_id="", class_name="", help_text="", parent=None)
        ctrl.AutomationId = None
        type(ctrl).AutomationId = property(lambda self: (_ for _ in ()).throw(Exception("boom")))
        with patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("")):
            result = _find_element_at_point(100, 200)
        assert result["label"] == ""
        assert result["automation_id"] == ""

    def test_name_None_treated_as_empty(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        ctrl = _make_mock_ctrl(name=None, automation_id="btn-ok")
        with patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("")):
            result = _find_element_at_point(100, 200)
        assert result["label"] == "btn-ok"

    def test_separate_fields_kept_independent_from_label_fallback(self):
        from capture.native.ax_hook_win32 import _find_element_at_point
        ctrl = _make_mock_ctrl(name="Visible Label", automation_id="internal-id", class_name="Edit", help_text="Fill this in")
        with patch("capture.native.ax_hook_win32.auto.ControlFromPoint", return_value=ctrl), \
             patch("capture.native.ax_hook_win32.auto.GetForegroundControl", return_value=_mock_fg_window("App")):
            result = _find_element_at_point(100, 200)
        assert result["label"] == "Visible Label"
        assert result["automation_id"] == "internal-id"
        assert result["class_name"] == "Edit"
        assert result["help_text"] == "Fill this in"
        assert result["foreground_window"] == "App"


class _FakeTextControl(MagicMock):
    pass


class TestFindNearbyLabel:
    def test_finds_nearby_text_control(self):
        from capture.native.ax_hook_win32 import _find_nearby_label

        parent = MagicMock()
        label = _FakeTextControl()
        label.Name = "Username:"
        label.BoundingRectangle = MagicMock()
        label.BoundingRectangle.left = 100
        label.BoundingRectangle.top = 170
        label.BoundingRectangle.width.return_value = 70
        label.BoundingRectangle.height.return_value = 20

        ctrl = _FakeTextControl()
        ctrl.Name = ""
        ctrl.BoundingRectangle = MagicMock()
        ctrl.BoundingRectangle.left = 100
        ctrl.BoundingRectangle.top = 195
        ctrl.BoundingRectangle.width.return_value = 150
        ctrl.BoundingRectangle.height.return_value = 40
        ctrl.GetParentControl.return_value = parent
        parent.GetChildren.return_value = [label, ctrl]

        with patch("capture.native.ax_hook_win32.auto.TextControl", _FakeTextControl):
            result = _find_nearby_label(ctrl)
        assert result == "Username:"

    def test_ignores_far_text_control(self):
        from capture.native.ax_hook_win32 import _find_nearby_label

        parent = MagicMock()
        label = _FakeTextControl()
        label.Name = "Far Label"
        label.BoundingRectangle = MagicMock()
        label.BoundingRectangle.left = 500
        label.BoundingRectangle.top = 500
        label.BoundingRectangle.width.return_value = 70
        label.BoundingRectangle.height.return_value = 20

        ctrl = _FakeTextControl()
        ctrl.BoundingRectangle = MagicMock()
        ctrl.BoundingRectangle.left = 100
        ctrl.BoundingRectangle.top = 200
        ctrl.BoundingRectangle.width.return_value = 150
        ctrl.BoundingRectangle.height.return_value = 40
        ctrl.GetParentControl.return_value = parent
        parent.GetChildren.return_value = [label, ctrl]

        with patch("capture.native.ax_hook_win32.auto.TextControl", _FakeTextControl):
            result = _find_nearby_label(ctrl)
        assert result == ""

    def test_returns_empty_when_no_parent(self):
        from capture.native.ax_hook_win32 import _find_nearby_label
        ctrl = _make_mock_ctrl(parent=None)
        result = _find_nearby_label(ctrl)
        assert result == ""

    def test_returns_empty_when_no_children(self):
        from capture.native.ax_hook_win32 import _find_nearby_label
        parent = MagicMock()
        parent.GetChildren.side_effect = Exception("boom")
        ctrl = _make_mock_ctrl(parent=parent)
        result = _find_nearby_label(ctrl)
        assert result == ""


class TestNormalizeTargetPreservesFields:
    def test_preserves_automation_id_class_name_help_text(self):
        from compiler.compiler import _normalize_target
        target = {
            "type": "AXTextField",
            "label": "username",
            "automation_id": "user-input",
            "class_name": "Edit",
            "help_text": "Enter your username",
        }
        result = _normalize_target(target)
        assert result["automation_id"] == "user-input"
        assert result["class_name"] == "Edit"
        assert result["help_text"] == "Enter your username"

    def test_preserves_foreground_window(self):
        from compiler.compiler import _normalize_target
        target = {
            "type": "AXButton",
            "label": "Search",
            "foreground_window": "Windows Search",
        }
        result = _normalize_target(target)
        assert result["foreground_window"] == "Windows Search"

    def test_omits_empty_optional_fields(self):
        from compiler.compiler import _normalize_target
        target = {"type": "AXButton", "label": "Submit", "automation_id": "", "class_name": "", "help_text": "", "foreground_window": ""}
        result = _normalize_target(target)
        assert "automation_id" not in result
        assert "class_name" not in result
        assert "help_text" not in result
        assert "foreground_window" not in result

    def test_handles_target_without_optional_fields(self):
        from compiler.compiler import _normalize_target
        target = {"type": "AXButton", "label": "Submit"}
        result = _normalize_target(target)
        assert result["type"] == "AXButton"
        assert result["label"] == "Submit"
        assert "automation_id" not in result


class TestEmitTypeForLastClick:
    def test_includes_foreground_window(self):
        from capture.native.ax_hook_win32 import _emit_type_for_last_click
        import capture.native.ax_hook_win32 as hook

        info = {"label": "search", "automation_id": "q", "class_name": "Edit", "help_text": "", "foreground_window": "Chrome"}
        with patch.object(hook, "_last_click_pos", (500, 300)), \
             patch.object(hook, "_last_click_target_info", info), \
             patch("capture.native.ax_hook_win32.write_event") as mock_write:
            _emit_type_for_last_click("test query", 1000)
            args = mock_write.call_args[0][0]
            assert args["target"]["foreground_window"] == "Chrome"
            assert args["target"]["automation_id"] == "q"

    def test_skips_when_buffer_empty(self):
        from capture.native.ax_hook_win32 import _emit_type_for_last_click
        import capture.native.ax_hook_win32 as hook

        with patch.object(hook, "_last_click_pos", (500, 300)), \
             patch("capture.native.ax_hook_win32.write_event") as mock_write:
            _emit_type_for_last_click("  ", 1000)
            mock_write.assert_not_called()


class TestGetForegroundWindowTitle:
    def test_uia_foreground_used_first(self):
        import capture.native.ax_hook_win32 as hook
        fg = MagicMock()
        fg.Name = "Notepad"
        with patch.object(hook.auto, "GetForegroundControl", return_value=fg), \
             patch.object(hook.auto, "GetRootControl") as mock_root:
            result = hook._get_foreground_window_title()
        assert result == "Notepad"
        mock_root.assert_not_called()

    def test_uia_root_used_when_foreground_empty_name(self):
        import capture.native.ax_hook_win32 as hook
        fg = MagicMock()
        fg.Name = ""
        root = MagicMock()
        root.Name = "Desktop"
        with patch.object(hook.auto, "GetForegroundControl", return_value=fg), \
             patch.object(hook.auto, "GetRootControl", return_value=root), \
             patch.object(hook.user32, "GetForegroundWindow") as mock_gfw:
            result = hook._get_foreground_window_title()
        assert result == "Desktop"
        mock_gfw.assert_not_called()

    def test_win32_fallback_when_uia_empty(self):
        import capture.native.ax_hook_win32 as hook
        import ctypes
        fg = MagicMock()
        fg.Name = ""
        root = MagicMock()
        root.Name = ""
        hwnd = 12345
        with patch.object(hook.auto, "GetForegroundControl", return_value=fg), \
             patch.object(hook.auto, "GetRootControl", return_value=root), \
             patch.object(hook.user32, "GetForegroundWindow", return_value=hwnd), \
             patch.object(hook.user32, "GetWindowTextLengthW", return_value=7), \
             patch.object(hook.user32, "GetWindowTextW") as mock_gettext:
            def _fake_gettext(h, buf, n):
                buf.value = "Chrome"
            mock_gettext.side_effect = _fake_gettext
            result = hook._get_foreground_window_title()
        assert result == "Chrome"

    def test_win32_fallback_empty_window(self):
        import capture.native.ax_hook_win32 as hook
        fg = MagicMock()
        fg.Name = ""
        root = MagicMock()
        root.Name = ""
        with patch.object(hook.auto, "GetForegroundControl", return_value=fg), \
             patch.object(hook.auto, "GetRootControl", return_value=root), \
             patch.object(hook.user32, "GetForegroundWindow", return_value=None):
            result = hook._get_foreground_window_title()
        assert result == ""
