# Flutter and Dart Architecture Standards

Apply layered architecture, predictable state management, rendering performance, and platform-specific security to Flutter and Dart work. Confirm framework versions and existing project patterns before selecting APIs.

## I. Architecture and Layer Boundaries

1.  **Domain-Driven Design (DDD)**:
    *   **Value Objects**: All validation occurs within the constructor. Leverage zero-cost `extension types` for domain wrappers.
    *   **Entities and Aggregates**: Explicitly define identity and consistency boundaries.
2.  **Layered Isolation**:
    *   **Domain Layer**: Pure Dart (no `dart:ui`, no `package:flutter`). Use `Failures` instead of throwing exceptions.
    *   **Infrastructure Layer**: DTOs, repository implementations, API and database clients.
    *   **Presentation Layer**: State management (BLoC/Riverpod/Signals). Presentation logic strictly decoupled from UI rendering.

## II. DART LANGUAGE MASTERY (INTERNAL LEVEL)

1.  **Type System**: Mandatory utilization of `Records`, `Sealed Classes`, and `Pattern Matching`. Strict sound null-safety. Zero tolerance for `dynamic`.
2.  **Concurrency (Isolates)**:
    *   Employ `Isolate.run()` for all CPU-intensive workloads (>8 ms) or JSON serialization.
    *   Master `IsolateGroup` mechanics for shared memory and O(1) data transfer.
3.  **Low-Level Interop**:
    *   **Pigeon**: Mandatory for type-safe IPC between Dart and Native platforms.
    *   **Dart FFI**: Use for high-performance C interoperability strictly when required.

## III. FLUTTER ENGINE & RENDERING INTERNALS

1.  **Three Trees**: Deep mastery of Widget, Element, and RenderObject lifecycles. Optimize `mount`, `update`, and `performRebuild`.
2.  **Rendering Pipeline**:
    *   **Impeller Optimization**: Zero tolerance for shader compilation jank.
    *   **Layout Protocol**: Single-pass constraints with linear runtime complexity. Avoid `Intrinsics` unless strictly unavoidable.
    *   **RepaintBoundary**: Isolate complex paint operations to guarantee 120 FPS.
3.  **Frame Budget**: UI thread execution must remain under 8 ms per frame. Profile aggressively to eliminate dropped frames.

## IV. DEFENSIVE SECURITY & HARDENING (MILITARY-GRADE)

1.  **Data at Rest**:
    *   **AES-256-GCM**: Encryption for all local databases (Isar/Drift).
    *   **Secure Storage**: Mandatory use of platform keystores (`KeyStore` on Android, `Keychain` on iOS) backed by TEE/Secure Enclave.
2.  **Network Security**:
    *   **Certificate Pinning**: Hardcoded SHA-256 SPKI hashes. Strict TLS 1.3.
    *   **HMAC Signing**: All requests must be cryptographically signed to prevent payload tampering.
3.  **Application Shielding**:
    *   **Anti-Tamper**: Detection of Root/Jailbreak, emulators, and attached debuggers.
    *   **Obfuscation**: Release builds must enforce `--obfuscate` and `--split-debug-info`.
4.  **Privacy Safeguards**:
    *   **Screen Protection**: Prevent screenshots and task switcher leaks on sensitive screens.
    *   **PII Masking**: Mask all personally identifiable information across logs and crash analytics.

## V. RIGOROUS VERIFICATION (AAA STANDARD)

1.  **Golden Testing**: Pixel-perfect UI verification via `Alchemist` or `Golden Toolkit`.
2.  **Performance Testing**: Automated `Flutter Driver` benchmarks validating frame times and memory footprint.
3.  **Static Analysis**: Cyclomatic complexity < 8. Function length < 30 lines.

## VI. LOCAL RAG ECOSYSTEM INTEGRATION

1.  **Postgres RAG**: Synchronize local state with `local_rag` vector memory following `postgres-engineering.md` protocols.
2.  **Zero-Sudo Policy**: Never execute `sudo`. Operate within local `fvm` and `pub global` environments.
3.  **Android 16 KB Page Alignment**: Ensure all native assets and plugins support 16 KB page sizes for modern kernels.

## VII. MANDATORY RESPONSE FORMAT

> ### [TECHNICAL_VIOLATION] — [MODULE_NAME]
> *   **Location**: `path/to/file.dart` -> `Class.method`
> *   **Context / Engine Analysis**: (Internal state and performance impact).
> *   **Senior Architect Implementation**:
> ```dart
> // Production-ready, optimized Dart code.
> ```
> *   **Rationale**: Why this is mandatory for framework reliability, security, or performance.
