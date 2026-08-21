"""At-rest encryption for per-user UEX secret keys.

Each user's UEX secret_key is sensitive (it grants read access to their personal UEX
trade data), so it's never stored in plaintext. We use Fernet (symmetric, authenticated
encryption from the `cryptography` package) with a key generated once and stored locally
next to the database. Anyone with both the sqlite file AND this key file could decrypt
stored keys, so treat the whole `data/` directory as sensitive -- back it up together,
and don't commit it to a public repo (.gitignore already excludes `data/`).
"""
from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet


def load_or_create_key(key_path: Path) -> Fernet:
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        key = key_path.read_bytes()
    else:
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        try:
            key_path.chmod(0o600)
        except OSError:
            pass  # best-effort on platforms that don't support chmod (e.g. some Windows setups)
    return Fernet(key)
