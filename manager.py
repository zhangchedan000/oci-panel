"""
Isolation core.

Design rule: each account is a SEALED unit.
  - its own OCI credentials
  - its own egress IP (proxy or bound source IP)
  - its own client instances (never shared / reused across accounts)
  - its own rate limiter
There is deliberately NO method that loops over all accounts to act.
Every operation must name exactly one account id. This kills two things
at once: cross-tenant batch behavior (the association / ban signal) and
accidental "act on the wrong account" mistakes.
"""
from __future__ import annotations

import random
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

import oci
import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager


# --------------------------------------------------------------------------
# Egress isolation
# --------------------------------------------------------------------------
class _SourceIPAdapter(HTTPAdapter):
    """Force all requests on this session to leave from a specific source IP.
    Used when one host has several public IPs and each account binds one."""

    def __init__(self, source_ip: str, **kw):
        self._source_ip = source_ip
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            source_address=(self._source_ip, 0),
            **kw,
        )


@dataclass
class Egress:
    proxy: str | None = None       # e.g. "http://user:pass@1.2.3.4:8080"  (recommended)
    source_ip: str | None = None   # e.g. "5.6.7.8"  (single host, multi-IP)

    def apply(self, session: requests.Session) -> None:
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        elif self.source_ip:
            session.mount("https://", _SourceIPAdapter(self.source_ip))
            session.mount("http://", _SourceIPAdapter(self.source_ip))

    def describe(self) -> str:
        if self.proxy:
            # hide credentials when showing in UI
            shown = self.proxy.split("@")[-1] if "@" in self.proxy else self.proxy
            return f"proxy:{shown}"
        if self.source_ip:
            return f"source_ip:{self.source_ip}"
        return "DIRECT (no isolation!)"

    def to_dict(self) -> dict:
        if self.proxy:
            return {"mode": "proxy", "proxy": self.proxy}
        if self.source_ip:
            return {"mode": "source_ip", "source_ip": self.source_ip}
        return {"mode": "direct"}

    def new_session(self) -> requests.Session:
        """A standalone session bound to this egress -- used by the self-check."""
        s = requests.Session()
        self.apply(s)
        return s

    @classmethod
    def from_dict(cls, d: dict | None) -> "Egress":
        if not d:
            return cls()
        mode = d.get("mode")
        if mode == "proxy":
            proxy = (d.get("proxy") or "").strip()
            if not _valid_proxy(proxy):
                raise ValueError("代理地址格式不对，应形如 http://用户:密码@IP:端口 或 socks5://IP:端口")
            return cls(proxy=proxy)
        if mode == "source_ip":
            ip = (d.get("source_ip") or "").strip()
            _valid_ip(ip)
            return cls(source_ip=ip)
        if mode == "direct":
            return cls()
        raise ValueError("未知的出口类型")


def _valid_proxy(p: str) -> bool:
    import re
    return bool(re.match(r"^(https?|socks5h?)://[^\s]+:\d+$", p or ""))


def _valid_ip(ip: str) -> None:
    import ipaddress
    ipaddress.ip_address(ip)   # raises ValueError if not a valid IP


# --------------------------------------------------------------------------
# Per-account rate limiting  (manual-single-shot guardrail)
# --------------------------------------------------------------------------
class RateLimiter:
    """min interval + jitter between calls, plus hard hourly caps per op type.
    Even if the user hammers a button, this makes high-frequency impossible."""

    def __init__(self, min_interval_s: float, hourly: dict[str, int]):
        self.min_interval_s = min_interval_s
        self.hourly = hourly or {}
        self._last = 0.0
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, op: str) -> None:
        with self._lock:
            now = time.time()

            # 1) hourly hard cap for this op
            cap = self.hourly.get(op)
            if cap is not None:
                q = self._hits[op]
                while q and now - q[0] > 3600:
                    q.popleft()
                if len(q) >= cap:
                    wait = int(3600 - (now - q[0]))
                    raise RateLimited(f"{op} 已达每小时上限 {cap} 次，请 {wait}s 后再试")

            # 2) global min interval between any two calls (+jitter)
            gap = now - self._last
            need = self.min_interval_s + random.uniform(0, self.min_interval_s * 0.4)
            if gap < need:
                raise RateLimited(f"操作太频繁，请 {need - gap:.1f}s 后再试")

    def commit(self, op: str) -> None:
        with self._lock:
            now = time.time()
            self._last = now
            if op in self.hourly:
                self._hits[op].append(now)


class RateLimited(Exception):
    pass


# --------------------------------------------------------------------------
# Account + bound clients
# --------------------------------------------------------------------------
@dataclass
class Clients:
    compute: oci.core.ComputeClient
    network: oci.core.VirtualNetworkClient
    identity: oci.identity.IdentityClient
    block: oci.core.BlockstorageClient
    limits: oci.limits.LimitsClient
    monitoring: oci.monitoring.MonitoringClient


@dataclass
class Account:
    id: str
    oci_config: dict
    compartment_id: str
    egress: Egress = field(default_factory=Egress)
    user_agent: str = "oci-panel/1.0"
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(3.0, {}))


def build_clients(acct: Account) -> Clients:
    """Create OCI clients whose HTTP session is isolated to this account:
    its own egress + UA, and NO SDK auto-retry (single-shot principle)."""
    no_retry = oci.retry.NoneRetryStrategy()

    def make(cls):
        client = cls(acct.oci_config, retry_strategy=no_retry)
        # UA MUST be set on base_client: the SDK overrides the session header
        # per request, so setting session.headers["User-Agent"] would not stick.
        client.base_client.user_agent = acct.user_agent   # stable per-account fingerprint
        acct.egress.apply(client.base_client.session)      # bind egress per account
        return client

    return Clients(
        compute=make(oci.core.ComputeClient),
        network=make(oci.core.VirtualNetworkClient),
        identity=make(oci.identity.IdentityClient),
        block=make(oci.core.BlockstorageClient),
        limits=make(oci.limits.LimitsClient),
        monitoring=make(oci.monitoring.MonitoringClient),
    )


# --------------------------------------------------------------------------
# Manager  (the only entry point the app uses)
# --------------------------------------------------------------------------
class Manager:
    def __init__(self, accounts: dict[str, Account]):
        self._accounts = accounts
        self._clients: dict[str, Clients] = {}     # lazily built, cached per account
        self._lock = threading.Lock()
        self.settings = None                        # SettingsStore, attached by config.load

    def account_ids(self) -> list[dict]:
        return [
            {
                "id": a.id,
                "region": a.oci_config.get("region"),
                "egress": a.egress.describe(),          # the account's assigned IP
                "fingerprint": a.user_agent,            # the account's fixed UA
            }
            for a in self._accounts.values()
        ]

    def get(self, acct_id: str) -> Account:
        if acct_id not in self._accounts:
            raise KeyError(f"unknown account: {acct_id}")
        return self._accounts[acct_id]

    def clients_for(self, acct_id: str) -> Clients:
        with self._lock:
            if acct_id not in self._clients:
                self._clients[acct_id] = build_clients(self.get(acct_id))
            return self._clients[acct_id]

    # helper: enforce this account's limiter around an op, then run it
    def guarded(self, acct_id: str, op: str, fn):
        acct = self.get(acct_id)
        acct.limiter.check(op)
        result = fn(self.clients_for(acct_id))
        acct.limiter.commit(op)
        return result

    def set_egress(self, acct_id: str, egress: "Egress") -> str:
        """Change an account's IP binding at runtime and drop its cached
        clients so the next call rebuilds through the new egress."""
        acct = self.get(acct_id)
        with self._lock:
            acct.egress = egress
            self._clients.pop(acct_id, None)   # force rebuild with new egress
        return egress.describe()

    def check_egress(self, acct_id: str) -> str:
        """Return the real public IP this account currently exits from,
        by calling an external IP-echo through the account's egress.
        Lets you confirm the binding actually took effect."""
        acct = self.get(acct_id)
        acct.limiter.check("check")
        sess = acct.egress.new_session()
        ip = None
        for url in ("https://checkip.amazonaws.com", "https://api.ipify.org"):
            try:
                r = sess.get(url, timeout=10)
                if r.ok:
                    ip = r.text.strip()
                    break
            except Exception:
                continue
        acct.limiter.commit("check")
        if not ip:
            raise RuntimeError("查不到出口 IP（代理不通或网络受限）")
        return ip
