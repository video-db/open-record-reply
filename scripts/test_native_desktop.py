"""Record native Windows desktop interactions and inspect UIA data quality.

Launches the AX hook directly (no VideoDB), records events, and analyzes
whether automation_id, class_name, and foreground_window are populated.

Usage:
    uv run python scripts/test_native_desktop.py
    uv run python scripts/test_native_desktop.py my-workflow
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def _get_ax_binary_path() -> str:
    binary_name = "ax_hook_win32.py"
    native_dir = Path(__file__).parent.parent / "capture" / "native"
    candidate = native_dir / binary_name
    if not candidate.exists():
        raise FileNotFoundError(f"AX binary not found at {candidate}")
    return str(candidate)


async def _connect_tcp(port_file: str, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(port_file):
            try:
                with open(port_file) as f:
                    port = int(f.read().strip())
                if port > 0:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    return reader, writer
            except (ValueError, OSError):
                pass
        await asyncio.sleep(0.2)
    raise RuntimeError(f"AX hook port file not found: {port_file}")


async def _send(writer, msg: dict) -> None:
    data = (json.dumps(msg) + "\n").encode("utf-8")
    writer.write(data)
    await writer.drain()


class _AxSession:
    """Manages TCP connection and event streaming from the AX hook."""

    def __init__(self, reader, writer, events_path: str):
        self._reader = reader
        self._writer = writer
        self._events_path = events_path
        self._events_file = open(events_path, "a", encoding="utf-8")
        self._pending: dict[str, asyncio.Future] = {}
        self._counter = 0
        self._read_task: asyncio.Task | None = None

    def start_read_loop(self):
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        buf = b""
        while True:
            try:
                data = await asyncio.wait_for(self._reader.read(4096), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if "id" in msg:
                    future = self._pending.pop(msg["id"], None)
                    if future and not future.done():
                        future.set_result(msg)
                elif "event" in msg:
                    self._events_file.write(json.dumps(msg) + "\n")
                    self._events_file.flush()

    async def request(self, method: str, params: dict, timeout: float = 15.0) -> dict:
        rid = f"req-{self._counter}"
        self._counter += 1
        future = asyncio.get_event_loop().create_future()
        self._pending[rid] = future
        await _send(self._writer, {"id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            return {"status": "error", "error": {"code": "TIMEOUT", "message": f"'{method}' timed out"}}

    async def shutdown(self):
        try:
            await self.request("shutdown", {})
        except Exception:
            pass
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        self._events_file.close()
        self._writer.close()
        await self._writer.wait_closed()


async def record_and_inspect(name: str = "desktop-workflow", lead_in: float = 5.0) -> None:
    output_path = str(
        Path.home()
        / ".mcp-videodb"
        / "sessions"
        / f"{int(time.time())}_{name}"
        / "events.jsonl"
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    print(f"\n{'=' * 55}")
    print(f"  Native Desktop Recorder")
    print(f"{'=' * 55}")
    print(f"  Switch to your target app, do whatever you want,")
    print(f"  then come back here and press ENTER.")
    print(f"  Lead-in: {lead_in}s")
    print(f"{'=' * 55}")

    binary = _get_ax_binary_path()
    temp_dir = os.environ.get("TEMP", os.environ.get("TMP", "C:\\Temp"))
    port_file = os.path.join(temp_dir, "ax_hook_port.txt")
    if os.path.exists(port_file):
        os.remove(port_file)

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(binary)],
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        reader, writer = await _connect_tcp(port_file)
        session = _AxSession(reader, writer, output_path)
        session.start_read_loop()

        response = await session.request("start_recording", {"output_path": output_path})
        if response.get("status") != "ok":
            print(f"  ERROR: AX hook failed to start: {response}")
            await session.shutdown()
            return

        for remaining in range(int(lead_in), 0, -1):
            print(f"  Ready in {remaining}s...", end="\r")
            await asyncio.sleep(1)
        print(f"  [RECORDING] Go!                               ")

        # Use a thread for input() since asyncio can't do blocking I/O nicely
        done = threading.Event()
        def _wait_input():
            input(f"\n  Press ENTER to stop...\n")
            done.set()
        threading.Thread(target=_wait_input, daemon=True).start()

        while not done.is_set():
            await asyncio.sleep(0.2)

        response = await session.request("stop_recording", {})
        print(f"  Stopped.")

        await session.shutdown()

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print(f"  Output: {output_path}")
    _print_event_analysis(output_path)


def _print_event_analysis(events_path: str) -> None:
    content = Path(events_path).read_text(encoding="utf-8").strip()
    if not content:
        print("\n  No events captured!")
        return

    events = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("event") == "action":
                events.append(event)
        except json.JSONDecodeError:
            pass

    if not events:
        print("\n  No action events found.")
        return

    rich_count = 0
    poor_count = 0

    print(f"\n{'=' * 90}")
    print(f"  Event Log Analysis ({len(events)} action events)")
    print(f"{'=' * 90}")
    header = f"  {'#':<4} {'action':<8} {'label':<40} {'auto_id':<25} {'class':<20} {'window':<30} UIA"
    print(header)
    print(f"  {'-'*4} {'-'*8} {'-'*40} {'-'*25} {'-'*20} {'-'*30} ---")

    for i, event in enumerate(events):
        target = event.get("target", {})
        action = event.get("action", "?")[:7]
        value = event.get("value", "")

        label = (target.get("label") or "")[:39]
        auto_id = (target.get("automation_id") or "")[:24]
        class_name = (target.get("class_name") or "")[:19]
        fg_window = (target.get("foreground_window") or "")[:29]

        has_rich = bool(target.get("automation_id") or target.get("class_name"))
        if has_rich:
            rich_count += 1
            uia_rating = "RICH"
        else:
            poor_count += 1
            uia_rating = "POOR"

        print(f"  {i+1:<4} {action:<8} {label:<40} {auto_id:<25} {class_name:<20} {fg_window:<30} {uia_rating}")
        if value and value.strip():
            print(f"       value: '{value[:90]}'")

    pct = rich_count / len(events) * 100
    print(f"\n  {'=' * 90}")
    print(f"  Summary:")
    print(f"    Total:  {len(events)}")
    print(f"    Rich:   {rich_count} ({pct:.0f}%)")
    print(f"    Poor:   {poor_count} ({100 - pct:.0f}%)")

    if rich_count == 0:
        print(f"\n  WARNING: No UIA data — behaves like a web app.")
        print(f"  Needs computer_use (visual) or browser automation.")
    elif pct > 70:
        print(f"\n  GOOD: {pct:.0f}% rich UIA data.")
        print(f"  Can use UIA-based replay (mcp:videodb-record-replay).")
    else:
        print(f"\n  MIXED: Partial UIA coverage.")
        print(f"  UIA + pixel fallback recommended.")
    print()


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "desktop-workflow"
    asyncio.run(record_and_inspect(name))


if __name__ == "__main__":
    main()
