"""Dual recording orchestrator: AX companion + optional VideoDB Capture SDK."""

import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

from capture.capture_client import CaptureClient

from config import (
    BASE_URL,
    CAPTURE_ACTIVE_TIMEOUT_SECONDS,
    EXPORT_TIMEOUT_SECONDS,
    EXPORT_POLL_INTERVAL_SECONDS,
    SESSIONS_ROOT,
)
from state import state
from capture.ax_client import AxClient, TcpAxClient

logger = logging.getLogger(__name__)


def _get_ax_binary_path() -> str:
    platform_map = {
        "darwin": "ax_hook_darwin.py",
        "win32": "ax_hook_win32.py",
        "linux": "ax_hook_linux.py",
    }
    binary_name = platform_map.get(sys.platform)
    if not binary_name:
        raise RuntimeError(
            f"AX hook not supported on {sys.platform}. "
            f"Supported: {', '.join(platform_map.keys())}"
        )
    native_dir = Path(__file__).parent / "native"
    candidate = native_dir / binary_name
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError(
        f"AX binary '{binary_name}' not found. Place it in {native_dir}"
    )


_capture_available = None


async def _check_capture_available() -> bool:
    global _capture_available
    if _capture_available is not None:
        return _capture_available
    try:
        kwargs = {"client_token": "test"}
        if BASE_URL:
            kwargs["base_url"] = BASE_URL
        client = CaptureClient(**kwargs)
        _capture_available = True
        await client.shutdown()
    except Exception as e:
        logger.warning(f"VideoDB capture unavailable: {e}")
        _capture_available = False
    return _capture_available


def enable_capture():
    global _capture_available
    _capture_available = True


async def record_skill(name: str, lead_in_seconds: float = 0.0) -> dict:
    if state.is_recording:
        raise RuntimeError("Already recording. Call stop_recording() first.")

    lead_in_seconds = max(0.0, float(lead_in_seconds or 0.0))
    state.session_dir = str(SESSIONS_ROOT / f"{int(time.time())}_{name}")
    Path(state.session_dir).mkdir(parents=True, exist_ok=True)
    state.recording_skill_name = name
    state.events_path = str(Path(state.session_dir) / "events.jsonl")
    state.recording_start_epoch_ms = int(time.time() * 1000)
    state.effective_recording_start_epoch_ms = state.recording_start_epoch_ms
    state.effective_recording_end_epoch_ms = 0

    binary = _get_ax_binary_path()
    use_tcp = sys.platform == "win32"

    if use_tcp:
        port_file = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "ax_hook_port.txt")
        if os.path.exists(port_file):
            os.remove(port_file)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        state.ax_subproc = subprocess.Popen(
            [sys.executable, os.path.abspath(binary)],
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        state.ax_client = TcpAxClient(state.ax_subproc, port_file)
        await state.ax_client.start(event_handler=_handle_ax_event)
    else:
        state.ax_proc = await asyncio.create_subprocess_exec(
            sys.executable, binary,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        state.ax_client = AxClient(state.ax_proc)
        await state.ax_client.start(event_handler=_handle_ax_event)

    ax_permission_warning = None
    if sys.platform == "darwin":
        permission_response = await state.ax_client.send("check_permissions", {"prompt": True})
        permission_result = permission_response.get("result", {})
        if permission_response.get("status") == "ok" and not permission_result.get("ready_for_event_recording", False):
            ax_permission_warning = permission_result.get(
                "note",
                "macOS Accessibility/Input Monitoring permissions are not ready.",
            )

    response = await state.ax_client.send("start_recording", {
        "output_path": state.events_path,
    })
    if response.get("status") != "ok":
        raise RuntimeError(f"AX hook failed to start: {response}")

    capture_ok = await _check_capture_available()
    if not capture_ok:
        logger.warning("Capture SDK unavailable — recording events only (no video)")
        state.is_recording = True
        state.effective_recording_start_epoch_ms = int((time.time() + lead_in_seconds) * 1000)
        state.capture_client = None
        return {
            "status": "recording",
            "mode": "events_only",
            "session_dir": state.session_dir,
            "warning": "VideoDB capture unavailable. Events being recorded, but no video will be saved.",
            "ax_permission_warning": ax_permission_warning,
            "lead_in_seconds": lead_in_seconds,
            "workflow_starts_at_epoch_ms": state.effective_recording_start_epoch_ms,
        }

    try:
        token = state.conn.generate_client_token()
        state.capture_session = state.conn.create_capture_session(
            end_user_id="mcp-user",
            collection_id=state.coll.id,
            ws_connection_id=state.ws_connection_id,
            metadata={
                "skill_name": name,
                "started_at_ms": state.recording_start_epoch_ms,
            },
        )

        state.capture_client = CaptureClient(
            client_token=token,
            base_url=BASE_URL if BASE_URL else None,
        )

        try:
            await asyncio.wait_for(
                state.capture_client.request_permission("microphone"),
                timeout=CAPTURE_ACTIVE_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.warning(f"Microphone permission unavailable ({e}) - continuing without mic")

        await asyncio.wait_for(
            state.capture_client.request_permission("screen_capture"),
            timeout=CAPTURE_ACTIVE_TIMEOUT_SECONDS,
        )

        channels = await state.capture_client.list_channels()
        display = channels.displays.default
        if not display:
            raise RuntimeError("No display available")

        display.is_primary = True
        display.store = True
        selected = [display]
        for extra in [channels.mics.default, channels.system_audio.default]:
            if extra:
                extra.store = True
                selected.append(extra)

        await state.capture_client.start_session(
            capture_session_id=state.capture_session.id,
            channels=selected,
        )

        deadline = time.time() + CAPTURE_ACTIVE_TIMEOUT_SECONDS
        try:
            async for event in state.capture_client.events():
                if event.get("event") == "recording-started":
                    state.is_recording = True
                    state.effective_recording_start_epoch_ms = int(
                        (time.time() + lead_in_seconds) * 1000
                    )
                    return {
                        "status": "recording",
                        "mode": "full",
                        "session_dir": state.session_dir,
                        "ax_permission_warning": ax_permission_warning,
                        "lead_in_seconds": lead_in_seconds,
                        "workflow_starts_at_epoch_ms": state.effective_recording_start_epoch_ms,
                    }
                if event.get("event") == "recording-complete":
                    await _abort_capture()
                    raise RuntimeError("Recording ended unexpectedly")
                if time.time() > deadline:
                    break
        except Exception:
            pass

        await _abort_capture()
        raise RuntimeError("Capture session activation timed out")

    except (asyncio.TimeoutError, RuntimeError) as e:
        logger.warning(f"Capture failed ({e}), falling back to events-only recording")
        await _abort_capture()
        state.is_recording = True
        state.effective_recording_start_epoch_ms = int((time.time() + lead_in_seconds) * 1000)
        return {
            "status": "recording",
            "mode": "events_only",
            "session_dir": state.session_dir,
            "warning": f"Capture failed: {e}. Events being recorded, but no video.",
            "ax_permission_warning": ax_permission_warning,
            "lead_in_seconds": lead_in_seconds,
            "workflow_starts_at_epoch_ms": state.effective_recording_start_epoch_ms,
        }
    except Exception as e:
        await _abort_capture()
        raise RuntimeError(f"Recording failed: {e}")


async def stop_recording(trim_end_seconds: float = 0.0) -> dict:
    if not state.is_recording:
        raise RuntimeError("Not recording. Call record_skill() first.")
    trim_end_seconds = max(0.0, float(trim_end_seconds or 0.0))

    await state.ax_client.send("stop_recording", {})
    video_id = None

    if state.capture_client:
        try:
            await asyncio.wait_for(state.capture_client.stop_session(), timeout=30.0)
        except Exception as e:
            logger.error(f"Capture stop failed: {e}")

        video_id = await _poll_export(
            state.capture_session.id,
            state.coll.id,
            timeout=EXPORT_TIMEOUT_SECONDS,
        )

        try:
            await asyncio.wait_for(state.capture_client.shutdown(), timeout=10.0)
        except Exception as e:
            logger.warning(f"Capture shutdown error: {e}")

        if not video_id:
            logger.warning("Export timed out or failed")

    await state.ax_client.shutdown()

    recording_end_ms = int(time.time() * 1000)
    effective_end_ms = max(
        state.effective_recording_start_epoch_ms or state.recording_start_epoch_ms,
        recording_end_ms - int(trim_end_seconds * 1000),
    )
    state.effective_recording_end_epoch_ms = effective_end_ms
    duration = (recording_end_ms - state.recording_start_epoch_ms) / 1000.0

    event_count = 0
    error_count = 0
    if Path(state.events_path).exists():
        with open(state.events_path) as f:
            for line in f:
                if line.strip():
                    try:
                        evt = json.loads(line)
                        if evt.get("event") == "action":
                            event_count += 1
                        elif evt.get("event") == "error":
                            error_count += 1
                    except json.JSONDecodeError:
                        pass

    metadata = {
        "skill_name": state.recording_skill_name,
        "video_id": video_id,
        "recording_start_epoch_ms": state.recording_start_epoch_ms,
        "effective_recording_start_epoch_ms": state.effective_recording_start_epoch_ms or state.recording_start_epoch_ms,
        "effective_recording_end_epoch_ms": state.effective_recording_end_epoch_ms or recording_end_ms,
        "lead_in_seconds": max(
            0.0,
            ((state.effective_recording_start_epoch_ms or state.recording_start_epoch_ms) - state.recording_start_epoch_ms) / 1000.0,
        ),
        "trim_end_seconds": trim_end_seconds,
        "recording_end_epoch_ms": recording_end_ms,
        "duration_seconds": duration,
        "platform": platform.system().lower(),
        "event_count": event_count,
        "error_count": error_count,
    }
    (Path(state.session_dir) / "metadata.json").write_text(
        json.dumps(metadata, indent=2)
    )

    state.is_recording = False
    state.capture_client = None
    state.capture_session = None

    return {
        "events_path": state.events_path,
        "video_id": video_id,
        "duration_seconds": duration,
        "event_count": event_count,
        "has_video": video_id is not None,
    }


async def _handle_ax_event(event: dict):
    with open(state.events_path, "a") as f:
        f.write(json.dumps(event) + "\n")


async def _poll_export(session_id: str, collection_id: str, timeout: int = 120) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cs = state.conn.get_capture_session(session_id, collection_id)
            vid = getattr(cs, "exported_video_id", None)
            if vid:
                logger.info(f"Export already done: {vid}")
                return vid
        except Exception as e:
            logger.warning(f"Export poll error: {e}")
        try:
            cs = state.conn.get_capture_session(session_id, collection_id)
            result = cs.export()
            vid = result.get("video_id") or result.get("data", {}).get("video_id", "")
            if vid:
                logger.info(f"Export triggered: {vid}")
                return vid
        except Exception as e:
            message = str(e)
            logger.warning(f"Export trigger error: {e}")
            if "failed" in message.lower() or "cannot export session" in message.lower():
                return None
        await asyncio.sleep(EXPORT_POLL_INTERVAL_SECONDS)
    return None


async def _abort_capture():
    try:
        if state.capture_client:
            try:
                await asyncio.wait_for(state.capture_client.stop_session(), timeout=5.0)
            except Exception:
                pass
            try:
                await asyncio.wait_for(state.capture_client.shutdown(), timeout=5.0)
            except Exception:
                pass
    except Exception:
        pass
    state.capture_client = None
    state.capture_session = None


async def _abort_recording(reason: str):
    logger.error(f"Aborting recording: {reason}")
    try:
        await state.ax_client.send("stop_recording", {})
        await state.ax_client.shutdown()
    except Exception:
        pass
    await _abort_capture()
    state.is_recording = False
