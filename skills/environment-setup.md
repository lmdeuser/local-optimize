# Environment Setup & Dependency Verification Protocol

Procedures for inspecting, configuring, and troubleshooting development environments and dependencies.

## I. Inspection Protocol
1. **System Discovery**: Inspect OS, Python version, virtualenv, system packages, and GPU status.
2. **Dependency Audit**: Compare `requirements.txt` / `pyproject.toml` against installed environment packages (`pip list`).
3. **Virtualenv Validation**: Ensure all executions run within the isolated project venv.

## II. Troubleshooting Workflow
1. Check missing binary tools (`which gcc`, `which docker`, `which nvidia-smi`).
2. Resolve version conflicts cleanly without polluting global system Python packages.
3. Provide clean, reproducible setup scripts for missing dependencies.
