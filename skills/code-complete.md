# Code Complete Construction Guidance & Engineering Heuristics

> **Source**: Operational adaptation of Steve McConnell's *Code Complete* (2nd Edition) combined with modern agent-safety practices.
> **Scope**: Guidance for software construction, architecture, testing, refactoring, and code review. Project instructions and existing conventions always take precedence.

---

## 1. Authority, Scope, and Blast Radius

1. **Instruction Precedence**: Follow system, developer, user, and applicable project instructions before this guidance.
2. **Respect Existing Conventions**: Discover and adhere to the target project's own architecture, manifests, source code, and tests. Existing project idioms win over every heuristic here.
3. **Strict Scope Discipline**: Stay within the requested change. Do not reformat unrelated files, refactor surrounding code, add unauthorized dependencies, change build systems, or modify global settings.
4. **Heuristics vs. Automatic Gates**: Numeric thresholds (e.g. cyclomatic complexity, routine length) are diagnostic review signals, never automatic rejection criteria unless the project explicitly enforces them.
5. **Mocks, Stubs, and Scaffolding**: Mocks and test fixtures at system or external boundaries (APIs, databases, hardware) are legitimate and necessary. In production code, stubs or intentional placeholders are allowed only when authorized as part of phased scaffolding; always report unfinished behavior honestly.
6. **Empirical Honesty**: Never claim an unexecuted check passed. Explicitly distinguish changes, verified test results, external side effects, and remaining uncertainties.

---

## 2. Six-Phase Construction Workflow

### Phase 1 — Understand and Define Acceptance

1. **Read Before Editing**: Inspect applicable project instructions, relevant source, interfaces, manifests, and existing test suites before writing or modifying code.
2. **Determine Acceptance Evidence**: Identify the requested behavior, edge cases, compatibility constraints, and the commands needed to verify success.
3. **Resolve Ambiguity from Evidence**: Seek answers in project code and configuration first. Ask the user only when an unresolvable decision materially affects architecture; do not invent requirements.
4. **No Speculation**: Never invent nonexistent APIs, libraries, architecture layers, or benchmark figures.

### Phase 2 — Design Only What Is Needed

1. **Manage Complexity via Information Hiding**: Identify what implementation secret each module encapsulates: storage formats, network protocols, third-party libraries, or volatile business rules. Keep internal details behind clean interfaces.
2. **Cohesion and Coupling**: Aim for high cohesion (single responsibility) and loose coupling. An interface is valuable when it clarifies a real boundary, not merely because a class exists.
3. **Preserve Architectural Strata**: Do not impose an arbitrary layered structure (e.g., Domain/Application/Infrastructure) onto a project that uses a different paradigm.
4. **Composition over Inheritance**: Prefer composition when reuse is the goal. Use inheritance only when substitutability (Liskov Substitution Principle) genuinely holds.
5. **Pseudocode Programming Process (PPP)**: For complex algorithms, draft intent-level pseudocode or desk-check the logic before coding. Retain only meaningful comments that clarify high-level structure.

### Phase 3 — Implement Clearly and Defensively

#### Routines and Data
1. **Cohesive Routines**: Give each routine a single purpose. Name it honestly to reflect observable behavior and any significant side effects.
2. **Parameters**: Keep parameter lists concise (around 7 is a strong readability warning). Group parameters only when they form a coherent domain abstraction.
3. **Variables and Scope**: Declare and initialize variables close to first use. Minimize variable lifespan (the distance between first and last use) and scope.
4. **Problem-Domain Naming**: Prefer descriptive names from the problem domain. Conventional short names (e.g., `i`, `k`, `n`) are acceptable in compact, standard loops.
5. **Constants vs. Literals**: Extract constants when value, ownership, or repetition warrants naming. Do not create named constants for self-evident numbers (like `0` or `1`).
6. **Numeric Rigor**:
   - Use exact representations (integers, minor units, or `Decimal`) with explicit rounding policies for monetary values.
   - Use tolerance-based floating-point comparison for calculated floats (`math.isclose`), with relative and absolute tolerances appropriate to the domain. Exact equality is valid only for sentinels and exact zero checks.
   - Account for character encoding (UTF-8), integer overflow, truncation, and timezones.

#### Errors and Defensive Coding
1. **Boundary Validation**: Validate untrusted input at public system boundaries and convert it into validated domain types. Avoid repetitive defensive assertions internally that add noise without protecting boundaries.
2. **Invariants vs. Runtime Errors**: Use assertions for internal programmer invariants that should never occur in defect-free code. Handle expected operational failures (network timeouts, missing files) through standard error flows and exceptions.
3. **Specific Error Handling**: Catch specific exceptions. Never swallow errors silently with empty `except:` or `pass`. Add context when re-raising (`raise NewError(...) from exc`).
4. **Control Flow**: Prefer guard clauses and positive conditionals to flatten deep nesting. Deep nesting (> 3 levels) is a signal for refactoring, not an automatic defect.
5. **Bounded Operations**: Bound all retries, timeouts, network calls, and uncertain loops with limits. Naturally terminating deterministic loops do not need artificial counters.
6. **Comments**: Write comments that explain intent, rationale, contracts, assumptions, and useful high-level *what*. Remove obsolete comments, but do not delete valuable interface or algorithm documentation.

### Phase 4 — Verify and Debug

1. **Standard Verification**: Run standard project build, typecheck, lint, and test suites covering the change.
2. **Meaningful Test Boundaries**: Write tests within existing test conventions. Test requirements, representative partitions, and boundary values (`0`, `1`, `N-1`, `N`, empty inputs, nulls).
3. **Structured Basis Testing**: Use cyclomatic complexity `$v(G)$` as a heuristic to estimate the minimum number of independent test paths. Arbitrary coverage percentages do not prove correctness.
4. **Scientific Debugging**:
   - Formulate a falsifiable hypothesis based on evidence.
   - Test the hypothesis with a minimal reproducer or targeted test.
   - Fix the confirmed root cause; do not mask symptoms with speculative adjustments or arbitrary delays.
   - Verify that the regression fails on the old code and passes on the fix.
5. **Verification Integrity**: Exercise real end-to-end scenarios where possible. A clean build or compilation is a prerequisite, not proof of functional correctness.

### Phase 5 — Refactor and Optimize Deliberately

1. **Behavior-Preserving Steps**: Keep refactorings small, verifiable, and distinct from functional changes. Use existing tests as a safety net.
2. **Duplication vs. Abstraction**: Do not extract every duplicated 2-line snippet mechanically. Premature abstraction often causes more coupling than mild duplication.
3. **Empirical Optimization**:
   - Establish realistic performance requirements and workloads.
   - Profile before modifying code; find the bottleneck.
   - Prioritize architectural, algorithmic, and data-structure improvements over micro-tuning.
   - Re-measure after modifications under comparable conditions; retain changes only if the measured speedup justifies added complexity.

### Phase 6 — Integrate and Hand Off

1. **Incremental Integration**: Integrate multi-component changes in verifiable steps. Run smoke or integration checks after wiring.
2. **Root Cause Attribution**: Distinguish pre-existing failures from regressions caused by the new changes.
3. **Deliverable Review**: Inspect the final diff for scope boundaries, temporary artifacts, secrets, and documentation accuracy.

---

## 3. Steve McConnell Heuristics and Metrics Reference

Numbers in *Code Complete* synthesize historical empirical studies and heuristics. They serve as review aids, not rigid automated gates:

| Metric / Heuristic | Safe Interpretation & Guidance | McConnell Source |
|---|---|---|
| **Cognitive Chunks** | `$7 \pm 2$` items: human short-term memory heuristic. Minimize simultaneous concepts needed to understand a module; do not enforce arbitrary field or class limits. | Ch. 5, 33 |
| **Routine Length** | Do not enforce arbitrary 20–50 line limits. Cohesive algorithms of 100–200 lines are acceptable when cohesion and data flow are clear. Caution advised above 200 lines. | Ch. 7 |
| **Parameters** | Around 7 parameters is a strong warning signal. Group into domain objects only when cohesion improves. | Ch. 7 |
| **Cyclomatic Complexity** | `$v(G) > 10$` is a review signal for testing and refactoring. It does not prove a routine is defective; `$v(G) > 20$` indicates high testing effort. | Ch. 19, 22 |
| **Nesting Depth** | 3 or more levels of nesting justifies simplification (guard clauses, decomposition). Deep nesting in domain tables or state machines may be acceptable. | Ch. 15–17 |
| **Variable Names** | 8–20 characters is an empirical average, not a rule. Scope and clarity determine optimal name length. | Ch. 11 |
| **Span & Live Time** | Minimize the span (statements between references) and live time (total statements between first and last reference) to localize data flow. | Ch. 10 |
| **Assertions** | No universal density metric. Place assertions at internal invariant boundaries without side effects. | Ch. 8 |
| **Test Coverage** | Coverage percentages (e.g. 85–95%) are diagnostic aids. Path, boundary, and requirement coverage matter more than raw line counts. | Ch. 22 |
| **Refactoring Size** | Keep changes focused and independently verifiable. Avoid massive structural rewrites in a single step. | Ch. 24 |
| **Line Width** | Adhere to project formatter settings. Neither 80 nor 100 columns is a universal quality threshold. | Ch. 31 |

---

## 4. Metrics to Avoid as Automatic Gates

Do not enforce universal thresholds for:
- Strict assertion density (assertions per KLOC).
- Strict return-statement counts (single-return dogma vs. clean early guard clauses).
- Pure percentage test coverage without requirement validation.
- Arbitrary class or file line-count limits.
- Blind prohibition of all code repetition regardless of abstraction cost.

---

## 5. Construction and Pre-Handoff Checklist

- [ ] **Behavior**: Satisfies requested requirements with concrete acceptance evidence.
- [ ] **Conventions**: Preserves established project architecture, idioms, and formatting.
- [ ] **Scope**: Confined to requested files; no speculative abstractions or unsolicited refactoring.
- [ ] **Encapsulation**: Implementation details and volatility are hidden behind clear interfaces.
- [ ] **Boundaries**: Untrusted inputs validated at boundaries; invariants protected via assertions.
- [ ] **Errors**: Specific exceptions handled with context; no silent swallowing of errors.
- [ ] **Verification**: Verified using project tests, build commands, and end-to-end execution.
- [ ] **Honesty**: Verified results and remaining unverified aspects reported transparently.
