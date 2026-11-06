#!/usr/bin/env python3
"""Homelab dashboard TUI — Proxmox VE, OPNsense, QNAP.

Pensata per un monitor 1366x768 a schermo intero (cage + foot), circa 150x42 celle.

Uso:
    dashboard.py [config.toml]      # dati reali
    dashboard.py --demo             # dati finti, per provare il layout
"""
from __future__ import annotations

import asyncio
import random
import re
import sys
import time
import tomllib
from collections import deque
from datetime import datetime
from pathlib import Path

import httpx
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Grid, Vertical
from textual.widgets import Sparkline, Static

HISTORY = 120  # campioni WAN nella sparkline


# ───────────────────────── helper di formattazione ─────────────────────────
def hbytes(n: float) -> str:
    n = float(n or 0)
    for u in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}E"


def hbits(n: float) -> str:
    n = float(n or 0)
    for u in ("b", "Kb", "Mb", "Gb"):
        if n < 1000:
            return f"{n:5.1f} {u}/s"
        n /= 1000
    return f"{n:.1f} Tb/s"


def hdur(s: float) -> str:
    s = int(s or 0)
    d, s = divmod(s, 86400)
    h = s // 3600
    return f"{d}g{h:02d}h" if d else f"{h}h{(s % 3600) // 60:02d}m"


def level(p: float, warn: float = 70, crit: float = 90) -> str:
    return "red" if p >= crit else "yellow" if p >= warn else "green"


def bar(p: float, width: int = 10, warn: float = 70, crit: float = 90) -> Text:
    p = max(0.0, min(100.0, float(p or 0)))
    style = level(p, warn, crit)
    filled = round(p / 100 * width)
    t = Text("█" * filled, style=style)
    t.append("░" * (width - filled), style="grey30")
    t.append(f" {p:3.0f}%", style=style)
    return t


def mini_bar(v: float, vmax: float, width: int, style: str) -> Text:
    filled = round(v / vmax * width) if vmax else 0
    t = Text("▬" * filled, style=style)
    t.append(" " * (width - filled))
    return t


def first_key(d: dict, *keys, default=0):
    for k in keys:
        if k in d:
            return d[k]
    return default


_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4, "PB": 1024**5}


def parse_num(s: str) -> float:
    m = re.search(r"-?\d+(?:\.\d+)?", s or "")
    return float(m.group()) if m else 0.0


def parse_size(s: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*([KMGTP]?B)", (s or "").upper())
    return float(m.group(1)) * _UNITS[m.group(2)] if m else 0.0


# ───────────────────────── sorgenti dati ─────────────────────────
class PVE:
    """Proxmox VE via API token (ruolo PVEAuditor è sufficiente)."""

    def __init__(self, c: dict):
        self.c = c
        self.http = httpx.AsyncClient(
            base_url=c["url"].rstrip("/") + "/api2/json",
            headers={"Authorization": f"PVEAPIToken={c['token_id']}={c['token_secret']}"},
            verify=c.get("verify_tls", True),
            timeout=8,
        )
        self.prev_net: dict[int, tuple[float, int, int]] = {}

    async def _get(self, path: str):
        r = await self.http.get(path)
        r.raise_for_status()
        return r.json()["data"]

    async def fetch(self) -> dict:
        res = await self._get("/cluster/resources")
        now = time.monotonic()
        guests = []
        for g in res:
            if g.get("type") not in ("qemu", "lxc") or g.get("status") != "running":
                continue
            prev = self.prev_net.get(g.get("vmid"))
            nin, nout = g.get("netin", 0), g.get("netout", 0)
            if prev and now > prev[0]:
                dt = now - prev[0]
                g["rate"] = max(0, (nin - prev[1]) + (nout - prev[2])) * 8 / dt
            else:
                g["rate"] = 0
            self.prev_net[g.get("vmid")] = (now, nin, nout)
            guests.append(g)

        storage, seen = [], set()
        for s in res:
            if s.get("type") != "storage" or s.get("status") != "available" or not s.get("maxdisk"):
                continue
            key = s["storage"] if s.get("shared") else f"{s['node']}/{s['storage']}"
            if key in seen or s["storage"] in self.c.get("hide_storage", []):
                continue
            seen.add(key)
            s["label"] = key
            storage.append(s)

        out = {
            "nodes": sorted((r for r in res if r.get("type") == "node"), key=lambda r: r.get("node", "")),
            "guests": guests,
            "storage": sorted(storage, key=lambda s: s["label"]),
            "ceph": None,
        }
        if self.c.get("ceph"):
            try:
                out["ceph"] = await self._get("/cluster/ceph/status")
            except Exception as e:  # Ceph opzionale: non deve far cadere il pannello
                out["ceph"] = {"error": str(e)}
        return out


class OPNsense:
    """OPNsense via API key/secret (utente con privilegi Diagnostics + Gateways)."""

    def __init__(self, c: dict):
        self.c = c
        self.http = httpx.AsyncClient(
            base_url=c["url"].rstrip("/") + "/api",
            auth=(c["key"], c["secret"]),
            verify=c.get("verify_tls", True),
            timeout=15,  # /traffic/top campiona per qualche secondo
        )
        self.prev: dict[str, tuple[float, int, int]] = {}
        self.names: dict[str, str] = c.get("names", {})

    async def _get(self, path: str):
        r = await self.http.get(path)
        r.raise_for_status()
        return r.json()

    def _wan_list(self) -> list[str]:
        w = self.c.get("wan", "wan")
        items = w if isinstance(w, list) else str(w).split(",")
        return [i.strip() for i in items if i.strip()]

    @staticmethod
    def _find_if(ifs: dict, want: str) -> dict:
        """Cerca per id interno (wan, opt1...), poi per descrizione (WAN1, ...)."""
        if want in ifs:
            return ifs[want]
        for k, v in ifs.items():
            if isinstance(v, dict) and str(v.get("name", "")).lower() == want.lower():
                return v
        avail = ", ".join(f"{k}={v.get('name', '?')}" for k, v in ifs.items() if isinstance(v, dict))
        raise KeyError(f"interfaccia '{want}' non trovata; disponibili: {avail}")

    async def fetch_wan(self) -> dict:
        data = await self._get("/diagnostics/traffic/interface")
        ifs = data.get("interfaces", data)
        now = time.monotonic()
        per = []
        for name in self._wan_list():
            w = self._find_if(ifs, name)
            ib = int(first_key(w, "bytes received", "inbytes"))
            ob = int(first_key(w, "bytes transmitted", "outbytes"))
            prev = self.prev.get(name)
            rx = tx = None
            if prev and now > prev[0]:
                dt = now - prev[0]
                rx = max(0, ib - prev[1]) * 8 / dt
                tx = max(0, ob - prev[2]) * 8 / dt
            self.prev[name] = (now, ib, ob)
            per.append({"name": w.get("name", name), "rx": rx, "tx": tx})
        ready = [p for p in per if p["rx"] is not None]
        rx = sum(p["rx"] for p in ready) if len(ready) == len(per) else None
        tx = sum(p["tx"] for p in ready) if len(ready) == len(per) else None
        try:
            gws = (await self._get("/routes/gateway/status")).get("items", [])
        except Exception:
            gws = []
        return {"rx": rx, "tx": tx, "per_wan": per if len(per) > 1 else [], "gateways": gws}

    async def fetch_top(self) -> list[dict]:
        ifaces = self.c.get("top_interfaces", ["lan"])
        if isinstance(ifaces, str):
            ifaces = [i.strip() for i in ifaces.split(",") if i.strip()]
        data = await self._get(f"/diagnostics/traffic/top/{','.join(ifaces)}")
        # OPNsense risponde [] (lista PHP vuota) quando nessuna interfaccia richiesta esiste
        if isinstance(data, list):
            if not data:
                raise KeyError(f"top_interfaces {ifaces} non riconosciute: usa gli id interni (lan, opt1...)")
            contents = data
        else:
            contents = list(data.values())
        agg: dict[str, dict] = {}
        for content in contents:
            records = content.get("records", []) if isinstance(content, dict) else content
            for r in records or []:
                if not isinstance(r, dict):
                    continue
                addr = r.get("address")
                if not addr:
                    continue
                e = agg.setdefault(addr, {"addr": addr, "down": 0.0, "up": 0.0,
                                          "name": self.names.get(addr) or r.get("rname") or ""})
                # Su un'interfaccia LAN: "out" dal firewall = download dell'host
                e["down"] += float(r.get("rate_bits_out", 0) or 0)
                e["up"] += float(r.get("rate_bits_in", 0) or 0)
        return sorted(agg.values(), key=lambda e: e["down"] + e["up"], reverse=True)


class QNAP:
    """QNAP via SNMP v2c (NAS-MIB, enterprise 24681). Richiede net-snmp (snmpget/snmpwalk)."""

    BASE = "1.3.6.1.4.1.24681.1.2"

    def __init__(self, c: dict):
        self.c = c

    async def _snmp(self, cmd: str, *oids: str) -> list[str]:
        p = await asyncio.create_subprocess_exec(
            cmd, "-v2c", "-c", self.c["community"], "-Oqv", "-t", "3", "-r", "1",
            self.c["host"], *oids,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await p.communicate()
        if p.returncode:
            raise RuntimeError(err.decode().strip() or f"{cmd} rc={p.returncode}")
        return [l.strip().strip('"') for l in out.decode().splitlines() if l.strip()]

    async def fetch(self) -> dict:
        b = self.BASE
        cpu, tmem, fmem, stemp = await self._snmp(
            "snmpget", f"{b}.1.0", f"{b}.2.0", f"{b}.3.0", f"{b}.6.0")
        d_name, d_temp, d_smart, v_name, v_tot, v_free, v_stat = await asyncio.gather(
            self._snmp("snmpwalk", f"{b}.11.1.2"), self._snmp("snmpwalk", f"{b}.11.1.3"),
            self._snmp("snmpwalk", f"{b}.11.1.7"), self._snmp("snmpwalk", f"{b}.17.1.2"),
            self._snmp("snmpwalk", f"{b}.17.1.4"), self._snmp("snmpwalk", f"{b}.17.1.5"),
            self._snmp("snmpwalk", f"{b}.17.1.6"),
        )
        tm, fm = parse_size(tmem), parse_size(fmem)
        return {
            "cpu": parse_num(cpu),
            "mem": (tm - fm) / tm * 100 if tm else 0,
            "mem_total": tm,
            "temp": parse_num(stemp),
            "disks": [{"name": n, "temp": parse_num(t), "smart": s}
                      for n, t, s in zip(d_name, d_temp, d_smart)],
            "volumes": [{"name": n, "total": parse_size(t), "free": parse_size(f), "status": st}
                        for n, t, f, st in zip(v_name, v_tot, v_free, v_stat)],
        }


class QNAPs:
    """Uno o più QNAP; un NAS irraggiungibile non nasconde gli altri."""

    def __init__(self, c):
        items = c if isinstance(c, list) else [c]
        self.nas = [(i.get("name") or i["host"], QNAP(i)) for i in items]
        self.interval = min(i.get("interval", 30) for i in items)

    async def fetch(self) -> list[dict]:
        async def one(name, q):
            try:
                return {"name": name, **await q.fetch()}
            except Exception as e:
                return {"name": name, "error": f"{type(e).__name__}: {e}"}
        res = await asyncio.gather(*(one(n, q) for n, q in self.nas))
        if all("error" in r for r in res):
            raise RuntimeError(res[0]["error"])
        return list(res)


# ───────────────────────── dati demo ─────────────────────────
class Demo:
    def __init__(self):
        self.t = 0

    async def pve(self) -> dict:
        nodes = [{"node": n, "status": "online", "cpu": random.uniform(.05, .6),
                  "mem": random.uniform(40, 80) * 1e9 / 100 * 64, "maxmem": 64e9,
                  "uptime": random.randint(2, 90) * 86400} for n in ("pve-a", "pve-b", "pve-c")]
        names = ["rke2-cp1", "rke2-w1", "rke2-w2", "authentik", "nextcloud", "immich", "zammad",
                 "librenms", "checkmk", "n8n", "rproxy", "netbox", "openobserve", "guac"]
        guests = [{"vmid": 100 + i, "name": n, "type": "qemu", "node": random.choice(nodes)["node"],
                   "cpu": random.betavariate(1.2, 6), "mem": random.uniform(.2, .9) * 8e9,
                   "maxmem": 8e9, "rate": random.expovariate(1 / 4e6)} for i, n in enumerate(names)]
        storage = [{"label": "ceph-rbd", "disk": 3.1e12, "maxdisk": 7.2e12},
                   {"label": "cephfs", "disk": 0.8e12, "maxdisk": 7.2e12},
                   {"label": "pve-a/local", "disk": 88e9, "maxdisk": 98e9}]
        ceph = {"health": {"status": "HEALTH_WARN", "checks": {"X": {"summary": {
            "message": "1 pool(s) have no replicas configured"}}}},
                "pgmap": {"bytes_used": 3.9e12, "bytes_total": 14.4e12}}
        return {"nodes": nodes, "guests": guests, "storage": storage, "ceph": ceph}

    async def wan(self) -> dict:
        self.t += 1
        base = 60e6 + 50e6 * (1 + __import__("math").sin(self.t / 9))
        rx, tx = base * random.uniform(.6, 1.4), base * random.uniform(.05, .3)
        return {"rx": rx, "tx": tx,
                "per_wan": [{"name": "WAN1", "rx": rx * .8, "tx": tx * .7},
                            {"name": "WAN2", "rx": rx * .2, "tx": tx * .3}],
                "gateways": [{"name": "WAN_DHCP", "status_translated": "Online",
                              "delay": f"{random.uniform(6, 9):.1f} ms", "loss": "0.0 %"},
                             {"name": "WAN_LTE", "status_translated": "Online",
                              "delay": f"{random.uniform(28, 45):.1f} ms", "loss": "1.0 %"}]}

    async def top(self) -> list[dict]:
        hosts = [("192.168.10.21", "immich"), ("192.168.10.40", "pc-ale"), ("192.168.10.12", "nextcloud"),
                 ("192.168.20.5", "tv-sala"), ("192.168.10.30", "qnap"), ("192.168.20.11", "iphone"),
                 ("192.168.10.15", "rke2-w1"), ("192.168.30.2", "ap-studio"), ("192.168.10.50", ""),
                 ("192.168.20.8", "switch-casa"), ("192.168.10.60", "gophish"), ("192.168.10.61", "")]
        rows = [{"addr": a, "name": n, "down": random.expovariate(1 / 15e6),
                 "up": random.expovariate(1 / 3e6)} for a, n in hosts]
        return sorted(rows, key=lambda e: e["down"] + e["up"], reverse=True)

    async def qnap(self) -> list[dict]:
        a = await self._qnap_one("nas-main", 4, 8)
        b = await self._qnap_one("nas-backup", 2, 6)
        return [a, b]

    async def _qnap_one(self, name: str, ndisks: int, nvol: int = 2) -> dict:
        return {"name": name, "cpu": random.uniform(3, 35), "mem": random.uniform(45, 60), "mem_total": 16 * 1024**3,
                "temp": random.uniform(38, 44),
                "disks": [{"name": f"HDD{i}", "temp": random.uniform(33, 46), "smart": "GOOD"}
                          for i in range(1, ndisks + 1)],
                "volumes": [{"name": f"{n}", "total": 4 * 1024**4,
                             "free": random.uniform(.05, .9) * 4 * 1024**4, "status": "Ready"}
                            for n in ("DataVol1", "Backup", "Media", "Foto", "VM-iSCSI",
                                      "Docker", "Archivio", "Scratch")[:nvol]]}


# ───────────────────────── rendering ─────────────────────────
def render_pve(d: dict, height: int = 11) -> Group:
    t = Table(box=None, expand=True, pad_edge=False, header_style="bold grey62")
    for col, kw in (("nodo", {}), ("CPU", {}), ("RAM", {}), ("up", {"justify": "right"})):
        t.add_column(col, **kw)
    for n in d["nodes"]:
        if n.get("status") != "online":
            t.add_row(Text(n["node"], style="bold red"), Text("OFFLINE", style="bold red"), "", "")
            continue
        mem = n.get("mem", 0) / n["maxmem"] * 100 if n.get("maxmem") else 0
        t.add_row(n["node"], bar(n.get("cpu", 0) * 100, 8), bar(mem, 8), hdur(n.get("uptime", 0)))
    parts: list = [t, Text()]

    ceph = d.get("ceph")
    if ceph:
        if "error" in ceph:
            parts.append(Text(f"Ceph: {ceph['error'][:60]}", style="red"))
        else:
            st = ceph.get("health", {}).get("status", "?")
            line = Text("Ceph ", style="bold")
            line.append(st, style="bold " + {"HEALTH_OK": "green", "HEALTH_WARN": "yellow"}.get(st, "red"))
            parts.append(line)
            for chk in list(ceph.get("health", {}).get("checks", {}).values())[:2]:
                msg = chk.get("summary", {}).get("message", "?")
                parts.append(Text("  · " + msg, style="yellow", overflow="ellipsis",
                                  no_wrap=True))
            pg = ceph.get("pgmap", {})
            used, tot = pg.get("bytes_used", 0), pg.get("bytes_total", 0) or 1
            raw = Text("  raw  ")
            raw.append_text(bar(used / tot * 100, 12, 75, 85))
            raw.append(f"  {hbytes(used)}/{hbytes(tot)}", style="grey62")
            parts.append(raw)
        parts.append(Text())

    st = Table(box=None, expand=True, pad_edge=False, show_header=False)
    st.add_column(ratio=2, no_wrap=True, overflow="ellipsis")
    st.add_column(ratio=3)
    st.add_column(justify="right", style="grey62")
    used_rows = sum(1 + len(p.rows) if isinstance(p, Table) else 1 for p in parts)
    room = max(1, height - used_rows)
    stor = d["storage"]
    shown = stor if len(stor) <= room else stor[:room - 1]
    for s in shown:
        st.add_row(s["label"], bar(s.get("disk", 0) / s["maxdisk"] * 100, 10, 80, 90),
                   f"{hbytes(s['disk'])}/{hbytes(s['maxdisk'])}")
    if len(shown) < len(stor):
        st.add_row(Text(f"+{len(stor) - len(shown)} altri", style="grey50"), "", "")
    parts.append(st)
    return Group(*parts)


def pct(p: float, warn: float = 70, crit: float = 90) -> Text:
    p = float(p or 0)
    return Text(f"{p:3.0f}%", style=level(p, warn, crit))


def render_guests(guests: list[dict], n: int = 16) -> Table:
    t = Table(box=None, expand=True, pad_edge=False, header_style="bold grey62")
    t.add_column("id", style="grey62", no_wrap=True)
    t.add_column("nome", no_wrap=True, overflow="ellipsis", ratio=1)
    t.add_column("nodo", style="grey62", no_wrap=True)
    t.add_column("CPU", justify="right", no_wrap=True)
    t.add_column("RAM", justify="right", no_wrap=True)
    t.add_column("rete", justify="right", no_wrap=True)
    for g in sorted(guests, key=lambda g: g.get("cpu", 0), reverse=True)[:n]:
        icon = "▣ " if g["type"] == "qemu" else "◇ "
        mem = g.get("mem", 0) / g["maxmem"] * 100 if g.get("maxmem") else 0
        t.add_row(str(g.get("vmid", "?")), icon + g.get("name", "?"), g.get("node", "?"),
                  pct(g.get("cpu", 0) * 100),
                  pct(mem, 85, 95), hbits(g.get("rate", 0)))
    return t


def render_wan(d: dict) -> Group:
    rx, tx = d.get("rx"), d.get("tx")
    head = Text("↓ ", style="bold green")
    head.append(hbits(rx) if rx is not None else "   …   ", style="bold green")
    head.append("    ↑ ", style="bold cyan")
    head.append(hbits(tx) if tx is not None else "   …   ", style="bold cyan")
    rows = [head]
    for p in d.get("per_wan", []):
        t = Text(f"  {p['name'][:10]:<10} ", style="grey62")
        if p["rx"] is not None:
            t.append(f"↓{hbits(p['rx'])}", style="green")
            t.append(f"  ↑{hbits(p['tx'])}", style="cyan")
        rows.append(t)
    return Group(*rows)


def render_gateways(gws: list[dict]) -> Group:
    rows = []
    for g in gws:
        status = g.get("status_translated") or g.get("status", "?")
        ok = status.lower() in ("online", "none")
        t = Text("● " if ok else "✖ ", style="green" if ok else "bold red")
        t.append(f"{g.get('name', '?'):<12}")
        t.append(f" {g.get('delay', '–'):>9}  loss {g.get('loss', '–')}", style="grey62")
        rows.append(t)
    return Group(*rows) if rows else Group(Text("nessun gateway", style="grey50"))


def render_top(rows: list[dict], n: int = 14) -> Table:
    t = Table(box=None, expand=True, pad_edge=False, header_style="bold grey62")
    t.add_column("#", style="grey50", width=3)
    t.add_column("host", no_wrap=True, overflow="ellipsis", ratio=2)
    t.add_column("indirizzo", style="grey62", ratio=2)
    t.add_column("↓ download", justify="right", style="green")
    t.add_column("", ratio=3)
    t.add_column("↑ upload", justify="right", style="cyan")
    t.add_column("", ratio=2)
    rows = rows[:n]
    vmax = max((max(r["down"], r["up"]) for r in rows), default=0)
    for i, r in enumerate(rows, 1):
        t.add_row(str(i), r["name"] or Text("—", style="grey42"), r["addr"],
                  hbits(r["down"]), mini_bar(r["down"], vmax, 18, "green"),
                  hbits(r["up"]), mini_bar(r["up"], vmax, 12, "cyan"))
    if not rows:
        t.add_row("", Text("nessun traffico rilevato", style="grey50"), "", "", "", "", "")
    return t


def _qnap_head(d: dict) -> Text:
    head = Text(d["name"], style="bold #bb9af7")
    if "error" in d:
        head.append("  ✖ " + d["error"][:60], style="bold red")
        return head
    head.append(f"  {d['temp']:.0f}°C", style=level(d["temp"], 50, 60))
    head.append("  CPU ", style="grey62")
    head.append(f"{d['cpu']:.0f}%", style=level(d["cpu"]))
    head.append("  RAM ", style="grey62")
    head.append(f"{d['mem']:.0f}%", style=level(d["mem"], 80, 92))
    head.append(f" {hbytes(d['mem_total'])}", style="grey50")
    return head


def _qnap_disks(d: dict) -> Text:
    disks = Text()
    for dk in d["disks"]:
        good = dk["smart"].upper() in ("GOOD", "NORMAL", "OK")
        disks.append(dk["name"].replace("HDD", "D"), style="grey62" if good else "bold red")
        disks.append(f" {dk['temp']:.0f}°", style=level(dk["temp"], 45, 52))
        if not good:
            disks.append(f" [{dk['smart']}]", style="bold red")
        disks.append("  ")
    return disks


def _vol_ok(v: dict) -> bool:
    return v["status"].lower() in ("ready", "ok", "good")


def _vol_pct(v: dict) -> float:
    return (v["total"] - v["free"]) / v["total"] * 100 if v["total"] else 0


def render_qnaps(nas: list[dict], height: int = 20, hide: dict | None = None) -> Group:
    """Tutti i NAS nello spazio disponibile. Se i volumi non entrano, mostra prima quelli
    in errore e i più pieni, e riassume gli altri in una riga."""
    hide = hide or {}
    vols = {}
    for d in nas:
        vs = [v for v in d.get("volumes", []) if v["name"] not in hide.get(d["name"], [])]
        vols[d["name"]] = sorted(vs, key=lambda v: (_vol_ok(v), -_vol_pct(v)))
    # righe fisse: intestazione + dischi per NAS, separatori tra NAS
    fixed = sum(1 if "error" in d else 2 for d in nas) + max(0, len(nas) - 1)
    budget = max(0, height - fixed)
    want = {n: len(v) for n, v in vols.items()}
    # ripartisce il budget: ogni NAS prende quanto gli serve, a turno, una riga alla volta
    alloc = {n: 0 for n in want}
    while budget > 0 and any(alloc[n] < want[n] for n in want):
        for n in want:
            if budget and alloc[n] < want[n]:
                alloc[n] += 1
                budget -= 1

    parts = []
    for i, d in enumerate(nas):
        if i:
            parts.append(Text("─" * 200, style="#3b4261", no_wrap=True, overflow="crop"))
        parts.append(_qnap_head(d))
        if "error" in d:
            continue
        vs, n = vols[d["name"]], alloc[d["name"]]
        hidden = vs[n:] if n < len(vs) else []
        if hidden and n:          # una riga va al riepilogo
            vs, hidden = vs[:n - 1], vs[n - 1:]
        else:
            vs = vs[:n]
        vt = Table(box=None, expand=True, pad_edge=False, show_header=False)
        vt.add_column(no_wrap=True, overflow="ellipsis", ratio=2)
        vt.add_column(no_wrap=True)
        vt.add_column(justify="right", style="grey62", no_wrap=True)
        for v in vs:
            ok = _vol_ok(v)
            name = Text(("● " if ok else "✖ ") + v["name"], style="" if ok else "bold red")
            vt.add_row(name, bar(_vol_pct(v), 8, 80, 90),
                       f"{hbytes(v['total'] - v['free'])}/{hbytes(v['total'])}")
        if hidden:
            worst = max(_vol_pct(v) for v in hidden)
            vt.add_row(Text(f"  +{len(hidden)} altri", style="grey50"),
                       Text(f"max {worst:.0f}%", style=level(worst, 80, 90)), "")
        if vs or hidden:
            parts.append(vt)
        parts.append(_qnap_disks(d))
    return Group(*parts)


# ───────────────────────── app ─────────────────────────
class Panel(Static):
    def __init__(self, title: str, **kw):
        super().__init__(**kw)
        self.border_title = title
        self.add_class("panel")


class Dashboard(App):
    TITLE = "homelab"
    BINDINGS = [("q", "quit", "esci")]
    CSS = """
    Screen { background: #0b0e14; }
    #header { height: 1; background: #161b26; color: #c0caf5; padding: 0 1; }
    #grid {
        grid-size: 3 3; grid-columns: 1fr 1fr 1fr; grid-rows: 13 13 1fr;
        grid-gutter: 0 1; padding: 0 1;
    }
    .panel {
        border: round #3b4261; border-title-color: #7aa2f7; border-title-style: bold;
        border-subtitle-color: #565f89; padding: 0 1; height: 100%;
    }
    .panel.stale { border: round #e0af68; }
    .panel.error { border: round #f7768e; }
    #top { column-span: 2; }
    #wanbox, #qnap, #guests { row-span: 2; }
    #wanbox Static { height: auto; }
    #wan-rate { margin-bottom: 1; }
    #wanbox Sparkline { height: 4; margin-bottom: 1; }
    #spark-rx > .sparkline--max-color { color: #9ece6a; }
    #spark-rx > .sparkline--min-color { color: #9ece6a 30%; }
    #spark-tx > .sparkline--max-color { color: #7dcfff; }
    #spark-tx > .sparkline--min-color { color: #7dcfff 30%; }
    """

    def __init__(self, cfg: dict, demo: bool = False):
        super().__init__()
        self.cfg, self.demo = cfg, demo
        self.last_ok: dict[str, float] = {}
        self.last_data: dict[str, object] = {}
        self.rx_hist = deque([0.0] * HISTORY, maxlen=HISTORY)
        self.tx_hist = deque([0.0] * HISTORY, maxlen=HISTORY)

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Grid(id="grid"):
            yield Panel("Proxmox VE", id="pve")
            with Vertical(id="wanbox", classes="panel"):
                yield Static(id="wan-rate")
                yield Sparkline(list(self.rx_hist), id="spark-rx", summary_function=max)
                yield Sparkline(list(self.tx_hist), id="spark-tx", summary_function=max)
                yield Static(id="gateways")
            yield Panel("QNAP", id="qnap")
            yield Panel("Guest per CPU", id="guests")
            yield Panel("Top host per banda", id="top")

    def on_mount(self) -> None:
        self.query_one("#wanbox").border_title = "Internet · OPNsense"
        self.sources: dict[str, list[str]] = {}
        if self.demo:
            d = Demo()
            jobs = [("pve", d.pve, 5, self.show_pve), ("wan", d.wan, 2, self.show_wan),
                    ("top", d.top, 5, self.show_top), ("qnap", d.qnap, 30, self.show_qnap)]
        else:
            jobs = []
            if c := self.cfg.get("pve"):
                jobs.append(("pve", PVE(c).fetch, c.get("interval", 5), self.show_pve))
            if c := self.cfg.get("opnsense"):
                opn = OPNsense(c)
                jobs.append(("wan", opn.fetch_wan, c.get("interval", 2), self.show_wan))
                jobs.append(("top", opn.fetch_top, c.get("top_interval", 6), self.show_top))
            if c := self.cfg.get("qnap"):
                q = QNAPs(c)
                jobs.append(("qnap", q.fetch, q.interval, self.show_qnap))
        self.intervals = {name: iv for name, _, iv, _ in jobs}
        for job in jobs:
            self.run_worker(self.poll(*job), name=job[0])
        self.set_interval(1, self.tick)
        self.tick()

    async def poll(self, name, fetch, interval, show) -> None:
        while True:
            try:
                show(await fetch())
                if not self.query_one(self.PANEL_OF[name]).content_region.height:
                    self.call_after_refresh(self._redraw)
                self.last_ok[name] = time.monotonic()
                self.mark(name, None)
            except Exception as e:
                self.mark(name, f"{type(e).__name__}: {e}")
            await asyncio.sleep(interval)

    PANEL_OF = {"pve": "#pve", "wan": "#wanbox", "top": "#top", "qnap": "#qnap"}

    def mark(self, name: str, err: str | None) -> None:
        w = self.query_one(self.PANEL_OF[name])
        w.set_class(err is not None, "error")
        if err:
            w.border_subtitle = err[:70]
        else:
            w.border_subtitle = datetime.now().strftime("agg. %H:%M:%S")

    def tick(self) -> None:
        now = time.monotonic()
        h = Text(datetime.now().strftime("%a %d %b  %H:%M:%S"), style="bold")
        h.append("   ")
        for name, label in (("pve", "PVE"), ("wan", "OPNsense"), ("top", "top"), ("qnap", "QNAP")):
            if name not in self.intervals:
                continue
            age = now - self.last_ok.get(name, -1e9)
            stale = age > self.intervals[name] * 3 + 5
            h.append("● ", style="red" if stale else "green")
            h.append(label + "   ", style="grey62")
            self.query_one(self.PANEL_OF[name]).set_class(stale and name in self.last_ok, "stale")
        if self.demo:
            h.append("[DEMO]", style="bold yellow")
        self.query_one("#header", Static).update(h)

    # ── aggiornamento pannelli
    def _rows(self, sel: str, reserved: int = 1) -> int:
        """Righe di tabella che entrano nel pannello (header escluso)."""
        h = self.query_one(sel).content_region.height
        return max(3, h - reserved) if h else 14

    def _redraw(self) -> None:
        """Ridisegna i pannelli che dipendono dall'altezza con gli ultimi dati ricevuti."""
        for name, show in (("pve", self.show_pve), ("top", self.show_top), ("qnap", self.show_qnap)):
            if name in self.last_data:
                show(self.last_data[name])

    def on_resize(self) -> None:
        self.call_after_refresh(self._redraw)

    def show_pve(self, d: dict) -> None:
        self.last_data["pve"] = d
        self.query_one("#pve", Static).update(render_pve(d, self._rows("#pve", 0)))
        g = self.query_one("#guests", Static)
        g.border_title = f"Guest per CPU · {len(d['guests'])} in esecuzione"
        g.update(render_guests(d["guests"], self._rows("#guests")))

    def show_wan(self, d: dict) -> None:
        if d["rx"] is not None:
            self.rx_hist.append(d["rx"])
            self.tx_hist.append(d["tx"])
            self.query_one("#spark-rx", Sparkline).data = list(self.rx_hist)
            self.query_one("#spark-tx", Sparkline).data = list(self.tx_hist)
        rate = render_wan(d)
        peak = Text(f"picco {HISTORY * self.intervals.get('wan', 2) // 60} min: ", style="grey50")
        peak.append(f"↓{hbits(max(self.rx_hist)).strip()}  ↑{hbits(max(self.tx_hist)).strip()}",
                    style="grey62")
        self.query_one("#wan-rate", Static).update(Group(rate, peak))
        self.query_one("#gateways", Static).update(render_gateways(d["gateways"]))

    def show_top(self, rows: list[dict]) -> None:
        self.last_data["top"] = rows
        self.query_one("#top", Static).update(render_top(rows, self._rows("#top")))

    def show_qnap(self, nas: list[dict]) -> None:
        self.last_data["qnap"] = nas
        hide = {q.get("name") or q["host"]: q.get("hide_volumes", [])
                for q in (self.cfg.get("qnap") if isinstance(self.cfg.get("qnap"), list)
                          else [self.cfg["qnap"]] if self.cfg.get("qnap") else [])}
        self.query_one("#qnap", Static).update(
            render_qnaps(nas, self._rows("#qnap", 0), hide))


def main() -> None:
    args = sys.argv[1:]
    demo = "--demo" in args
    args = [a for a in args if a != "--demo"]
    path = Path(args[0]) if args else Path.home() / ".config/homelab-dash/config.toml"
    cfg = {} if demo else tomllib.loads(path.read_text())
    Dashboard(cfg, demo=demo).run()


if __name__ == "__main__":
    main()
