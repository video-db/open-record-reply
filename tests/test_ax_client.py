"""Tests for AX client JSONL IPC protocol."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from capture.ax_client import AxClient


class _LinkedReader:
    def __init__(self):
        self._queue = asyncio.Queue()

    async def readline(self):
        line = await self._queue.get()
        if line is None:
            return b""
        return (line + "\n").encode("utf-8")

    def push(self, response: str):
        self._queue.put_nowait(response)


async def _async_noop():
    pass


def _make_proc():
    reader = _LinkedReader()
    proc = MagicMock()
    mock_stdin = MagicMock()
    mock_stdin.drain = _async_noop

    def _write(data):
        mock_stdin.write_called = True
        msg = json.loads(data.decode().strip())
        rid = msg["id"]
        reader.push(json.dumps({"id": rid, "status": "ok", "result": {"echo": rid}}))

    mock_stdin.write = _write
    mock_stdin.write_called = False
    proc.stdin = mock_stdin
    proc.stdout = reader
    proc.wait = _async_noop
    proc.terminate = MagicMock()
    return proc


class TestAxClient:
    def test_init_binds_process(self):
        proc = _make_proc()
        client = AxClient(proc)
        assert client._proc is proc

    @pytest.mark.asyncio
    async def test_start_creates_reader_task(self):
        proc = _make_proc()
        client = AxClient(proc)
        await client.start(event_handler=lambda e: None)
        assert client._reader_task is not None

    @pytest.mark.asyncio
    async def test_send_writes_to_stdin(self):
        proc = _make_proc()
        client = AxClient(proc)
        await client.start(event_handler=lambda e: None)
        await client.send("find_element", {"label": "Submit"})
        assert proc.stdin.write_called

    @pytest.mark.asyncio
    async def test_send_returns_response(self):
        proc = _make_proc()
        client = AxClient(proc)
        await client.start(event_handler=lambda e: None)
        result = await client.send("find_element", {"label": "Submit"})
        assert result["status"] == "ok"
        assert "echo" in result["result"]

    @pytest.mark.asyncio
    async def test_send_matches_by_request_id(self):
        proc = _make_proc()
        client = AxClient(proc)
        await client.start(event_handler=lambda e: None)

        r0 = await client.send("method_a", {})
        r1 = await client.send("method_b", {})
        r2 = await client.send("method_c", {})

        assert r0["id"] == "req-0"
        assert r1["id"] == "req-1"
        assert r2["id"] == "req-2"

    @pytest.mark.asyncio
    async def test_send_incrementing_ids(self):
        proc = _make_proc()
        client = AxClient(proc)
        await client.start(event_handler=lambda e: None)

        ids = []
        for i in range(5):
            result = await client.send("method", {})
            ids.append(result["id"])

        assert ids == ["req-0", "req-1", "req-2", "req-3", "req-4"]

    @pytest.mark.asyncio
    async def test_shutdown_terminates_process(self):
        proc = _make_proc()
        client = AxClient(proc)
        await client.start(event_handler=lambda e: None)
        await client.shutdown()
        assert proc.terminate.called
