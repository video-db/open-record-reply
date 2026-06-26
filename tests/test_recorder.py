"""Tests for capture/recorder.py flow."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from capture.recorder import _get_ax_binary_path, _poll_export
from capture.ax_client import AxClient


class TestGetAxBinaryPath:
    @pytest.mark.parametrize(
        ("platform_name", "expected_hook"),
        [
            ("darwin", "ax_hook_darwin.py"),
            ("win32", "ax_hook_win32.py"),
            ("linux", "ax_hook_linux.py"),
        ],
    )
    def test_returns_path_for_supported_platforms(self, platform_name, expected_hook):
        with patch("sys.platform", platform_name):
            path = _get_ax_binary_path()
        assert path.endswith(expected_hook)

    def test_raises_for_unsupported_platform(self):
        with patch("sys.platform", "freebsd"):
            with pytest.raises(RuntimeError, match="AX hook not supported"):
                _get_ax_binary_path()

    def test_returns_path_for_current_platform(self):
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
