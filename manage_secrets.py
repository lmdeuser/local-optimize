#!/usr/bin/env python3
"""Interactive management for local_optimize-owned secrets only."""

from __future__ import annotations

import argparse
import getpass

from config import load_secrets_file, save_secrets_file

ALLOWED_KEYS = {
    "DB_PASSWORD",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
}


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage local_optimize secrets")
    parser.add_argument("key", choices=sorted(ALLOWED_KEYS))
    args = parser.parse_args()
    set_secret(args.key, getpass.getpass(f"Enter value for {args.key}: "))
    print(f"Successfully set {args.key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
