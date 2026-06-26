"""Tests for compiler/compiler.py."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from compiler.compiler import (
    compile_skill_events_only,
    _effective_start_ms,
    _ground_steps_in_matched_events,
    _match_events_to_scenes,
    _normalize_llm_output,
    _trim_events_to_effective_start,
    _trim_events_to_effective_window,
)


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

class TestGroundStepsInMatchedEvents:
    def test_rebuilds_steps_from_events_and_scenes(self):
        skill = {
            "steps": [
                {
                    "id": 99,
                    "action": "select",
                    "target": {"type": "AXButton", "label": "wrong"},
                    "recording_ref": {"start": 0, "end": 0},
                    "visual_context": "terminal-like window with no clear form field",
                },
                {
                    "id": 100,
                    "action": "click",
                    "target": {"type": "AXTextField", "label": "wrong"},
                    "recording_ref": {"start": 0, "end": 0},
                    "visual_context": "(no scene match)",
                },
            ],
            "verification": [],
        }
        matched = [
            {
                "event": {
                    "event": "action",
                    "action": "click",
                    "target": {"type": "AXButton", "label": "element_at_10_20", "role": "AXButton"},
                },
                "scene_description": "Chrome New Tab is visible and the address bar is ready.",
                "video_time": 6.5,
                "scene_start": 5.996,
                "scene_end": 8.994,
            },
            {
                "event": {
                    "event": "action",
                    "action": "type",
                    "target": {"type": "AXTextField", "label": "element_at_10_20", "role": "AXTextField"},
                    "value": "you",
                },
                "scene_description": "The user is typing into the Chrome address bar and suggestions appear.",
                "video_time": 9.4,
                "scene_start": 8.994,
                "scene_end": 11.992,
            },
        ]

        _ground_steps_in_matched_events(skill, matched)

        assert [step["action"] for step in skill["steps"]] == ["click", "type"]
        assert skill["steps"][0]["recording_ref"] == {"start": 5.996, "end": 8.994}
        assert skill["steps"][0]["expected_scene"] == "Chrome New Tab is visible and the address bar is ready."
        assert "terminal-like" not in skill["steps"][0]["visual_context"]
        assert skill["steps"][1]["value"] == "you"

    def test_preserves_llm_variables_and_semantic_labels(self):
        skill = {
            "steps": [
                {
                    "id": 1,
                    "action": "click",
                    "target": {"type": "AXTextField", "label": "YouTube search bar"},
                    "recording_ref": {"start": 0, "end": 0},
                    "visual_context": "YouTube homepage with search bar visible at top center",
                },
                {
                    "id": 2,
                    "action": "type",
                    "target": {"type": "AXTextField", "label": "YouTube search bar"},
                    "recording_ref": {"start": 0, "end": 0},
                    "visual_context": "User clicked the search bar, cursor is blinking",
                    "value": "{{search_query}}",
                },
                {
                    "id": 3,
                    "action": "click",
                    "target": {"type": "AXButton", "label": "Submit"},
                    "recording_ref": {"start": 0, "end": 0},
                    "visual_context": "Submit button is blue, bottom-right of the form",
                },
            ],
            "verification": [],
        }
        matched = [
            {
                "event": {
                    "event": "action",
                    "action": "click",
                    "target": {"type": "AXTextField", "label": "element_at_500_200"},
                },
                "scene_description": "YouTube homepage with search bar visible at top center",
                "video_time": 2.0,
                "scene_start": 1.0,
                "scene_end": 3.0,
            },
            {
                "event": {
                    "event": "action",
                    "action": "type",
                    "target": {"type": "AXTextField", "label": "element_at_500_200"},
                    "value": "please dont go",
                },
                "scene_description": "User is typing a search query, suggestions dropdown is open",
                "video_time": 5.0,
                "scene_start": 3.0,
                "scene_end": 7.0,
            },
            {
                "event": {
                    "event": "action",
                    "action": "click",
                    "target": {"type": "AXButton", "label": "Submit"},
                    "position": {"x": 100, "y": 500},
                },
                "scene_description": "Submit button clicked, video playing",
                "video_time": 9.0,
                "scene_start": 7.0,
                "scene_end": 11.0,
            },
        ]

        _ground_steps_in_matched_events(skill, matched)

        assert [step["action"] for step in skill["steps"]] == ["click", "type", "click"]
        assert skill["steps"][0]["target"]["label"] == "YouTube search bar"
        assert skill["steps"][1]["target"]["label"] == "YouTube search bar"
        assert skill["steps"][1]["value"] == "{{search_query}}"
        assert skill["steps"][2]["target"]["label"] == "Submit"
    def test_match_events_to_scenes_uses_semantic_sequence(self):
        start_ms = 100000
        events = [
            {"event": "action", "ts": start_ms + 69015, "action": "click", "target": {"type": "AXButton", "label": "omnibox"}},
            {"event": "action", "ts": start_ms + 73646, "action": "type", "target": {"type": "AXTextField", "label": "omnibox"}, "value": "you"},
            {"event": "action", "ts": start_ms + 73647, "action": "click", "target": {"type": "AXButton", "label": "youtube-result"}},
            {"event": "action", "ts": start_ms + 78581, "action": "click", "target": {"type": "AXButton", "label": "youtube-search"}},
            {"event": "action", "ts": start_ms + 87392, "action": "type", "target": {"type": "AXTextField", "label": "youtube-search"}, "value": "pleasedontgosong"},
            {"event": "action", "ts": start_ms + 87393, "action": "click", "target": {"type": "AXButton", "label": "video-result"}},
        ]
        scenes = [
            {"start": 0.0, "end": 2.998, "description": "Terminal output showing recording started."},
            {"start": 2.998, "end": 5.996, "description": "More terminal output."},
            {"start": 5.996, "end": 8.994, "description": "Chrome New Tab page with address bar ready for input."},
            {"start": 8.994, "end": 11.992, "description": "The user is typing youtube into the Chrome address bar and autocomplete suggestions appear."},
            {"start": 11.992, "end": 14.99, "description": "Google search results page for youtube is visible."},
            {"start": 20.986, "end": 23.984, "description": "The user clicked the YouTube result and YouTube is loading."},
            {"start": 23.984, "end": 26.982, "description": "The YouTube homepage is loaded with the search box visible."},
            {"start": 26.982, "end": 29.98, "description": "The user is typing please dont go song into YouTube search and suggestions appear."},
            {"start": 29.98, "end": 32.978, "description": "YouTube search results page for please dont go song is visible."},
            {"start": 32.978, "end": 35.976, "description": "The user clicked Joel Adams - Please Don't Go and the watch page loads."},
        ]

        matched = _match_events_to_scenes(events, scenes, start_ms, fallback_offset=60.0)
        starts = [item["scene_start"] for item in matched]

        assert starts == [5.996, 8.994, 20.986, 23.984, 26.982, 32.978]


class TestEffectiveStart:
    def test_effective_start_prefers_trimmed_start(self):
        assert _effective_start_ms({
            "recording_start_epoch_ms": 1000,
            "effective_recording_start_epoch_ms": 6000,
        }) == 6000

    def test_trim_events_to_effective_start_discards_lead_in(self):
        events = [
            {"event": "action", "ts": 1000, "action": "click"},
            {"event": "action", "ts": 6000, "action": "click"},
            {"event": "action", "ts": 7000, "action": "type"},
        ]

        assert _trim_events_to_effective_start(events, 6000) == events[1:]

    def test_trim_events_to_effective_window_discards_lead_in_and_tail(self):
        events = [
            {"event": "action", "ts": 1000, "action": "click"},
            {"event": "action", "ts": 6000, "action": "click"},
            {"event": "action", "ts": 7000, "action": "type"},
            {"event": "action", "ts": 12000, "action": "click"},
        ]

        assert _trim_events_to_effective_window(events, 6000, 8000) == events[1:3]


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
