"""Compile a recording into a replayable SKILL.json."""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from videodb import SceneExtractionType

from config import (
    SCENE_INDEX_TIME_INTERVAL,
    SCENE_INDEX_FRAME_COUNT,
    LLM_MODEL,
    LLM_MAX_RETRIES,
)
from state import state
from compiler.prompts import build_prompt, COMPILATION_SYSTEM_PROMPT
from registry import save_skill

logger = logging.getLogger(__name__)


async def compile_skill(video_id: str, name: str) -> dict:
    video = state.coll.get_video(video_id)

    scene_index_id = None
    try:
        scene_index_id = video.index_scenes(
            extraction_type=SceneExtractionType.time_based,
            extraction_config={
                "time": SCENE_INDEX_TIME_INTERVAL,
                "frame_count": SCENE_INDEX_FRAME_COUNT,
            },
            prompt="Describe what the user is doing on screen at this moment: which element they are interacting with (clicking into a field, typing a value, selecting an option from a dropdown, choosing a radio button or checkbox, pressing a button), what value they are entering or selecting, and any visible changes that result from the interaction (e.g., dropdown closes with new value shown, radio button becomes selected, checkbox becomes checked, success message appears, new page loads).",
        )
    except Exception as e:
        msg = str(e)
        match = re.search(r"with id (\w+) already exists", msg)
        if match:
            old_id = match.group(1)
            try:
                video.delete_scene_index(old_id)
                logger.info(f"Deleted stale scene index {old_id}, creating fresh")
            except Exception:
                logger.warning(f"Could not delete stale index {old_id}")
            scene_index_id = video.index_scenes(
                extraction_type=SceneExtractionType.time_based,
                extraction_config={
                    "time": SCENE_INDEX_TIME_INTERVAL,
                    "frame_count": SCENE_INDEX_FRAME_COUNT,
                },
                prompt="Describe what the user is doing on screen at this moment: which element they are interacting with (clicking into a field, typing a value, selecting an option from a dropdown, choosing a radio button or checkbox, pressing a button), what value they are entering or selecting, and any visible changes that result from the interaction (e.g., dropdown closes with new value shown, radio button becomes selected, checkbox becomes checked, success message appears, new page loads).",
            )
        else:
            raise

    scenes = await _poll_scene_index(video, scene_index_id)
    logger.info(f"Scene index ready: {len(scenes)} scenes")
    transcript = _safe_get_transcript(video)

    sessions = sorted(Path.home().glob(f".mcp-videodb/sessions/*_{name}/metadata.json"))
    if not sessions:
        sessions = sorted(Path.home().glob(".mcp-videodb/sessions/*/metadata.json"))

    metadata = None
    events = []
    for mpath in sessions:
        meta = json.loads(mpath.read_text())
        if meta.get("video_id") == video_id:
            metadata = meta
            ep = mpath.parent / "events.jsonl"
            if ep.exists():
                events = [
                    json.loads(line)
                    for line in ep.read_text().strip().split("\n")
                    if line.strip()
                ]
            break

    if not metadata:
        raise RuntimeError(f"No session metadata found for video_id {video_id}")

    start_ms = _effective_start_ms(metadata)
    end_ms = _effective_end_ms(metadata)
    events = _trim_events_to_effective_window(events, start_ms, end_ms)
    events = _prefilter_events(events)
    if not events:
        raise RuntimeError("No events remain after noise filtering")

    event_scene_offset = _estimate_event_scene_offset(events, scenes, start_ms)
    matched = _match_events_to_scenes(events, scenes, start_ms, event_scene_offset)
    prompt = build_prompt(name, events, matched, transcript)
    skill_json = None

    for attempt in range(LLM_MAX_RETRIES):
        try:
            response = state.coll.generate_text(
                prompt=prompt, model_name=LLM_MODEL, response_type="json"
            )
            raw = response["output"]
            skill_json = raw if isinstance(raw, dict) else json.loads(raw)
            ts_err = _validate_timestamps_raw(skill_json)
            if ts_err:
                if attempt < LLM_MAX_RETRIES - 1:
                    prompt += f"\n\n{ts_err}"
                    continue
                logger.warning(f"Timestamp format wrong on final attempt — applying normalization")
            break
        except (json.JSONDecodeError, KeyError) as e:
            prompt += f"\n\nPREVIOUS OUTPUT WAS INVALID JSON. Error: {e}. Output valid JSON only."
        except Exception as e:
            if attempt < LLM_MAX_RETRIES - 1:
                await asyncio.sleep(5 * (2 ** attempt))
            else:
                raise

    if skill_json is None:
        raise RuntimeError(f"LLM compilation failed after {LLM_MAX_RETRIES} retries")

    skill_json = _normalize_llm_output(skill_json, name)
    _ground_steps_in_matched_events(skill_json, matched)
    _attach_recorded_surfaces(skill_json)
    skill_json["video_id"] = video_id
    skill_json["scene_index_id"] = scene_index_id
    skill_json["compiled_at"] = datetime.now(timezone.utc).isoformat()
    _attach_expected_scenes(skill_json, scenes, metadata, event_scene_offset)

    errors = _validate_skill(skill_json)
    if errors:
        prompt += f"\n\nVALIDATION ERRORS: {json.dumps(errors, indent=2)}"
        response = state.coll.generate_text(
            prompt=prompt, model_name=LLM_MODEL, response_type="json"
        )
        skill_json = (
            response["output"]
            if isinstance(response["output"], dict)
            else json.loads(response["output"])
        )
        skill_json = _normalize_llm_output(skill_json, name)
        _ground_steps_in_matched_events(skill_json, matched)
        _attach_recorded_surfaces(skill_json)
        skill_json["video_id"] = video_id
        skill_json["scene_index_id"] = scene_index_id
        skill_json["compiled_at"] = datetime.now(timezone.utc).isoformat()
        _attach_expected_scenes(skill_json, scenes, metadata, event_scene_offset)

    save_skill(skill_json)
    return skill_json


def _safe_get_transcript(video) -> str:
    try:
        return video.get_transcript_text()
    except Exception:
        return ""


def _validate_skill(skill: dict) -> list[str]:
    import jsonschema
    schema_path = Path(__file__).parent.parent / "schema" / "skill.schema.json"
    schema = json.loads(schema_path.read_text())
    try:
        jsonschema.validate(instance=skill, schema=schema)
        return []
    except jsonschema.ValidationError as e:
        return [str(e)]


def _validate_timestamps_raw(skill_json: dict) -> str:
    """Check raw LLM output for absolute epoch timestamps before normalization.

    Returns an error message to append to the retry prompt, or empty string if OK.
    """
    bad_steps = []
    for step in skill_json.get("steps", []):
        ref = step.get("recording_ref", {})
        end = ref.get("end", 0)
        if isinstance(end, (int, float)) and end > 1_000_000_000:
            bad_steps.append(f"Step {step.get('id', '?')}: recording_ref.end={end}")
    if bad_steps:
        return (
            "TIMESTAMP FORMAT ERROR: recording_ref.end must be relative seconds from "
            "recording start, NOT absolute epoch timestamps.\n"
            "Example: 5.967 seconds is correct. 1782319061217 is WRONG.\n"
            f"Offending steps: {', '.join(bad_steps)}.\n"
            "Re-output with relative seconds only."
        )
    return ""


async def compile_skill_events_only(name: str) -> dict:
    from registry import save_skill
    sessions = sorted(Path.home().glob(f".mcp-videodb/sessions/*_{name}/metadata.json"))
    if not sessions:
        sessions = sorted(Path.home().glob(".mcp-videodb/sessions/*/metadata.json"))

    metadata = None
    events = []
    for mpath in reversed(sessions):
        meta = json.loads(mpath.read_text())
        if meta.get("skill_name") == name:
            metadata = meta
            ep = mpath.parent / "events.jsonl"
            if ep.exists():
                events = [
                    json.loads(line)
                    for line in ep.read_text().strip().split("\n")
                    if line.strip()
                ]
            break

    if not metadata:
        raise RuntimeError(f"No session metadata found for skill '{name}'")

    start_ms = _effective_start_ms(metadata)
    end_ms = _effective_end_ms(metadata)
    events = _trim_events_to_effective_window(events, start_ms, end_ms)
    events = _prefilter_events(events)
    if not events:
        raise RuntimeError("No events recorded")

    action_events = [e for e in events if e.get("event") == "action"]

    prompt = COMPILATION_SYSTEM_PROMPT + "\n\n" + _build_events_only_prompt(
        name, action_events, metadata
    )

    skill_json = None
    for attempt in range(LLM_MAX_RETRIES):
        try:
            response = state.coll.generate_text(
                prompt=prompt, model_name=LLM_MODEL, response_type="json"
            )
            raw = response["output"]
            skill_json = raw if isinstance(raw, dict) else json.loads(raw)
            break
        except (json.JSONDecodeError, KeyError) as e:
            prompt += f"\n\nPREVIOUS OUTPUT WAS INVALID JSON. Error: {e}. Output valid JSON only."
        except Exception as e:
            if attempt < LLM_MAX_RETRIES - 1:
                await asyncio.sleep(5 * (2 ** attempt))
            else:
                raise

    if skill_json is None:
        raise RuntimeError(f"LLM compilation failed after {LLM_MAX_RETRIES} retries")

    skill_json = _normalize_llm_output(skill_json, name)
    _attach_step_surfaces_from_events(skill_json, action_events)
    _attach_recorded_surfaces(skill_json)
    skill_json["video_id"] = "v_events_only"
    skill_json["scene_index_id"] = ""
    skill_json["compiled_at"] = datetime.now(timezone.utc).isoformat()

    errors = _validate_skill(skill_json)
    if errors:
        raise RuntimeError(f"Schema validation failed: {errors}")

    save_skill(skill_json)
    return skill_json


async def _poll_scene_index(video, scene_index_id: str, max_attempts: int = 20) -> list[dict]:
    """Poll scene index until processing completes (count stabilizes or max attempts)."""
    prev_count = -1
    stable = 0
    for i in range(max_attempts):
        try:
            scenes = video.get_scene_index(scene_index_id)
            count = len(scenes)
            if count > 0 and count == prev_count:
                stable += 1
                if stable >= 3:
                    logger.info(f"Scene index stable at {count} scenes after {i+1} polls")
                    return scenes
            else:
                stable = 0
            prev_count = count
        except Exception as e:
            logger.warning(f"Scene index poll error (attempt {i+1}): {e}")
        await asyncio.sleep(1.5)
    scenes = video.get_scene_index(scene_index_id)
    logger.warning(f"Scene index poll exhausted — got {len(scenes)} scenes")
    return scenes or []

def _match_events_to_scenes(events: list[dict], scenes: list[dict], start_ms: int, fallback_offset: float = 0.0) -> list[dict]:
    action_events = [event for event in events if event.get("event") == "action"]
    semantic_scene_indices: list[int | None] = [None] * len(action_events)

    cursor = 0
    for idx, event in enumerate(action_events):
        if event.get("action") not in {"type", "select"}:
            continue
        value = str(event.get("value") or "").strip()
        if not value:
            continue
        scene_idx = _find_scene_index_for_value(scenes, value, cursor)
        if scene_idx is not None:
            semantic_scene_indices[idx] = scene_idx
            cursor = max(cursor, scene_idx)

    for idx, event in enumerate(action_events):
        if semantic_scene_indices[idx] is not None:
            continue
        label = event.get("target", {}).get("label")
        next_typed_idx = _next_typed_event_index(action_events, idx, label)
        if next_typed_idx is not None and semantic_scene_indices[next_typed_idx] is not None:
            semantic_scene_indices[idx] = max(0, semantic_scene_indices[next_typed_idx] - 1)
            continue
        prev_idx = _previous_matched_scene_index(semantic_scene_indices, idx)
        if prev_idx is not None:
            semantic_scene_indices[idx] = _next_interaction_scene_index(scenes, prev_idx + 1) or prev_idx

    matched = []
    for idx, event in enumerate(action_events):
        event_time = (event["ts"] - start_ms) / 1000.0
        scene_match = None
        scene_idx = semantic_scene_indices[idx]
        if scene_idx is not None and 0 <= scene_idx < len(scenes):
            scene_match = scenes[scene_idx]
            video_time = (float(scene_match.get("start", 0)) + float(scene_match.get("end", 0))) / 2.0
        else:
            video_time = max(0.0, event_time - fallback_offset)
            scene_match = _find_scene_for_time(scenes, video_time)
        matched.append({
            "event": event,
            "scene_description": scene_match["description"] if scene_match else "(no scene match)",
            "video_time": video_time,
            "event_time": event_time,
            "scene_start": scene_match["start"] if scene_match else None,
            "scene_end": scene_match["end"] if scene_match else None,
        })
    return matched


def _find_scene_index_for_value(scenes: list[dict], value: str, start_index: int = 0) -> int | None:
    normalized_value = _compact_text(value)
    if not normalized_value:
        return None
    candidates = []
    for idx in range(max(0, start_index), len(scenes)):
        desc = str(scenes[idx].get("description") or "")
        compact_desc = _compact_text(desc)
        if normalized_value in compact_desc or (len(normalized_value) <= 3 and "youtube" in compact_desc and normalized_value in "youtube"):
            score = 0
            lowered = desc.lower()
            if "typing" in lowered or "entered" in lowered or "search" in lowered:
                score += 2
            if "autocomplete" in lowered or "suggestion" in lowered:
                score += 1
            candidates.append((score, idx))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1]


def _next_typed_event_index(events: list[dict], index: int, label: str | None) -> int | None:
    for next_index in range(index + 1, len(events)):
        event = events[next_index]
        if event.get("action") == "type":
            if not label or event.get("target", {}).get("label") == label:
                return next_index
        if event.get("action") == "click" and event.get("target", {}).get("label") != label:
            return None
    return None


def _previous_matched_scene_index(scene_indices: list[int | None], index: int) -> int | None:
    for prev_index in range(index - 1, -1, -1):
        if scene_indices[prev_index] is not None:
            return scene_indices[prev_index]
    return None


def _next_interaction_scene_index(scenes: list[dict], start_index: int) -> int | None:
    strong_keywords = (
        "clicked", "clicks", "loading", "loads", "watch page", "opens", "navigat",
    )
    weak_keywords = (
        "results page", "search results", "homepage", "home page",
    )
    first_weak = None
    for idx in range(max(0, start_index), len(scenes)):
        desc = str(scenes[idx].get("description") or "").lower()
        if "terminal" in desc and "youtube" not in desc:
            continue
        if any(keyword in desc for keyword in strong_keywords):
            return idx
        if first_weak is None and any(keyword in desc for keyword in weak_keywords):
            first_weak = idx
    if first_weak is not None:
        return first_weak
    return start_index if start_index < len(scenes) else None


def _compact_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())
def _ground_steps_in_matched_events(skill_json: dict, matched: list[dict]) -> None:
    """Merge LLM steps with event-log ground truth.

    The event log is the source of truth for step count, chronological order,
    timing (recording_ref), and basic action type (click/type/select). The LLM
    provides variable names, semantic target labels, navigate/wait action
    inference, and visual context — these are PRESERVED when present.
    """
    old_steps = [s for s in skill_json.get("steps", []) if isinstance(s, dict)]
    grounded = []

    for index, match in enumerate(matched):
        event = match.get("event", {})
        if event.get("event") != "action":
            continue

        old = old_steps[index] if index < len(old_steps) else {}
        scene_description = str(match.get("scene_description") or "").strip()
        if scene_description.lower() == "(no scene match)":
            scene_description = ""

        video_time = float(match.get("video_time") or 0.0)
        scene_start = match.get("scene_start")
        scene_end = match.get("scene_end")
        if scene_start is None or scene_end is None:
            recording_ref = {"start": video_time, "end": video_time}
        else:
            recording_ref = {"start": float(scene_start), "end": float(scene_end)}

        event_action = _normalize_action(event.get("action"))
        llm_action = old.get("action")
        if llm_action in {"navigate", "wait"}:
            action = llm_action
        else:
            action = event_action

        event_target = _normalize_target(event.get("target"))
        llm_target = old.get("target")
        event_label_is_positional = str(event_target.get("label", "")).startswith("element_at_")
        llm_has_semantic_label = (
            isinstance(llm_target, dict)
            and bool(llm_target.get("label"))
            and not str(llm_target.get("label", "")).startswith("element_at_")
        )
        if llm_has_semantic_label and event_label_is_positional:
            target = llm_target
        else:
            target = event_target

        old_value = old.get("value")
        event_value = event.get("value")
        is_llm_variable = isinstance(old_value, str) and old_value.startswith("{{") and old_value.endswith("}}")
        if is_llm_variable:
            step_value = old_value
        elif event_value is not None:
            step_value = _redact_sensitive_value(event_value)
        else:
            step_value = None

        step = {
            "id": len(grounded) + 1,
            "action": action,
            "target": target,
            "recording_ref": recording_ref,
            "visual_context": _best_step_context(old, scene_description, event, action),
        }
        surface = _normalize_surface(event.get("surface"))
        if surface:
            step["surface"] = surface
        if scene_description:
            step["expected_scene"] = scene_description
        if step_value is not None:
            step["value"] = step_value
        grounded.append(step)

    if grounded:
        skill_json["steps"] = grounded
        skill_json["verification"] = _synthesize_verification(skill_json)


def _best_step_context(old_step: dict, scene_description: str, event: dict, action: str) -> str:
    old_context = str(old_step.get("visual_context") or "").strip()
    if old_context and not _looks_unmatched_or_internal(old_context):
        return old_context
    if scene_description:
        return _summarize_scene_for_step(scene_description, event, action)
    return _fallback_visual_context(action, _normalize_target(event.get("target")))


def _looks_unmatched_or_internal(text: str) -> bool:
    lowered = text.lower()
    return (
        not text.strip()
        or "(no scene match)" in lowered
        or "element_at_" in lowered
        or "terminal-like" in lowered
        or "no clear form field" in lowered
    )


def _summarize_scene_for_step(scene_description: str, event: dict, action: str) -> str:
    cleaned = re.sub(r"\s+", " ", scene_description.replace("**", "")).strip()
    if len(cleaned) > 420:
        cleaned = cleaned[:420].rstrip(" ,;:") + "."
    value = event.get("value")
    if action == "type" and value:
        return f"{cleaned} Type the recorded value into the active text field."
    if action == "click":
        return f"{cleaned} Click the visible control or result that matches this state."
    return cleaned

def _find_scene_for_time(scenes: list[dict], video_time: float) -> dict | None:
    for scene in scenes:
        if scene.get("start", 0) <= video_time <= scene.get("end", 0):
            return scene
    return None


def _estimate_event_scene_offset(events: list[dict], scenes: list[dict], start_ms: int) -> float:
    """Estimate delay between MCP event timestamps and exported video time.

    The event recorder starts before the capture binary has produced the exported
    video. When that startup delay exists, raw event times can exceed the video
    duration, causing every step to match the final scene. Estimate the offset by
    anchoring typed values to scene descriptions that mention those values.
    """
    anchors = []
    for event in events:
        if event.get("event") != "action" or event.get("action") not in {"type", "select"}:
            continue
        value = str(event.get("value") or "").strip().lower()
        if not value:
            continue
        event_time = (event["ts"] - start_ms) / 1000.0
        for scene in scenes:
            desc = str(scene.get("description") or "").lower()
            if value in desc or desc.replace(" ", "") .find(value.replace(" ", "")) >= 0:
                scene_mid = (float(scene.get("start", 0)) + float(scene.get("end", 0))) / 2.0
                anchors.append(event_time - scene_mid)
                break
    if anchors:
        anchors.sort()
        return anchors[len(anchors) // 2]

    max_scene_end = max((float(s.get("end", 0)) for s in scenes), default=0.0)
    event_times = [
        (e["ts"] - start_ms) / 1000.0
        for e in events
        if e.get("event") == "action"
    ]
    if event_times and max(event_times) > max_scene_end and max_scene_end > 0:
        return max(0.0, min(event_times) - scenes[0].get("start", 0))
    return 0.0

def _to_relative_seconds(end_time: float, metadata: dict) -> float:
    """Normalize recording_ref.end to relative seconds from recording start.

    VLM may output one of three formats:
    1. Relative seconds: e.g. 5.967
    2. Absolute epoch milliseconds: e.g. 1782319061217 (> 1e12)
    3. Absolute epoch seconds: e.g. 1782319061.716 (> 1e9, < start_ms)

    Detect and convert to relative seconds.
    """
    start_ms = _effective_start_ms(metadata)
    if end_time > 1000000000000:
        return (end_time - start_ms) / 1000.0
    if start_ms > 0 and end_time > 1000000000:
        start_sec = start_ms / 1000.0
        return end_time - start_sec
    return end_time


def _attach_expected_scenes(skill_json: dict, scenes: list[dict], metadata: dict, event_scene_offset: float = 0.0) -> None:
    max_scene_end = max((s.get("end", 0) for s in scenes), default=0)
    step_refs = [
        _to_relative_seconds(step.get("recording_ref", {}).get("end", 0), metadata)
        for step in skill_json.get("steps", [])
    ]
    # The LLM may output recording_ref values either in event-recorder time or
    # already in video/scene-index time. Only subtract the startup offset when
    # refs extend beyond the indexed video timeline.
    ref_offset = event_scene_offset if step_refs and max(step_refs) > max_scene_end else 0.0
    steps_no_match = 0
    for step in skill_json.get("steps", []):
        ref = step.get("recording_ref", {})
        end_time = max(0.0, _to_relative_seconds(ref.get("end", 0), metadata) - ref_offset)
        best = ""
        for scene in scenes:
            if scene.get("start", 0) <= end_time <= scene.get("end", 0):
                desc = scene.get("description", "")
                if desc:
                    best = desc
                    break
        if not best:
            if end_time > max_scene_end and scenes:
                last_desc = scenes[-1].get("description", "")
                step["expected_scene"] = f"[AFTER RECORDING END] Last visible: {last_desc}"
                steps_no_match += 1
            else:
                step["expected_scene"] = best
            continue
        step["expected_scene"] = best
    if steps_no_match > 0:
        logger.warning(
            f"Recording scenes only cover up to {max_scene_end:.1f}s — "
            f"{steps_no_match} step(s) beyond this range have no expected_scene. "
            f"Re-record the full workflow for complete step-by-step VLM guidance."
        )


def _prefilter_events(events: list[dict], screen_h: int = 1080, screen_w: int = 1920) -> list[dict]:
    noise_values = {"stop", "done", "stop recording", "stoprecording", "stoptherecording"}
    filtered = []
    skip_next = False
    for i, e in enumerate(events):
        if e.get("event") != "action":
            continue
        val = (e.get("value") or "").strip().lower()
        if val and val in noise_values:
            if (e.get("action") in ("type", "select") and filtered and
                    filtered[-1].get("target", {}).get("label") == e.get("target", {}).get("label")):
                filtered.pop()
            skip_next = False
            continue
        pos = e.get("position") or _parse_position_from_label(e.get("target", {}).get("label", ""))
        if pos:
            x, y = pos.get("x", 0), pos.get("y", 0)
            if y > screen_h - 40:
                continue
            if y < 25:
                continue
            if x > screen_w - 40:
                continue
        filtered.append(e)
    return filtered


def _effective_start_ms(metadata: dict) -> int:
    return int(
        metadata.get("effective_recording_start_epoch_ms")
        or metadata.get("recording_start_epoch_ms")
        or 0
    )


def _effective_end_ms(metadata: dict) -> int:
    return int(
        metadata.get("effective_recording_end_epoch_ms")
        or metadata.get("recording_end_epoch_ms")
        or 0
    )


def _trim_events_to_effective_start(events: list[dict], effective_start_ms: int) -> list[dict]:
    return _trim_events_to_effective_window(events, effective_start_ms, 0)


def _trim_events_to_effective_window(events: list[dict], effective_start_ms: int, effective_end_ms: int = 0) -> list[dict]:
    if not effective_start_ms:
        effective_start_ms = 0
    return [
        event
        for event in events
        if event.get("ts", 0) >= effective_start_ms
        and (not effective_end_ms or event.get("ts", 0) <= effective_end_ms)
    ]


def _parse_position_from_label(label: str) -> dict | None:
    m = re.match(r"element_at_(\d+)_(\d+)", str(label))
    if m:
        return {"x": int(m.group(1)), "y": int(m.group(2))}
    return None


def _build_events_only_prompt(name: str, events: list[dict], metadata: dict) -> str:
    import compiler.prompts as p
    start_ms = _effective_start_ms(metadata)
    events_text = "\n".join(
        json.dumps({"ts": round((e["ts"] - start_ms) / 1000.0, 3),
                     "action": e["action"], "target": e.get("target"),
                     "value": e.get("value"), "surface": e.get("surface")})
        for e in events
    )
    return p.build_user_prompt(
        name, events_text,
        [{"event": e, "scene_description": "(no video — events only)",
          "video_time": (e["ts"] - start_ms) / 1000.0,
          "scene_start": None, "scene_end": None} for e in events],
        "(no narration)"
    )


def _normalize_llm_output(skill: dict, name: str) -> dict:
    if not isinstance(skill, dict):
        skill = {}

    skill["name"] = _slugify_skill_name(skill.get("name") or name)
    if not skill.get("description"):
        skill["description"] = f"Automated skill: {skill['name'].replace('-', ' ')}"

    if not isinstance(skill.get("version"), int):
        try:
            skill["version"] = int(float(skill.get("version", 1)))
        except (TypeError, ValueError):
            skill["version"] = 1

    if "variables" in skill and "inputs" not in skill:
        skill["inputs"] = skill.pop("variables")
    skill["inputs"] = _normalize_inputs(skill.get("inputs", {}))

    preconditions = skill.get("preconditions")
    if isinstance(preconditions, str):
        preconditions = [preconditions]
    skill["preconditions"] = preconditions or ["Application is open and ready"]
    skill["start_context"] = _normalize_start_context(
        skill.get("start_context"),
        skill["preconditions"],
    )
    skill["execution_strategy"] = _normalize_execution_strategy(
        skill.get("execution_strategy"),
        skill["start_context"],
    )
    recorded_surface = _normalize_surface(skill.get("recorded_surface"))
    if recorded_surface:
        skill["recorded_surface"] = recorded_surface

    normalized_steps = []
    for i, raw_step in enumerate(skill.get("steps", []) or []):
        if not isinstance(raw_step, dict):
            continue
        step = dict(raw_step)
        action = _normalize_action(step.get("action") or step.get("type"))
        target = _normalize_target(step.get("target"))
        recording_ref = _normalize_recording_ref(step.get("recording_ref") or step.get("time"), i)

        try:
            step_id = int(step.get("id", i + 1))
        except (TypeError, ValueError):
            step_id = i + 1

        expected = step.get("expected_scene") or ""
        visual_context = step.get("visual_context") or expected
        if not visual_context or str(visual_context).strip().lower() == "(no scene match)":
            visual_context = _fallback_visual_context(action, target)

        clean_step = {
            "id": step_id,
            "action": action,
            "target": target,
            "recording_ref": recording_ref,
            "visual_context": str(visual_context),
        }
        if expected and str(expected).strip().lower() != "(no scene match)":
            clean_step["expected_scene"] = str(expected)
        if "value" in step:
            clean_step["value"] = _redact_sensitive_value(step.get("value"))
        surface = _normalize_surface(step.get("surface"))
        if surface:
            clean_step["surface"] = surface
        normalized_steps.append(clean_step)

    skill["steps"] = normalized_steps
    _augment_standalone_inputs(skill)

    norm_verification = []
    verifications_sources = []
    if "verifications" in skill:
        verifications_sources.extend(skill["verifications"])
    if "verification" in skill:
        verifications_sources.extend(skill["verification"])
    for raw_check in verifications_sources:
        if isinstance(raw_check, str):
            check_type = "ax_element"
            check = raw_check
        elif isinstance(raw_check, dict):
            check_type = raw_check.get("type", "ax_element")
            if check_type == "element_present":
                check_type = "ax_element"
            if check_type == "primary":
                check_type = "visual"
            if check_type == "secondary":
                check_type = "ax_element"
            if check_type == "fallback":
                check_type = "ax_element"
            if check_type not in {"ax_element", "visual", "transcript"}:
                check_type = "ax_element"
            check = raw_check.get("check") or raw_check.get("description") or raw_check.get("assertion") or raw_check.get("how_to_check") or ""
        else:
            continue

        check = str(check).strip()
        if check and not _is_generic_verification_check(check):
            norm_verification.append({"type": check_type, "check": check})

    skill.pop("verifications", None)
    skill["verification"] = norm_verification or _synthesize_verification(skill)

    return skill


def _augment_standalone_inputs(skill: dict) -> None:
    """Add generic inputs needed for a standalone skill when the recording implies them."""
    inputs = skill.setdefault("inputs", {})
    text = _skill_search_text(skill)

    upload_terms = ("upload", "attach", "file picker", "open button", "choose file", "select file")
    if any(term in text for term in upload_terms):
        if "file_path" not in inputs:
            inputs["file_path"] = {
                "type": "string",
                "example": "/path/to/file.ext",
                "description": "Full local path of the file to upload or attach. Provide this at run time; do not hardcode a recorded path.",
            }

    conversation_terms = ("channel", "direct message", " dm ", "conversation", "composer", "chat", "message")
    if any(term in text for term in conversation_terms):
        if "target_conversation" not in inputs:
            inputs["target_conversation"] = {
                "type": "string",
                "example": "channel, chat, recipient, or conversation name",
                "description": "Destination channel, direct message, recipient, chat, thread, or conversation where the file/message should be sent.",
            }


def _skill_search_text(skill: dict) -> str:
    parts = [
        str(skill.get("name", "")),
        str(skill.get("description", "")),
        " ".join(str(item) for item in skill.get("preconditions", []) if item),
    ]
    for key, spec in (skill.get("inputs") or {}).items():
        parts.append(str(key))
        if isinstance(spec, dict):
            parts.extend(str(spec.get(field, "")) for field in ("description", "example", "format"))
    for step in skill.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        parts.extend(
            str(step.get(field, ""))
            for field in ("visual_context", "expected_scene", "value", "action")
        )
        target = step.get("target")
        if isinstance(target, dict):
            parts.extend(str(target.get(field, "")) for field in ("label", "type"))
    return " ".join(parts).lower()


def _is_generic_verification_check(check: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", check.lower()).strip()
    generic_checks = {
        "task completed successfully",
        "completed successfully",
        "workflow completed successfully",
        "success",
        "done",
        "task complete",
    }
    return normalized in generic_checks or len(normalized) < 8


def _synthesize_verification(skill: dict) -> list[dict]:
    checks = []

    for step in reversed(skill.get("steps", []) or []):
        action = step.get("action")
        value = step.get("value")
        if action in {"type", "select"} and value and value != "[REDACTED]":
            value_text = str(value)
            if value_text.startswith("{{") and value_text.endswith("}}"):
                variable = value_text.strip("{}")
                if action == "select":
                    checks.append({"type": "ax_element", "check": f"Verify the `{variable}` option is selected or visibly applied."})
                else:
                    checks.append({"type": "ax_element", "check": f"Verify the field for `{variable}` contains the entered value."})
            elif action == "select":
                checks.append({"type": "ax_element", "check": f"Verify '{value_text}' is selected or visibly applied."})
            else:
                checks.append({"type": "ax_element", "check": f"Verify the field contains '{value_text}'."})
            if len(checks) >= 2:
                break

    status_keywords = (
        "selected", "saved", "confirmed", "submitted", "complete", "uploaded",
        "uploading", "progress", "visible", "shows", "appears", "title",
        "filename", "modal", "dialog", "radio", "checked", "step", "status",
    )
    for step in reversed(skill.get("steps", []) or []):
        context = str(step.get("expected_scene") or step.get("visual_context") or "").strip()
        if not context or context.lower() == "(no scene match)":
            continue
        sentence = _best_verification_sentence(context, status_keywords)
        if sentence:
            check = f"Verify {sentence}"
            if all(existing["check"] != check for existing in checks):
                checks.append({"type": "ax_element", "check": check})
        if len(checks) >= 3:
            break

    if checks:
        return checks[:3]
    return [{"type": "ax_element", "check": "Verify the final screen still shows the expected workflow state from the last recorded step."}]


def _best_verification_sentence(context: str, keywords: tuple[str, ...]) -> str:
    cleaned = re.sub(r"\s+", " ", context.replace("**", "")).strip()
    parts = re.split(r"(?<=[.!?])\s+|\n+| - ", cleaned)
    for part in parts:
        part = part.strip(" -")
        if not part:
            continue
        lowered = part.lower()
        if any(keyword in lowered for keyword in keywords):
            part = part[0].lower() + part[1:] if part[:1].isupper() else part
            return part[:220].rstrip(" ,;:") + "."
    if cleaned:
        cleaned = cleaned[0].lower() + cleaned[1:] if cleaned[:1].isupper() else cleaned
        return cleaned[:220].rstrip(" ,;:") + "."
    return ""

def _slugify_skill_name(value: object) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value).lower().replace("_", "-"))
    return slug.strip("-") or "recorded-skill"


def _normalize_action(action: object) -> str:
    action = str(action or "click").lower().strip()
    aliases = {
        "input": "type",
        "enter": "type",
        "text": "type",
        "dropdown": "select",
        "choose": "select",
        "open": "click",
        "press": "click",
    }
    action = aliases.get(action, action)
    return action if action in {"click", "type", "select", "navigate", "wait"} else "click"


def _normalize_target(target: object) -> dict:
    if isinstance(target, str):
        return {"type": "element", "label": target}
    if not isinstance(target, dict):
        return {"type": "element", "label": "target element"}
    target_type = target.get("type") or target.get("role") or target.get("kind") or "element"
    label = target.get("label") or target.get("name") or target.get("text") or target.get("id") or "target element"
    return {"type": str(target_type), "label": str(label)}


def _normalize_recording_ref(ref: object, index: int) -> dict:
    if not isinstance(ref, dict):
        return {"start": float(index), "end": float(index + 1)}
    try:
        start = float(ref.get("start", index))
    except (TypeError, ValueError):
        start = float(index)
    try:
        end = float(ref.get("end", start + 1.0))
    except (TypeError, ValueError):
        end = start + 1.0
    start = max(0.0, start)
    end = max(start, end)
    return {"start": start, "end": end}


def _fallback_visual_context(action: str, target: dict) -> str:
    label = target.get("label", "target element")
    target_type = target.get("type", "element")
    if label.startswith("element_at_"):
        return f"The {target_type} used for the {action} action in the recorded workflow."
    return f"The {target_type} labeled '{label}' used for the {action} action."


def _normalize_start_context(start_context: object, preconditions: list[str]) -> dict:
    if not isinstance(start_context, dict):
        instructions = "; ".join(str(p) for p in preconditions if p) or "Application is open and ready"
        return {
            "kind": "unknown",
            "label": "Starting application state",
            "instructions": instructions,
            "evidence": "No structured start context was produced during compilation.",
        }

    allowed_kinds = {
        "web",
        "desktop_app",
        "file",
        "terminal",
        "workspace",
        "screen_state",
        "unknown",
    }
    kind = str(start_context.get("kind") or "unknown").strip().lower()
    aliases = {
        "website": "web",
        "browser": "web",
        "app": "desktop_app",
        "application": "desktop_app",
        "desktop": "desktop_app",
        "page": "screen_state",
        "screen": "screen_state",
    }
    kind = aliases.get(kind, kind)
    if kind not in allowed_kinds:
        kind = "unknown"

    label = str(start_context.get("label") or "Starting application state").strip()
    instructions = str(start_context.get("instructions") or "").strip()
    if not instructions:
        instructions = "; ".join(str(p) for p in preconditions if p) or "Application is open and ready"

    normalized = {
        "kind": kind,
        "label": label,
        "instructions": instructions,
    }
    locator = str(start_context.get("locator") or "").strip()
    if locator:
        normalized["locator"] = locator
    evidence = str(start_context.get("evidence") or "").strip()
    if evidence:
        normalized["evidence"] = evidence
    return normalized


def _attach_step_surfaces_from_events(skill_json: dict, events: list[dict]) -> None:
    steps = [step for step in skill_json.get("steps", []) if isinstance(step, dict)]
    action_events = [event for event in events if isinstance(event, dict) and event.get("event") == "action"]
    for step, event in zip(steps, action_events):
        surface = _normalize_surface(event.get("surface"))
        if surface:
            step["surface"] = surface


def _attach_recorded_surfaces(skill_json: dict) -> None:
    existing = _normalize_surface(skill_json.get("recorded_surface"))
    if existing:
        skill_json["recorded_surface"] = existing
        return

    surfaces = [
        _normalize_surface(step.get("surface"))
        for step in skill_json.get("steps", [])
        if isinstance(step, dict)
    ]
    surfaces = [surface for surface in surfaces if surface]
    if not surfaces:
        return

    skill_json["recorded_surface"] = _most_common_surface(surfaces)


def _most_common_surface(surfaces: list[dict]) -> dict:
    counts = {}
    for surface in surfaces:
        key = (
            surface.get("platform", ""),
            surface.get("app_name", ""),
            surface.get("process_id", 0),
            surface.get("window_title", ""),
        )
        counts[key] = counts.get(key, 0) + 1
    best_key = max(counts, key=counts.get)
    for surface in surfaces:
        key = (
            surface.get("platform", ""),
            surface.get("app_name", ""),
            surface.get("process_id", 0),
            surface.get("window_title", ""),
        )
        if key == best_key:
            return dict(surface)
    return dict(surfaces[0])


def _normalize_surface(surface: object) -> dict:
    if not isinstance(surface, dict):
        return {}

    normalized = {}
    for key in ("platform", "app_name", "window_title"):
        value = str(surface.get(key) or "").strip()
        if value:
            normalized[key] = value

    try:
        process_id = int(surface.get("process_id", 0) or 0)
    except (TypeError, ValueError):
        process_id = 0
    if process_id:
        normalized["process_id"] = process_id

    bounds = _normalize_int_rect(surface.get("window_bounds"))
    if bounds:
        normalized["window_bounds"] = bounds

    relative = _normalize_int_point(surface.get("relative_position"))
    if relative:
        normalized["relative_position"] = relative

    return normalized


def _normalize_int_rect(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    keys = ("x", "y", "width", "height")
    try:
        return {key: int(value[key]) for key in keys if key in value}
    except (TypeError, ValueError):
        return {}


def _normalize_int_point(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    try:
        return {"x": int(value["x"]), "y": int(value["y"])}
    except (KeyError, TypeError, ValueError):
        return {}


def _normalize_execution_strategy(strategy: object, start_context: dict) -> dict:
    inferred_surface = _surface_from_start_context(start_context)
    if not isinstance(strategy, dict):
        return _default_execution_strategy(inferred_surface)

    raw_surface = str(strategy.get("surface") or inferred_surface).strip().lower()
    surface_aliases = {
        "web": "web_browser",
        "browser": "web_browser",
        "website": "web_browser",
        "desktop": "desktop_app",
        "native": "desktop_app",
        "app": "desktop_app",
        "application": "desktop_app",
        "file": "file_system",
        "filesystem": "file_system",
        "screen_state": inferred_surface,
        "workspace": inferred_surface,
    }
    surface = surface_aliases.get(raw_surface, raw_surface)
    allowed_surfaces = {
        "web_browser",
        "desktop_app",
        "hybrid",
        "terminal",
        "file_system",
        "unknown",
    }
    if surface not in allowed_surfaces:
        surface = inferred_surface

    defaults = _default_execution_strategy(surface)
    preferred_tools = _normalize_tool_list(
        strategy.get("preferred_tools") or strategy.get("preferred") or strategy.get("tools"),
        defaults["preferred_tools"],
    )
    fallback_tools = _normalize_tool_list(
        strategy.get("fallback_tools") or strategy.get("fallback"),
        defaults["fallback_tools"],
    )
    notes = _normalize_string_list(strategy.get("notes"), defaults["notes"])

    return {
        "surface": surface,
        "preferred_tools": preferred_tools,
        "fallback_tools": fallback_tools,
        "notes": notes,
    }


def _surface_from_start_context(start_context: dict) -> str:
    kind = str((start_context or {}).get("kind") or "unknown").strip().lower()
    mapping = {
        "web": "web_browser",
        "desktop_app": "desktop_app",
        "terminal": "terminal",
        "file": "file_system",
        "workspace": "unknown",
        "screen_state": "unknown",
        "unknown": "unknown",
    }
    return mapping.get(kind, "unknown")


def _default_execution_strategy(surface: str) -> dict:
    defaults = {
        "web_browser": {
            "preferred_tools": ["native_accessibility"],
            "fallback_tools": ["visual_computer_use"],
            "notes": [
                "Replay the recorded visible browser app directly with native desktop automation; do not switch to another browser or app unless the user approves.",
                "Use native accessibility and system commands for the existing browser window, with visual computer-use as fallback.",
                "Do not use any separate browser automation session for normal replay.",
            ],
        },
        "desktop_app": {
            "preferred_tools": ["native_accessibility"],
            "fallback_tools": ["visual_computer_use"],
            "notes": [
                "Use platform-native accessibility controls for desktop app windows and OS UI.",
                "Use macOS Accessibility API / AX on macOS, UI Automation / UIA on Windows, and AT-SPI/accessibility APIs on Linux.",
            ],
        },
        "hybrid": {
            "preferred_tools": ["native_accessibility"],
            "fallback_tools": ["visual_computer_use"],
            "notes": [
                "Replay the recorded visible app/browser directly with native desktop automation; do not switch to another app or browser unless the user approves.",
                "Use native accessibility across browser, desktop, file picker, and OS-dialog steps.",
                "On macOS, use osascript/System Events, AX inspection, Finder clipboard file paste, keyboard shortcuts, screencapture, and visual checks for browser plus OS-dialog workflows.",
            ],
        },
        "terminal": {
            "preferred_tools": ["terminal"],
            "fallback_tools": ["native_accessibility"],
            "notes": [
                "Use shell commands for terminal workflows and verify command output before continuing.",
            ],
        },
        "file_system": {
            "preferred_tools": ["file_system"],
            "fallback_tools": ["native_accessibility", "visual_computer_use"],
            "notes": [
                "Use file-system operations for direct file changes and native accessibility for file pickers or Finder/Explorer dialogs.",
            ],
        },
        "unknown": {
            "preferred_tools": ["native_accessibility"],
            "fallback_tools": ["visual_computer_use"],
            "notes": [
                "Start with structured native accessibility when available, then fall back to visual computer-use if the surface cannot be identified.",
            ],
        },
    }
    selected = defaults.get(surface, defaults["unknown"])
    return {
        "surface": surface if surface in defaults else "unknown",
        "preferred_tools": list(selected["preferred_tools"]),
        "fallback_tools": list(selected["fallback_tools"]),
        "notes": list(selected["notes"]),
    }


def _normalize_tool_list(value: object, fallback: list[str]) -> list[str]:
    normalized = _normalize_string_list(value, fallback)
    aliases = {
        "browser": "native_accessibility",
        "browser_automation": "native_accessibility",
        "browser_use": "native_accessibility",
        "cdp": "native_accessibility",
        "selenium": "native_accessibility",
        "computer_use": "visual_computer_use",
        "computer-use": "visual_computer_use",
        "accessibility": "native_accessibility",
        "ax": "native_accessibility",
        "uia": "native_accessibility",
        "ui_automation": "native_accessibility",
    }
    result = []
    for item in normalized:
        key = re.sub(r"[^a-z0-9]+", "_", item.lower()).strip("_")
        result.append(aliases.get(key, key))
    return _dedupe_preserve_order(result) or list(fallback)


def _normalize_string_list(value: object, fallback: list[str]) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return list(fallback)
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    return _dedupe_preserve_order(cleaned) or list(fallback)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _redact_sensitive_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    sensitive = [
        r"sk-[A-Za-z0-9_-]+",
        r"Bearer\s+[A-Za-z0-9._-]+",
        r"eyJ[A-Za-z0-9._-]+",
        r"\b\d{13,19}\b",
        r"\b\d{3}-\d{2}-\d{4}\b",
    ]
    if any(re.search(pattern, value) for pattern in sensitive):
        return "[REDACTED]"
    return value

def _normalize_inputs(inputs) -> dict:
    normalized = {}
    if isinstance(inputs, dict) and not isinstance(inputs, list):
        iterable = inputs.items()
    elif isinstance(inputs, list):
        iterable = []
        for item in inputs:
            if isinstance(item, dict):
                key = item.get("name") or item.get("key") or str(len(iterable) + 1)
                iterable.append((key, item))
            else:
                iterable.append((str(item), {"type": "string", "example": str(item)}))
    else:
        iterable = []

    for key, spec in iterable:
        input_name = re.sub(r"[^a-zA-Z0-9_]+", "_", str(key)).strip("_") or f"input_{len(normalized) + 1}"
        if not isinstance(spec, dict):
            spec = {"type": "string", "example": str(spec)}
        input_type = str(spec.get("type", "string")).lower()
        if input_type not in {"string", "number", "enum"}:
            input_type = "string"
        clean_spec = {
            "type": input_type,
            "example": spec.get("example", ""),
        }
        if spec.get("format"):
            clean_spec["format"] = str(spec["format"])
        if spec.get("description"):
            clean_spec["description"] = str(spec["description"])
        values = spec.get("values")
        if values:
            clean_spec["values"] = [str(value) for value in values]
        normalized[input_name] = clean_spec
    return normalized
