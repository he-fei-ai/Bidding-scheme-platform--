"""Rotate the Fernet master key used to encrypt stored API keys.

Rewrites every ``ENC:``-encrypted API key column in the database with a NEW
master key, so the secret can be rotated without losing existing credentials.

USAGE (PowerShell / cmd, ASCII only):

    # 1. dry run - print what would change, touch nothing
    python scripts/rotate_api_key_encryption.py --dry-run

    # 2. real rotation - new key must be a Fernet key (url-safe base64, 32 bytes)
    python scripts/rotate_api_key_encryption.py --new-key-file C:\\secure\\new.key

    # or read the new key from an environment variable
    set BID_ASSISTANT_KEY_NEW=<url-safe-base64-32-bytes>
    python scripts/rotate_api_key_encryption.py

IMPORTANT NOTES:

- Run this with the OLD key still active (do NOT set BID_ASSISTANT_KEY to the
  new value before running). The script decrypts with the old key first, then
  re-encrypts with the new one.
- After a successful rotation, set BID_ASSISTANT_KEY (or BID_ASSISTANT_KEY_FILE)
  to the new key and restart the backend.
- A backup of the database is strongly recommended.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _read_new_key(args: argparse.Namespace) -> str:
    if args.new_key_file:
        raw = Path(args.new_key_file).read_bytes().strip()
        return raw.decode("utf-8")
    env_key = os.environ.get("BID_ASSISTANT_KEY_NEW", "").strip()
    if not env_key:
        raise SystemExit(
            "No new key provided. Use --new-key-file <path> or set BID_ASSISTANT_KEY_NEW."
        )
    return env_key


def main() -> int:
    parser = argparse.ArgumentParser(description="Rotate the API key encryption master key.")
    parser.add_argument("--new-key-file", help="File containing the new Fernet key")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report the scope, do not write anything",
    )
    args = parser.parse_args()

    import app.db  # noqa: F401  (initialise app package / db module first)
    from app.db_migrations import _API_KEY_STORAGE_COLUMNS
    from app.services.crypto import decrypt_api_key, is_encrypted, rotate_master_key
    from app.db import get_db

    new_key = _read_new_key(args)

    # Validate the new key early - fail before touching the database.
    from cryptography.fernet import Fernet

    Fernet(new_key.encode("utf-8"))

    unreadable = 0
    encrypted_rows = 0
    with get_db(readonly=True) as conn:
        for table, col in _API_KEY_STORAGE_COLUMNS:
            for row in conn.execute(f"SELECT {col} AS k FROM {table}").fetchall():
                value = row["k"] or ""
                if not value:
                    continue
                if is_encrypted(value):
                    encrypted_rows += 1
                    plain = decrypt_api_key(value)
                    if is_encrypted(plain):
                        unreadable += 1
                else:
                    encrypted_rows += 1

    print("Scope:")
    for table, col in _API_KEY_STORAGE_COLUMNS:
        print(f"  - {table}.{col}")
    print(f"Candidate rows: {encrypted_rows}")
    print(f"Undecryptable rows (encrypted with an older key): {unreadable}")

    if unreadable:
        print(
            "ABORT: some rows cannot be decrypted with the current key. "
            "Restore the matching master key before rotating."
        )
        return 2

    if args.dry_run:
        print("DRY RUN: nothing was written.")
        return 0

    stats = rotate_master_key(new_key)
    print("Rotation complete:", stats)
    if stats.get("failed"):
        print("WARNING: some rows failed to decrypt and were left untouched.")
        return 1
    print("Now set BID_ASSISTANT_KEY (or BID_ASSISTANT_KEY_FILE) to the new key and restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())