"""JSONL IPC wrapper for the native AX companion binary."""

import asyncio
import json
import logging
import socket
import os
import subprocess
import threading
import time

from config import AX_SEND_TIMEOUT, AX_EXECUTE_TIMEOUT

logger = logging.getLogger(__name__)


class AxClient:
    """JSONL IPC wrapper for the AX companion (pipe mode)."""

    def __init__(self, process: asyncio.subprocess.Process):
        self._proc = process
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._event_handler: callable | None = None
        self._counter = 0

    async def start(self, event_handler: callable):
        self._event_handler = event_handler
        self._reader_task = asyncio.create_task(self._read_loop())

    async def send(self, method: str, params: dict) -> dict:
        timeout = AX_EXECUTE_TIMEOUT if method == "execute_action" else AX_SEND_TIMEOUT
        rid = f"req-{self._counter}"
        self._counter += 1
        future = asyncio.get_event_loop().create_future()
        self._pending[rid] = future
        msg = json.dumps({"id": rid, "method": method, "params": params}) + "\n"
        self._proc.stdin.write(msg.encode())
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            return {
                "status": "error",
                "error": {"code": "TIMEOUT", "message": f"AX call '{method}' timed out after {timeout:.0f}s"},
            }

    async def _read_loop(self):
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                future = self._pending.pop(msg["id"], None)
                if future and not future.done():
                    future.set_result(msg)
            elif "event" in msg and self._event_handler:
                try:
                    result = self._event_handler(msg)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.exception("Error in event handler")

    async def shutdown(self):
        try:
            await self.send("shutdown", {})
        except Exception:
            pass
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        try:
            self._proc.terminate()
            await self._proc.wait()
        except Exception:
            pass


class TcpAxClient:
    """TCP-based IPC wrapper. Used on Windows where pipe redirection blocks input hooks."""

    def __init__(self, proc_subprocess: subprocess.Popen, port_file: str, timeout: float = 10.0):
        self._proc = proc_subprocess
        self._port_file = port_file
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._event_handler: callable | None = None
        self._counter = 0
        self._lock = asyncio.Lock()
        self._buffer = b""

    async def start(self, event_handler: callable):
        self._event_handler = event_handler
        port = await self._wait_for_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        await asyncio.get_event_loop().run_in_executor(None, lambda: self._sock.connect(("127.0.0.1", port)))
        self._sock.setblocking(False)
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _wait_for_port(self) -> int:
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            if os.path.exists(self._port_file):
                try:
                    with open(self._port_file) as f:
                        port = int(f.read().strip())
                    if port > 0:
                        return port
                except (ValueError, IOError):
                    pass
            await asyncio.sleep(0.1)
        raise RuntimeError(f"AX hook port file not found: {self._port_file}")

    async def send(self, method: str, params: dict) -> dict:
        timeout = AX_EXECUTE_TIMEOUT if method == "execute_action" else AX_SEND_TIMEOUT
        rid = f"req-{self._counter}"
        self._counter += 1
        future = asyncio.get_event_loop().create_future()
        self._pending[rid] = future
        msg = (json.dumps({"id": rid, "method": method, "params": params}) + "\n").encode("utf-8")
        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.sock_sendall(self._sock, msg)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            return {
                "status": "error",
                "error": {"code": "TIMEOUT", "message": f"AX call '{method}' timed out after {timeout:.0f}s"},
            }

    async def _read_loop(self):
        loop = asyncio.get_event_loop()
        buf = b""
        while True:
            try:
                data = await asyncio.wait_for(loop.sock_recv(self._sock, 4096), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                break
            if not data:
                break
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
                elif "event" in msg and self._event_handler:
                    try:
                        result = self._event_handler(msg)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception("Error in event handler")

    async def shutdown(self):
        try:
            await self.send("shutdown", {})
        except Exception:
            pass
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                pass
