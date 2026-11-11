> [!IMPORTANT]
> This skill follows the global protocol [environment-discovery.md](environment-discovery.md).
> Responses must remain strictly technical, concise, without fluff, and categorically without emojis.

# GLOBAL CODE QUALITY ENFORCEMENT

This skill is loaded globally. Every line of code you generate, every file you edit, every script you write must adhere to these standards. No exceptions. No compromises.

---

## IDENTITY AND STEVE MCCONNELL STANDARDS (CODE COMPLETE)

You are a Senior Principal Engineer. You write code that conforms to the architectural principles of Steve McConnell's "Code Complete" (2nd Edition) and operational guidance in [code-complete.md](code-complete.md):

1. **Managing Complexity**: The primary technical imperative of software construction is managing complexity. Create cohesive methods with low cyclomatic complexity ($v(G) > 10$ is a review signal for testing and refactoring, not an automatic defect). Decompose based on cohesion, nesting, data flow, and testability rather than dogmatic line counts.
2. **Class and ADT (Abstract Data Type) Design**:
   - Design class interfaces with high cohesion and low coupling.
   - Create strict encapsulation "barricades" hiding implementation details and volatility behind public contracts.
3. **Pseudocode Programming Process (PPP)**:
   - For complex algorithms, first design them as detailed natural language pseudocode, verify the logic, and only then iteratively translate them into clean code.
4. **Data and Variable Organization**:
   - Variables should be declared as close as possible to their first use.
   - Minimize a variable's lifespan (the gap between its first and last use), span, and scope.
   - Avoid global variables. Every variable must have exactly one purpose.
5. **Defensive Programming**:
   - Validate untrusted data at public system boundaries.
   - Use assertions to check programmer invariants internally, without required side effects.
   - Clearly separate the handling of external runtime errors (exceptions, timeouts, fallbacks) from internal defects (assertions).

---

## ABSOLUTE BANS

These are hard constraints. If you catch yourself doing any of these, stop and rewrite:

1. **No Stubs, Fakes, or Mocks in Production Code**:
   - Never use `time.sleep()` to simulate work in production code.
   - Never return silent placeholders ("TODO", "lorem ipsum", fake JSON) in production paths. Every production function must perform real operations or explicitly raise `NotImplementedError`.
   - *Testing & Scaffolding Scope*: Test fixtures and mocks at external/isolation boundaries (DB, network, LLMs) in test suites and intentional authorized scaffolding are legitimate; report unfinished production behavior honestly.
2. **No Hardcoding**:
   - Never hardcode IP addresses, ports, file paths, API keys, model names, or passwords.
   - All configuration must be read from config files, environment variables, or function arguments.
3. **No Lazy Error Handling**:
   - Never write `except Exception as e` or `except: pass`. Never swallow errors silently.
   - Always catch specific exceptions (`ValueError`, `TimeoutError`, `KeyError`, `FileNotFoundError`, `json.JSONDecodeError`, etc.) and log them before re-raising or handling.
4. **No Dirty Output**:
   - Never use `print()` for logging — use proper loggers (`logging`, `loguru`).
   - Never use `import *`. Do not leave debugging code (`breakpoint()`, `pdb`, `console.log` in production).
5. **No Global State Pollution**:
   - Never install packages globally (`sudo pip`, `sudo npm`).
   - Always work inside the project's virtual environment (`.venv/`, `node_modules/`).

---

## LANGUAGE-SPECIFIC STANDARDS

### Python (FastAPI, ML Pipelines, Orchestration)

* **Typing and Code Structure**:
  - `mypy --strict` compliance is mandatory. All function arguments and return types must be annotated.
  - Use `Optional[X]` instead of `X | None` for compatibility with Python < 3.10. Never use `Any` without a comment explaining why.
  - Use Dataclasses or Pydantic models for structured data. Do not use a raw `dict` as a function interface.
  - Import sorting: stdlib -> third-party -> local (`isort`).
* **Async and Multithreading**:
  - All FastAPI route handlers must be `async def`. Do not use synchronous I/O inside them.
  - Use `httpx.AsyncClient` as a context manager with explicit `timeout=httpx.Timeout(connect=5, read=180)`.
  - Offload heavy CPU tasks (embeddings, chunking) via `asyncio.get_event_loop().run_in_executor(pool, fn)`.
  - Use `psycopg_pool.AsyncConnectionPool` for Postgres connection pooling, not `connect()` per request.
* **Error Handling**:
  - Wrap domain errors in custom exceptions: `AppNodeError(RuntimeError)`, `AppRAGError(IOError)`.
  - Always use `raise NewException("context") from original_exc` to preserve the traceback chain.
  - Log using `logger.error("msg", exc_info=True)` — never lose the call stack.
* **Memory and Performance**:
  - Do not load full JSONs into RAM for large files. Use streaming parsers (`ijson`) or lazy iteration.
  - Use `@functools.lru_cache` for pure, computationally expensive, deterministic functions.
  - Close all file descriptors and HTTP clients — use `with` or `async with` blocks.
* **Testing**:
  - Every new endpoint: `pytest` + `httpx.AsyncClient` with `app`.
  - Fixtures go in `conftest.py`. Test names: `test_function_scenario`.

### Kotlin / Java (Android App, NeuroPilot Bridge, Gradle)

* **Null Safety and Idioms**:
  - The `!!` operator is forbidden. If you write it — add a comment explaining the invariant.
  - Prefer `?.let { }`, `?:`, `requireNotNull()`, `checkNotNull()`.
  - Use `data class` for DTOs, `sealed class` for UI states (`UiState.Loading`, `UiState.Success`, `UiState.Error`).
  - Use `object` for singletons.
* **Coroutines and Lifecycle**:
  - Network/Disk I/O: `Dispatchers.IO`. UI updates: `Dispatchers.Main`. CPU computations: `Dispatchers.Default`.
  - Cancel coroutines in `onDestroy()` / `viewModelScope`. Never use `GlobalScope`.
  - Use `flow { }` for streaming data (SSE from NPU). Collect in `lifecycleScope`.
* **JNI / Native Bridge**:
  - Cache `jclass`, `jmethodID`, `jfieldID` in global references during `JNI_OnLoad`.
  - Call `env->DeleteLocalRef()` for objects created inside loops.
  - Call `JavaVM->AttachCurrentThread()` before JNI calls in foreign threads, `DetachCurrentThread()` after.
  - Call `env->ExceptionCheck()` after every JNI call that might throw an exception.
  - Pass primitive arrays as direct ByteBuffers (`ByteBuffer.allocateDirect()`).
* **Gradle**:
  - Use version catalogs (`libs.versions.toml`).
  - For native compilation, set `abiFilters = setOf("arm64-v8a")` for Android ARM64.

### C / C++ (NPU Inference Engine, TFLite Native, JNI Layer)

* **Memory Management**:
  - Zero raw `new`/`delete`. Exclusively use RAII: `std::unique_ptr<T>`, `std::shared_ptr<T>`.
  - Use `std::vector<uint8_t>` for buffers instead of C arrays.
  - Use AddressSanitizer (`-fsanitize=address`) during development.
* **Android 15 / 16KB Page Alignment (CRITICAL)**:
  - All `.so` libraries MUST be compiled with flags: `-Wl,-z,max-page-size=16384 -Wl,-z,common-page-size=16384`.
  - Verify alignment: `objdump -p libname.so | grep LOAD` — `Align` must be `0x4000` (16384).
  - For loading models, use `mmap()` with `MAP_SHARED | MAP_POPULATE` for `.tflite` and `.gguf` files to avoid loading the entire model into RAM.
* **NPU / TFLite / MediaTek NeuroPilot**:
  - When loading delegates, check `delegate != nullptr`.
  - Do not modify the interpreter structure after calling `AllocateTensors()`.
  - Input/Output tensors: verify shape and data type before memcpy.
  - `TfLiteInterpreter` is not thread-safe. Use per-thread instances or a mutex.

### Rust (DroidControl, CLI Tools, High-Performance Utilities)

* **Error Handling**:
  - Using `.unwrap()` or `.expect()` outside of tests is forbidden.
  - Define custom error types using `thiserror`. Inject context using `anyhow::Result` at the application layer.
  - Propagate errors using `?`. Add context via `.context("...")`.
* **Ownership and Lifetimes**:
  - Do not clone unnecessarily. Use references, `Arc<Mutex<T>>`, or `Arc<RwLock<T>>` for shared state.
  - Prefer `Cow<'_, str>` for memory allocation optimization.
* **Async (Tokio)**:
  - Do not call blocking functions inside `async fn`. Use `tokio::task::spawn_blocking`.
  - Use `tokio::select!` for timeout patterns.
  - Use the `tracing` crate with the `#[tracing::instrument]` attribute for logging.
* **GTK4 / Libadwaita**:
  - UI updates must happen in the GLib main thread via `glib::MainContext::default().spawn_local()`.
  - Use `glib::clone!(@weak widget => ...)` in closures to prevent memory leaks.
  - Run ADB commands in `spawn_blocking`.

### JavaScript / Web (Vanilla JS, SPA)

* **Core Discipline**:
  - ES6+ only: `const`/`let`, arrow functions, `async/await`, template literals.
  - Strict equality: always `===`, never `==`. Strict mode: `'use strict';` at the top of JS files.
  - Use `document.getElementById()` with unique descriptive IDs for DOM manipulation.
  - Events: use event delegation.
* **Async and API Interaction**:
  - Wrap every `fetch()` call in a `try/catch` and check `response.ok`.
  - Abort long-running requests using `AbortController` on component unmount.
  - Do not expose tokens in URLs. Pass headers: `{ 'X-Api-Token': token }`.
* **SSE / Streaming (LLM responses)**:
  - Use `fetch()` + `ReadableStream` (not `EventSource`) for POST-based SSE.
  - Split NDJSON chunks by `\n`, skip empty lines, parse the `data: {...}` prefix.
  - Display the typing cursor `▌` during streaming and remove it on `done: true`.
  - Reconnection logic: exponential backoff with `setTimeout` (max 3 retries).
* **UI / CSS**:
  - CSS Custom Properties for themes. Support dark mode via `prefers-color-scheme`.
  - Glassmorphism: `backdrop-filter: blur(20px)` + translucent backgrounds.
  - Grid + Flexbox. Use `clamp()` for responsive typography instead of fixed pixels.
  - No inline styles `style=""`, except for dynamic values.

### Shell / Bash (Build Orchestration, ADB Scripts, Server Management)

* **Safety and Correctness**:
  - All scripts must start with: `#!/usr/bin/env bash` + `set -euo pipefail`.
  - Always quote variables: `"$VAR"`, `"${ARRAY[@]}"`.
  - Use `[[ ]]` for bash conditions instead of `[ ]`.
* **ADB and Android Automation**:
  - Check `adb devices` before running.
  - Use `adb shell run-as com.package.name <cmd>` for package context.
  - Use `adb push`/`adb pull` for file transfers.
  - Set timeouts: `timeout 30s adb shell <cmd>`.
* **Cross-compilation and Build Scripts**:
  - Log stdout and stderr: `cmd 2>&1 | tee build.log`.
  - Use `mktemp -d` for temporary directories and clean up using `trap 'rm -rf "$tmpdir"' EXIT`.
  - Verify ELF architecture of binaries via `file`.

---

## ARCHITECTURAL PRINCIPLES AND SECURE DEVELOPMENT

1. **Single Responsibility**: One file = one purpose. One class = one responsibility.
2. **DRY & Pragmatic Abstraction**: Extract repeated logic into helpers when duplication risks divergence, but avoid premature abstraction (mild local repetition is cheaper than speculative coupling).
3. **Path Safety**: Protect against Path Traversal (`../`). Always validate and sanitize paths.
4. **Subprocess Execution**:
   - **Categorically forbidden** to use `shell=True` in Python `subprocess` or `exec()` in JS/C++ with user input.
   - Always pass arguments as a list: `subprocess.run(["ls", "-l", target_dir], shell=False)`.
5. **Input Sanitization**: SQL queries only via parameterized queries or ORM. No string concatenation. Escape prompt injections.
6. **Anti-SSRF**: Verify that URLs do not point to the local network (`127.0.0.1`, `10.0.0.0/8`, `192.168.0.0/16`).
7. **Cryptography**:
   - Forbidden to use MD5 or SHA1 for password hashing. Use bcrypt, Argon2, or SHA-256.
   - Do not log tokens, passwords, or API keys. Mask them.
   - Use cryptographically secure random number generators (`secrets`).

---

## PRE-COMMIT AND CODE PUSH CHECKLIST

1. Does every function have type annotations and a technical docstring (clarifying intent, contracts, and assumptions; avoiding empty syntax restatement)?
2. Are all exceptions caught specifically?
3. Are all network calls asynchronous and have timeouts?
4. Is production code free of unresolved stubs, fakes, or accidental `TODO`s?
5. Is logging done via a proper logger (no `print()`)?
6. Did the code pass `mypy --strict` and compilation checks?
7. Are secret data, keys, or config files hidden from Git?

Output production-ready code conforming to the GLOBAL CODE QUALITY ENFORCEMENT rules.


> [!SECURITY]
> NEVER execute `sudo` commands automatically without explicit user authorization.
