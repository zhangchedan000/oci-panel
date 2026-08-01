"""
Per-account identity (anti-association fingerprint).

Rule (as specified):
  - each account gets ONE fingerprint, generated once, then FIXED forever
  - accounts' fingerprints are unrelated to each other
  - it changes ONLY when the account's API key changes (i.e. delete + re-import
    a new key). Same account + same key => same fingerprint across restarts.
  - pruned when the account is removed from config, so re-adding regenerates.

A "fingerprint" here = a realistic, stable OCI-SDK User-Agent string, e.g.
  Oracle-PythonSDK/2.181.0 (python 3.11.9; aarch64-Linux)
Different accounts get different (but plausible) combos, so their API traffic
looks like independent clients rather than one operator. This is the UA axis
only -- egress IP is still the primary anti-association lever.

Stored in identity.json (local, chmod 600, gitignored). Human-readable: you can
open it to see "which account -> which fingerprint", and delete an entry to
force that account to get a fresh one.
"""
from __future__ import annotations

import json
import os
import random
import secrets

# realistic pools -- fixed per account, varied across accounts
_SDK = ["2.183.0", "2.181.0", "2.179.0", "2.176.1", "2.174.0", "2.170.0", "2.166.0", "2.160.1"]
_PY = ["3.9.18", "3.10.14", "3.11.9", "3.12.4", "3.10.12", "3.11.7", "3.9.19"]
_ARCH_OS = ["x86_64-Linux", "aarch64-Linux", "amd64-Windows", "x86_64-Darwin", "arm64-Darwin"]


def _gen_ua(seed: str) -> str:
    r = random.Random(seed)   # deterministic: same seed -> same UA even if file lost
    return "Oracle-PythonSDK/{} (python {}; {})".format(
        r.choice(_SDK), r.choice(_PY), r.choice(_ARCH_OS)
    )


class IdentityStore:
    def __init__(self, path: str):
        self.path = path
        self.salt = ""
        self.accounts: dict = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            self.salt = blob.get("_salt", "")
            self.accounts = blob.get("accounts", {})
        if not self.salt:
            self.salt = secrets.token_hex(16)   # per-install secret, generated once
            self._save()

    def resolve_ua(self, account_id: str, key_fingerprint: str) -> str:
        entry = self.accounts.get(account_id)
        if entry and entry.get("key_fp") == key_fingerprint:
            return entry["user_agent"]                       # fixed -> reuse forever
        # first sight OR key changed (re-import) -> assign a fresh, stable identity
        ua = _gen_ua(f"{self.salt}:{account_id}:{key_fingerprint}")
        self.accounts[account_id] = {"key_fp": key_fingerprint, "user_agent": ua}
        self._save()
        return ua

    def prune(self, live_ids) -> None:
        gone = [k for k in self.accounts if k not in set(live_ids)]
        for k in gone:
            del self.accounts[k]
        if gone:
            self._save()

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"_salt": self.salt, "accounts": self.accounts}, f,
                      indent=2, ensure_ascii=False)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
