# Comprehensive Security and Anti-Spying Policy

## Our "Enemies"
- **Telemetry**: Any background reports, metrics, and crash data.
- **WebRTC**: A potential source of real IP leaks. Must be restricted or disabled.
- **Fingerprinting**: Unique identifiers (Client ID, seeds, hardware metrics) that can be used to track the user.
- **Adware/Bloatware**: Unwanted regional services or spyware extensions.

## Operational Rules
1. **Configuration Control**: Critical project files (`.env`, `cursor_mcp_config.json`, SQL schemas, and PowerShell launchers) must not be changed without a clear reason and subsequent verification.
2. **Temporary Data**: Create temporary files in `$env:TEMP` or inside a dedicated project directory and delete them after execution.
3. **Risky Actions**: Always request user confirmation for destructive or hard-to-reverse actions:
   - `rm -rf` equivalents for non-temporary directories.
   - Force-push or deleting git branches.
   - Modifying shared system infrastructure.
   - Sending messages to external services (Slack, Email, GitHub PRs).
4. **Verification**: When implementing security fixes, always verify them using available Windows/Python/SQL commands.

## Privacy Protocol
- Never generate or guess URLs for the user unless they are necessary for programming assistance.
- Do not transmit any session data to external trackers.
- If you notice insecure or "spyware" behavior in code or tools, immediately report it to the user.
