"""Tests for compiler/compiler.py."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from compiler.compiler import compile_skill_events_only, _normalize_llm_output


class TestNormalizeLlmOutput:
    def test_adds_missing_fields(self):
        raw = {
            "steps": [
                {"action": "click", "target": {"type": "AXButton", "label": "Submit"}},
            ],
        }
        result = _normalize_llm_output(raw, "test-skill")

        assert result["name"] == "test-skill"
        assert result["description"] is not None
        assert result["preconditions"] == ["Application is open and ready"]
        assert result["steps"][0]["id"] == 1
        assert "recording_ref" in result["steps"][0]

    def test_normalizes_step_ids(self):
        raw = {
            "steps": [
                {"action": "click", "target": {"type": "AXButton", "label": "A"}},
                {"action": "type", "target": {"type": "AXTextField", "label": "B"}},
            ],
        }
        result = _normalize_llm_output(raw, "test")

        assert result["steps"][0]["id"] == 1
        assert result["steps"][1]["id"] == 2

    def test_normalizes_verification(self):
        raw = {
            "steps": [],
        }
        result = _normalize_llm_output(raw, "test")

        assert len(result["verification"]) == 1
        assert result["verification"][0]["type"] == "ax_element"

    def test_renames_variables_to_inputs(self):
        raw = {
            "steps": [],
            "variables": [{"name": "date", "type": "string", "example": "2026-01-01"}],
        }
        result = _normalize_llm_output(raw, "test")

        assert "inputs" in result
        assert "variables" not in result
        assert "date" in result["inputs"]
        assert result["inputs"]["date"]["type"] == "string"

    def test_removes_role_from_target(self):
        raw = {
            "steps": [
                {"action": "click", "target": {"type": "AXButton", "label": "Submit", "role": "AXButton"}},
            ],
        }
        result = _normalize_llm_output(raw, "test")

        assert "role" not in result["steps"][0]["target"]
    def test_adds_fallback_start_context(self):
        raw = {
            "preconditions": ["Open the target app"],
            "steps": [],
        }
        result = _normalize_llm_output(raw, "test")

        assert result["start_context"] == {
            "kind": "unknown",
            "label": "Starting application state",
            "instructions": "Open the target app",
            "evidence": "No structured start context was produced during compilation.",
        }

    def test_normalizes_start_context(self):
        raw = {
            "start_context": {
                "kind": "browser",
                "label": "YouTube Studio upload page",
                "locator": "https://studio.youtube.com",
                "instructions": "Open the upload dialog before running the steps.",
                "evidence": "The recording shows YouTube Studio.",
            },
            "steps": [],
        }
        result = _normalize_llm_output(raw, "test")

        assert result["start_context"] == {
            "kind": "web",
            "label": "YouTube Studio upload page",
            "locator": "https://studio.youtube.com",
            "instructions": "Open the upload dialog before running the steps.",
            "evidence": "The recording shows YouTube Studio.",
        }

    def test_preserves_existing_fields(self):
        raw = {
            "name": "my-skill",
            "description": "My description",
            "preconditions": ["App open"],
            "steps": [
                {"id": 10, "action": "click", "target": {"type": "AXButton", "label": "Go"},
                 "recording_ref": {"start": 0, "end": 2}, "visual_context": "Click Go button"},
            ],
            "verification": [{"type": "ax_element", "check": "Done"}],
        }
        result = _normalize_llm_output(raw, "my-skill")

        assert result["name"] == "my-skill"
        assert result["preconditions"] == ["App open"]
        assert result["steps"][0]["id"] == 10
        assert result["verification"][0]["type"] == "ax_element"



    def test_normalizes_llm_shape_variants(self):
        raw = {
            "name": "upload_file_to_youtube",
            "version": "1.0",
            "steps": [
                {
                    "id": "1",
                    "type": "type",
                    "target": "element_at_968_848",
                    "time": {"start": 66.39, "end": 67.39},
                    "value": "{{video_title}}",
                },
                {
                    "action": "press",
                    "target": {"role": "AXButton", "label": "Next"},
                },
            ],
            "verification": [{"type": "element_present", "description": "Upload complete"}],
        }
        result = _normalize_llm_output(raw, "upload_file_to_youtube")

        assert result["name"] == "upload-file-to-youtube"
        assert result["version"] == 1
        assert result["steps"][0]["action"] == "type"
        assert result["steps"][0]["target"] == {"type": "element", "label": "element_at_968_848"}
        assert result["steps"][0]["recording_ref"] == {"start": 66.39, "end": 67.39}
        assert result["steps"][1]["action"] == "click"
        assert result["steps"][1]["target"] == {"type": "AXButton", "label": "Next"}
        assert result["verification"][0] == {"type": "ax_element", "check": "Upload complete"}

    def test_replaces_generic_verification_with_specific_checks(self):
        raw = {
            "steps": [
                {
                    "id": 1,
                    "action": "select",
                    "target": {"type": "AXRadioButton", "label": "Audience"},
                    "value": "{{audience}}",
                    "visual_context": "The Audience section shows the radio choice selected and the status briefly says Saving.",
                },
                {
                    "id": 2,
                    "action": "click",
                    "target": {"type": "AXButton", "label": "Next"},
                    "visual_context": "The upload dialog shows an Uploading video progress row and the Details step remains visible.",
                },
            ],
            "verification": [
                {"type": "visual", "check": "Task completed successfully"},
                {"type": "visual", "check": "Task completed successfully"},
            ],
        }
        result = _normalize_llm_output(raw, "test")

        checks = [item["check"] for item in result["verification"]]
        assert "Task completed successfully" not in checks
        assert any("`audience` option is selected" in check for check in checks)
        assert any("upload" in check.lower() or "progress" in check.lower() for check in checks)

    def test_keeps_specific_verification(self):
        raw = {
            "steps": [
                {"action": "click", "target": {"type": "AXButton", "label": "Save"}},
            ],
            "verification": [{"type": "ax_element", "check": "Saved as private"}],
        }
        result = _normalize_llm_output(raw, "test")

        assert result["verification"] == [{"type": "ax_element", "check": "Saved as private"}]

class TestCompileEventsOnly:
    @pytest.mark.asyncio
    async def test_requires_events(self, tmp_path):
        session_dir = tmp_path / ".mcp-videodb" / "sessions" / "1234_test-skill"
        session_dir.mkdir(parents=True)
        (session_dir / "metadata.json").write_text(json.dumps({
            "skill_name": "test-skill",
            "video_id": None,
            "recording_start_epoch_ms": 0,
        }))

        with patch.object(Path, "home", return_value=tmp_path):
            with patch("compiler.compiler.state") as mock_state:
                with pytest.raises(RuntimeError, match="No events recorded"):
                    await compile_skill_events_only("test-skill")

    @pytest.mark.asyncio
    async def test_compiles_from_events(self, tmp_path):
        skill_name = "test-compile"
        session_dir = tmp_path / ".mcp-videodb" / "sessions" / "1234_test-compile"
        session_dir.mkdir(parents=True)
        (session_dir / "metadata.json").write_text(json.dumps({
            "skill_name": skill_name,
            "video_id": None,
            "recording_start_epoch_ms": 0,
        }))
        (session_dir / "events.jsonl").write_text("\n".join([
            json.dumps({"event": "action", "ts": 1000, "action": "click",
                        "target": {"type": "AXButton", "label": "Start", "role": "AXButton"}}),
            json.dumps({"event": "action", "ts": 3000, "action": "click",
                        "target": {"type": "AXButton", "label": "Finish", "role": "AXButton"}}),
        ]))

        mock_response = {
            "output": {
                "name": skill_name,
                "description": "Test skill",
                "preconditions": ["App open"],
                "inputs": {},
                "steps": [
                    {"id": 1, "action": "click", "target": {"type": "AXButton", "label": "Start"},
                     "recording_ref": {"start": 1, "end": 2}},
                    {"id": 2, "action": "click", "target": {"type": "AXButton", "label": "Finish"},
                     "recording_ref": {"start": 3, "end": 4}},
                ],
                "verification": [{"type": "ax_element", "check": "Done"}],
                "video_id": "v_events_only",
                "scene_index_id": "",
            }
        }

        with patch.object(Path, "home", return_value=tmp_path):
            with patch("compiler.compiler.state") as mock_state:
                mock_state.coll.generate_text = MagicMock(return_value=mock_response)
                with patch("registry.save_skill") as mock_save:
                    result = await compile_skill_events_only(skill_name)

                    assert result["name"] == skill_name
                    assert result["video_id"] == "v_events_only"
                    assert len(result["steps"]) == 2
                    mock_save.assert_called_once()
