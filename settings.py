"""
Panel-editable settings, persisted locally (settings.json, chmod 600, gitignored).

Right now it holds each account's egress (the IP binding), so it can be changed
from the web UI instead of hand-editing accounts.yaml. accounts.yaml egress is
just the initial default; once you edit egress in the panel, the value here wins.
Credentials/keys are NEVER stored here -- they stay in accounts.yaml / keys/.
"""
from __future__ import annotations

import json
import os


class SettingsStore:
    def __init__(self, path: str):
        self.path = path
        self.data: dict = {"egress": {}}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        self.data.setdefault("egress", {})

    def get_egress(self, account_id: str) -> dict | None:
        return self.data["egress"].get(account_id)

    def set_egress(self, account_id: str, egress: dict) -> None:
        self.data["egress"][account_id] = egress
        self._save()

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
