# Environment Setup & Dependency Verification Protocol

Procedures for inspecting, configuring, and troubleshooting development environments and dependencies.

## I. Inspection Protocol
1. **System Discovery**: Inspect OS, Python version, virtualenv, system packages, and GPU status.
2. **Dependency Audit**: Compare `requirements.txt` / `pyproject.toml` against installed environment packages (`pip list`).
3. **Virtualenv Validation**: Ensure all executions run within the isolated project venv.

## II. Troubleshooting Workflow
1. Check missing binary tools with `command -v gcc`, `command -v docker`, or the applicable platform equivalent.
2. Resolve version conflicts cleanly without polluting global system Python packages.
3. Provide clean, reproducible setup scripts for missing dependencies.

## III. Python and Runtime Validation
- Confirm the active interpreter with `python3 --version` and `python3 -c 'import sys; print(sys.executable); print(sys.prefix)'`; compare it with the project's `.venv` before installing anything.
- Inspect declared dependencies in `pyproject.toml`, requirements files, and the lockfile. Use the project's existing installer and versions rather than assuming `pip`, `uv`, or Poetry.
- For compiler-dependent packages, check the configured compiler and a minimal build in the designated disk-backed `TMPDIR`.
- For GPU work, verify the backend required by the project (`torch.xpu`, CUDA, or another configured backend) and record the actual device and driver evidence. Do not infer availability from installed packages alone.
- After changes, run a real import or application-level smoke check and report any unverified hardware or external-service dependency.
