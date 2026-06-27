"""Tests for compiler/tool_manifest.py."""

from pathlib import Path

from compiler.tool_manifest import (
    FALLBACK_MANIFEST,
    load_tool_manifest,
    surface_tool_guidance,
    tool_details,
)


def test_loads_default_tool_manifest():
    manifest = load_tool_manifest()

    assert manifest["version"] == 1
    assert "playwright" in manifest["tools"]
    assert manifest["surfaces"]["web_browser"]["preferred_tools"] == ["playwright"]
    assert manifest["surfaces"]["desktop_app"]["preferred_tools"] == ["native_accessibility"]


def test_invalid_manifest_uses_fallback(tmp_path: Path):
    bad_manifest = tmp_path / "recommended_tools.json"
    bad_manifest.write_text('{"tools": []}')

    manifest = load_tool_manifest(bad_manifest)

    assert manifest["surfaces"] == FALLBACK_MANIFEST["surfaces"]


def test_surface_tool_guidance_falls_back_to_unknown():
    guidance = surface_tool_guidance("not-real", FALLBACK_MANIFEST)

    assert guidance["surface"] == "unknown"
    assert guidance["preferred_tools"] == ["native_accessibility"]


def test_tool_details_include_platform_guidance():
    details = tool_details(["native_accessibility"], FALLBACK_MANIFEST)

    assert details[0]["name"] == "native_accessibility"
    assert details[0]["platforms"]["macos"] == "Accessibility API / AX"
    assert details[0]["platforms"]["windows"] == "UI Automation / UIA"
