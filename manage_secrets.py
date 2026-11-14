#!/usr/bin/env python3
"""Interactive management for local_optimize-owned configuration and secrets."""

from __future__ import annotations

import argparse
import getpass

from config import OWNED_SECRET_KEYS, load_secrets_file, save_secrets_file

SENSITIVE_KEYS = frozenset({"DB_PASSWORD", "RAG_BEARER_TOKEN"})
ALLOWED_KEYS = OWNED_SECRET_KEYS


def validate_secret(value: str) -> str:
    if any(char in value for char in ("\0", "\n", "\r")):
        raise ValueError("Invalid characters in secret")
    if not value:
        raise ValueError("Empty secret provided")
    return value


def set_secret(key: str, value: str) -> None:
    if key not in ALLOWED_KEYS:
        raise ValueError(f"Unsupported local_optimize secret key: {key}")
    existing = load_secrets_file()
    existing[key] = validate_secret(value)
    if not save_secrets_file(existing):
        raise RuntimeError("Could not persist local_optimize secrets")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage local_optimize secrets")
    parser.add_argument("key", nargs="?", choices=sorted(ALLOWED_KEYS))
    parser.add_argument("--list-keys", action="store_true", help="List supported keys and exit")
    args = parser.parse_args(argv)
    if args.list_keys:
        for key in sorted(ALLOWED_KEYS):
            print(key)
        return 0
    if not args.key:
        parser.error("key is required unless --list-keys is set")
    if args.key in SENSITIVE_KEYS:
        value = getpass.getpass(f"Enter value for {args.key}: ")
    else:
        value = input(f"Enter value for {args.key}: ")
    set_secret(args.key, value)
    print(f"Successfully set {args.key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
