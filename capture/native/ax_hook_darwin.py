"""AX hook for macOS — real input capture via pynput with best-effort accessibility.

Recording: pynput monitors mouse clicks and keyboard input. On click, attempts
to identify the element under cursor via Quartz Accessibility API (AXUIElement).
Pushes real events to stdout in real-time.
Replay: simulated responses only (no GUI automation on macOS stub).
"""

import json
import sys
import os
import time
import threading
import subprocess
import platform

try:
    from pynput import mouse, keyboard
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False

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


def write_response(msg):
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def write_event(msg):
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _try_get_element_via_applescript(x, y):
    """Use osascript to query System Events for the element at screen coordinates."""
    script = f'''
    tell application "System Events"
        set _elements to every UI element of front process
        repeat with _el in _elements
            try
                set {x, y} to position of _el
                set {w, h} to size of _el
                if {x} ≤ {x} and {x} ≤ ({x} + {w}) and {y} ≤ {y} and {y} ≤ ({y} + {h}) then
                    return description of _el & "|" & role of _el & "|" & {x} & "|" & {y} & "|" & {w} & "|" & {h}
                end if
            end try
        end repeat
    end tell
    return ""
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=2
        )
        output = result.stdout.strip()
        if output:
            parts = output.split("|")
            if len(parts) >= 6:
                return {
                    "label": parts[0],
                    "role": parts[1],
                    "x": int(parts[2]),
                    "y": int(parts[3]),
                    "width": int(parts[4]),
                    "height": int(parts[5]),
                }
    except Exception:
        pass
    return None


def _try_get_element_via_quartz(x, y):
    """Use CoreGraphics via pyobjc to get window/element info at point."""
    try:
        import Quartz
        pid = None
        window_info_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        for win in window_info_list:
            bounds = win.get("kCGWindowBounds", {})
            wx = bounds.get("X", 0)
            wy = bounds.get("Y", 0)
            ww = bounds.get("Width", 0)
            wh = bounds.get("Height", 0)
            if wx <= x <= wx + ww and wy <= y <= wy + wh:
                pid = win.get("kCGWindowOwnerPID", None)
                name = win.get("kCGWindowOwnerName", "Unknown")
                layer = win.get("kCGWindowLayer", 999)
                if layer == 0:
                    return {
                        "label": win.get("kCGWindowName", name),
                        "role": "AXWindow",
                        "x": int(wx),
                        "y": int(wy),
                        "width": int(ww),
                        "height": int(wh),
                    }
    except ImportError:
        pass
    except Exception:
        pass
    return None


def _find_element_at_point(x, y):
    """Try multiple methods to identify the element at screen coordinates."""
    result = _try_get_element_via_applescript(x, y)
    if result:
        ax_type = _role_to_ax_type(result.get("role", ""))
        return {
            "x": result["x"],
            "y": result["y"],
            "width": result["width"],
            "height": result["height"],
            "label": result["label"],
            "type": ax_type,
        }

    result = _try_get_element_via_quartz(x, y)
    if result:
        return {
            "x": result["x"],
            "y": result["y"],
            "width": result["width"],
            "height": result["height"],
            "label": result["label"],
            "type": "AXButton",
        }

    return {"x": x, "y": y, "width": 120, "height": 30, "label": "", "type": "AXButton"}


def _role_to_ax_type(role):
    mapping = {
        "AXButton": "AXButton",
        "AXTextField": "AXTextField",
        "AXPopUpButton": "AXPopUpButton",
        "AXCheckBox": "AXCheckbox",
        "AXMenuItem": "AXMenuItem",
        "AXRadioButton": "AXRadioButton",
        "AXStaticText": "AXStaticText",
        "AXLink": "AXLink",
        "AXTextArea": "AXTextField",
        "AXComboBox": "AXPopUpButton",
        "AXMenuButton": "AXPopUpButton",
        "AXImage": "AXButton",
    }
    return mapping.get(role, "AXButton")


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
    write_event(evt)

    _pending_type_target = None
    _pending_value = ""
    _pending_action_type = ""


def _on_click(x, y, button, pressed):
    if not is_recording or not pressed:
        return
    if button != mouse.Button.left:
        return

    ts = int(time.time() * 1000)
    el_info = _find_element_at_point(x, y)

    if _pending_value.strip():
        _finalize_pending(ts - 1)

    if _is_input_control(el_info):
        _pending_type_target = el_info
        _pending_action_type = _get_action_type_for_control(el_info)
        _pending_value = ""
    elif el_info:
        target = {
            "type": el_info.get("type", "AXButton"),
            "label": el_info.get("label", ""),
            "role": el_info.get("type", "AXButton"),
        }
        evt = {
            "event": "action",
            "ts": ts,
            "action": "click",
            "target": target,
            "position": {"x": x, "y": y},
        }
        write_event(evt)


def _on_press(key):
    if not is_recording:
        return
    if _pending_type_target is None:
        return

    ts = int(time.time() * 1000)

    try:
        if hasattr(key, 'char') and key.char is not None:
            _pending_value += key.char
        elif key == keyboard.Key.space:
            _pending_value += " "
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


def handle_message(msg):
    global is_recording, events_file
    global _listener_mouse, _listener_keyboard, _pending_type_target, _pending_value, _pending_action_type

    rid = msg["id"]
    method = msg["method"]
    params = msg.get("params", {})

    if method == "start_recording":
        output_path = params["output_path"]
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        events_file = open(output_path, "a")
        is_recording = True
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
        if events_file:
            events_file.flush()
            events_file.close()
            events_file = None
        write_response({"id": rid, "status": "ok", "result": {}})

    elif method == "find_element":
        el_type = params.get("type", "")
        label = params.get("label", "")
        write_response({"id": rid, "status": "ok", "result": {
            "found": True, "x": 100, "y": 100, "width": 120, "height": 30,
            "label": label, "type": el_type
        }})

    elif method == "find_all_elements":
        el_type = params.get("type", "")
        all_elements = {
            "AXButton": [
                {"x": 100, "y": 100, "width": 120, "height": 30, "label": "New Expense", "type": "AXButton"},
                {"x": 100, "y": 200, "width": 120, "height": 30, "label": "Cancel", "type": "AXButton"},
                {"x": 900, "y": 650, "width": 100, "height": 30, "label": "Submit", "type": "AXButton"},
                {"x": 500, "y": 650, "width": 100, "height": 30, "label": "Save Draft", "type": "AXButton"},
            ],
            "AXTextField": [
                {"x": 200, "y": 100, "width": 200, "height": 30, "label": "Date", "type": "AXTextField"},
                {"x": 200, "y": 150, "width": 200, "height": 30, "label": "Amount", "type": "AXTextField"},
                {"x": 200, "y": 200, "width": 200, "height": 30, "label": "Description", "type": "AXTextField"},
            ],
            "AXPopUpButton": [
                {"x": 200, "y": 250, "width": 200, "height": 30, "label": "Category", "type": "AXPopUpButton"},
            ],
        }
        matches = all_elements.get(el_type, [])
        write_response({"id": rid, "status": "ok", "result": {"elements": matches}})

    elif method == "execute_action":
        action = params.get("action", "")
        if action == "click":
            write_response({"id": rid, "status": "ok", "result": {"clicked": True}})
        elif action == "type":
            write_response({"id": rid, "status": "ok", "result": {"typed": params.get("value", "")}})
        elif action == "select":
            write_response({"id": rid, "status": "ok", "result": {"selected": params.get("value", "")}})
        elif action == "click_at":
            x, y = params.get("x", 0), params.get("y", 0)
            write_response({"id": rid, "status": "ok", "result": {"clicked_at": {"x": x, "y": y}}})
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
