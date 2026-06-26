"""Tests for compiler/md_generator.py."""

from compiler.md_generator import (
    _append_self_improvement_section,
    _template_fallback,
)


def test_appends_self_improvement_section():
    content = """---
name: example
description: "Example skill"
---

# Example

## Steps
1. Do the task.
"""

    result = _append_self_improvement_section(content)

    assert "## Continuous Improvement" in result
    assert "Complete the user's requested task first" in result
    assert "Do not add secrets" in result


def test_does_not_duplicate_self_improvement_section():
    content = """# Example

## Continuous Improvement
- Existing guidance.
"""

    result = _append_self_improvement_section(content)

    assert result.count("## Continuous Improvement") == 1
    assert "Existing guidance." in result


def test_template_fallback_includes_self_improvement_section():
    skill = {
        "name": "example-skill",
        "description": "Example skill",
        "preconditions": [],
        "inputs": {},
        "steps": [],
        "verification": [],
    }

    result = _template_fallback(skill)

    assert "## Continuous Improvement" in result
    assert "Keep edits concise" in result
