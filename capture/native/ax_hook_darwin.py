"""AX hook for macOS - real input capture with best-effort accessibility.

Recording: pynput monitors mouse clicks and keyboard input. On click, attempts
to identify the element under cursor via macOS Accessibility APIs.
Pushes real events to stdout in real-time.
Replay: simulated responses only (no GUI automation on macOS stub).
"""

import json
import sys
import os
import time
import threading
import subprocess
from collections.abc import Iterable

try:
    from pynput import mouse, keyboard
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.2
    HAS_PYAUTOGUI = True
except Exception:
    HAS_PYAUTOGUI = False

PID_FILE = "/tmp/ax_hook.pid"
with open(PID_FILE, "w") as f:
    f.write(str(os.getpid()))

events_file = None
is_recording = False

_write_lock = threading.Lock()
_pending_type_target = None
_pending_value = ""
_pending_action_type = ""
_listener_mouse = None
_listener_keyboard = None
_last_element = None
_last_element_info = None
_last_click_target = None
_last_click_ts = 0


def write_response(msg):
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def write_event(msg):
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _get_ax_module():
    try:
        import ApplicationServices as AX
        return AX
    except Exception:
        return None


def _normalize_ax_result(result, success_code=0):
    """Normalize PyObjC AX calls across tuple/object return variants."""
    if isinstance(result, tuple):
        if len(result) == 2:
            first, second = result
            if isinstance(first, int):
                return first, second
            if isinstance(second, int):
                return second, first
        if len(result) == 1:
            return success_code, result[0]
    if isinstance(result, int):
        return result, None
    return success_code, result


def _ax_call_value(fn, *args):
    AX = _get_ax_module()
    if AX is None:
        return None
    try:
        err, value = _normalize_ax_result(fn(*args), AX.kAXErrorSuccess)
    except TypeError:
        try:
            err, value = _normalize_ax_result(fn(*args[:-1]), AX.kAXErrorSuccess)
        except Exception:
            return None
    except Exception:
        return None
    return value if err == AX.kAXErrorSuccess else None


def _is_accessibility_trusted(prompt=False):
    AX = _get_ax_module()
    if AX is None:
        return False
    try:
        if prompt and hasattr(AX, "AXIsProcessTrustedWithOptions"):
            return bool(AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True}))
        return bool(AX.AXIsProcessTrusted())
    except Exception:
        return False


def _copy_attribute(element, attr_name):
    AX = _get_ax_module()
    if AX is None:
        return None
    return _ax_call_value(AX.AXUIElementCopyAttributeValue, element, attr_name, None)


def _copy_element_at_position(application, x, y):
    AX = _get_ax_module()
    if AX is None:
        return None
    return _ax_call_value(AX.AXUIElementCopyElementAtPosition, application, float(x), float(y), None)


def _set_attribute(element, attr_name, value):
    AX = _get_ax_module()
    if AX is None:
        return False
    try:
        err, _value = _normalize_ax_result(
            AX.AXUIElementSetAttributeValue(element, attr_name, value),
            AX.kAXErrorSuccess,
        )
        return err == AX.kAXErrorSuccess
    except Exception:
        return False


def _perform_action(element, action_name):
    AX = _get_ax_module()
    if AX is None:
        return False
    try:
        err, _value = _normalize_ax_result(
            AX.AXUIElementPerformAction(element, action_name),
            AX.kAXErrorSuccess,
        )
        return err == AX.kAXErrorSuccess
    except Exception:
        return False


def _to_number(value, field, default=0):
    if value is None:
        return default
    decoded = _decode_ax_value(value, field)
    if decoded is not None:
        return decoded
    if hasattr(value, field):
        return getattr(value, field)
    if isinstance(value, dict):
        return value.get(field, default)
    if isinstance(value, (list, tuple)):
        index = 0 if field in ("x", "width") else 1
        if len(value) > index:
            return value[index]
    return default


def _decode_ax_value(value, field):
    AX = _get_ax_module()
    if AX is None or not hasattr(AX, "AXValueGetValue"):
        return None

    value_type = None
    if field in ("x", "y") and hasattr(AX, "kAXValueCGPointType"):
        value_type = AX.kAXValueCGPointType
    elif field in ("width", "height") and hasattr(AX, "kAXValueCGSizeType"):
        value_type = AX.kAXValueCGSizeType
    if value_type is None:
        return None

    try:
        ok, decoded = AX.AXValueGetValue(value, value_type, None)
    except Exception:
        return None
    if not ok or decoded is None:
        return None
    if hasattr(decoded, field):
        return getattr(decoded, field)
    return None


def _coerce_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _window_at_point(x, y):
    try:
        import Quartz

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
        )
    except Exception:
        return None

    candidates = []
    for win in windows:
        bounds = win.get("kCGWindowBounds", {})
        wx = bounds.get("X", 0)
        wy = bounds.get("Y", 0)
        ww = bounds.get("Width", 0)
        wh = bounds.get("Height", 0)
        layer = win.get("kCGWindowLayer", 999)
        if layer == 0 and wx <= x <= wx + ww and wy <= y <= wy + wh:
            candidates.append((layer, win))
    return candidates[0][1] if candidates else None


def _surface_from_window(win, x=None, y=None):
    if not win:
        return None
    bounds = win.get("kCGWindowBounds", {}) or {}
    wx = int(bounds.get("X", 0))
    wy = int(bounds.get("Y", 0))
    ww = int(bounds.get("Width", 0))
    wh = int(bounds.get("Height", 0))
    surface = {
        "platform": "darwin",
        "app_name": _coerce_text(win.get("kCGWindowOwnerName", "")).strip(),
        "process_id": int(win.get("kCGWindowOwnerPID", 0) or 0),
        "window_title": _coerce_text(win.get("kCGWindowName", "")).strip(),
        "window_bounds": {"x": wx, "y": wy, "width": ww, "height": wh},
    }
    if x is not None and y is not None:
        surface["relative_position"] = {"x": int(x - wx), "y": int(y - wy)}
    return surface


def _event_surface_from_info(el_info):
    surface = (el_info or {}).get("surface")
    return dict(surface) if isinstance(surface, dict) else None


def _ax_element_to_info(element, fallback_x, fallback_y):
    AX = _get_ax_module()
    if AX is None:
        return None

    role = _coerce_text(_copy_attribute(element, AX.kAXRoleAttribute))
    label = ""
    for attr in _label_attributes():
        label = _coerce_text(_copy_attribute(element, attr)).strip()
        if label:
            break

    position = _copy_attribute(element, AX.kAXPositionAttribute)
    size = _copy_attribute(element, AX.kAXSizeAttribute)
    return {
        "x": int(_to_number(position, "x", fallback_x)),
        "y": int(_to_number(position, "y", fallback_y)),
        "width": int(_to_number(size, "width", 120)),
        "height": int(_to_number(size, "height", 30)),
        "label": label,
        "type": _role_to_ax_type(role, default="AXUnknown"),
    }


def _label_attributes():
    AX = _get_ax_module()
    if AX is None:
        return []
    names = [
        "kAXTitleAttribute",
        "kAXDescriptionAttribute",
        "kAXValueAttribute",
        "kAXPlaceholderValueAttribute",
        "kAXHelpAttribute",
        "kAXIdentifierAttribute",
        "kAXRoleDescriptionAttribute",
    ]
    attrs = []
    for name in names:
        attr = getattr(AX, name, None)
        if attr and attr not in attrs:
            attrs.append(attr)
    return attrs


def _try_get_element_via_accessibility(x, y):
    AX = _get_ax_module()
    if AX is None or not _is_accessibility_trusted(prompt=False):
        return None

    win = _window_at_point(x, y)
    app = None
    if win:
        pid = win.get("kCGWindowOwnerPID")
        if pid:
            try:
                app = AX.AXUIElementCreateApplication(pid)
            except Exception:
                app = None

    if app is None and hasattr(AX, "AXUIElementCreateSystemWide"):
        try:
            app = AX.AXUIElementCreateSystemWide()
        except Exception:
            app = None
    if app is None:
        return None

    element = _copy_element_at_position(app, x, y)
    if element is None:
        return None
    info = _ax_element_to_info(element, x, y)
    if info is not None:
        info["surface"] = _surface_from_window(win, x, y)
    return info


def _try_get_element_via_quartz(x, y):
    """Use CoreGraphics window data when Accessibility cannot resolve a control."""
    win = _window_at_point(x, y)
    if win:
        bounds = win.get("kCGWindowBounds", {})
        wx = bounds.get("X", x)
        wy = bounds.get("Y", y)
        ww = bounds.get("Width", 120)
        wh = bounds.get("Height", 30)
        name = win.get("kCGWindowName") or win.get("kCGWindowOwnerName", "")
        return {
            "label": name,
            "role": "AXWindow",
            "x": int(wx),
            "y": int(wy),
            "width": int(ww),
            "height": int(wh),
            "surface": _surface_from_window(win, x, y),
        }
    return None


def _find_element_at_point(x, y):
    """Try multiple methods to identify the element at screen coordinates."""
    result = _try_get_element_via_accessibility(x, y)
    if result:
        return result

    result = _try_get_element_via_quartz(x, y)
    if result:
        return {
            "x": result["x"],
            "y": result["y"],
            "width": result["width"],
            "height": result["height"],
            "label": result["label"],
            "type": "AXButton",
            "surface": result.get("surface"),
        }

    surface = _surface_from_window(_window_at_point(x, y), x, y)
    return {"x": x, "y": y, "width": 120, "height": 30, "label": "", "type": "AXButton", "surface": surface}


def _role_to_ax_type(role, default="AXButton"):
    role = role or ""
    mapping = {
        "AXButton": "AXButton",
        "AXTextField": "AXTextField",
        "AXTextArea": "AXTextField",
        "AXPopUpButton": "AXPopUpButton",
        "AXComboBox": "AXPopUpButton",
        "AXMenuButton": "AXPopUpButton",
        "AXCheckBox": "AXCheckbox",
        "AXCheckbox": "AXCheckbox",
        "AXMenuItem": "AXMenuItem",
        "AXRadioButton": "AXRadioButton",
        "AXStaticText": "AXStaticText",
        "AXLink": "AXLink",
        "AXImage": "AXButton",
        "AXMenuBarItem": "AXMenuItem",
    }
    return mapping.get(role, default)


def _ax_attr(name, fallback):
    AX = _get_ax_module()
    return getattr(AX, name, fallback) if AX else fallback


def _copy_children(element):
    children = _copy_attribute(element, _ax_attr("kAXChildrenAttribute", "AXChildren"))
    if children is None:
        return []
    if isinstance(children, (list, tuple)):
        return list(children)
    if isinstance(children, Iterable) and not isinstance(children, (str, bytes)):
        try:
            return list(children)
        except Exception:
            pass
    return [children]


def _visible_application_roots():
    AX = _get_ax_module()
    if AX is None or not _is_accessibility_trusted(prompt=False):
        return []

    roots = []
    seen_pids = set()
    try:
        import Quartz

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
        )
    except Exception:
        windows = []

    for win in windows:
        if win.get("kCGWindowLayer", 999) != 0:
            continue
        pid = win.get("kCGWindowOwnerPID")
        if not pid or pid in seen_pids:
            continue
        seen_pids.add(pid)
        try:
            roots.append(AX.AXUIElementCreateApplication(pid))
        except Exception:
            continue

    if hasattr(AX, "AXUIElementCreateSystemWide"):
        try:
            system = AX.AXUIElementCreateSystemWide()
            focused = _copy_attribute(system, _ax_attr("kAXFocusedApplicationAttribute", "AXFocusedApplication"))
            if focused is not None:
                roots.insert(0, focused)
        except Exception:
            pass
    return roots


def _walk_ax_elements(root, max_depth=8):
    stack = [(root, 0)]
    seen = set()
    while stack:
        element, depth = stack.pop()
        marker = id(element)
        if marker in seen:
            continue
        seen.add(marker)
        yield element
        if depth >= max_depth:
            continue
        children = _copy_children(element)
        for child in reversed(children):
            stack.append((child, depth + 1))


def _label_score(candidate, label):
    if not label:
        return 1
    candidate = (candidate or "").lower()
    label = label.lower()
    if candidate == label:
        return 100
    if label in candidate:
        return 80 + len(label) / max(len(candidate), 1)
    if candidate in label:
        return 60 + len(candidate) / max(len(label), 1)
    return 0


def _find_elements(el_type="", label="", limit=100):
    matches = []
    for root in _visible_application_roots():
        for element in _walk_ax_elements(root):
            info = _ax_element_to_info(element, 0, 0)
            if not info:
                continue
            if el_type and info.get("type") != el_type:
                continue
            score = _label_score(info.get("label", ""), label)
            if label and score == 0:
                continue
            info["_element"] = element
            info["found"] = True
            matches.append((score, info))
            if len(matches) >= limit:
                break
        if len(matches) >= limit:
            break
    matches.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in matches]


def _public_info(info):
    public = dict(info)
    public.pop("_element", None)
    return public


def _center_of(info):
    return (
        int(info.get("x", 0) + info.get("width", 0) / 2),
        int(info.get("y", 0) + info.get("height", 0) / 2),
    )


def _click_info(info):
    if not HAS_PYAUTOGUI:
        return False
    x, y = _center_of(info)
    try:
        pyautogui.click(x, y)
        return True
    except Exception:
        return False


def _type_with_pyautogui(info, value):
    if not HAS_PYAUTOGUI:
        return False
    try:
        _click_info(info)
        time.sleep(0.2)
        pyautogui.hotkey("command", "a")
        time.sleep(0.05)
        pyautogui.write(str(value))
        return True
    except Exception:
        return False


def _execute_click(element, info):
    AX = _get_ax_module()
    press_action = _ax_attr("kAXPressAction", "AXPress")
    if element is not None and AX is not None and _perform_action(element, press_action):
        return True
    return _click_info(info)


def _execute_type(element, info, value):
    AX = _get_ax_module()
    value_attr = _ax_attr("kAXValueAttribute", "AXValue")
    if element is not None and AX is not None:
        _perform_action(element, _ax_attr("kAXRaiseAction", "AXRaise"))
        if _set_attribute(element, value_attr, str(value)):
            return True
    return _type_with_pyautogui(info, value)


def _execute_select(element, info, value):
    clicked = _execute_click(element, info)
    if not HAS_PYAUTOGUI:
        return clicked
    try:
        time.sleep(0.3)
        pyautogui.write(str(value))
        time.sleep(0.1)
        pyautogui.press("enter")
        return True
    except Exception:
        return clicked


def _is_input_control(el_info):
    return el_info.get("type") in ("AXTextField", "AXPopUpButton")


def _get_action_type_for_control(el_info):
    el_type = el_info.get("type", "")
    if el_type == "AXTextField":
        return "type"
    if el_type == "AXPopUpButton":
        return "select"
    return "click"


def _finalize_pending(ts=None):
    global _pending_type_target, _pending_value, _pending_action_type
    if _pending_type_target is None or not _pending_value.strip():
        _pending_type_target = None
        _pending_value = ""
        _pending_action_type = ""
        return

    ts = ts or int(time.time() * 1000)
    target = {
        "type": _pending_type_target.get("type", "AXTextField"),
        "label": _pending_type_target.get("label", ""),
        "role": _pending_type_target.get("type", "AXTextField"),
    }
    evt = {
        "event": "action",
        "ts": ts,
        "action": _pending_action_type,
        "target": target,
        "value": _pending_value.strip(),
    }
    surface = _event_surface_from_info(_pending_type_target)
    if surface:
        evt["surface"] = surface
    write_event(evt)

    _pending_type_target = None
    _pending_value = ""
    _pending_action_type = ""


def _target_from_info(el_info, default_type="AXUnknown"):
    target_type = el_info.get("type") or default_type
    return {
        "type": target_type,
        "label": el_info.get("label", ""),
        "role": target_type,
    }


def _on_click(x, y, button, pressed):
    global _pending_type_target, _pending_value, _pending_action_type
    global _last_click_target, _last_click_ts
    if not is_recording or not pressed:
        return
    if button != mouse.Button.left:
        return

    ts = int(time.time() * 1000)
    el_info = _find_element_at_point(x, y)

    if _pending_value.strip():
        _finalize_pending(ts - 1)

    _last_click_target = el_info
    _last_click_ts = ts

    if _is_input_control(el_info):
        _pending_type_target = el_info
        _pending_action_type = _get_action_type_for_control(el_info)
        _pending_value = ""
    elif el_info:
        evt = {
            "event": "action",
            "ts": ts,
            "action": "click",
            "target": _target_from_info(el_info),
            "position": {"x": x, "y": y},
        }
        surface = _event_surface_from_info(el_info)
        if surface:
            evt["surface"] = surface
        write_event(evt)


def _on_press(key):
    global _pending_type_target, _pending_value, _pending_action_type
    if not is_recording:
        return

    ts = int(time.time() * 1000)

    try:
        if hasattr(key, 'char') and key.char is not None:
            _append_pending_text(key.char)
        elif key == keyboard.Key.space:
            _append_pending_text(" ")
        elif key == keyboard.Key.tab:
            _finalize_pending(ts)
        elif key == keyboard.Key.enter:
            _finalize_pending(ts)
        elif key == keyboard.Key.esc:
            _pending_type_target = None
            _pending_value = ""
            _pending_action_type = ""
        elif key == keyboard.Key.backspace:
            if _pending_value:
                _pending_value = _pending_value[:-1]
    except Exception:
        pass


def _append_pending_text(text):
    global _pending_type_target, _pending_action_type, _pending_value
    if _pending_type_target is None:
        if _last_click_target is None:
            return
        _pending_type_target = dict(_last_click_target)
        if _pending_type_target.get("type") in ("", "AXUnknown"):
            _pending_type_target["type"] = "AXTextField"
        _pending_action_type = "type"
        _pending_value = ""
    _pending_value += text


def handle_message(msg):
    global is_recording, events_file, _last_element, _last_element_info
    global _listener_mouse, _listener_keyboard, _pending_type_target, _pending_value, _pending_action_type
    global _last_click_target, _last_click_ts

    rid = msg["id"]
    method = msg["method"]
    params = msg.get("params", {})

    if method == "start_recording":
        output_path = params["output_path"]
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        events_file = open(output_path, "a")
        is_recording = True
        _pending_type_target = None
        _pending_value = ""
        _pending_action_type = ""
        _last_click_target = None
        _last_click_ts = 0
        write_response({"id": rid, "status": "ok", "result": {"pid": os.getpid()}})

        if HAS_PYNPUT:
            _listener_mouse = mouse.Listener(on_click=_on_click)
            _listener_mouse.start()
            _listener_keyboard = keyboard.Listener(on_press=_on_press)
            _listener_keyboard.start()

    elif method == "stop_recording":
        _finalize_pending()

        if _listener_mouse:
            try:
                _listener_mouse.stop()
            except Exception:
                pass
            _listener_mouse = None
        if _listener_keyboard:
            try:
                _listener_keyboard.stop()
            except Exception:
                pass
            _listener_keyboard = None

        is_recording = False
        _pending_type_target = None
        _pending_value = ""
        _pending_action_type = ""
        _last_click_target = None
        _last_click_ts = 0
        if events_file:
            events_file.flush()
            events_file.close()
            events_file = None
        write_response({"id": rid, "status": "ok", "result": {}})

    elif method == "find_element":
        el_type = params.get("type", "")
        label = params.get("label", "")
        matches = _find_elements(el_type, label, limit=1)
        if matches:
            _last_element = matches[0].get("_element")
            _last_element_info = _public_info(matches[0])
            write_response({"id": rid, "status": "ok", "result": _last_element_info})
        else:
            _last_element = None
            _last_element_info = None
            write_response({"id": rid, "status": "ok", "result": {
                "found": False, "label": label, "type": el_type
            }})

    elif method == "find_all_elements":
        el_type = params.get("type", "")
        matches = _find_elements(el_type, limit=100)
        write_response({"id": rid, "status": "ok", "result": {
            "elements": [_public_info(match) for match in matches]
        }})

    elif method == "check_permissions":
        trusted = _is_accessibility_trusted(prompt=params.get("prompt", False))
        write_response({"id": rid, "status": "ok", "result": {
            "accessibility": trusted,
            "input_monitoring": HAS_PYNPUT,
            "ready_for_event_recording": trusted and HAS_PYNPUT,
            "note": (
                "Enable Accessibility and Input Monitoring for the terminal or Codex host process "
                "in System Settings > Privacy & Security if this is false."
            ),
        }})

    elif method == "execute_action":
        action = params.get("action", "")
        info = _last_element_info or {}
        if action == "click":
            clicked = _execute_click(_last_element, info)
            write_response({"id": rid, "status": "ok", "result": {"clicked": clicked}})
        elif action == "type":
            value = params.get("value", "")
            typed = _execute_type(_last_element, info, value)
            write_response({"id": rid, "status": "ok", "result": {"typed": value, "performed": typed}})
        elif action == "select":
            value = params.get("value", "")
            selected = _execute_select(_last_element, info, value)
            write_response({"id": rid, "status": "ok", "result": {"selected": value, "performed": selected}})
        elif action == "click_at":
            x, y = params.get("x", 0), params.get("y", 0)
            clicked = False
            if HAS_PYAUTOGUI:
                try:
                    pyautogui.click(x, y)
                    clicked = True
                except Exception:
                    clicked = False
            write_response({"id": rid, "status": "ok", "result": {"clicked_at": {"x": x, "y": y}, "performed": clicked}})
        else:
            write_response({"id": rid, "status": "error", "error": {
                "code": "INVALID_ACTION", "message": f"Unknown action: {action}"
            }})

    elif method == "take_screenshot":
        output_path = params["output_path"]
        try:
            result = subprocess.run(
                ["screencapture", "-x", output_path],
                capture_output=True, timeout=5
            )
            write_response({"id": rid, "status": "ok", "result": {
                "path": output_path, "width": 1920, "height": 1080
            }})
        except Exception:
            with open(output_path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
            write_response({"id": rid, "status": "ok", "result": {
                "path": output_path, "width": 1920, "height": 1080
            }})

    elif method == "shutdown":
        if _listener_mouse:
            try:
                _listener_mouse.stop()
            except Exception:
                pass
            _listener_mouse = None
        if _listener_keyboard:
            try:
                _listener_keyboard.stop()
            except Exception:
                pass
            _listener_keyboard = None
        if events_file:
            events_file.close()
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        write_response({"id": rid, "status": "ok", "result": {}})
        sys.exit(0)

    else:
        write_response({"id": rid, "status": "error", "error": {
            "code": "INVALID_ACTION", "message": f"Unknown method: {method}"
        }})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            handle_message(msg)
        except json.JSONDecodeError:
            continue
        except SystemExit:
            break


if __name__ == "__main__":
    main()
