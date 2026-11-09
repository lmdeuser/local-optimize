# Automated Security Code Review Protocol

Defines standards for conducting security-focused code reviews and vulnerability audits.

## I. Code Audit Scope
1. **Input Validation**: Check for SQL injection, command injection, path traversal, and XSS.
2. **Authentication & Authorization**: Verify access control checks, token validation, and password hashing (Argon2id/scrypt/bcrypt).
3. **Secrets & Credentials**: Ensure no hardcoded API keys, tokens, or private keys exist in source code.
4. **Data Handling**: Verify proper encryption at rest and in transit (TLS 1.3).

## II. Review Output Format
- **Severity Rating**: Critical / High / Medium / Low / Info.
- **Vulnerable Code Location**: File path and line numbers (`file:///path/to/file#L10-L20`).
- **Impact Analysis**: Potential exploit vector.
- **Remediation**: Secure refactored code diff.
