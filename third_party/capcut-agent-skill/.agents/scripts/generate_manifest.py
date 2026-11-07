#!/usr/bin/env python3
"""
Manifest Generator for AG Kit (Version 2026.9.0).
Scans tracked kit files in .agents/, computes SHA-256 hashes, byte sizes,
and line counts, and atomically generates manifest.json and manifest.lock.json.
"""

import datetime
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_agents_root() -> Path:
    """Dynamically resolves the .agents directory root."""
    return Path(__file__).resolve().parent.parent


def compute_sha256(file_path: Path) -> str:
    """Computes SHA-256 checksum of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def count_lines(file_path: Path) -> int:
    """Counts lines in a text file."""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        return sum(1 for _ in f)


def parse_frontmatter(file_path: Path) -> Dict[str, Any]:
    """Extracts basic YAML frontmatter from a markdown file."""
    meta: Dict[str, Any] = {}
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    if not lines or lines[0].strip() != "---":
        return meta

    fm_lines = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        fm_lines.append(line)

    # Try PyYAML if present, otherwise parse simple key/values
    fm_text = "".join(fm_lines)
    try:
        import yaml
        parsed = yaml.safe_load(fm_text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # Simple fallback parser
    cur_list_key = None
    for line in fm_lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- ") and cur_list_key:
            item = s[2:].strip().strip("\"'")
            meta[cur_list_key].append(item)
        elif ":" in s:
            k, v = s.split(":", 1)
            k = k.strip()
            v = v.strip().strip("\"'")
            if v == "":
                meta[k] = []
                cur_list_key = k
            else:
                meta[k] = v
                cur_list_key = None
    return meta


def build_manifests() -> None:
    root = get_agents_root()
    tracked_dirs = ["agent", "rules", "scripts", "skills", "workflows"]
    ignored_patterns = ["__pycache__", ".DS_Store", ".tmp", ".bak", "teamwork_preview_", "sentinel"]

    files_lock: Dict[str, Dict[str, Any]] = {}
    for d_name in tracked_dirs:
        dir_path = root / d_name
        if not dir_path.exists():
            continue
        for p in dir_path.rglob("*"):
            if not p.is_file():
                continue
            # Check ignore patterns
            rel_str = str(p.relative_to(root))
            if any(ign in rel_str for ign in ignored_patterns):
                continue

            sha = compute_sha256(p)
            size = p.stat().st_size
            mtime = datetime.datetime.fromtimestamp(
                p.stat().st_mtime, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

            entry: Dict[str, Any] = {
                "sha256": sha,
                "size_bytes": size,
                "modified_time": mtime
            }
            if p.suffix == ".md":
                entry["line_count"] = count_lines(p)

            files_lock[rel_str] = entry

    # Composite bundle checksum
    sorted_paths = sorted(files_lock.keys())
    composite_text = "".join(f"{path}:{files_lock[path]['sha256']}\n" for path in sorted_paths)
    bundle_checksum = hashlib.sha256(composite_text.encode("utf-8")).hexdigest()

    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lock_doc = {
        "schema_version": "2026.9.0",
        "generated_at": now_utc,
        "algorithm": "sha256",
        "bundle_checksum": bundle_checksum,
        "files": files_lock
    }

    # Manifest Inventory Doc
    skills_list = []
    workflows_list = []
    agents_list = []
    rules_list = []
    scripts_list = []

    # Skill discovery
    skill_dir = root / "skills" / "capcut-editor"
    if skill_dir.exists():
        skills_list.append({
            "name": "capcut-editor",
            "path": "skills/capcut-editor",
            "entry_point": "skills/capcut-editor/scripts/capcut_draft.py",
            "skill_md": "skills/capcut-editor/SKILL.md",
            "references": [
                "skills/capcut-editor/references/draft_schema.md",
                "skills/capcut-editor/references/text_styles.md",
                "skills/capcut-editor/references/audio_guide.md"
            ]
        })

    # Workflow discovery
    wf_file = root / "workflows" / "capcut.md"
    if wf_file.exists():
        workflows_list.append({
            "name": "capcut",
            "slash_command": "/capcut",
            "path": "workflows/capcut.md",
            "requires_agents": ["video-editor"],
            "requires_skills": ["capcut-editor"],
            "artifact_outputs": ["outputs/renders/", "outputs/test/"]
        })

    # Agent discovery
    for a_file in sorted((root / "agent").glob("*.md")):
        name = a_file.stem
        role = "specialist" if name == "video-editor" else "coordinator"
        agents_list.append({
            "name": name,
            "path": f"agent/{a_file.name}",
            "role": role
        })

    # Rule discovery
    for r_file in sorted((root / "rules").glob("*.md")):
        rules_list.append({
            "name": r_file.stem,
            "path": f"rules/{r_file.name}"
        })

    # Script discovery
    scripts_meta = {
        "generate_manifest": "Scans .agents/ hierarchy and atomically regenerates manifest.json and manifest.lock.json.",
        "validate_kit": "Pre-flight structural and cryptographic validation against the 8 Ironclad Rules."
    }
    for s_file in sorted((root / "scripts").glob("*.py")):
        scripts_list.append({
            "name": s_file.stem,
            "path": f"scripts/{s_file.name}",
            "description": scripts_meta.get(s_file.stem, "Toolkit automation script.")
        })

    manifest_doc = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "name": "capcut-agkit",
        "version": "2026.9.0",
        "description": "Modular AG Kit for CapCut Desktop video editing automation and Shorts Hook production.",
        "architecture_version": "2026.9.0",
        "skills": skills_list,
        "workflows": workflows_list,
        "agents": agents_list,
        "rules": rules_list,
        "scripts": scripts_list
    }

    # Atomically write manifest.json
    manifest_tmp = root / "manifest.json.tmp"
    with open(manifest_tmp, "w", encoding="utf-8") as f:
        json.dump(manifest_doc, f, indent=2, ensure_ascii=False)
    os.replace(manifest_tmp, root / "manifest.json")

    # Atomically write manifest.lock.json
    lock_tmp = root / "manifest.lock.json.tmp"
    with open(lock_tmp, "w", encoding="utf-8") as f:
        json.dump(lock_doc, f, indent=2, ensure_ascii=False)
    os.replace(lock_tmp, root / "manifest.lock.json")

    print("[OK] Generated manifest.json and manifest.lock.json")


def main() -> int:
    try:
        build_manifests()
        return 0
    except Exception as exc:
        print(f"[FAIL] generate_manifest failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
