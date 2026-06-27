"""AX hook for Windows — real UI Automation with TCP-based IPC + input polling.

Uses GetAsyncKeyState polling + UIAutomation for recording.
Uses UIAutomation + pyautogui for replay.
TCP socket IPC avoids pipe redirection issues that block input detection.
"""

import json
import sys
import os
import time
import ctypes
import socket
import threading

import uiautomation as auto

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.3
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False

PID_FILE = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "ax_hook.pid")
with open(PID_FILE, "w") as f:
    f.write(str(os.getpid()))

events_file = None
is_recording = False
_last_element = None
_client_conn = None
_client_lock = threading.Lock()

_pending_ctrl = None
_pending_type = ""
_initial_value = ""
_poll_thread = None
_poll_stop = threading.Event()

_keyboard_buffer = []
_keyboard_thread = None
_keyboard_stop = threading.Event()
_last_click_pos = None
_last_click_ts = 0
_last_click_target_info = None

VK_LBUTTON = 0x01
VK_TAB = 0x09
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_CAPITAL = 0x14
VK_RETURN = 0x0D
VK_BACK = 0x08
VK_SPACE = 0x20
VK_V = 0x56

_UNSHIFTED_CHARS = {
    0x30: '0', 0x31: '1', 0x32: '2', 0x33: '3', 0x34: '4',
    0x35: '5', 0x36: '6', 0x37: '7', 0x38: '8', 0x39: '9',
    0x41: 'a', 0x42: 'b', 0x43: 'c', 0x44: 'd', 0x45: 'e',
    0x46: 'f', 0x47: 'g', 0x48: 'h', 0x49: 'i', 0x4A: 'j',
    0x4B: 'k', 0x4C: 'l', 0x4D: 'm', 0x4E: 'n', 0x4F: 'o',
    0x50: 'p', 0x51: 'q', 0x52: 'r', 0x53: 's', 0x54: 't',
    0x55: 'u', 0x56: 'v', 0x57: 'w', 0x58: 'x', 0x59: 'y', 0x5A: 'z',
    0xBA: ';', 0xBB: '=', 0xBC: ',', 0xBD: '-', 0xBE: '.', 0xBF: '/',
    0xC0: '`', 0xDB: '[', 0xDC: '\\', 0xDD: ']', 0xDE: "'",
    0x6A: '*', 0x6B: '+', 0x6D: '-', 0x6E: '.', 0x6F: '/',
}

_SHIFTED_CHARS = {
    0x30: ')', 0x31: '!', 0x32: '@', 0x33: '#', 0x34: '$',
    0x35: '%', 0x36: '^', 0x37: '&', 0x38: '*', 0x39: '(',
    0x41: 'A', 0x42: 'B', 0x43: 'C', 0x44: 'D', 0x45: 'E',
    0x46: 'F', 0x47: 'G', 0x48: 'H', 0x49: 'I', 0x4A: 'J',
    0x4B: 'K', 0x4C: 'L', 0x4D: 'M', 0x4E: 'N', 0x4F: 'O',
    0x50: 'P', 0x51: 'Q', 0x52: 'R', 0x53: 'S', 0x54: 'T',
    0x55: 'U', 0x56: 'V', 0x57: 'W', 0x58: 'X', 0x59: 'Y', 0x5A: 'Z',
    0xBA: ':', 0xBB: '+', 0xBC: '<', 0xBD: '_', 0xBE: '>', 0xBF: '?',
    0xC0: '~', 0xDB: '{', 0xDC: '|', 0xDD: '}', 0xDE: '"',
}
user32 = ctypes.windll.user32

TYPE_MAP = {
    "AXButton": auto.ButtonControl,
    "AXTextField": auto.EditControl,
    "AXPopUpButton": auto.ComboBoxControl,
    "AXCheckbox": auto.CheckBoxControl,
    "AXMenuItem": auto.MenuItemControl,
    "AXRadioButton": auto.RadioButtonControl,
    "AXStaticText": auto.TextControl,
    "AXLink": auto.HyperlinkControl,
}
TYPE_MAP_REVERSE = {v: k for k, v in TYPE_MAP.items()}


def _send(msg: dict):
    global _client_conn
    with _client_lock:
        if _client_conn:
            try:
                data = (json.dumps(msg) + "\n").encode("utf-8")
                _client_conn.sendall(data)
            except Exception:
                pass


def write_response(msg):
    _send(msg)


def write_event(msg):
    _send(msg)


def _find_control(el_type, label):
    global _last_element
    _last_element = None
    uia_type = TYPE_MAP.get(el_type)
    if not uia_type:
        return None
    label_lower = label.lower()
    best = None
    best_score = 0

    def _check(ctrl):
        nonlocal best, best_score
        if _matches_type(ctrl, uia_type):
            try:
                name = (ctrl.Name or "").lower()
            except Exception:
                return
            if label_lower in name or name in label_lower:
                score = len(label_lower) / max(len(name), 1)
                if score > best_score:
                    best_score = score
                    best = ctrl

    def walk(ctrl, depth):
        if depth > 4 or best_score >= 0.95:
            return
        try:
            _check(ctrl)
            if depth < 3:
                for child in ctrl.GetChildren():
                    walk(child, depth + 1)
        except Exception:
            pass

    try:
        fg = auto.GetForegroundControl()
        if fg:
            walk(fg, 0)
        if not best:
            for wnd in auto.GetRootControl().GetChildren():
                walk(wnd, 0)
                if best:
                    break
    except Exception:
        pass

    if best:
        _last_element = best
        return best
    return None


def _matches_type(ctrl, uia_type):
    if isinstance(ctrl, uia_type):
        return True
    for base in type(ctrl).__mro__:
        if base is uia_type:
            return True
    return False


def _find_all_controls(el_type):
    uia_type = TYPE_MAP.get(el_type)
    if not uia_type:
        return []
    results = []

    def walk(ctrl, depth):
        if depth > 4 or len(results) >= 100:
            return
        try:
            if _matches_type(ctrl, uia_type):
                results.append(ctrl)
            if len(results) < 100:
                for child in ctrl.GetChildren():
                    walk(child, depth + 1)
        except Exception:
            pass

    try:
        fg = auto.GetForegroundControl()
        if fg:
            walk(fg, 0)
    except Exception:
        pass
    return results


def _control_to_result(ctrl):
    try:
        rect = ctrl.BoundingRectangle
    except Exception:
        return {"x": 500, "y": 400, "width": 120, "height": 30}
    return {
        "x": rect.left, "y": rect.top, "width": rect.width(), "height": rect.height(),
        "label": ctrl.Name or "", "type": TYPE_MAP_REVERSE.get(type(ctrl), "AXButton"),
    }


def _get_control_value(ctrl):
    try:
        if hasattr(ctrl, "GetValuePattern") and ctrl.GetValuePattern():
            return ctrl.GetValuePattern().Value or ""
    except Exception:
        pass
    try:
        return ctrl.Name or ""
    except Exception:
        pass
    return ""


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _find_nearby_label(ctrl, max_distance=80):
    try:
        cr = ctrl.BoundingRectangle
        cx = cr.left + cr.width() / 2
        cy = cr.top + cr.height() / 2
    except Exception:
        return ""
    try:
        parent = ctrl.GetParentControl()
    except Exception:
        return ""
    if not parent:
        return ""
    try:
        children = parent.GetChildren()
    except Exception:
        return ""
    best_text = ""
    best_dist = float("inf")
    for child in children:
        if child is ctrl:
            continue
        if not isinstance(child, auto.TextControl):
            continue
        try:
            tr = child.BoundingRectangle
            child_text = child.Name or ""
        except Exception:
            continue
        if not child_text.strip():
            continue
        tx = tr.left + tr.width() / 2
        ty = tr.top + tr.height() / 2
        dist = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
        if dist <= max_distance and dist < best_dist:
            best_dist = dist
            best_text = child_text.strip()
    return best_text


def _get_foreground_window_title():
    try:
        fg = auto.GetForegroundControl()
        if fg:
            title = fg.Name or ""
            if title:
                return title
    except Exception:
        pass
    try:
        root = auto.GetRootControl()
        if root:
            title = root.Name or ""
            if title:
                return title
    except Exception:
        pass
    try:
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                return buf.value or ""
    except Exception:
        pass
    return ""


def _find_element_at_point(x, y):
    try:
        ctrl = auto.ControlFromPoint(x, y)
        if ctrl is None:
            return {"x": x, "y": y, "width": 120, "height": 30, "label": "", "type": "AXButton", "control": None, "automation_id": "", "class_name": "", "help_text": "", "foreground_window": _get_foreground_window_title()}
        ax_type = TYPE_MAP_REVERSE.get(type(ctrl))
        if ax_type is None:
            for cls, ax_name in TYPE_MAP_REVERSE.items():
                if isinstance(ctrl, cls):
                    ax_type = ax_name
                    break
        automation_id = ""
        try:
            automation_id = ctrl.AutomationId or ""
        except Exception:
            pass
        class_name = ""
        try:
            class_name = ctrl.ClassName or ""
        except Exception:
            pass
        help_text = ""
        try:
            help_text = ctrl.HelpText or ""
        except Exception:
            pass
        label = ""
        try:
            label = ctrl.Name or ""
        except Exception:
            pass
        if not label:
            label = automation_id
        if not label:
            label = class_name
        if not label:
            label = help_text
        if not label:
            try:
                parent = ctrl.GetParentControl()
                if parent:
                    label = parent.Name or ""
            except Exception:
                pass
        if not label:
            label = _find_nearby_label(ctrl)
        foreground_window = _get_foreground_window_title()
        try:
            rect = ctrl.BoundingRectangle
        except Exception:
            return {"x": x, "y": y, "width": 120, "height": 30, "label": label, "type": ax_type, "control": ctrl, "automation_id": automation_id, "class_name": class_name, "help_text": help_text, "foreground_window": foreground_window}
        return {"x": rect.left, "y": rect.top, "width": rect.width(), "height": rect.height(),
                "label": label, "type": ax_type or "AXButton", "control": ctrl,
                "automation_id": automation_id, "class_name": class_name, "help_text": help_text,
                "foreground_window": foreground_window}
    except Exception:
        return {"x": x, "y": y, "width": 120, "height": 30, "label": "", "type": "AXButton", "control": None, "automation_id": "", "class_name": "", "help_text": "", "foreground_window": ""}


def _is_input_ctrl(ctrl):
    if ctrl is None:
        return False
    return isinstance(ctrl, auto.EditControl) or isinstance(ctrl, auto.ComboBoxControl)


def _mouse_pressed():
    return (user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000) != 0


def _finalize_pending(ts_ms=None):
    global _pending_ctrl, _pending_type, _initial_value
    if _pending_ctrl is None:
        return
    ts = ts_ms or int(time.time() * 1000)
    try:
        current = _get_control_value(_pending_ctrl)
    except Exception:
        current = ""
    val = current.strip()
    if val and val != _initial_value.strip():
        label = ""
        try:
            label = _pending_ctrl.Name or ""
        except Exception:
            pass
        ax_type = TYPE_MAP_REVERSE.get(type(_pending_ctrl), "AXTextField")
        automation_id = ""
        try:
            automation_id = _pending_ctrl.AutomationId or ""
        except Exception:
            pass
        class_name = ""
        try:
            class_name = _pending_ctrl.ClassName or ""
        except Exception:
            pass
        help_text = ""
        try:
            help_text = _pending_ctrl.HelpText or ""
        except Exception:
            pass
        target = {
            "type": ax_type,
            "label": label,
            "role": ax_type,
            "automation_id": automation_id,
            "class_name": class_name,
            "help_text": help_text,
            "foreground_window": _get_foreground_window_title(),
        }
        write_event({"event": "action", "ts": ts, "action": _pending_type, "target": target, "value": val})
    _pending_ctrl = None
    _pending_type = ""
    _initial_value = ""


def _on_click(x, y, ts):
    global _pending_ctrl, _pending_type, _initial_value
    global _last_click_pos, _last_click_ts, _last_click_target_info

    buffer_text = "".join(_keyboard_buffer)

    if _pending_ctrl is not None:
        _finalize_pending(ts - 1)
    elif buffer_text.strip() and _last_click_ts > 0:
        _emit_type_for_last_click(buffer_text, ts - 1)

    _keyboard_buffer.clear()

    el_info = _find_element_at_point(x, y)
    ctrl = el_info.get("control")
    label = el_info.get("label", "")
    el_type = el_info.get("type", "AXButton")

    _last_click_pos = (x, y)
    _last_click_ts = ts
    _last_click_target_info = el_info

    if _is_input_ctrl(ctrl):
        _pending_ctrl = ctrl
        if isinstance(ctrl, auto.EditControl):
            _pending_type = "type"
        elif isinstance(ctrl, auto.ComboBoxControl):
            _pending_type = "select"
        else:
            _pending_type = "type"
        try:
            _initial_value = _get_control_value(ctrl)
        except Exception:
            _initial_value = ""
    else:
        target = {
            "type": el_type,
            "label": label if label else f"element_at_{x}_{y}",
            "role": el_type,
            "automation_id": el_info.get("automation_id", ""),
            "class_name": el_info.get("class_name", ""),
            "help_text": el_info.get("help_text", ""),
            "foreground_window": el_info.get("foreground_window", ""),
        }
        evt = {
            "event": "action",
            "ts": ts,
            "action": "click",
            "target": target,
            "position": {"x": x, "y": y},
        }
        write_event(evt)


def _key_pressed(vk):
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


def _get_clipboard_text():
    try:
        import win32clipboard
    except ImportError:
        return ""
    try:
        win32clipboard.OpenClipboard()
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_TEXT):
            data = win32clipboard.GetClipboardData(win32clipboard.CF_TEXT)
            return data.decode("utf-8", errors="replace").strip()
    except Exception:
        pass
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
    return ""


def _keyboard_loop():
    global _keyboard_buffer
    prev = {}
    while not _keyboard_stop.is_set():
        time.sleep(0.03)
        if not is_recording:
            prev.clear()
            continue
        shift = _key_pressed(VK_SHIFT)
        caps = (user32.GetKeyState(VK_CAPITAL) & 0x0001) != 0
        ctrl = _key_pressed(VK_CONTROL)
        for vk, ch in _UNSHIFTED_CHARS.items():
            down = _key_pressed(vk)
            was = prev.get(vk, False)
            prev[vk] = down
            if down and not was:
                if ctrl:
                    continue
                if vk >= 0x41 and vk <= 0x5A:
                    _keyboard_buffer.append(ch.upper() if (shift or caps) else ch)
                elif shift:
                    _keyboard_buffer.append(_SHIFTED_CHARS.get(vk, ch))
                else:
                    _keyboard_buffer.append(ch)
        bk = _key_pressed(VK_BACK)
        if bk and not prev.get("_bk", False):
            if _keyboard_buffer:
                _keyboard_buffer.pop()
        prev["_bk"] = bk
        enter = _key_pressed(VK_RETURN)
        tab = _key_pressed(VK_TAB)
        enter_edge = enter and not prev.get("_ent", False)
        tab_edge = tab and not prev.get("_tab", False)
        if enter_edge or tab_edge:
            ts = int(time.time() * 1000)
            buffer_text = "".join(_keyboard_buffer)
            if _pending_ctrl is not None:
                _finalize_pending(ts)
            elif buffer_text.strip():
                _emit_type_for_last_click(buffer_text, ts)
            _keyboard_buffer.clear()
        prev["_ent"] = enter
        prev["_tab"] = tab
        if ctrl and _key_pressed(VK_V) and not prev.get("_cv", False):
            clip = _get_clipboard_text()
            if clip:
                _keyboard_buffer.extend(list(clip))
        prev["_cv"] = _key_pressed(VK_V) if ctrl else False


def _emit_type_for_last_click(text, ts):
    global _last_click_target_info
    if not _last_click_pos or not text.strip():
        return
    x, y = _last_click_pos
    info = _last_click_target_info or {}
    target = {
        "type": "AXTextField",
        "label": info.get("label") or f"element_at_{x}_{y}",
        "role": "AXTextField",
        "automation_id": info.get("automation_id", ""),
        "class_name": info.get("class_name", ""),
        "help_text": info.get("help_text", ""),
        "foreground_window": info.get("foreground_window", ""),
    }
    write_event({"event": "action", "ts": ts, "action": "type", "target": target, "value": text})


def _mouse_loop():
    was_pressed = False
    while not _poll_stop.is_set():
        time.sleep(0.05)
        if not is_recording:
            continue
        is_pressed = _mouse_pressed()
        if is_pressed and not was_pressed:
            now = int(time.time() * 1000)
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            try:
                _on_click(pt.x, pt.y, now)
            except Exception:
                pass
        was_pressed = is_pressed


def _start_input_listeners():
    global _poll_thread, _poll_stop, _keyboard_thread, _keyboard_stop
    _poll_stop.clear()
    _keyboard_stop.clear()
    _poll_thread = threading.Thread(target=_mouse_loop, daemon=True)
    _poll_thread.start()
    _keyboard_thread = threading.Thread(target=_keyboard_loop, daemon=True)
    _keyboard_thread.start()


def _stop_input_listeners():
    global _poll_thread, _pending_ctrl, _pending_type, _initial_value
    global _keyboard_thread, _last_click_pos, _last_click_ts, _last_click_target_info

    _finalize_pending()
    buffer_text = "".join(_keyboard_buffer)
    if buffer_text.strip() and _last_click_ts > 0:
        _emit_type_for_last_click(buffer_text, int(time.time() * 1000) - 1)
    _keyboard_buffer.clear()

    if _keyboard_thread:
        _keyboard_stop.set()
        _keyboard_thread.join(timeout=2)
        _keyboard_thread = None
    if _poll_thread:
        _poll_stop.set()
        _poll_thread.join(timeout=2)
        _poll_thread = None
    _pending_ctrl = None
    _pending_type = ""
    _initial_value = ""
    _last_click_pos = None
    _last_click_ts = 0
    _last_click_target_info = None


def handle_message(msg):
    global is_recording, events_file, _last_element
    global _pending_ctrl, _pending_type, _initial_value

    rid = msg["id"]
    method = msg["method"]
    params = msg.get("params", {})

    if method == "start_recording":
        output_path = params["output_path"]
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        events_file = open(output_path, "a")
        is_recording = True
        write_response({"id": rid, "status": "ok", "result": {"pid": os.getpid()}})
        _start_input_listeners()

    elif method == "stop_recording":
        _stop_input_listeners()
        is_recording = False
        if events_file:
            events_file.flush()
            events_file.close()
            events_file = None
        write_response({"id": rid, "status": "ok", "result": {}})

    elif method == "find_element":
        el_type = params.get("type", "")
        label = params.get("label", "")
        ctrl = _find_control(el_type, label)
        if ctrl:
            result = _control_to_result(ctrl)
            result["found"] = True
        else:
            result = {"found": False, "label": label, "type": el_type}
        write_response({"id": rid, "status": "ok", "result": result})

    elif method == "find_all_elements":
        el_type = params.get("type", "")
        controls = _find_all_controls(el_type)
        elements = [_control_to_result(c) for c in controls] if controls else []
        write_response({"id": rid, "status": "ok", "result": {"elements": elements}})

    elif method == "execute_action":
        action = params.get("action", "")
        if action == "click":
            if _last_element and HAS_PYAUTOGUI:
                try:
                    r = _control_to_result(_last_element)
                    pyautogui.click(r["x"] + r["width"] // 2, r["y"] + r["height"] // 2)
                except Exception:
                    pass
            write_response({"id": rid, "status": "ok", "result": {"clicked": True}})
        elif action == "type":
            value = params.get("value", "")
            if _last_element and HAS_PYAUTOGUI:
                try:
                    r = _control_to_result(_last_element)
                    pyautogui.click(r["x"] + r["width"] // 2, r["y"] + r["height"] // 2)
                    time.sleep(0.3)
                except Exception:
                    pass
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.write(str(value))
            write_response({"id": rid, "status": "ok", "result": {"typed": value}})
        elif action == "select":
            value = params.get("value", "")
            if _last_element and HAS_PYAUTOGUI:
                try:
                    r = _control_to_result(_last_element)
                    pyautogui.click(r["x"] + r["width"] // 2, r["y"] + r["height"] // 2)
                except Exception:
                    pass
                time.sleep(0.5)
                pyautogui.write(str(value))
                time.sleep(0.2)
                pyautogui.press("enter")
            write_response({"id": rid, "status": "ok", "result": {"selected": value}})
        elif action == "click_at":
            x = params.get("x", 0)
            y = params.get("y", 0)
            if HAS_PYAUTOGUI:
                pyautogui.click(x, y)
            write_response({"id": rid, "status": "ok", "result": {"clicked_at": {"x": x, "y": y}}})
        else:
            write_response({"id": rid, "status": "error", "error": {"code": "INVALID_ACTION", "message": f"Unknown action: {action}"}})

    elif method == "take_screenshot":
        output_path = params["output_path"]
        if HAS_PYAUTOGUI:
            img = pyautogui.screenshot()
            img.save(output_path)
            write_response({"id": rid, "status": "ok", "result": {"path": output_path, "width": img.width, "height": img.height}})
        else:
            with open(output_path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
            write_response({"id": rid, "status": "ok", "result": {"path": output_path, "width": 1920, "height": 1080}})

    elif method == "shutdown":
        _stop_input_listeners()
        if events_file:
            events_file.close()
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        write_response({"id": rid, "status": "ok", "result": {}})
        sys.exit(0)

    else:
        write_response({"id": rid, "status": "error", "error": {"code": "INVALID_ACTION", "message": f"Unknown method: {method}"}})


def _write_port_file(port):
    port_path = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "ax_hook_port.txt")
    with open(port_path, "w") as f:
        f.write(str(port))
    return port_path


def _run_tcp_server():
    global _client_conn
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    _write_port_file(port)
    server.listen(1)
    server.settimeout(30)

    try:
        conn, addr = server.accept()
        _client_conn = conn
        conn.settimeout(600)
        buffer = b""
        while True:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except Exception:
                break
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                    handle_message(msg)
                except json.JSONDecodeError:
                    continue
                except SystemExit:
                    return
    finally:
        _client_conn = None
        try:
            server.close()
        except Exception:
            pass
        try:
            os.remove(port_file)
        except Exception:
            pass


def main():
    _run_tcp_server()


if __name__ == "__main__":
    main()
