"""Shared test fixtures."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture
def sample_events():
    return [
        {"event": "action", "ts": 1000, "action": "click", "target": {"type": "AXButton", "label": "New Expense", "role": "AXButton"}, "position": {"x": 310, "y": 100}},
        {"event": "action", "ts": 3000, "action": "type", "target": {"type": "AXTextField", "label": "Date", "role": "AXTextField"}, "value": "06/23/2026"},
        {"event": "action", "ts": 5000, "action": "type", "target": {"type": "AXTextField", "label": "Amount", "role": "AXTextField"}, "value": "150.00"},
        {"event": "action", "ts": 7000, "action": "select", "target": {"type": "AXPopUpButton", "label": "Category", "role": "AXPopUpButton"}, "value": "Travel"},
        {"event": "action", "ts": 9000, "action": "click", "target": {"type": "AXButton", "label": "Submit", "role": "AXButton"}, "position": {"x": 500, "y": 600}},
    ]


@pytest.fixture
def sample_skill():
    return {
        "name": "expense-report",
        "description": "File a new expense report",
        "preconditions": ["Application is open and ready"],
        "inputs": {
            "date": {"type": "string", "format": "MM/DD/YYYY", "example": "06/23/2026"},
            "amount": {"type": "number", "example": 150.00},
            "category": {"type": "enum", "values": ["Travel", "Meals", "Office Supplies"], "example": "Travel"},
        },
        "steps": [
            {"id": 1, "action": "click", "target": {"type": "AXButton", "label": "New Expense"}, "recording_ref": {"start": 0, "end": 2}},
            {"id": 2, "action": "type", "target": {"type": "AXTextField", "label": "Date"}, "value": "{{date}}", "recording_ref": {"start": 2, "end": 4}},
            {"id": 3, "action": "type", "target": {"type": "AXTextField", "label": "Amount"}, "value": "{{amount}}", "recording_ref": {"start": 4, "end": 6}},
            {"id": 4, "action": "select", "target": {"type": "AXPopUpButton", "label": "Category"}, "value": "{{category}}", "recording_ref": {"start": 6, "end": 8}},
            {"id": 5, "action": "click", "target": {"type": "AXButton", "label": "Submit"}, "recording_ref": {"start": 8, "end": 10}},
        ],
        "verification": [
            {"type": "ax_element", "check": "Expense submitted successfully"},
        ],
        "video_id": "v_test123",
        "scene_index_id": "scene_idx_1",
        "version": 1,
    }


@pytest.fixture
def mock_ax_client():
    client = MagicMock()
    client.send = AsyncMock()
    return client


@pytest.fixture
def mock_videodb_conn():
    conn = MagicMock()
    conn.generate_client_token = MagicMock(return_value="mock-token")
    conn.connect_websocket = MagicMock()
    return conn


@pytest.fixture
def sample_events_jsonl(sample_events):
    return "\n".join(json.dumps(e) for e in sample_events)


@pytest.fixture
def sample_metadata():
    return {
        "skill_name": "expense-report",
        "video_id": "v_test123",
        "recording_start_epoch_ms": 0,
        "recording_end_epoch_ms": 10000,
        "duration_seconds": 10.0,
        "platform": "win32",
        "event_count": 5,
        "error_count": 0,
    }
