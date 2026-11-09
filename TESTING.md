# Testing

Markers are registered in `pytest.ini`:

- `unit`: pure tests without PostgreSQL, FastAPI server startup, or ML model loading.
- `integration`: component-boundary behavior.
- `postgres`: live PostgreSQL and fresh-database behavior.
- `network`: explicit network access only.
- `systemd`: mocks or isolated harnesses only; never the user service manager.

## Required order

```bash
python3 -m compileall -q .
python3 -m pytest --collect-only -q
python3 -m pytest -q -m unit
python3 -m pytest -q -m integration
python3 -m pytest -q -m postgres
python3 -m pytest -q -m network
python3 -m pytest -q -m systemd
```

Integration dependencies use explicit markers and environment SKIPs. The fresh database test may SKIP when the PostgreSQL user lacks `CREATEDB` or cannot install the required vector extension in the newly created database; it must not use a temporary schema in the working database.

Every test must be reachable through the order above. An unmarked test silently never runs, so the following must report an empty selection:

```bash
python3 -m pytest --collect-only -q -m "not unit and not integration"
```

Behavioral evidence observes calls, SQL parameters, HTTP responses, rollback order, and filesystem state. AST guards may detect duplicate definitions or forbidden architecture patterns, but source substrings are not accepted as proof of runtime behavior.

## Archive hygiene

The reviewed ZIP is written canonically to `local_optimize_reviewed.zip` next to the project directory. Run `python3 build_review_archive.py` from the project to build it. The archive includes required production code, migrations, tests, skills, current documentation, and only explicitly allowlisted production shell scripts. It excludes caches, bytecode, logs, backup directories, secrets, environment dumps, IDE state, investigation material, manual/local-hardcoded scripts, build artifacts, dependency trees, and nested/previous archives. A same-directory temporary ZIP is fsynced and fully verified for absolute paths, `..`, duplicates, CRC failures, and extracted hashes before atomic replacement and parent-directory fsync.
