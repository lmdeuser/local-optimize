# Agent Skill Authoring & Registration Protocol

Guidelines for creating, validating, and registering new skills in the system catalog.

## I. Skill File Requirements
1. **Location**: Place Markdown files in `skills/<filename>.md`.
2. **Naming**: Use kebab-case filenames ending in `.md` (e.g. `fastapi-engineering.md`).
3. **Content**: Include clear domain standards, operational guidelines, and code patterns.

## II. Manifest Registration (`skills/manifest.json`)
Every skill MUST be registered in `skills/manifest.json` with:
- `id`: Unique kebab-case identifier.
- `filename`: Matching `.md` filename.
- `required`: `true` for Core system skills, `false` for Conditional domain skills.
- `version`: SemVer string (e.g. `"1.0.0"`).
- `description`: Concise summary.
- `aliases`: Array of unique alias strings.
- `activation_keywords`: Array of trigger words for dynamic routing.

## III. Validation Checklist
- Run `python3 skills_manager.py` to verify schema compliance and check for alias collisions.
- Run `pytest tests/unit/test_skills_manager.py` to verify catalog integrity.
