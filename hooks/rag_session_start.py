#!/usr/bin/env python3
"""Disabled: RAG results must not enter a cloud-capable Claude Code session."""


def main() -> None:
    # Claude Code may send its conversation context to a cloud model. This hook
    # therefore performs no RAG request and emits no retrieved content.
    return


if __name__ == "__main__":
    main()
