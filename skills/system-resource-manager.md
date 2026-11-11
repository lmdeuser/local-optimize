> [!IMPORTANT]
> This skill follows the global protocol [environment-discovery.md](environment-discovery.md).
> Responses must remain strictly technical, concise, without fluff, and categorically without emojis.

# SYSTEM RESOURCE MANAGER: OPERATIONAL LIMITS AND PERFORMANCE TUNING

This document governs the consumption policies for system resources (memory, CPU scheduling, disk I/O) during intensive binary and RAG workloads on local workstations.

---

## PRE-FLIGHT CHECKS

Before launching resource-intensive workloads (such as RAG vector indexing, large binary decompilation, compiling heavy C++ codebases), the agent must verify the current system state:

1. **Disk Storage**:
   - Minimum free space on the active volume: 2 GB.
   - Verification via PowerShell: `Get-PSDrive -PSProvider FileSystem` (or `df -h .` on Linux).
2. **RAM Capacity**:
   - Minimum free RAM for compilation or ML inference: 1 GB.
   - Verification via PowerShell: `Get-CimInstance Win32_OperatingSystem` (or `free -m` on Linux).
3. **GPU State**:
   - For Intel Arc, verify PyTorch XPU availability via `torch.xpu.is_available()`; for NVIDIA, use `nvidia-smi` and `torch.cuda.is_available()`.

---

## PROCESS PRIORITIZATION AND SCHEDULING (NICE/IONICE)

All heavy background processes capable of degrading user interface responsiveness must execute under reduced scheduler priority:

1. **CPU Priority (Niceness)**:
   - For background workloads on Windows, invoke processes with lowered priority via `Start-Process -Priority BelowNormal`.
   - Example: `Start-Process .\rag-venv\Scripts\python.exe -ArgumentList '.\scripts\rag_engine.py index-code .\scripts' -Priority BelowNormal -Wait`.
2. **Disk I/O Priority**:
   - Windows provides no direct equivalent to Linux `ionice` in standard PowerShell; mitigate disk contention by rate-limiting batch sizes and scheduling off-peak jobs.

---

## MEMORY OPTIMIZATION AND EPHEMERAL STORAGE

1. **Temporary Files**:
   - Use an explicitly configured per-task scratch directory. Honor `TMPDIR`; keep non-build materials in the existing project scratch directory or `~/.local/state/local_optimize`, and use a disk-backed task directory under `/srv/temp` for builds unless the project configures another location.
   - Do not default working files, intermediate builds, or logs to shared `/tmp`. Pass the chosen directory explicitly to `tempfile`, `mktemp`, or the corresponding platform API.
2. **VRAM Constraints and GPU Utilization**:
   - Primary path for Intel RAG: local SentenceTransformers model running via PyTorch XPU (`torch.xpu`). NVIDIA CUDA remains a separate supported backend.
   - RAG mathematical tensor operations must not run on CPU.
   - `RAG_STRICT_GPU=1` is mandatory for this project.

---

## DATABASES AND CONNECTION POOLS (POSTGRESQL POOL LIMITS)

1. **Connection Pooling**:
   - Never establish new database connections per HTTP request or per RAG invocation.
   - Utilize connection pools (`psycopg_pool` for Python) with strict limits: `max_size=10`.
2. **Database Timeouts**:
   - Enforce explicit query timeouts: `statement_timeout = 30000` (30 seconds) to prevent hung transactions and connection leaks.

---

## HYGIENE AND AUTOMATED EPHEMERAL CLEANUP

1. **Log Rotation**:
   - Active log files (`rag_engine.log`, `app.log`) must not exceed 50 MB.
   - Apply automatic rotation (e.g. Python's `RotatingFileHandler` with `backupCount=3`).
2. **Scratch Folder Cleanup**:
   - Create the configured task directory before use and clean it in `finally` blocks or with an equivalent deterministic mechanism.
   - Leave no residual build artifacts in working trees.

---

## PRIVILEGED COMMAND EXECUTION RULES (SUDO)

1. **Strict Prohibition on Automated Sudo Invocations**:
   - The agent is categorically prohibited from generating or attempting automated `sudo` invocations. Doing so triggers security sandboxes and execution blocks.
   - If an operation requires elevated privileges (e.g., tuning `chattr` or binding privileged ports), the agent must generate the exact command and instruct the user to execute it manually.
