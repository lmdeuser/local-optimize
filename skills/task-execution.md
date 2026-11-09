# Task Execution and Code Quality Standards

## Task Execution
- **Understand First**: Do not propose changes to code you have not read. Understand the existing logic before suggesting edits.
- **Minimal Complexity**: The right level of complexity is exactly what the task requires. Avoid speculative abstractions and "gold-plating" the code (excessive perfecting).
- **File Management**: Do not create new files unless absolutely necessary. Prefer editing existing ones to prevent project bloat.
- **Verification**: Before reporting completion, ensure everything works. Run a test, execute a script, check the output. If you cannot verify it, say so directly.

## Code Style and Improvements
- **No Extraneous Edits**: Do not refactor or "improve" code beyond what was requested. Fixing a bug does not require cleaning up all the surrounding code.
- **Comments**: By default — NO comments. Add them only when the "WHY" is non-obvious (hidden constraints, workarounds).
- **Abstractions**: Do not create helpers or utilities for one-off operations. Three similar lines of code are better than premature abstraction.
- **Modern Standards**: Use modern syntax and language features (Python 3.14+, TypeScript, etc.).

## Problem Solving
- If an approach failed, figure out WHY before changing tactics. Read the error, check assumptions, try a targeted fix.
- Do not blindly repeat the same action.
- Only reach out to the user when you are truly stuck after deep investigation.
