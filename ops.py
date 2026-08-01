"""
OCI operations (the action layer).

Every function here is ONE shot: it makes the minimal API calls to do the job
and returns, letting any error (including capacity errors) propagate. There is
no looping, no retry, no background polling. The panel calls these through
Manager.guarded(account_id, op, fn), which applies the per-account rate limit.

Protocol numbers for security rules: TCP=6, UDP=17, ICMP=1, all="all".
"""
from __future__ import annotations

import datetime

import oci
from oci.core.models import (
    CreateIpv6Details,
    CreatePublicIpDetails,
    CreateVnicDetails,
    GetPublicIpByPrivateIpIdDetails,
    IngressSecurityRule,
    InstanceSourceViaImageDetails,
    LaunchInstanceDetails,
    LaunchInstanceShapeConfigDetails,
    PortRange,
    TcpOptions,
    UdpOptions,
    UpdateBootVolumeDetails,
    UpdateInstanceDetails,
    UpdateInstanceShapeConfigDetails,
    UpdateSecurityListDetails,
)

_PROTO = {"tcp": "6", "udp": "17", "icmp": "1", "all": "all"}
_PROTO_NAME = {"6": "TCP", "17": "UDP", "1": "ICMP", "all": "ALL"}


# ----- helpers -----------------------------------------------------------
def _primary_vnic(clients, instance_id, compartment_id):
    atts = clients.compute.list_vnic_attachments(
        compartment_id=compartment_id, instance_id=instance_id).data
    vnic_id = next((a.vnic_id for a in atts if a.vnic_id), None)
    if not vnic_id:
        raise RuntimeError("找不到该实例的 VNIC")
    return clients.network.get_vnic(vnic_id).data


def _primary_private_ip_id(clients, vnic_id):
    pips = clients.network.list_private_ips(vnic_id=vnic_id).data
    prim = next((p for p in pips if p.is_primary), pips[0] if pips else None)
    if not prim:
        raise RuntimeError("找不到私有 IP")
    return prim.id


# ----- instances ---------------------------------------------------------
def list_instances(clients, compartment_id):
    rows = []
    for i in clients.compute.list_instances(compartment_id=compartment_id).data:
        if i.lifecycle_state == "TERMINATED":
            continue
        ip = None
        if i.lifecycle_state == "RUNNING":
            try:
                ip = _primary_vnic(clients, i.id, compartment_id).public_ip
            except Exception:
                ip = None
        sc = i.shape_config
        rows.append({
            "id": i.id, "name": i.display_name, "state": i.lifecycle_state,
            "shape": i.shape, "ad": i.availability_domain, "public_ip": ip,
            "ocpus": getattr(sc, "ocpus", None), "memory": getattr(sc, "memory_in_gbs", None),
        })
    return rows


_ACTION = {"start": "START", "stop": "SOFTSTOP", "reboot": "SOFTRESET"}


def instance_action(clients, instance_id, action):
    return clients.compute.instance_action(instance_id, _ACTION[action]).data.lifecycle_state


def change_shape(clients, instance_id, ocpus, memory_gbs):
    details = UpdateInstanceDetails(shape_config=UpdateInstanceShapeConfigDetails(
        ocpus=float(ocpus), memory_in_gbs=float(memory_gbs)))
    clients.compute.update_instance(instance_id, details)
    return {"ocpus": ocpus, "memory_in_gbs": memory_gbs}


def snapshot_profile(clients, instance_id, compartment_id):
    """Capture a re-launchable spec from a live instance."""
    inst = clients.compute.get_instance(instance_id).data
    vnic = _primary_vnic(clients, instance_id, compartment_id)
    sc = inst.shape_config
    return {
        "availability_domain": inst.availability_domain,
        "compartment_id": inst.compartment_id,
        "shape": inst.shape,
        "shape_config": ({"ocpus": sc.ocpus, "memory_in_gbs": sc.memory_in_gbs} if sc else None),
        "image_id": inst.image_id,
        "subnet_id": vnic.subnet_id,
        "display_name": inst.display_name,
        "ssh_authorized_keys": (inst.metadata or {}).get("ssh_authorized_keys"),
        "assign_public_ip": bool(vnic.public_ip),
    }


def launch_from_profile(clients, p):
    """ONE launch attempt. Capacity errors propagate -- no retry loop."""
    details = LaunchInstanceDetails(
        availability_domain=p["availability_domain"],
        compartment_id=p["compartment_id"],
        shape=p["shape"],
        source_details=InstanceSourceViaImageDetails(image_id=p["image_id"]),
        create_vnic_details=CreateVnicDetails(
            subnet_id=p["subnet_id"], assign_public_ip=p.get("assign_public_ip", True)),
        display_name=p.get("display_name"),
    )
    if p.get("shape_config"):
        details.shape_config = LaunchInstanceShapeConfigDetails(**p["shape_config"])
    if p.get("ssh_authorized_keys"):
        details.metadata = {"ssh_authorized_keys": p["ssh_authorized_keys"]}
    inst = clients.compute.launch_instance(details).data
    return {"id": inst.id, "name": inst.display_name, "state": inst.lifecycle_state}


def rebuild_instance(clients, instance_id, compartment_id, preserve_boot_volume=False):
    """Snapshot -> terminate -> ONE launch attempt. If capacity is unavailable
    the launch fails and you retry manually; nothing loops."""
    profile = snapshot_profile(clients, instance_id, compartment_id)
    clients.compute.terminate_instance(instance_id, preserve_boot_volume=preserve_boot_volume)
    return launch_from_profile(clients, profile)


def clone_instance(clients, instance_id, compartment_id):
    """Snapshot an existing instance and launch a NEW one from it (original kept).
    ONE launch attempt. This is the safe 'create' path."""
    profile = snapshot_profile(clients, instance_id, compartment_id)
    profile["display_name"] = (profile.get("display_name") or "instance") + "-clone"
    return launch_from_profile(clients, profile)


def terminate_instance(clients, instance_id, preserve_boot_volume=False):
    clients.compute.terminate_instance(instance_id, preserve_boot_volume=preserve_boot_volume)
    return {"ok": True}


# ----- public IP (manual change) -----------------------------------------
def get_public_ip(clients, instance_id, compartment_id):
    vnic = _primary_vnic(clients, instance_id, compartment_id)
    return vnic.public_ip


def change_public_ip(clients, instance_id, compartment_id):
    """Release the current ephemeral public IP and assign a fresh one.
    ONE swap per call. Only works on EPHEMERAL IPs (the free-tier default)."""
    vnic = _primary_vnic(clients, instance_id, compartment_id)
    priv_id = _primary_private_ip_id(clients, vnic.id)
    try:
        cur = clients.network.get_public_ip_by_private_ip_id(
            GetPublicIpByPrivateIpIdDetails(private_ip_id=priv_id)).data
    except oci.exceptions.ServiceError as e:
        if e.status != 404:
            raise
        cur = None
    if cur:
        if cur.lifetime != "EPHEMERAL":
            raise RuntimeError("当前是保留(Reserved)IP，不能这样直接更换；本工具只换临时(Ephemeral)IP")
        clients.network.delete_public_ip(cur.id)
    new = clients.network.create_public_ip(CreatePublicIpDetails(
        compartment_id=compartment_id, lifetime="EPHEMERAL", private_ip_id=priv_id)).data
    return new.ip_address


def attach_ipv6(clients, instance_id, compartment_id):
    vnic = _primary_vnic(clients, instance_id, compartment_id)
    ip6 = clients.network.create_ipv6(CreateIpv6Details(vnic_id=vnic.id)).data
    return ip6.ip_address


# ----- firewall (security list ingress) ----------------------------------
def _subnet_security_lists(clients, instance_id, compartment_id):
    vnic = _primary_vnic(clients, instance_id, compartment_id)
    subnet = clients.network.get_subnet(vnic.subnet_id).data
    return subnet.security_list_ids


def list_ingress(clients, instance_id, compartment_id):
    out = []
    for sl_id in _subnet_security_lists(clients, instance_id, compartment_id):
        sl = clients.network.get_security_list(sl_id).data
        rules = []
        for idx, r in enumerate(sl.ingress_security_rules):
            port = ""
            if r.tcp_options and r.tcp_options.destination_port_range:
                pr = r.tcp_options.destination_port_range
                port = f"{pr.min}-{pr.max}" if pr.min != pr.max else str(pr.min)
            elif r.udp_options and r.udp_options.destination_port_range:
                pr = r.udp_options.destination_port_range
                port = f"{pr.min}-{pr.max}" if pr.min != pr.max else str(pr.min)
            rules.append({
                "index": idx,
                "protocol": _PROTO_NAME.get(r.protocol, r.protocol),
                "source": r.source, "port": port or "*",
                "description": r.description or "",
            })
        out.append({"security_list_id": sl_id, "name": sl.display_name, "rules": rules})
    return out


def add_ingress(clients, security_list_id, protocol, port, source, description=""):
    sl = clients.network.get_security_list(security_list_id).data
    rules = sl.ingress_security_rules
    proto = _PROTO.get(protocol.lower())
    if not proto:
        raise ValueError("协议只支持 tcp/udp/icmp/all")
    rule = IngressSecurityRule(
        protocol=proto, source=source, source_type="CIDR_BLOCK",
        is_stateless=False, description=description or None)
    if protocol.lower() in ("tcp", "udp") and port:
        pr = PortRange(min=int(port), max=int(port))
        if protocol.lower() == "tcp":
            rule.tcp_options = TcpOptions(destination_port_range=pr)
        else:
            rule.udp_options = UdpOptions(destination_port_range=pr)
    rules.append(rule)
    clients.network.update_security_list(
        security_list_id, UpdateSecurityListDetails(ingress_security_rules=rules))
    return {"ok": True, "count": len(rules)}


def delete_ingress(clients, security_list_id, index):
    sl = clients.network.get_security_list(security_list_id).data
    rules = sl.ingress_security_rules
    if index < 0 or index >= len(rules):
        raise ValueError("规则序号不存在")
    del rules[index]
    clients.network.update_security_list(
        security_list_id, UpdateSecurityListDetails(ingress_security_rules=rules))
    return {"ok": True, "count": len(rules)}


# ----- boot volume -------------------------------------------------------
def get_boot_volume(clients, instance_id, compartment_id, ad):
    atts = clients.compute.list_boot_volume_attachments(
        availability_domain=ad, compartment_id=compartment_id, instance_id=instance_id).data
    if not atts:
        raise RuntimeError("找不到引导卷")
    bv = clients.block.get_boot_volume(atts[0].boot_volume_id).data
    return {"id": bv.id, "size_gbs": bv.size_in_gbs, "vpus_per_gb": bv.vpus_per_gb}


def resize_boot_volume(clients, boot_volume_id, size_gbs):
    clients.block.update_boot_volume(
        boot_volume_id, UpdateBootVolumeDetails(size_in_gbs=int(size_gbs)))
    return {"ok": True, "size_gbs": int(size_gbs)}


# ----- quota / limits (read-only) ----------------------------------------
def compute_limits(clients, compartment_id, tenancy_id):
    out = []
    try:
        vals = clients.limits.list_limit_values(
            compartment_id=tenancy_id, service_name="compute").data
        for v in vals:
            out.append({"name": v.name, "ad": v.availability_domain or "-", "value": v.value})
    except oci.exceptions.ServiceError as e:
        raise RuntimeError(f"配额查询失败: {e.message}")
    return out


# ----- traffic (read-only, best-effort) ----------------------------------
def traffic(clients, compartment_id, hours=6):
    """VNIC in/out bytes over the last N hours (oci_vcn namespace)."""
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(hours=hours)
    from oci.monitoring.models import SummarizeMetricsDataDetails
    series = {}
    for metric in ("VnicBytesIn", "VnicBytesOut"):
        try:
            det = SummarizeMetricsDataDetails(
                namespace="oci_vcn",
                query=f"{metric}[1m].sum()",
                start_time=start, end_time=end)
            res = clients.monitoring.summarize_metrics_data(
                compartment_id=compartment_id, summarize_metrics_data_details=det).data
            total = 0.0
            for md in res:
                total += sum(p.value for p in md.aggregated_datapoints)
            series[metric] = total
        except oci.exceptions.ServiceError as e:
            series[metric] = None
    return {"hours": hours, "bytes_in": series.get("VnicBytesIn"),
            "bytes_out": series.get("VnicBytesOut")}
