"""Tests for capture/recorder.py flow."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from capture.recorder import _get_ax_binary_path, _poll_export
from capture.ax_client import AxClient


class TestGetAxBinaryPath:
    def test_returns_path_for_windows(self):
        path = _get_ax_binary_path()
        assert "ax_hook_win32.py" in path or "ax_hook_win32" in path.lower()

    @patch("sys.platform", "linux")
    def test_does_not_raise_for_linux(self):
        path = _get_ax_binary_path()
        assert path.endswith(".py")


class TestPollExport:
    @pytest.mark.asyncio
    async def test_detects_exported_video_id(self):
        mock_conn = MagicMock()
        mock_session = MagicMock()
        mock_session.exported_video_id = "v_abc123"
        mock_conn.get_capture_session = MagicMock(return_value=mock_session)

        old_conn = None
        try:
            from state import state
            old_conn = state.conn
            state.conn = mock_conn

            result = await _poll_export("sess_1", "coll_1", timeout=5)
            assert result == "v_abc123"
        finally:
            if old_conn is not None:
                from state import state
                state.conn = old_conn

    @pytest.mark.asyncio
    async def test_polls_until_timeout(self):
        mock_conn = MagicMock()
        mock_session = MagicMock()
        del mock_session.exported_video_id
        mock_session.export = MagicMock(return_value={})
        mock_conn.get_capture_session = MagicMock(return_value=mock_session)

        old_conn = None
        try:
            from state import state
            old_conn = state.conn
            state.conn = mock_conn

            result = await _poll_export("sess_1", "coll_1", timeout=1)
            assert result is None
        finally:
            if old_conn is not None:
                from state import state
                state.conn = old_conn

    @pytest.mark.asyncio
    async def test_handles_export_poll_error(self):
        mock_conn = MagicMock()
        mock_conn.get_capture_session = MagicMock(side_effect=Exception("API error"))

        old_conn = None
        try:
            from state import state
            old_conn = state.conn
            state.conn = mock_conn

            result = await _poll_export("sess_1", "coll_1", timeout=1)
            assert result is None
        finally:
            if old_conn is not None:
                from state import state
                state.conn = old_conn
