"""Skill registry management: load, save, version, index."""

import json
from datetime import datetime, timezone
from pathlib import Path

from compiler.md_generator import generate_skill_md
from config import SKILLS_ROOT


def load_registry() -> dict:
    registry_path = SKILLS_ROOT / "registry.json"
    if registry_path.exists():
        return json.loads(registry_path.read_text())
    return {"skills": {}}


def save_registry(reg: dict):
    (SKILLS_ROOT / "registry.json").write_text(json.dumps(reg, indent=2))


def load_skill(name: str) -> dict:
    skill_path = SKILLS_ROOT / name / "SKILL.json"
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill '{name}' not found")
    return json.loads(skill_path.read_text())


def save_skill(skill: dict) -> Path:
    name = skill["name"]
    skill_dir = SKILLS_ROOT / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    registry = load_registry()
    existing = registry["skills"].get(name)

    if existing:
        new_version = existing["version"] + 1
        old = skill_dir / "SKILL.json"
        if old.exists():
            old.rename(skill_dir / f"SKILL.v{existing['version']}.json")
        created_at = existing["created_at"]
    else:
        new_version = 1
        created_at = datetime.now(timezone.utc).isoformat()

    skill["version"] = new_version
    skill_path = skill_dir / "SKILL.json"
    skill_path.write_text(json.dumps(skill, indent=2))

    registry["skills"][name] = {
        "name": name,
        "description": skill.get("description", ""),
        "created_at": created_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "version": new_version,
        "video_id": skill["video_id"],
        "path": f"{name}/SKILL.json",
    }
    save_registry(registry)

    return skill_path


def list_skills() -> list[dict]:
    registry = load_registry()
    return list(registry["skills"].values())


async def save_skill_md(skill: dict) -> Path:
    md = await generate_skill_md(skill)
    path = SKILLS_ROOT / skill["name"] / "SKILL.md"
    path.write_text(md, encoding="utf-8")
    return path


def delete_skill(name: str):
    registry = load_registry()
    if name not in registry["skills"]:
        raise FileNotFoundError(f"Skill '{name}' not found")
    import shutil
    shutil.rmtree(SKILLS_ROOT / name)
    del registry["skills"][name]
    save_registry(registry)
