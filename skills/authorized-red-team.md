---
name: authorized-red-team
description: Authorized security assessment, lab testing, and penetration testing within explicitly defined authorization scope.
---

# Authorized Red Team Assessment Protocol

## Purpose
Conduct authorized vulnerability assessments, lab security testing, and penetration tests under explicit authorization.

## Activation Triggers
- Explicit user request for security pentesting on an authorized target, CTF challenge, or lab environment.

## Preflight Verification (MANDATORY)
1. Verify explicit authorization and clear scope boundaries before performing any active probes.
2. Log target IP ranges / domains into the scope record.

## Rules & Safeguards
- **Forbidden Actions**: Absolute ban on ransomware, credential harvesting, persistence establishment, lateral movement outside scope, and destructive payloads.
- **Controlled Probing**: Start with passive analysis. Request confirmation before executing active scanning.
- **No Automated Mass Scanners**: Do NOT run aggressive automated scanners (`masscan`, `Responder`, `ffuf`) automatically.
- **Audit Logging**: Maintain an exact record of all commands executed during the assessment.
