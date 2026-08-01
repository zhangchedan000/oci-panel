"""Load accounts.yaml -> Manager. Secrets stay on disk, never in the repo."""
from __future__ import annotations

import os
import stat

import yaml

from identity import IdentityStore
from settings import SettingsStore
from manager import Account, Egress, Manager, RateLimiter


def _oci_config(a: dict) -> dict:
    cfg = {
        "user": a["user"],
        "tenancy": a["tenancy"],
        "fingerprint": a["fingerprint"],
        "region": a["region"],
        "key_file": os.path.expanduser(a["key_file"]),
    }
    if a.get("pass_phrase"):
        cfg["pass_phrase"] = a["pass_phrase"]
    from oci.config import validate_config
    validate_config(cfg)   # fail fast on a malformed account
    return cfg


def _check_key_perms(key_file: str, acct_id: str) -> None:
    """Refuse to start if a private key is readable by group/others.
    Better to stop loudly than to run with an exposed key on disk."""
    try:
        mode = os.stat(key_file).st_mode
    except OSError as e:
        raise SystemExit(f"账号 '{acct_id}' 的密钥文件读不到: {key_file} ({e})")
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(
            f"账号 '{acct_id}' 的密钥 {key_file} 权限过松(组/其他人可访问)。"
            f"\n请执行:  chmod 600 {key_file}   然后重启。"
        )


def _dump_yaml(path: str, raw: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def add_account(path: str, acct: dict) -> None:
    """Append a new account to accounts.yaml (used by the in-panel add form)."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw.setdefault("accounts", [])
    if any(a.get("id") == acct["id"] for a in raw["accounts"]):
        raise ValueError(f"账号 id '{acct['id']}' 已存在")
    raw["accounts"].append(acct)
    _dump_yaml(path, raw)


def remove_account(path: str, acct_id: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    before = len(raw.get("accounts", []))
    raw["accounts"] = [a for a in raw.get("accounts", []) if a.get("id") != acct_id]
    if len(raw["accounts"]) == before:
        raise ValueError(f"账号 '{acct_id}' 不存在")
    _dump_yaml(path, raw)


def load(path: str) -> tuple[Manager, str]:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    panel = raw.get("panel", {}) or {}
    username = str(panel.get("username", "admin")).strip() or "admin"
    pw_hash = str(panel.get("password_hash", "")).strip()
    pw_plain = str(panel.get("password", "")).strip()
    configured = bool(pw_hash) or bool(pw_plain and pw_plain != "change-me")
    # not configured => panel starts in first-run web-setup mode (token-gated), no SystemExit
    panel_auth = {"username": username, "hash": pw_hash or None,
                  "plain": pw_plain or None, "configured": configured}

    # identity.json lives next to accounts.yaml, local only
    base_dir = os.path.dirname(os.path.abspath(path))
    ident = IdentityStore(os.path.join(base_dir, "identity.json"))
    settings = SettingsStore(os.path.join(base_dir, "settings.json"))

    accounts: dict[str, Account] = {}
    for a in raw.get("accounts", []):
        # egress: panel-saved value (settings.json) wins over the yaml default
        saved = settings.get_egress(a["id"])
        if saved:
            egress = Egress.from_dict(saved)
        else:
            eg = a.get("egress", {}) or {}
            egress = Egress(proxy=eg.get("proxy"), source_ip=eg.get("source_ip"))
        if not egress.proxy and not egress.source_ip:
            print(f"[warn] account '{a['id']}' 没有分配独立出口 IP —— 多账号共用同一 IP 会造成关联风险")

        cfg = _oci_config(a)
        _check_key_perms(cfg["key_file"], a["id"])

        lim = a.get("limits", {}) or {}
        limiter = RateLimiter(
            min_interval_s=float(lim.get("min_interval_s", 3)),
            hourly=lim.get("hourly", {}) or {},
        )
        # fixed per-account fingerprint: generated once, tied to this key,
        # changes only when the key changes (delete + re-import)
        user_agent = ident.resolve_ua(a["id"], cfg["fingerprint"])

        accounts[a["id"]] = Account(
            id=a["id"],
            oci_config=cfg,
            compartment_id=a.get("compartment") or cfg["tenancy"],
            egress=egress,
            user_agent=user_agent,
            limiter=limiter,
        )

    ident.prune(accounts.keys())   # drop identities for accounts no longer configured
    # zero accounts is OK: the panel starts empty and accounts are added via the web UI
    mgr = Manager(accounts)
    mgr.settings = settings        # so the app can persist egress edits
    return mgr, panel_auth
