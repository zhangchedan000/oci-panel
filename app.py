"""
OCI panel backend (skeleton v1).

Scope of this first version, on purpose:
  - login
  - list accounts (ids + which egress each uses)
  - per account: list instances (state + public IP), start / stop / reboot
Everything routes through Manager.guarded(account_id, op, fn):
one named account per call, per-account egress, per-account rate limit,
no SDK auto-retry, no cross-account loop.
"""
from __future__ import annotations

import os
import secrets

import oci
from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config import load
from manager import Egress, RateLimited
import ops
import auth

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.environ.get("OCI_PANEL_CONFIG", os.path.join(HERE, "accounts.yaml"))

manager, PANEL_AUTH = load(CFG_PATH)

# First-run setup: if no password is configured yet, generate a one-time setup
# token so ONLY whoever can see the server logs/terminal can set the initial
# password (prevents a public scanner from claiming the panel first).
SETUP_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(CFG_PATH)), "setup_token.txt")
SETUP_TOKEN = None
if not PANEL_AUTH["configured"]:
    SETUP_TOKEN = secrets.token_urlsafe(24)
    try:
        with open(SETUP_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(SETUP_TOKEN + "\n")
        os.chmod(SETUP_TOKEN_FILE, 0o600)
    except OSError:
        pass
    print(f"[setup] 面板尚未设置密码。首次设置令牌: {SETUP_TOKEN}", flush=True)
    print("[setup] 打开控制台后输入此令牌 + 你要设的用户名/密码完成初始化。", flush=True)

app = FastAPI(title="OCI Panel", docs_url=None, redoc_url=None, openapi_url=None)
_sessions: set[str] = set()          # in-memory session tokens (single-user panel)


# ---- auth ---------------------------------------------------------------
def require_auth(session: str | None = Cookie(default=None)):
    if not session or session not in _sessions:
        raise HTTPException(status_code=401, detail="未登录")
    return True


@app.get("/api/status")
def status():
    return {"configured": PANEL_AUTH["configured"]}


@app.get("/api/me")
def me(_=Depends(require_auth)):
    return {"username": PANEL_AUTH["username"]}


class Setup(BaseModel):
    token: str
    username: str = "admin"
    password: str


@app.post("/api/setup")
def setup(body: Setup):
    if PANEL_AUTH["configured"]:
        raise HTTPException(status_code=400, detail="已初始化，请直接登录")
    if not SETUP_TOKEN or not secrets.compare_digest((body.token or "").strip(), SETUP_TOKEN):
        raise HTTPException(status_code=401, detail="设置令牌不正确（见安装终端/日志）")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    uname = (body.username or "admin").strip() or "admin"
    h = auth.hash_password(body.password)
    auth.rewrite_panel(CFG_PATH, username=uname, password_hash=h)
    PANEL_AUTH.update(username=uname, hash=h, plain=None, configured=True)
    try:
        os.remove(SETUP_TOKEN_FILE)
    except OSError:
        pass
    return {"ok": True}


class Login(BaseModel):
    username: str = "admin"
    password: str


@app.post("/api/login")
def login(body: Login, response: Response):
    ok_user = secrets.compare_digest(body.username or "", PANEL_AUTH["username"])
    if PANEL_AUTH["hash"]:
        ok_pw = auth.verify_password(body.password, PANEL_AUTH["hash"])
    else:
        ok_pw = secrets.compare_digest(body.password, PANEL_AUTH["plain"] or "")
    if not (ok_user and ok_pw):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400)
    return {"ok": True}


class ChangePw(BaseModel):
    old_password: str
    new_password: str | None = None
    new_username: str | None = None


@app.post("/api/change-password")
def change_password(body: ChangePw, _=Depends(require_auth)):
    if PANEL_AUTH["hash"]:
        ok = auth.verify_password(body.old_password, PANEL_AUTH["hash"])
    else:
        ok = secrets.compare_digest(body.old_password, PANEL_AUTH["plain"] or "")
    if not ok:
        raise HTTPException(status_code=401, detail="原密码不正确")

    new_username = (body.new_username or "").strip()
    new_password = body.new_password or ""
    if not new_username and not new_password:
        raise HTTPException(status_code=400, detail="没有要修改的内容")
    if new_password and len(new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")

    kwargs = {}
    if new_username:
        kwargs["username"] = new_username
    if new_password:
        kwargs["password_hash"] = auth.hash_password(new_password)
    auth.rewrite_panel(CFG_PATH, **kwargs)
    if new_username:
        PANEL_AUTH["username"] = new_username
    if new_password:
        PANEL_AUTH.update(hash=kwargs["password_hash"], plain=None)
    PANEL_AUTH["configured"] = True
    return {"ok": True, "username": PANEL_AUTH["username"]}


@app.post("/api/logout")
def logout(response: Response, session: str | None = Cookie(default=None)):
    _sessions.discard(session or "")
    response.delete_cookie("session")
    return {"ok": True}


# ---- accounts -----------------------------------------------------------
@app.get("/api/accounts")
def accounts(_=Depends(require_auth)):
    return manager.account_ids()


BASE_DIR = os.path.dirname(os.path.abspath(CFG_PATH))
import config as _config
import re as _re


def reload_manager():
    global manager, PANEL_AUTH
    manager, PANEL_AUTH = load(CFG_PATH)


class AddAccount(BaseModel):
    id: str
    oci_config: str                  # pasted [DEFAULT] block from the console
    private_key: str                 # PEM content (pasted or read from a .pem file)
    compartment: str | None = None
    egress_mode: str = "proxy"       # proxy | source_ip | direct
    proxy: str | None = None         # frontend assembles this from protocol/host/port/user/pass
    source_ip: str | None = None


def _parse_oci_config(text: str) -> dict:
    """Extract user/fingerprint/tenancy/region from a pasted console config block.
    Ignores key_file and comments. Raises ValueError if anything is missing."""
    out = {}
    for key in ("user", "fingerprint", "tenancy", "region"):
        m = _re.search(rf"^\s*{key}\s*=\s*([^\s#]+)", text, _re.MULTILINE)
        if not m:
            raise ValueError(f"配置里缺少 {key}（请把控制台的 [DEFAULT] 段整段粘贴）")
        out[key] = m.group(1).strip()
    return out


@app.post("/api/accounts")
def add_account(body: AddAccount, _=Depends(require_auth)):
    if not _re.match(r"^[A-Za-z0-9_-]{1,40}$", body.id):
        raise HTTPException(status_code=400, detail="账号名只能用字母/数字/下划线/横线，长度 1-40")
    if body.id in manager._accounts:
        raise HTTPException(status_code=400, detail=f"账号 '{body.id}' 已存在")
    if "-----BEGIN" not in body.private_key or "PRIVATE KEY-----" not in body.private_key:
        raise HTTPException(status_code=400, detail="私钥格式不对（应是 -----BEGIN ... PRIVATE KEY----- 的 PEM）")
    try:
        parsed = _parse_oci_config(body.oci_config)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # validate egress shape early
    eg = {"mode": body.egress_mode, "proxy": body.proxy, "source_ip": body.source_ip}
    try:
        Egress.from_dict(eg)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # write private key
    keys_dir = os.path.join(BASE_DIR, "keys")
    os.makedirs(keys_dir, exist_ok=True)
    try:
        os.chmod(keys_dir, 0o700)
    except OSError:
        pass
    key_path = os.path.join(keys_dir, body.id + ".pem")
    with open(key_path, "w", encoding="utf-8") as f:
        f.write(body.private_key.strip() + "\n")
    os.chmod(key_path, 0o600)

    acct = {"id": body.id, "region": parsed["region"],
            "tenancy": parsed["tenancy"], "user": parsed["user"],
            "fingerprint": parsed["fingerprint"], "key_file": key_path}
    if body.compartment:
        acct["compartment"] = body.compartment.strip()
    if body.egress_mode == "proxy":
        acct["egress"] = {"proxy": body.proxy.strip()}
    elif body.egress_mode == "source_ip":
        acct["egress"] = {"source_ip": body.source_ip.strip()}
    else:
        acct["egress"] = {}

    try:
        _config.add_account(CFG_PATH, acct)
        reload_manager()          # validates creds format via oci; assigns fingerprint
    except HTTPException:
        raise
    except BaseException as e:     # any failure -> roll back the entry + key, never leave a broken config
        try:
            _config.remove_account(CFG_PATH, body.id)
        except Exception:
            pass
        try:
            os.remove(key_path)
        except OSError:
            pass
        msg = str(e) or e.__class__.__name__
        raise HTTPException(status_code=400, detail=f"账号信息有误（请检查配置和私钥）：{msg}")
    return {"ok": True, "id": body.id}


class DelAccount(BaseModel):
    delete_key: bool = True


@app.post("/api/accounts/{acct_id}/delete")
def delete_account(acct_id: str, body: DelAccount, _=Depends(require_auth)):
    try:
        acct = manager.get(acct_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="账号不存在")
    key_file = acct.oci_config.get("key_file")
    _config.remove_account(CFG_PATH, acct_id)
    if body.delete_key and key_file:
        kf = os.path.abspath(key_file)
        if kf.startswith(os.path.join(BASE_DIR, "keys")) and os.path.exists(kf):
            try:
                os.remove(kf)
            except OSError:
                pass
    reload_manager()          # identity.prune() drops the removed account's fingerprint
    return {"ok": True}


# ---- instance ops (all single-shot, rate-limited, per-account) -----------
def _run(acct_id, op_name, fn, ok_extra=None):
    """Run an op through the account guard; map errors to HTTP consistently."""
    try:
        result = manager.guarded(acct_id, op_name, fn)
        return result if isinstance(result, dict) else {"ok": True, "result": result}
    except RateLimited as e:
        return JSONResponse(status_code=429, content={"detail": str(e)})
    except oci.exceptions.ServiceError as e:
        raise HTTPException(status_code=502, detail=f"OCI: {e.message}")
    except oci.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"网络/代理不通（检查该账号的出口 IP 设置）: {e}")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/{acct_id}/instances")
def list_instances(acct_id: str, _=Depends(require_auth)):
    cid = manager.get(acct_id).compartment_id
    r = _run(acct_id, "list", lambda c: {"rows": ops.list_instances(c, cid)})
    return r["rows"] if isinstance(r, dict) and "rows" in r else r


class Action(BaseModel):
    action: str    # start | stop | reboot


@app.post("/api/{acct_id}/instances/{iid}/action")
def instance_action(acct_id: str, iid: str, body: Action, _=Depends(require_auth)):
    if body.action not in ("start", "stop", "reboot"):
        raise HTTPException(status_code=400, detail="不支持的操作")
    return _run(acct_id, "action", lambda c: {"ok": True, "state": ops.instance_action(c, iid, body.action)})


@app.post("/api/{acct_id}/instances/{iid}/change-ip")
def change_ip(acct_id: str, iid: str, _=Depends(require_auth)):
    cid = manager.get(acct_id).compartment_id
    return _run(acct_id, "change_ip", lambda c: {"ok": True, "ip": ops.change_public_ip(c, iid, cid)})


@app.post("/api/{acct_id}/instances/{iid}/ipv6")
def add_ipv6(acct_id: str, iid: str, _=Depends(require_auth)):
    cid = manager.get(acct_id).compartment_id
    return _run(acct_id, "action", lambda c: {"ok": True, "ipv6": ops.attach_ipv6(c, iid, cid)})


class ShapeBody(BaseModel):
    ocpus: float
    memory_in_gbs: float


@app.post("/api/{acct_id}/instances/{iid}/shape")
def change_shape(acct_id: str, iid: str, body: ShapeBody, _=Depends(require_auth)):
    return _run(acct_id, "action",
                lambda c: {"ok": True, **ops.change_shape(c, iid, body.ocpus, body.memory_in_gbs)})


class RebuildBody(BaseModel):
    preserve_boot_volume: bool = False


@app.post("/api/{acct_id}/instances/{iid}/rebuild")
def rebuild(acct_id: str, iid: str, body: RebuildBody, _=Depends(require_auth)):
    cid = manager.get(acct_id).compartment_id
    return _run(acct_id, "create",
                lambda c: ops.rebuild_instance(c, iid, cid, body.preserve_boot_volume))


@app.post("/api/{acct_id}/instances/{iid}/clone")
def clone(acct_id: str, iid: str, _=Depends(require_auth)):
    cid = manager.get(acct_id).compartment_id
    return _run(acct_id, "create", lambda c: ops.clone_instance(c, iid, cid))


class TermBody(BaseModel):
    preserve_boot_volume: bool = False


@app.post("/api/{acct_id}/instances/{iid}/terminate")
def terminate(acct_id: str, iid: str, body: TermBody, _=Depends(require_auth)):
    return _run(acct_id, "action", lambda c: ops.terminate_instance(c, iid, body.preserve_boot_volume))


# ---- boot volume --------------------------------------------------------
@app.get("/api/{acct_id}/instances/{iid}/boot-volume")
def boot_volume(acct_id: str, iid: str, ad: str, _=Depends(require_auth)):
    cid = manager.get(acct_id).compartment_id
    return _run(acct_id, "list", lambda c: ops.get_boot_volume(c, iid, cid, ad))


class BVResize(BaseModel):
    boot_volume_id: str
    size_gbs: int


@app.post("/api/{acct_id}/boot-volume/resize")
def boot_volume_resize(acct_id: str, body: BVResize, _=Depends(require_auth)):
    return _run(acct_id, "action", lambda c: ops.resize_boot_volume(c, body.boot_volume_id, body.size_gbs))


# ---- firewall (security list ingress) -----------------------------------
@app.get("/api/{acct_id}/instances/{iid}/firewall")
def firewall_list(acct_id: str, iid: str, _=Depends(require_auth)):
    cid = manager.get(acct_id).compartment_id
    return _run(acct_id, "list", lambda c: {"lists": ops.list_ingress(c, iid, cid)})


class IngressAdd(BaseModel):
    security_list_id: str
    protocol: str            # tcp | udp | icmp | all
    port: str | None = None
    source: str = "0.0.0.0/0"
    description: str = ""


@app.post("/api/{acct_id}/firewall/add")
def firewall_add(acct_id: str, body: IngressAdd, _=Depends(require_auth)):
    return _run(acct_id, "action",
                lambda c: ops.add_ingress(c, body.security_list_id, body.protocol, body.port,
                                          body.source, body.description))


class IngressDel(BaseModel):
    security_list_id: str
    index: int


@app.post("/api/{acct_id}/firewall/delete")
def firewall_delete(acct_id: str, body: IngressDel, _=Depends(require_auth)):
    return _run(acct_id, "action", lambda c: ops.delete_ingress(c, body.security_list_id, body.index))


# ---- quota / traffic (read-only) ----------------------------------------
@app.get("/api/{acct_id}/quota")
def quota(acct_id: str, _=Depends(require_auth)):
    acct = manager.get(acct_id)
    tid = acct.oci_config["tenancy"]
    return _run(acct_id, "list", lambda c: {"limits": ops.compute_limits(c, acct.compartment_id, tid)})


@app.get("/api/{acct_id}/traffic")
def traffic(acct_id: str, hours: int = 6, _=Depends(require_auth)):
    cid = manager.get(acct_id).compartment_id
    return _run(acct_id, "list", lambda c: ops.traffic(c, cid, hours))


# ---- egress (IP binding), editable from the panel -----------------------
@app.get("/api/{acct_id}/egress")
def get_egress(acct_id: str, _=Depends(require_auth)):
    acct = manager.get(acct_id)
    return acct.egress.to_dict()          # never returns key material


class EgressBody(BaseModel):
    mode: str                              # proxy | source_ip | direct
    proxy: str | None = None
    source_ip: str | None = None


@app.post("/api/{acct_id}/egress")
def set_egress(acct_id: str, body: EgressBody, _=Depends(require_auth)):
    manager.get(acct_id)                   # 404 if unknown
    try:
        egress = Egress.from_dict(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    desc = manager.set_egress(acct_id, egress)     # live rebuild
    if manager.settings:
        manager.settings.set_egress(acct_id, egress.to_dict())   # persist locally
    return {"ok": True, "egress": desc}


@app.get("/api/{acct_id}/egress/check")
def check_egress(acct_id: str, _=Depends(require_auth)):
    manager.get(acct_id)
    try:
        return {"ip": manager.check_egress(acct_id)}
    except RateLimited as e:
        return JSONResponse(status_code=429, content={"detail": str(e)})
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- panel (single self-contained html at repo root) --------------------
@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "index.html"))
