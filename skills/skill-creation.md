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
- `activation_keywords`: Array of English and Russian trigger words or phrases for conditional skills. Required core skills do not need activation keywords.

## III. Activation Keyword Contract
- Matching is case-insensitive and Unicode-aware. A keyword matches only at word or phrase boundaries, not inside another word: `rag` matches `RAG`, but not `storage`.
- Whitespace between words in a phrase is flexible. Escape-sensitive terms such as `C++` are treated literally.
- Use an explicit trailing `*` for an intentional prefix: `миграц*` matches `миграции` and `миграционный`. Do not use a bare short stem when it also occurs inside unrelated words.
- Prefer specific domain terms. Avoid ambiguous abbreviations unless their exact-word form is useful and regression-tested.
- Include negative examples in tests for every potentially ambiguous keyword.

## IV. Validation Checklist
- Run `python3 skills_manager.py` to verify schema compliance and check for alias collisions.
- Run `pytest tests/unit/test_skills_manager.py` to verify catalog integrity, link validity, and routing behavior.
