# The 8 Ironclad Rules of AG Kit (Version 2026.9.0)

All components, skills, workflows, agents, rules, and scripts within this repository must adhere unconditionally to these 8 foundational principles. Any violation results in an immediate build and validation failure.

---

## Rule 1: Modular Skills
- Every capability is encapsulated as a standalone package inside `.agents/skills/<skill-name>/`.
- Must contain:
  - `SKILL.md`: Core instructions with valid YAML frontmatter.
  - `scripts/`: Standalone, executable utilities with zero uninstalled third-party dependencies.
  - `references/`: Deep technical models, schemas, and domain guides.
- Skills must follow single responsibility (e.g. `capcut-editor` handles CapCut draft manipulation, not unrelated media processing).

---

## Rule 2: Clean Separation of Concerns
- The toolkit maintains five discrete architectural tiers:
  1. `agent/`: Personas, system prompt extensions, cognitive bounds, and handoff contracts.
  2. `workflows/`: Declarative multi-step workflows mapped to slash commands (e.g. `/capcut`).
  3. `skills/`: Execution knowledge, scripts, and progressive disclosure references.
  4. `rules/`: Architectural constraints and domain safety standards.
  5. `scripts/`: Tooling for manifest generation, integrity checking, and linting.
- The `.agents/` root directory contains only agent metadata. Source code, tests, and media must never reside in `.agents/` root.

---

## Rule 3: Zero Hardcoded Absolute Paths
- Absolute filesystem paths (such as system root user directories) are strictly prohibited in all documentation, workflows, configurations, and scripts.
- All internal cross-references must use POSIX-compliant relative paths.
- Executable scripts must resolve directories dynamically using standard library facilities (`Path.home()`, `Path(__file__).resolve()`, or CLI arguments).

---

## Rule 4: User Deliverable Isolation
- Output videos, audio exports, rendered graphics, and user draft directories must never be written inside `.agents/`.
- All outputs must be written to:
  - `outputs/<task_name>/` within the project root, OR
  - Designated external application directories (e.g. `~/Movies/CapCut/User Data/Projects/com.lveditor.draft/<project_name>`).
- `.agents/` is reserved strictly for ephemeral agent state, plans, handoffs, and toolkit specifications.

---

## Rule 5: Atomic Manifest & Integrity Locking
- Toolkits must maintain an atomic manifest pair:
  - `manifest.json`: High-level inventory of registered components with semantic versioning.
  - `manifest.lock.json`: Cryptographic integrity lockfile recording SHA-256 digests, exact file sizes in bytes, and generation timestamps.
- Any manual modification, accidental corruption, or drift between files and the lockfile triggers validation failure.

---

## Rule 6: Progressive Disclosure
- Top-level `SKILL.md` must provide an executive map, common commands, and decision trees.
- Exhaustive schemas, typography tables, and guides must be offloaded into `references/*.md`.
- Agents load reference documents on-demand using file inspection tools only when executing tasks that require that specific depth.

---

## Rule 7: Strict Line Limit (< 500 Lines for SKILL.md)
- `SKILL.md` files must never exceed 500 lines (target: 200–350 lines).
- Exceeding 500 lines causes immediate validation failure in `validate_kit.py`.
- Enforces modularity and prevents documentation bloat.

---

## Rule 8: Deterministic Pre-Flight Validation
- The toolkit must provide an automated validation script at `.agents/scripts/validate_kit.py`.
- Validation must run fully offline without network or LLM dependencies.
- It must test structural presence, frontmatter schemas, path compliance, line limits, script syntax, and manifest checksums.
- On success, it must print `[PASS] Toolkit is structurally valid.` and exit with code `0`.
