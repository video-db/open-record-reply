"""AX hook for Linux — real input capture via pynput with best-effort AT-SPI.

Recording: pynput monitors mouse clicks and keyboard input. On click, attempts
to identify the element under cursor via AT-SPI (pyatspi). Falls back to
coordinate-based events if accessibility unavailable.
Replay: simulated responses only (no GUI automation on Linux stub).
"""

import json
import sys
import os
import time
import threading
import subprocess

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


def _try_get_element_via_atspi(x, y):
    """Use AT-SPI via pyatspi or accerciser to find element at coordinates."""
    try:
        import pyatspi
        desktop = pyatspi.Registry.getDesktop(0)
        for app in desktop:
            try:
                for child in app:
                    result = _walk_atspi_tree(child, x, y, depth=0)
                    if result:
                        return result
            except Exception:
                continue
    except ImportError:
        pass
    except Exception:
        pass

    try:
        script = '''
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
import sys

desktop = Atspi.get_desktop(0)
def walk(obj, depth=0):
    if depth > 6:
        return
    try:
        extents = obj.get_extents(Atspi.CoordType.SCREEN)
        if extents.x <= %d <= extents.x + extents.width and extents.y <= %d <= extents.y + extents.height:
            try:
                name = obj.get_name() or ""
            except:
                name = ""
            try:
                role = obj.get_role_name() or ""
            except:
                role = "unknown"
            print(f"{name}|{role}|{extents.x}|{extents.y}|{extents.width}|{extents.height}")
            sys.exit(0)
    except:
        pass
    try:
        for i in range(obj.get_child_count()):
            child = obj.get_child_at_index(i)
            walk(child, depth + 1)
    except:
        pass

for app in [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]:
    try:
        walk(app)
    except:
        continue
''' % (x, y)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=3
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


def _walk_atspi_tree(obj, x, y, depth):
    if depth > 6:
        return None
    try:
        ext = obj.queryComponent().getExtents(0)
        if ext.x <= x <= ext.x + ext.width and ext.y <= y <= ext.y + ext.height:
            try:
                name = obj.name or ""
            except Exception:
                name = ""
            try:
                role = obj.getRoleName() or ""
            except Exception:
                role = "unknown"
            return {
                "label": name,
                "role": role,
                "x": ext.x,
                "y": ext.y,
                "width": ext.width,
                "height": ext.height,
            }
    except Exception:
        pass
    try:
        for i in range(obj.childCount):
            result = _walk_atspi_tree(obj.getChildAtIndex(i), x, y, depth + 1)
            if result:
                return result
    except Exception:
        pass
    return None


def _find_element_at_point(x, y):
    result = _try_get_element_via_atspi(x, y)
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
    return {"x": x, "y": y, "width": 120, "height": 30, "label": "", "type": "AXButton"}


def _role_to_ax_type(role):
    mapping = {
        "push button": "AXButton",
        "button": "AXButton",
        "text": "AXTextField",
        "entry": "AXTextField",
        "combo box": "AXPopUpButton",
        "check box": "AXCheckbox",
        "menu item": "AXMenuItem",
        "radio button": "AXRadioButton",
        "label": "AXStaticText",
        "link": "AXLink",
        "password text": "AXTextField",
        "toggle button": "AXButton",
    }
    role_lower = role.lower()
    for key, val in mapping.items():
        if key in role_lower:
            return val
    return "AXButton"


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
        write_response({"id": rid, "status": "ok", "result": {"elements": []}})

    elif method == "execute_action":
        action = params.get("action", "")
        write_response({"id": rid, "status": "ok", "result": {action: True}})

    elif method == "take_screenshot":
        output_path = params["output_path"]
        try:
            result = subprocess.run(
                ["import", "-window", "root", output_path],
                capture_output=True, timeout=5
            )
            write_response({"id": rid, "status": "ok", "result": {
                "path": output_path, "width": 1920, "height": 1080
            }})
        except Exception:
            try:
                result = subprocess.run(
                    ["gnome-screenshot", "-f", output_path],
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
