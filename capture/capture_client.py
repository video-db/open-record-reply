"""Local CaptureClient wrapper with race-free IPC command handling."""

import asyncio
import json
import uuid
from typing import Any, Dict, Optional

from videodb.capture import CaptureClient as VideoDBCaptureClient


class CaptureClient(VideoDBCaptureClient):
    """VideoDB CaptureClient with safer command and shutdown behavior."""

    async def _send_command(
        self, command: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        await self._ensure_process()

        command_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._futures[command_id] = future

        payload = {
            "command": command,
            "commandId": command_id,
            "params": params or {},
        }
        message = f"videodb_recorder|{json.dumps(payload)}\n"

        try:
            self._proc.stdin.write(message.encode("utf-8"))
            await self._proc.stdin.drain()
            return await future
        finally:
            self._futures.pop(command_id, None)

    async def shutdown(self):
        """Terminate the capture helper even if graceful shutdown does not answer."""
        proc = self._proc
        if not proc:
            return

        try:
            await asyncio.wait_for(self._send_command("shutdown"), timeout=2.0)
        except Exception:
            pass

        try:
            if proc.returncode is None:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            try:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            except Exception:
                pass
        finally:
            self._proc = None