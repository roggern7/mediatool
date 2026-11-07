#!/usr/bin/env python3
"""
Pre-flight Deterministic Validator for AG Kit (Version 2026.9.0).
Executes all 8 deterministic verification checks (V1 through V8)
in conformance with the 8 Ironclad Rules of AG Kit.

Exit code 0 on complete pass with verbatim output:
[PASS] Toolkit is structurally valid.
Exit code 1 on any check failure.
"""

import ast
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


def get_agents_root() -> Path:
    """Dynamically resolves the .agents directory root."""
    return Path(__file__).resolve().parent.parent


def parse_frontmatter(file_path: Path) -> Dict[str, Any]:
    """Extracts YAML frontmatter from a markdown file."""
    meta: Dict[str, Any] = {}
    if not file_path.exists():
        return meta

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    if not lines or lines[0].strip() != "---":
        return meta

    fm_lines = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        fm_lines.append(line)

    fm_text = "".join(fm_lines)
    try:
        import yaml
        parsed = yaml.safe_load(fm_text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # Fallback basic parser
    cur_list = None
    for line in fm_lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- ") and cur_list is not None:
            meta[cur_list].append(s[2:].strip().strip("\"'"))
        elif ":" in s:
            k, v = s.split(":", 1)
            k = k.strip()
            v = v.strip().strip("\"'")
            if not v:
                meta[k] = []
                cur_list = k
            else:
                meta[k] = v
                cur_list = None
    return meta


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def check_v1_layout(root: Path) -> List[str]:
    """V1: Structural Layout check."""
    errors = []
    required_relative_paths = [
        "manifest.json",
        "manifest.lock.json",
        "agent/orchestrator.md",
        "agent/video-editor.md",
        "rules/8-ironclad-rules.md",
        "rules/capcut-conventions.md",
        "scripts/generate_manifest.py",
        "scripts/validate_kit.py",
        "skills/capcut-editor/SKILL.md",
        "skills/capcut-editor/scripts/capcut_draft.py",
        "skills/capcut-editor/references/draft_schema.md",
        "skills/capcut-editor/references/text_styles.md",
        "skills/capcut-editor/references/audio_guide.md",
        "workflows/capcut.md"
    ]
    for rel in required_relative_paths:
        target = root / rel
        if not target.exists():
            errors.append(f"Required file missing: {rel}")
    return errors


def check_v2_line_limit(root: Path) -> List[str]:
    """V2: Rule 7 Line Limit check (SKILL.md < 500 lines)."""
    errors = []
    for skill_md in root.glob("skills/**/SKILL.md"):
        with open(skill_md, "r", encoding="utf-8", errors="ignore") as f:
            count = sum(1 for _ in f)
        if count >= 500:
            rel = skill_md.relative_to(root)
            errors.append(f"{rel} line count is {count} (exceeds limit of 500 lines).")
    return errors


def check_v3_relative_paths(root: Path) -> List[str]:
    """V3: Rule 3 Relative Paths check."""
    errors = []
    # Dynamic pieces to prevent self-matching
    slash = "/"
    forbidden_prefixes = [
        slash + "Users" + slash,
        slash + "home" + slash,
        "C:" + "\\",
        slash + "private" + slash
    ]

    tracked_dirs = ["agent", "rules", "scripts", "skills", "workflows"]
    allowed_exts = {".md", ".py", ".json", ".yaml", ".yml"}
    ignored_patterns = ["__pycache__", ".DS_Store", "teamwork_preview_", "sentinel"]

    for d in tracked_dirs:
        dir_path = root / d
        if not dir_path.exists():
            continue
        for p in dir_path.rglob("*"):
            if not p.is_file() or p.suffix not in allowed_exts:
                continue
            rel = str(p.relative_to(root))
            if any(ign in rel for ign in ignored_patterns):
                continue

            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for line_idx, line in enumerate(f, 1):
                    # Exclude the forbidden_prefixes definition line in validator itself
                    if "forbidden_prefixes" in line and p.name == "validate_kit.py":
                        continue
                    for prefix in forbidden_prefixes:
                        if prefix in line:
                            errors.append(
                                f"Absolute path '{prefix}' found in {rel}:{line_idx} -> {line.strip()}"
                            )
    return errors


def check_v4_isolation(root: Path) -> List[str]:
    """V4: Rule 4 Deliverable Isolation check."""
    errors = []
    media_extensions = {".mp4", ".mov", ".wav", ".mp3", ".m4a", ".aac", ".flac"}
    ignored = ["teamwork_preview_", "sentinel"]

    for p in root.rglob("*"):
        rel = str(p.relative_to(root))
        if any(ign in rel for ign in ignored):
            continue
        if p.is_file() and p.suffix.lower() in media_extensions:
            errors.append(f"Forbidden media deliverable found in .agents: {rel}")
        elif p.is_file() and p.name == "draft_info.json" and "references" not in rel:
            errors.append(f"Forbidden CapCut draft project found inside .agents: {rel}")
    return errors


def check_v5_integrity(root: Path) -> List[str]:
    """V5: Rule 5 Manifest Lockfile Integrity check."""
    errors = []
    lock_file = root / "manifest.lock.json"
    if not lock_file.exists():
        return ["manifest.lock.json is missing."]

    try:
        with open(lock_file, "r", encoding="utf-8") as f:
            lock_data = json.load(f)
    except Exception as exc:
        return [f"Could not parse manifest.lock.json: {exc}"]

    files_dict = lock_data.get("files", {})
    if not files_dict:
        return ["manifest.lock.json has empty 'files' dictionary."]

    for rel_path, entry in files_dict.items():
        actual_path = root / rel_path
        if not actual_path.exists():
            errors.append(f"Locked file missing on disk: {rel_path}")
            continue

        actual_hash = compute_sha256(actual_path)
        expected_hash = entry.get("sha256")
        if actual_hash != expected_hash:
            errors.append(
                f"Checksum mismatch for {rel_path}: expected {expected_hash}, got {actual_hash}"
            )

    return errors


def check_v6_workflow_schema(root: Path) -> List[str]:
    """V6: Workflow Frontmatter Schema check."""
    errors = []
    for wf in root.glob("workflows/*.md"):
        rel = str(wf.relative_to(root))
        meta = parse_frontmatter(wf)
        required_keys = ["description", "requires_agents", "requires_skills", "artifact_outputs"]
        for k in required_keys:
            if k not in meta or not meta[k]:
                errors.append(f"{rel} missing or empty frontmatter property: '{k}'")
    return errors


def check_v7_skill_schema(root: Path) -> List[str]:
    """V7: Skill Frontmatter Schema check."""
    errors = []
    for sk in root.glob("skills/**/SKILL.md"):
        rel = str(sk.relative_to(root))
        meta = parse_frontmatter(sk)
        required_keys = ["name", "description"]
        for k in required_keys:
            if k not in meta or not meta[k]:
                errors.append(f"{rel} missing or empty frontmatter property: '{k}'")
    return errors


def check_v8_python_syntax(root: Path) -> List[str]:
    """V8: Python Syntax validation check."""
    errors = []
    ignored = ["teamwork_preview_", "sentinel"]
    for py_file in root.rglob("*.py"):
        rel = str(py_file.relative_to(root))
        if any(ign in rel for ign in ignored):
            continue
        try:
            with open(py_file, "r", encoding="utf-8") as f:
                source = f.read()
            ast.parse(source, filename=str(py_file))
        except SyntaxError as syn_err:
            errors.append(f"Python syntax error in {rel}:{syn_err.lineno}: {syn_err.msg}")
        except Exception as exc:
            errors.append(f"Failed parsing {rel}: {exc}")
    return errors


def main() -> int:
    root = get_agents_root()
    all_failures: List[Tuple[str, str]] = []

    # Run checks V1 through V8
    checks = [
        ("V1_LAYOUT", check_v1_layout),
        ("V2_LINE_LIMIT", check_v2_line_limit),
        ("V3_RELATIVE_PATHS", check_v3_relative_paths),
        ("V4_ISOLATION", check_v4_isolation),
        ("V5_INTEGRITY", check_v5_integrity),
        ("V6_WORKFLOW_SCHEMA", check_v6_workflow_schema),
        ("V7_SKILL_SCHEMA", check_v7_skill_schema),
        ("V8_PYTHON_SYNTAX", check_v8_python_syntax),
    ]

    for check_id, func in checks:
        errs = func(root)
        for e in errs:
            all_failures.append((check_id, e))

    if all_failures:
        for check_id, msg in all_failures:
            print(f"[FAIL] {check_id}: {msg}")
        return 1

    print("[PASS] Toolkit is structurally valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
