# Automated Security Code Review Protocol

Defines standards for conducting security-focused code reviews and vulnerability audits.

## I. Code Audit Scope
1. **Input Validation**: Check for SQL injection, command injection, path traversal, and XSS.
2. **Authentication & Authorization**: Verify access control checks, token validation, and password hashing (Argon2id/scrypt/bcrypt).
3. **Secrets & Credentials**: Ensure no hardcoded API keys, tokens, or private keys exist in source code.
4. **Data Handling**: Verify proper encryption at rest and in transit (TLS 1.3).

## II. High-Signal Code Patterns
- Trace untrusted input into SQL construction, shell commands, filesystem paths, HTML, templates, and outbound requests; contextual escaping alone is not an authorization boundary.
- Review `eval`/`exec`, `pickle` and unsafe YAML loaders, dynamic imports, `subprocess(shell=True)`, weak randomness, custom cryptography, and permissive CORS or TLS verification overrides.
- Check canonicalization and containment for path traversal, including symlinks and sibling prefixes. Check tenant or project ownership on every database and cache read, not only on writes.
- Treat a suspicious API name as a lead, not proof. Confirm reachability, source/sink data flow, existing validation, and the security impact before reporting.

## III. Review Output Format
- **Severity Rating**: Critical / High / Medium / Low / Info.
- **Vulnerable Code Location**: Workspace-relative path and line numbers.
- **Evidence**: Reachable source, sink, missing control, and reproduction or focused negative test.
- **Impact Analysis**: Concrete effect within the authorized threat model; label unconfirmed exploitation as hypothetical.
- **Remediation**: Minimal fix that preserves the public contract, plus verification that covers the original failure.
