#!/usr/bin/env python3
"""nginx / OpenResty live performance monitor (terminal TUI).

Data sources:
  - nginx access logs (latency, statuses, top URL/IP)   -> --access-log
  - stub_status (connections)                           -> --status-url
  - host system metrics (CPU/RAM/disk/network)          -> psutil

Refreshes every N seconds, computes percentiles over a sliding window,
shows alerts when thresholds are exceeded.

Dependencies: rich, psutil  (pip install -r requirements.txt)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Deque, Optional

import psutil
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ───────────────────────── log parsing ─────────────────────────

# fields from the "perf" format:  ... rt=0.123 uct=0.001 uht=0.010 urt=0.110 cs=HIT
_RT = re.compile(r"\brt=(\d+\.?\d*)")
_UHT = re.compile(r"\buht=([\d.,:-]+)")
_URT = re.compile(r"\burt=([\d.,:-]+)")
_CS = re.compile(r"\bcs=([A-Za-z]+)")
# combined part: "GET /path HTTP/1.1" 200 1234
_REQ = re.compile(r'"(?P<method>[A-Z]+)\s+(?P<uri>[^ "?]+)[^"]*"\s+(?P<status>\d{3})')
_IP = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})")

# nginx $upstream_cache_status values
_MISS_STATES = {"MISS", "EXPIRED", "STALE", "BYPASS", "REVALIDATED", "UPDATING"}
_CACHE_STATES = {"HIT"} | _MISS_STATES


@dataclass
class Event:
    t: float            # moment the line was read (≈ now for live tail)
    status: int
    rt: float           # request_time, sec
    uht: float          # upstream_header_time (TTFB from backend), sec (-1 if absent)
    urt: float          # upstream_response_time, sec (-1 if absent)
    cache: str          # normalized $upstream_cache_status ("" if unknown)
    uri: str
    ip: str
    bytes: int


def _to_float(v) -> float:
    try:
        f = float(v)
        return f if f >= 0 else -1.0
    except (TypeError, ValueError):
        return -1.0


def _sum_times(v) -> float:
    """Parse an nginx upstream time that may be multi-valued.

    With several upstreams / internal redirects nginx writes e.g.
    "0.01, 0.02 : 0.03"; the components are summed. Returns -1 when there
    is no numeric value (absent, "-", or unparsable).
    """
    if v is None:
        return -1.0
    s = str(v).strip()
    if not s or s == "-":
        return -1.0
    total = 0.0
    found = False
    for tok in re.split(r"[,:]", s):
        tok = tok.strip()
        if not tok or tok == "-":
            continue
        try:
            total += float(tok)
            found = True
        except ValueError:
            return -1.0
    return total if found else -1.0


def _norm_cache(v) -> str:
    s = str(v or "").strip().upper()
    return s if s in _CACHE_STATES else ""


def parse_line(line: str, now: float) -> Optional[Event]:
    line = line.strip()
    if not line:
        return None
    # 1) JSON format (log_format ... escape=json '{...}')
    if line[0] == "{":
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        return Event(
            t=now,
            status=int(d.get("status", 0) or 0),
            rt=_to_float(d.get("request_time")),
            uht=_sum_times(d.get("upstream_header_time")),
            urt=_sum_times(d.get("upstream_time", d.get("upstream_response_time"))),
            cache=_norm_cache(d.get("cache", d.get("upstream_cache_status", ""))),
            uri=str(d.get("uri", d.get("request", "?")))[:60],
            ip=str(d.get("remote_addr", "?")),
            bytes=int(d.get("bytes", d.get("body_bytes_sent", 0)) or 0),
        )
    # 2) plain-text perf format
    m = _REQ.search(line)
    if not m:
        return None
    rt_m = _RT.search(line)
    uht_m = _UHT.search(line)
    urt_m = _URT.search(line)
    cs_m = _CS.search(line)
    ip_m = _IP.match(line)
    # size — the number before "$http_referer", right after the status
    bytes_m = re.search(r'"\s+\d{3}\s+(\d+)', line)
    return Event(
        t=now,
        status=int(m.group("status")),
        rt=_to_float(rt_m.group(1)) if rt_m else -1.0,
        uht=_sum_times(uht_m.group(1)) if uht_m else -1.0,
        urt=_sum_times(urt_m.group(1)) if urt_m else -1.0,
        cache=_norm_cache(cs_m.group(1) if cs_m else ""),
        uri=m.group("uri")[:60],
        ip=ip_m.group(1) if ip_m else "?",
        bytes=int(bytes_m.group(1)) if bytes_m else 0,
    )


class LogTailer:
    """Incremental access-log reading with rotation handling."""

    def __init__(self, path: str, from_start: bool = False):
        self.path = path
        self._fh = None
        self._inode = None
        self._open(seek_end=not from_start)

    def _open(self, seek_end: bool):
        try:
            self._fh = open(self.path, "r", errors="replace")
            st = os.fstat(self._fh.fileno())
            self._inode = st.st_ino
            if seek_end:
                self._fh.seek(0, os.SEEK_END)
        except OSError:
            self._fh = None
            self._inode = None

    def read_new(self) -> list[str]:
        if self._fh is None:
            self._open(seek_end=False)
            if self._fh is None:
                return []
        # detect rotation (file recreated/truncated)
        try:
            st = os.stat(self.path)
            if st.st_ino != self._inode or st.st_size < self._fh.tell():
                self._fh.close()
                self._open(seek_end=False)
        except OSError:
            pass
        if self._fh is None:
            return []
        lines = self._fh.readlines()
        return lines


# ───────────────────── sliding-window aggregator ─────────────────────


class Window:
    def __init__(self, seconds: int):
        self.window = seconds
        self.events: Deque[Event] = deque()

    def add(self, ev: Event):
        self.events.append(ev)

    def prune(self, now: float):
        cutoff = now - self.window
        ev = self.events
        while ev and ev[0].t < cutoff:
            ev.popleft()

    @staticmethod
    def pct(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        values = sorted(values)
        k = (len(values) - 1) * p
        f = int(k)
        c = min(f + 1, len(values) - 1)
        return values[f] + (values[c] - values[f]) * (k - f)

    @staticmethod
    def tail_mean(values: list[float], frac: float) -> float:
        """Mean of the slowest `frac` fraction of values.

        frac=0.01 -> average request_time of the worst 1%. Unlike a
        percentile (a boundary value) this averages the whole tail beyond
        that boundary, so it always sits at or above the matching pNN.
        """
        if not values:
            return 0.0
        values = sorted(values)
        k = max(1, int(round(len(values) * frac)))
        tail = values[-k:]
        return sum(tail) / len(tail)


# ───────────────────────── stub_status ─────────────────────────


@dataclass
class StubStatus:
    active: int = 0
    accepts: int = 0
    handled: int = 0
    requests: int = 0
    reading: int = 0
    writing: int = 0
    waiting: int = 0
    ok: bool = False
    err: str = ""


def fetch_stub(url: str, timeout: float = 1.0) -> StubStatus:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return StubStatus(ok=False, err=type(e).__name__)
    s = StubStatus(ok=True)
    try:
        s.active = int(re.search(r"Active connections:\s+(\d+)", txt).group(1))
        nums = re.search(r"\n\s*(\d+)\s+(\d+)\s+(\d+)", txt)
        s.accepts, s.handled, s.requests = map(int, nums.groups())
        rww = re.search(r"Reading:\s+(\d+)\s+Writing:\s+(\d+)\s+Waiting:\s+(\d+)", txt)
        s.reading, s.writing, s.waiting = map(int, rww.groups())
    except (AttributeError, ValueError):
        s.ok = False
        s.err = "parse"
    return s


# ───────────────────────── system ─────────────────────────


@dataclass
class SysState:
    prev_net: Optional[tuple] = None  # (ts, bytes_sent, bytes_recv)


def sys_metrics(state: SysState) -> dict:
    out: dict = {}
    out["cpu"] = psutil.cpu_percent(interval=None)
    try:
        ct = psutil.cpu_times_percent(interval=None)
        out["iowait"] = getattr(ct, "iowait", 0.0)
    except Exception:  # noqa: BLE001
        out["iowait"] = 0.0
    try:
        out["load"] = psutil.getloadavg()
    except (AttributeError, OSError):
        out["load"] = (0.0, 0.0, 0.0)
    vm = psutil.virtual_memory()
    out["mem_used"] = vm.used
    out["mem_total"] = vm.total
    out["mem_pct"] = vm.percent
    du = psutil.disk_usage("/")
    out["disk_pct"] = du.percent
    out["disk_free"] = du.free
    n = psutil.net_io_counters()
    now = time.time()
    if state.prev_net:
        dt = max(now - state.prev_net[0], 1e-6)
        out["net_up"] = (n.bytes_sent - state.prev_net[1]) / dt
        out["net_down"] = (n.bytes_recv - state.prev_net[2]) / dt
    else:
        out["net_up"] = out["net_down"] = 0.0
    state.prev_net = (now, n.bytes_sent, n.bytes_recv)
    return out


# ───────────────────────── formatting ─────────────────────────


def human_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}P"


def ms(sec: float) -> str:
    return "—" if sec < 0 else f"{sec * 1000:.0f}ms"


def bar(pct: float, width: int = 12, color: str = "green") -> Text:
    filled = int(round(pct / 100 * width))
    return Text("█" * filled + "▏" * (width - filled), style=color)


# ───────────────────────── alerts ─────────────────────────


@dataclass
class Thresholds:
    err5xx_pct: float = 1.0
    p99_ms: float = 1000.0
    disk_pct: float = 90.0
    cpu_pct: float = 90.0
    mem_pct: float = 90.0


def check_alerts(th: Thresholds, stats: dict, sysm: dict) -> list[str]:
    a = []
    if stats["total"] > 0 and stats["pct5xx"] > th.err5xx_pct:
        a.append(f"5xx {stats['pct5xx']:.1f}% > {th.err5xx_pct}%")
    if stats["p99"] * 1000 > th.p99_ms:
        a.append(f"p99 {ms(stats['p99'])} > {th.p99_ms:.0f}ms")
    if sysm.get("cpu", 0) > th.cpu_pct:
        a.append(f"CPU {sysm['cpu']:.0f}% > {th.cpu_pct}%")
    if sysm.get("mem_pct", 0) > th.mem_pct:
        a.append(f"RAM {sysm['mem_pct']:.0f}% > {th.mem_pct}%")
    if sysm.get("disk_pct", 0) > th.disk_pct:
        a.append(f"disk {sysm['disk_pct']:.0f}% > {th.disk_pct}%")
    return a


# ───────────────────────── render ─────────────────────────


def compute_stats(win: Window) -> dict:
    ev = win.events
    total = len(ev)
    rts = [e.rt for e in ev if e.rt >= 0]
    urts = [e.urt for e in ev if e.urt >= 0]
    uhts = [e.uht for e in ev if e.uht >= 0]
    hit_rts = [e.rt for e in ev if e.rt >= 0 and e.cache == "HIT"]
    miss_rts = [e.rt for e in ev if e.rt >= 0 and e.cache in _MISS_STATES]
    n_hit = sum(1 for e in ev if e.cache == "HIT")
    n_miss = sum(1 for e in ev if e.cache in _MISS_STATES)
    n_cache = n_hit + n_miss
    codes = Counter(e.status // 100 for e in ev)
    n5xx = codes.get(5, 0)
    n4xx = codes.get(4, 0)
    return {
        "total": total,
        "rps": total / win.window if win.window else 0,
        "p50": Window.pct(rts, 0.50),
        "p90": Window.pct(rts, 0.90),
        "p95": Window.pct(rts, 0.95),
        "p99": Window.pct(rts, 0.99),
        "tail1": Window.tail_mean(rts, 0.01),
        "tail5": Window.tail_mean(rts, 0.05),
        "tail10": Window.tail_mean(rts, 0.10),
        "tail50": Window.tail_mean(rts, 0.50),
        "u_p50": Window.pct(urts, 0.50),
        "u_p95": Window.pct(urts, 0.95),
        "uht_p50": Window.pct(uhts, 0.50),
        "uht_p95": Window.pct(uhts, 0.95),
        "hit_p50": Window.pct(hit_rts, 0.50),
        "hit_p95": Window.pct(hit_rts, 0.95),
        "miss_p50": Window.pct(miss_rts, 0.50),
        "miss_p95": Window.pct(miss_rts, 0.95),
        "n_hit": n_hit,
        "n_miss": n_miss,
        "hit_ratio": 100 * n_hit / n_cache if n_cache else 0,
        "cache_known": n_cache,
        "codes": codes,
        "pct5xx": 100 * n5xx / total if total else 0,
        "pct4xx": 100 * n4xx / total if total else 0,
        "bytes": sum(e.bytes for e in ev),
    }


def header_panel(stats: dict, stub: StubStatus, win: Window, interval: int) -> Panel:
    t = Table.grid(expand=True)
    t.add_column(justify="left")
    t.add_column(justify="left")
    t.add_column(justify="left")
    t.add_column(justify="left")
    err_color = "red" if stats["pct5xx"] > 1 else "green"
    conn = f"{stub.active}" if stub.ok else "n/a"
    t.add_row(
        Text.assemble(("RPS ", "bold"), (f"{stats['rps']:.0f}", "bold cyan")),
        Text.assemble(("5xx ", "bold"), (f"{stats['pct5xx']:.1f}%", err_color)),
        Text.assemble(("4xx ", "bold"), (f"{stats['pct4xx']:.1f}%", "yellow")),
        Text.assemble(("conn ", "bold"), (conn, "bold cyan")),
    )
    rt = Text.assemble(
        ("request_time  ", "bold"),
        (f"p50 {ms(stats['p50'])}  ", "white"),
        (f"p90 {ms(stats['p90'])}  ", "white"),
        (f"p95 {ms(stats['p95'])}  ", "yellow"),
        (f"p99 {ms(stats['p99'])}", "red" if stats["p99"] > 1 else "yellow"),
    )
    tail = Text.assemble(
        ("tail mean     ", "bold"),
        (f"worst1% {ms(stats['tail1'])}  ", "red"),
        (f"worst5% {ms(stats['tail5'])}  ", "yellow"),
        (f"worst10% {ms(stats['tail10'])}  ", "yellow"),
        (f"worst50% {ms(stats['tail50'])}", "white"),
    )
    up = Text.assemble(
        ("upstream      ", "bold dim"),
        (f"ttfb p50 {ms(stats['uht_p50'])}  ", "dim"),
        (f"resp p50 {ms(stats['u_p50'])}  p95 {ms(stats['u_p95'])}", "dim"),
    )
    if stats["cache_known"]:
        cache = Text.assemble(
            ("cache         ", "bold"),
            (f"HIT {stats['n_hit']} ({stats['hit_ratio']:.0f}%) p50 {ms(stats['hit_p50'])}  ", "green"),
            (f"MISS {stats['n_miss']} p50 {ms(stats['miss_p50'])}", "yellow"),
        )
    else:
        cache = Text("cache         no $upstream_cache_status in log", style="dim")
    sub = f"window {win.window}s · refresh {interval}s · traffic {human_bytes(stats['bytes'])}"
    body = Group(t, rt, tail, up, cache)
    return Panel(body, title="nginx monitor", subtitle=sub, border_style="cyan")


def slow_table(win: Window) -> Table:
    agg: dict[str, list[float]] = {}
    for e in win.events:
        if e.rt >= 0:
            agg.setdefault(e.uri, []).append(e.rt)
    rows = sorted(
        ((u, max(v), len(v)) for u, v in agg.items()),
        key=lambda x: x[1],
        reverse=True,
    )[:8]
    t = Table(title="Top slow URLs", expand=True, title_style="bold")
    t.add_column("URL", overflow="ellipsis", no_wrap=True)
    t.add_column("max", justify="right")
    t.add_column("n", justify="right")
    for uri, mx, n in rows:
        c = "red" if mx > 1 else "yellow" if mx > 0.3 else "white"
        t.add_row(uri, Text(ms(mx), style=c), str(n))
    if not rows:
        t.add_row("(no data)", "", "")
    return t


def status_table(stats: dict) -> Table:
    t = Table(title="Statuses", expand=True, title_style="bold")
    t.add_column("code", justify="left")
    t.add_column("", justify="left")
    t.add_column("%", justify="right")
    total = stats["total"] or 1
    colors = {2: "green", 3: "cyan", 4: "yellow", 5: "red"}
    for cls in (2, 3, 4, 5):
        n = stats["codes"].get(cls, 0)
        pct = 100 * n / total
        t.add_row(f"{cls}xx", bar(pct, 12, colors[cls]), f"{pct:.0f}%")
    return t


def sys_panel(sysm: dict) -> Panel:
    if not sysm:
        return Panel(Text("system metrics disabled", style="dim"), title="System")
    load = sysm.get("load", (0, 0, 0))
    t = Table.grid(expand=True)
    for _ in range(4):
        t.add_column(justify="left")
    cpu_c = "red" if sysm["cpu"] > 90 else "yellow" if sysm["cpu"] > 70 else "green"
    disk_c = "red" if sysm["disk_pct"] > 90 else "white"
    t.add_row(
        Text.assemble(("CPU ", "bold"), (f"{sysm['cpu']:.0f}%", cpu_c)),
        Text.assemble(("load ", "bold"), f"{load[0]:.2f}/{load[1]:.2f}/{load[2]:.2f}"),
        Text.assemble(("iowait ", "bold"), f"{sysm.get('iowait', 0):.0f}%"),
        Text.assemble(
            ("RAM ", "bold"),
            (f"{human_bytes(sysm['mem_used'])}/{human_bytes(sysm['mem_total'])}", "white"),
        ),
    )
    t.add_row(
        Text.assemble(("disk ", "bold"), (f"{sysm['disk_pct']:.0f}%", disk_c)),
        Text.assemble(("free ", "bold"), human_bytes(sysm["disk_free"])),
        Text.assemble(("net↓ ", "bold"), f"{human_bytes(sysm['net_down'])}/s"),
        Text.assemble(("net↑ ", "bold"), f"{human_bytes(sysm['net_up'])}/s"),
    )
    return Panel(t, title="System", border_style="blue")


def stub_line(stub: StubStatus) -> Text:
    if not stub.ok:
        return Text(f"stub_status: unavailable ({stub.err})", style="dim red")
    rpc = stub.requests / stub.handled if stub.handled else 0
    dropped = stub.accepts - stub.handled
    return Text.assemble(
        ("stub_status  ", "bold"),
        (f"reading {stub.reading}  writing {stub.writing}  waiting {stub.waiting}  ", "white"),
        (f"req/conn {rpc:.1f}  ", "cyan"),
        (f"dropped {dropped}", "red" if dropped else "dim"),
    )


def alerts_panel(alerts: list[str]) -> Panel:
    if not alerts:
        return Panel(Text("OK — no thresholds exceeded", style="green"), title="Alerts", border_style="green")
    body = Text("\n".join(f"⚠ {a}" for a in alerts), style="bold red")
    return Panel(body, title="⚠ ALERTS", border_style="red")


def build_layout(stats, stub, sysm, alerts, win, interval) -> Layout:
    root = Layout()
    root.split_column(
        Layout(header_panel(stats, stub, win, interval), size=10, name="head"),
        Layout(name="mid", size=12),
        Layout(stub_line(stub), size=1, name="stub"),
        Layout(sys_panel(sysm), size=5, name="sys"),
        Layout(alerts_panel(alerts), name="alerts"),
    )
    root["mid"].split_row(
        Layout(slow_table(win), name="slow"),
        Layout(status_table(stats), name="codes", ratio=1),
    )
    return root


# ───────────────────────── main loop ─────────────────────────


def main():
    ap = argparse.ArgumentParser(description="nginx/OpenResty live monitor")
    ap.add_argument("--access-log", default="/usr/local/openresty/nginx/logs/access.log",
                    help="path to the access log")
    ap.add_argument("--status-url", default="http://127.0.0.1/nginx_status",
                    help="stub_status URL (empty — disable)")
    ap.add_argument("--interval", type=int, default=2, help="refresh period, sec")
    ap.add_argument("--window", type=int, default=60, help="aggregation window, sec")
    ap.add_argument("--no-system", action="store_true", help="hide host metrics")
    ap.add_argument("--from-start", action="store_true", help="read log from the beginning")
    ap.add_argument("--bell", action="store_true", help="beep on alert")
    ap.add_argument("--th-5xx", type=float, default=1.0, help="5xx %% threshold")
    ap.add_argument("--th-p99", type=float, default=1000.0, help="p99 threshold, ms")
    ap.add_argument("--th-disk", type=float, default=90.0, help="disk %% threshold")
    ap.add_argument("--th-cpu", type=float, default=90.0, help="CPU %% threshold")
    ap.add_argument("--th-mem", type=float, default=90.0, help="RAM %% threshold")
    args = ap.parse_args()

    th = Thresholds(args.th_5xx, args.th_p99, args.th_disk, args.th_cpu, args.th_mem)
    win = Window(args.window)
    tailer = LogTailer(args.access_log, from_start=args.from_start)
    sysstate = SysState()
    psutil.cpu_percent(interval=None)  # priming
    sys_metrics(sysstate)

    with Live(refresh_per_second=4, screen=True) as live:
        while True:
            now = time.time()
            for line in tailer.read_new():
                ev = parse_line(line, now)
                if ev:
                    win.add(ev)
            win.prune(now)
            stats = compute_stats(win)
            stub = fetch_stub(args.status_url) if args.status_url else StubStatus(ok=False, err="off")
            sysm = {} if args.no_system else sys_metrics(sysstate)
            alerts = check_alerts(th, stats, sysm)
            if alerts and args.bell:
                print("\a", end="", flush=True)
            live.update(build_layout(stats, stub, sysm, alerts, win, args.interval))
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
