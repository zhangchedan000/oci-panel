"""Panel login password hashing (pbkdf2-sha256, no external deps).
Stored form: pbkdf2$<iterations>$<b64 salt>$<b64 hash>. Plaintext is never stored."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys


def hash_password(pw: str, iterations: int = 200_000) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return "pbkdf2${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def rewrite_panel(path: str, username: str | None = None, password_hash: str | None = None) -> None:
    """Update panel.username / panel.password_hash in accounts.yaml in place,
    leaving the rest (account credentials) untouched. Used by web setup and
    the in-panel change-password feature."""
    import os
    import re
    s = open(path, encoding="utf-8").read()
    if username is not None:
        if re.search(r'username:\s*".*?"', s):
            s = re.sub(r'username:\s*".*?"', f'username: "{username}"', s, count=1)
        else:  # insert under 'panel:' if missing
            s = re.sub(r'(panel:\s*\n)', rf'\1  username: "{username}"\n', s, count=1)
    if password_hash is not None:
        if re.search(r'password_hash:\s*".*?"', s):
            s = re.sub(r'password_hash:\s*".*?"', f'password_hash: "{password_hash}"', s, count=1)
        else:
            s = re.sub(r'(panel:\s*\n)', rf'\1  password_hash: "{password_hash}"\n', s, count=1)
    open(path, "w", encoding="utf-8").write(s)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


if __name__ == "__main__":
    # used by install.sh: `python auth.py <password>` -> prints the hash
    print(hash_password(sys.argv[1]))
