# Static Analysis & Automated Bug Review Protocol

Guidelines for automated bug detection, static analysis, and code quality verification.

## I. Focus Areas
1. **Null / None Dereferences**: Unhandled optional values or missing null checks.
2. **Resource Leaks**: Unclosed file handles, database connections, or HTTP sessions.
3. **Concurrency & Threading**: Race conditions, deadlocks, and unhandled async exceptions.
4. **Boundary & Edge Cases**: Empty arrays, integer overflow, out-of-bounds array access.

## II. Verification Rules
- Always verify findings with concrete test cases or reproduction steps.
- Do NOT introduce fallback default values that swallow errors silently.
- Fix underlying contracts rather than masking runtime exceptions.

## III. Tooling and Boundary Checks
- Use the analyzers already configured by the project, such as `ruff check`, `mypy`, `shellcheck`, or `semgrep`; do not install new tools solely for a routine review.
- Exercise empty, single-element, maximum-size, malformed, and repeated inputs where relevant. Include timeouts, cancellation, retries, and partial failures for asynchronous paths.
- Report a finding only after tracing the reachable data flow. Distinguish confirmed defects from hypotheses and provide the exact file, line, trigger, and verification command.
