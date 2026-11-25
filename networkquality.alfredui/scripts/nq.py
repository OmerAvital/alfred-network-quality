#!/usr/bin/python3
"""Network Quality: test runner, Alfred UI handlers and Script Filter in one file.

UI commands (stdout = Alfred UI JSON actions):
  load            preload: render filtered to the current network; auto-start if configured
  toggle | start | stop
  pulse           live numbers while running; final render when done
  select <key>    network filter changed
  copy            copy the latest shown result
Other:
  history         Script Filter JSON
  _run            internal: the detached test process
"""
import json
import math
import os
import pty
import re
import select
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

BUNDLE = "dev.omeravital.networkQuality"
DATA = os.environ.get("alfred_workflow_data") or os.path.expanduser(
    "~/Library/Application Support/Alfred/Workflow Data/" + BUNDLE)
LOCK = os.path.join(DATA, "run.lock")
LIVE = os.path.join(DATA, "live.json")
RESULT = os.path.join(DATA, "result.json")
ERROR = os.path.join(DATA, "error.json")
HISTORY = os.path.join(DATA, "history.jsonl")
HEARTBEAT = os.path.join(DATA, "ui.heartbeat")
FRAME = os.path.join(DATA, "frame.json")
DIALS = os.path.join(os.environ.get("alfred_workflow_cache") or os.path.expanduser(
    "~/Library/Caches/com.runningwithcrayons.Alfred/Workflow Data/" + BUNDLE), "dials")

MAX_SECONDS = 20
PULSE = {"src": "scripts/pulse.sh", "args": ["{var:net_filter}"], "interval": 0.1, "intervalmode": "regular"}
FPS = 10      # Alfred's pulse can't fire faster than every 0.1 s
EASE = 0.3    # fraction of the remaining distance covered per frame
PULSE_OFF = {"interval": 0}
MODES = {"parallel": [], "sequential": ["-s"], "download": ["-u"], "upload": ["-d"]}
LIVE_RE = re.compile(r"Downlink: ([\d.]+) Mbps, (\d+) RPM - Uplink: ([\d.]+) Mbps, (\d+) RPM")
ALL, VPN = "*", "vpn"

GREEN, YELLOW, RED = "systemGreenColor", "systemOrangeColor", "systemRedColor"
GREY = "secondaryLabelColor"


# ---------------------------------------------------------------- grading

def thresholds(name, default):
    """'25,100' -> (25.0, 100.0) from Configure Workflow, falling back to defaults."""
    try:
        a, b = (float(x) for x in os.environ.get("thr_" + name, "").split(","))
        return (a, b)
    except ValueError:
        return default


def grade(kind, v):
    """('Great'|'Okay'|'Poor', colour) or ('', GREY) when there is no value."""
    if not v:
        return "", GREY
    lo, hi = {
        "dl": thresholds("download", (25, 100)),
        "ul": thresholds("upload", (3, 20)),
        "rpm": thresholds("rpm", (300, 1000)),
        "idle": thresholds("latency", (30, 100)),
    }[kind]
    if kind == "idle":  # lower is better: great below lo, poor above hi
        return ("Great", GREEN) if v < lo else ("Okay", YELLOW) if v <= hi else ("Poor", RED)
    return ("Great", GREEN) if v >= hi else ("Okay", YELLOW) if v >= lo else ("Poor", RED)


def ring_fill(kind, v):
    """Log scale like a speedometer dial: full at 1000 Mbps / 3000 RPM."""
    if not v or v <= 1:
        return 0.0
    top = 3000 if kind == "rpm" else 1000
    return max(0.03, min(1.0, math.log10(v) / math.log10(top)))


# Apple HIG system colours (dark, light): exactly what watchOS tints complication gauges with
HIG = {GREEN: ("#30D158", "#34C759"), YELLOW: ("#FF9F0A", "#FF9500"), RED: ("#FF453A", "#FF3B30")}


def dark_mode():
    if "NQ_DARK" not in os.environ:
        os.environ["NQ_DARK"] = "1" if "Dark" in sh("/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle") else "0"
    return os.environ["NQ_DARK"] == "1"


def shade(hex_colour, k):
    return "#" + "".join(f"{int(int(hex_colour[i:i + 2], 16) * k):02X}" for i in (1, 3, 5))


def arc(a0, a1, r=52, c=60):
    p = lambda a: (c + r * math.cos(math.radians(a)), c + r * math.sin(math.radians(a)))
    (x0, y0), (x1, y1) = p(a0), p(a1)
    return f"M{x0:.2f},{y0:.2f} A{r},{r} 0 {1 if a1 - a0 > 180 else 0} 1 {x1:.2f},{y1:.2f}"


_written = {}


def dial(kind, fraction, colour):
    """Write a Watch-style 270° gauge (open at the bottom, cropped just below the arc ends) and return its path.

    Each frame gets a new file name so Alfred reloads the image; older frames are pruned."""
    os.makedirs(DIALS, exist_ok=True)
    dark = dark_mode()
    track = 'stroke="#808080" stroke-opacity="0.24"' if dark else 'stroke="#000000" stroke-opacity="0.09"'
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="232" height="201" viewBox="0 0 120 104">'
           f'<path d="{arc(135, 405)}" fill="none" {track} stroke-width="10" stroke-linecap="round"/>')
    if fraction > 0.004 and colour in HIG:
        tip = HIG[colour][0 if dark else 1]
        svg += ('<defs><linearGradient id="g" gradientUnits="userSpaceOnUse" x1="14" y1="96" x2="106" y2="96">'
                f'<stop offset="0" stop-color="{shade(tip, 0.72)}"/><stop offset="1" stop-color="{tip}"/>'
                '</linearGradient></defs>'
                f'<path d="{arc(135, 135 + 270 * fraction)}" fill="none" stroke="url(#g)" stroke-width="10" stroke-linecap="round"/>')
    path = os.path.join(DIALS, f"{kind}-{time.time_ns()}.svg")
    with open(path, "w") as f:
        f.write(svg + "</svg>")
    old = _written.setdefault(kind, [])
    old.append(path)
    while len(old) > 4:
        remove(old.pop(0))
    return path


def prune_dials():
    """Drop frames left behind by earlier processes (keeps the newest few per dial)."""
    try:
        files = sorted(os.listdir(DIALS))
    except OSError:
        return
    for kind in ("dl", "ul", "rpm"):
        mine = [f for f in files if f.startswith(kind + "-")]
        for f in mine[:-4]:
            remove(os.path.join(DIALS, f))


def error_tag(message):
    t = tag(message)
    t["background"] = "#FF3B302B"
    t["components"][0]["colour"] = RED
    return t


def uses(r):
    dl, ul, rpm, idle = r.get("dl"), r.get("ul"), r.get("rpm") or 0, r.get("idle_ms") or 999
    uses = [
        ("4K streaming", None if dl is None else dl >= 25),
        ("HD video calls", None if ul is None else (ul >= 3 and rpm >= 300)),
        ("gaming", rpm >= 500 and idle <= 60),
        ("large uploads", None if ul is None else ul >= 20),
    ]
    return [(u, ok) for u, ok in uses if ok is not None]


def tag(text, ok=None):
    """A pill: green check for good, orange cross for may-struggle, grey for plain text."""
    colour, fill, mark = {True: (GREEN, "#34C7592B", "✓ "), False: (YELLOW, "#FF9F0A2B", "✗ "),
                          None: (GREY, "#8080801F", "")}[ok]
    pill = {"type": "view", "layout": "stack", "cornerradius": 10, "background": fill, "padding": 4,
            "components": [{"type": "label", "text": mark + text[0].upper() + text[1:], "colour": colour,
                            "textalign": "centre", "font": {"size": "small", "weight": "medium"},
                            "constraints": {"insets": {"left": 6, "right": 6}}}]}
    if ok is not None:
        pill["tooltip"] = ("Good for " if ok else "May struggle with ") + text
    return pill


# ---------------------------------------------------------------- data

def ensure_dir():
    os.makedirs(DATA, exist_ok=True)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def remove(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def history():
    runs = []
    try:
        with open(HISTORY) as f:
            for line in f:
                try:
                    runs.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return runs  # oldest first


def net_key(r):
    return VPN if r.get("vpn") else f"{r.get('net', '')}|{r.get('router', '')}"


def filtered(runs, key):
    if key in (None, "", ALL):
        return runs
    return [r for r in runs if net_key(r) == key]


def running_lock():
    lock = read_json(LOCK)
    if not lock:
        return None
    try:
        os.kill(lock["pid"], 0)
        return lock
    except (OSError, KeyError, TypeError):
        remove(LOCK)
        return None


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def current_network():
    """{'net', 'net_type', 'router', 'vpn'} for the active connection (~1.5 s)."""
    iface = re.search(r"interface: (\S+)", sh("/sbin/route", "-n", "get", "default"))
    iface = iface.group(1) if iface else ""
    vpn = iface.startswith(("utun", "ppp", "ipsec"))
    m = re.search(r"Current Network Information:\n\s+(.+?):\n", sh("/usr/sbin/system_profiler", "SPAirPortDataType"))
    ssid = m.group(1).strip() if m else ""
    services = sh("/usr/sbin/networksetup", "-listnetworkserviceorder")
    service = re.search(r"\(\d+\) (.+)\n\(Hardware Port: [^,]+, Device: " + re.escape(iface) + r"\)", services)
    service = service.group(1) if service else ""
    if ssid and (vpn or "Wi-Fi" in service or not service):
        name, kind = ssid, "Wi-Fi"
    else:
        name, kind = service or iface or "Unknown", "Ethernet" if service else ""
    phys = "en0" if vpn else iface
    gw = sh("/usr/sbin/ipconfig", "getoption", phys, "router").strip()
    mac = re.search(r" at ([0-9a-f:]+) ", sh("/usr/sbin/arp", "-n", gw)) if gw else None
    return {"net": name, "net_type": kind, "router": mac.group(1) if mac else "", "vpn": vpn}


# ---------------------------------------------------------------- formatting

def fmt_num(v):
    if v is None:
        return "—"
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}"


def when(ts):
    d = datetime.fromtimestamp(ts)
    days = (datetime.now().date() - d.date()).days
    day = "Today" if days == 0 else "Yesterday" if days == 1 else d.strftime("%b %-d")
    return f"{day} {d:%H:%M}"


def net_label(r):
    label = r.get("net") or "Unknown network"
    return label + " (VPN)" if r.get("vpn") else label


def summary(r):
    parts = [f"↓ {fmt_num(r.get('dl'))} Mbps  ↑ {fmt_num(r.get('ul'))} Mbps"]
    if r.get("rpm"):
        parts.append(f"{grade('rpm', r['rpm'])[0]} responsiveness ({r['rpm']:.0f} RPM)")
    if r.get("idle_ms"):
        parts.append(f"latency {r['idle_ms']:.0f} ms")
    if r.get("net"):
        parts.append(net_label(r))
    return "  ·  ".join(parts)


# ---------------------------------------------------------------- runner

def cmd_run():
    """Detached: run networkQuality in a pty so it streams live numbers."""
    ensure_dir()
    mode = os.environ.get("test_mode", "parallel")
    write_json(LOCK, {"pid": os.getpid(), "started": time.time(), "mode": mode})
    write_json(LIVE, {})
    remove(RESULT, ERROR)

    net = {}
    detector = threading.Thread(target=lambda: net.update(current_network()))
    detector.start()

    args = ["/usr/bin/networkQuality", "-M", str(MAX_SECONDS), "-c" + RESULT] + MODES.get(mode, [])
    master, slave = pty.openpty()
    proc = subprocess.Popen(args, stdout=slave, stderr=slave, stdin=subprocess.DEVNULL, close_fds=True)
    os.close(slave)
    buf, last_write, last_frame = "", 0.0, 0.0
    target, shown = {}, {}
    while True:
        r, _, _ = select.select([master], [], [], 1 / FPS / 2)
        if r:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf = (buf + chunk.decode("utf-8", "replace"))[-4000:]
        if time.time() - last_write > 0.2:
            m = None
            for m in LIVE_RE.finditer(buf):
                pass
            if m:
                target = {"dl": float(m.group(1)), "ul": float(m.group(3)), "rpm": float(m.group(2))}
                write_json(LIVE, target)
                last_write = time.time()
        if target and time.time() - last_frame >= 1 / FPS:
            # Ease the shown values toward the latest reading; the pulse script just cats this frame.
            for k, v in target.items():
                shown[k] = v if k not in shown else shown[k] + (v - shown[k]) * EASE
            write_json(FRAME, {"update": {"ids": rings(shown["dl"], shown["ul"], shown["rpm"], live=True)}})
            last_frame = time.time()
        if proc.poll() is not None and not r:
            break
    proc.wait()
    detector.join(timeout=5)

    res = read_json(RESULT)
    if res and (res.get("dl_throughput") or res.get("ul_throughput")):
        run = {
            "ts": time.time(),
            "dl": res["dl_throughput"] / 1e6 if res.get("dl_throughput") else None,
            "ul": res["ul_throughput"] / 1e6 if res.get("ul_throughput") else None,
            "rpm": res.get("responsiveness"),
            "idle_ms": res.get("base_rtt"),
            "iface": res.get("interface_name", ""),
            "endpoint": res.get("test_endpoint", ""),
            "mode": mode,
        }
        run.update(net)
        with open(HISTORY, "a") as f:
            f.write(json.dumps(run) + "\n")
        message = summary(run)
    else:
        tail = [l.strip() for l in re.split(r"[\r\n]", buf) if l.strip() and not LIVE_RE.search(l)]
        message = "Test failed — " + (tail[-1][:120] if tail else "check your connection")
        write_json(ERROR, {"ts": time.time(), "message": message})
    try:
        ui_open = time.time() - os.path.getmtime(HEARTBEAT) < 1.5
    except OSError:
        ui_open = False
    if not ui_open:
        notify(message)
    remove(LOCK, LIVE, FRAME)
    prune_dials()


def notify(text):
    """Post via the workflow's "notify" External Trigger so it shows as an Alfred notification."""
    script = (f'tell application id "com.runningwithcrayons.Alfred" to run trigger "notify" '
              f'in workflow "{BUNDLE}" with argument {json.dumps(text)}')
    subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True)


def spawn_runner():
    if running_lock():
        return
    ensure_dir()
    remove(ERROR)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_run"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if os.path.exists(LOCK):
            break
        time.sleep(0.05)


def kill_runner():
    lock = running_lock()
    if lock:
        try:
            os.killpg(lock["pid"], signal.SIGTERM)
        except OSError:
            pass
    remove(LOCK, LIVE, FRAME)
    prune_dials()


# ---------------------------------------------------------------- UI rendering

def rings(dl, ul, rpm, live=False):
    ids = {}
    for kind, v in (("dl", dl), ("ul", ul), ("rpm", rpm)):
        g, colour = grade(kind, v)
        ids[f"r_{kind}_ring"] = {"image": {"type": "file", "src": dial(kind, ring_fill(kind, v), colour)}}
        ids[f"r_{kind}_value"] = {"text": fmt_num(v)}
        ids[f"r_{kind}_grade"] = {"text": " " if live else g or " ", "colour": colour}
    return ids


def header(idle, title, subtitle, live=False):
    g, colour = grade("idle", idle)
    return {
        "lat_value": {"text": f"{idle:.0f} ms" if idle else "— ms", "colour": "labelColor" if idle else GREY},
        "lat_grade": {"text": " " if live else g or " ", "colour": colour},
        "title_net": {"text": title},
        "title_sub": {"text": subtitle},
    }


def history_row(r):
    def colour(kind, v):
        g, c = grade(kind, v)
        return c if g == "Poor" else "labelColor"  # only problems get colour
    g, gc = grade("rpm", r.get("rpm"))
    return {
        "uid": str(r["ts"]),
        "h_when.text": when(r["ts"]),
        "h_net.text": net_label(r),
        "h_dl.text": fmt_num(r.get("dl")),
        "h_dl.colour": colour("dl", r.get("dl")),
        "h_ul.text": fmt_num(r.get("ul")),
        "h_ul.colour": colour("ul", r.get("ul")),
        "h_rpm.text": f"{r['rpm']:.0f}" if r.get("rpm") else "—",
        "h_grade.text": g,
        "h_grade.colour": gc if g == "Poor" else GREY,
        "h_idle.text": f"{r['idle_ms']:.0f} ms" if r.get("idle_ms") else "—",
        "h_idle.colour": colour("idle", r.get("idle_ms")),
    }


def picker_items(runs):
    """All networks, then each network (most recent first), then VPN."""
    seen, nets, has_vpn = set(), [], False
    for r in reversed(runs):
        k = net_key(r)
        if k == VPN:
            has_vpn = True
        elif k not in seen:
            seen.add(k)
            nets.append(r)
    names = [n.get("net") for n in nets]
    items = [{"text": "All networks", "value": ALL}]
    if nets:
        items.append({"type": "separator"})
    for n in nets:
        text = n.get("net") or "Unknown network"
        if names.count(n.get("net")) > 1 and n.get("router"):
            text += f" ({n['router'][-5:]})"
        items.append({"text": text, "value": net_key(n)})
    if has_vpn:
        items += [{"type": "separator"}, {"text": "VPN results", "value": VPN}]
    return items


def render(key=None, status=None):
    """Full window state for the given network filter."""
    key = key if key is not None else ARG or ALL
    runs = history()
    items = picker_items(runs)
    if key not in {i.get("value") for i in items}:
        key = ALL
    shown = filtered(runs, key)
    lock = running_lock()
    err = read_json(ERROR)
    ids = {"net_picker": {"items": items, "selected": key}}

    if lock:
        live = read_json(LIVE) or {}
        ids.update(rings(live.get("dl"), live.get("ul"), live.get("rpm"), live=True))
        prev = shown[-1] if shown else (runs[-1] if runs else {})
        ids.update(header(None, net_label(prev) if prev else "Network Quality", "Testing…", live=True))
        ids["tags"] = {"components": [tag("Testing…")]}
    elif shown:
        last = shown[-1]
        ids.update(rings(last.get("dl"), last.get("ul"), last.get("rpm")))
        kind = last.get("net_type") or ""
        ids.update(header(last.get("idle_ms"), net_label(last), (kind + " · " if kind else "") + "Tested " + when(last["ts"])))
        ids["tags"] = {"components": [tag(u, ok) for u, ok in uses(last)]}
    else:
        ids.update(rings(None, None, None))
        ids.update(header(None, "Network Quality", "No tests yet"))
        ids["tags"] = {"components": [tag("Press ↩ to run your first test")]}

    if err and not lock and (not runs or err["ts"] > runs[-1]["ts"]):
        ids["tags"] = {"components": [error_tag(err["message"])]}

    ids["history"] = {"data": {"type": "basic", "src": [history_row(r) for r in reversed(shown[-200:])]}}
    ids["run_button"] = {"text": "Stop" if lock else "Run Test"}
    if status is None:
        n = len(shown)
        status = f"{n} test{'s' if n != 1 else ''}"
    ids["status"] = {"text": status}
    return {"variables": {"net_filter": key}, "update": {"relayout": True, "ids": ids},
            "pulse": PULSE if lock else PULSE_OFF}


def emit(obj):
    prune_dials()
    print(json.dumps(obj))


def current_router():
    """Router MAC of the active connection (~10 ms); identifies known networks without the slow SSID lookup."""
    iface = re.search(r"interface: (\S+)", sh("/sbin/route", "-n", "get", "default"))
    if not iface or iface.group(1).startswith(("utun", "ppp", "ipsec")):
        return None
    gw = sh("/usr/sbin/ipconfig", "getoption", iface.group(1), "router").strip()
    mac = re.search(r" at ([0-9a-f:]+) ", sh("/usr/sbin/arp", "-n", gw)) if gw else None
    return mac.group(1) if mac else None


def cmd_load():
    if os.environ.get("autostart") in ("1", "true") and not running_lock():
        spawn_runner()
    runs = history()
    router = current_router()
    here = [r for r in runs if router and r.get("router") == router and not r.get("vpn")]
    emit(render(net_key(here[-1]) if here else ALL))


def cmd_toggle():
    if running_lock():
        cmd_stop()
    else:
        cmd_start()


def cmd_start():
    spawn_runner()
    emit(render())


def cmd_stop():
    kill_runner()
    emit(render(status="Test cancelled"))


def cmd_pulse():
    ensure_dir()
    with open(HEARTBEAT, "w"):
        pass
    if not running_lock():
        emit(render())  # finished: final render, pulse off
        return
    live = read_json(LIVE) or {}
    emit({"update": {"ids": rings(live.get("dl"), live.get("ul"), live.get("rpm"), live=True)}})


def cmd_copy():
    shown = filtered(history(), ARG or ALL)
    if not shown:
        emit({"update": {"ids": {"status": {"text": "Nothing to copy yet"}}}})
        return
    emit({"copy": {"type": "text", "src": summary(shown[-1])},
          "update": {"ids": {"status": {"text": "Copied latest result"}}}})


def cmd_help():
    """A sheet explaining the test, built from native labels using the configured thresholds."""
    W = 500
    n = lambda v: f"{v:g}"

    def text(t, **kw):
        lab = {"type": "label", "text": t, "linebreak": "wordwrap", "constraints": {"size": {"width": W}}}
        lab.update(kw)
        return lab

    def left_row(view):
        """Border layouts are the reliable way to left-align a row in Alfred UI."""
        view["constraints"] = {"region": "left"}
        return {"type": "view", "layout": "border", "components": [view, {"constraints": {"region": "centre"}}]}

    def scale(unit, lo, hi, lower_better=False):
        """Coloured ranges: Poor / Okay / Great."""
        if lower_better:
            parts = [(f"Great below {n(lo)}", GREEN), (f"Okay {n(lo)}–{n(hi)}", YELLOW), (f"Poor above {n(hi)} {unit}", RED)]
        else:
            parts = [(f"Poor below {n(lo)}", RED), (f"Okay {n(lo)}–{n(hi)}", YELLOW), (f"Great from {n(hi)} {unit}", GREEN)]
        row = []
        for i, (t, c) in enumerate(parts):
            if i:
                row.append({"type": "label", "text": "·", "colour": GREY, "font": {"size": "small"}})
            row.append({"type": "label", "text": t, "colour": c, "font": {"size": "small", "weight": "medium", "monospaced": "digits"}})
        return left_row({"type": "view", "layout": "inline", "direction": "horizontal", "spacing": 5, "components": row})

    def section(title, body, *extra):
        return {"type": "view", "layout": "inline", "direction": "vertical", "spacing": 4, "components":
                [{"type": "label", "text": title, "font": {"weight": "semibold"}},
                 text(body, colour="secondaryLabelColor")] + list(extra)}

    dl, ul = thresholds("download", (25, 100)), thresholds("upload", (3, 20))
    rpm, lat = thresholds("rpm", (300, 1000)), thresholds("latency", (30, 100))
    tags = [("4K streaming", "download of at least 25 Mbps"),
            ("HD video calls", "upload of at least 3 Mbps and responsiveness of at least 300 RPM"),
            ("Gaming", "responsiveness of at least 500 RPM and latency of 60 ms or less"),
            ("Large uploads", "upload of at least 20 Mbps")]
    tag_rows = [{"type": "view", "layout": "border", "spacing": 8, "components": [
        {"type": "label", "text": name, "font": {"size": "small", "weight": "medium"},
         "constraints": {"region": "left", "size": {"width": 104}}},
        {"type": "label", "text": rule, "colour": "secondaryLabelColor", "font": {"size": "small"},
         "constraints": {"region": "centre", "position": "left"}}]} for name, rule in tags]

    body = [
        {"type": "label", "text": "How the test works", "font": {"size": 18, "weight": "semibold"}},
        text("Network Quality runs Apple's built-in networkQuality tool. It opens many connections to a nearby "
             "Apple server and pushes as much data as it can in both directions until speeds level off, "
             "usually 10–20 seconds (it stops at 20).", colour="secondaryLabelColor"),
        section("Download and upload", "How much data your connection moves per second, in megabits per second (Mbps). "
                "The dials use a log scale up to 1,000 Mbps, like a speedometer. The defaults are the US FCC broadband "
                "benchmarks from 2015 (25/3 Mbps) and 2024 (100/20 Mbps).",
                scale("Mbps download", *dl), scale("Mbps upload", *ul)),
        section("Responsiveness", "How quickly the network answers while it is busy, in round-trips per minute (RPM). "
                "Low responsiveness is what makes calls stutter and games lag when someone else is streaming, "
                "even on a fast connection. The defaults follow the IETF responsiveness draft that networkQuality implements, where about 300 RPM is poor and 1,000 fair.", scale("RPM", *rpm)),
        section("Latency", "Round-trip time to the server while the network is idle, in milliseconds. Lower is better.",
                scale("ms", *lat, lower_better=True)),
        {"type": "view", "layout": "inline", "direction": "vertical", "spacing": 4, "components":
            [{"type": "label", "text": "Tags", "font": {"weight": "semibold"}}] + tag_rows},
        section("History", "Each test records the Wi-Fi name (or Ethernet connection) and router, so the dropdown can "
                "compare a network with itself. Results over a VPN are kept under VPN results."),
        text("Change the thresholds in Alfred Preferences → Workflows → Network Quality → Configure Workflow.",
             colour="tertiaryLabelColor", font={"size": "small"}),
    ]
    emit({"sheet": {"id": "help", "root": {
        "type": "view", "layout": "border", "padding": 24, "spacing": 16, "components": [
            {"type": "view", "layout": "inline", "direction": "vertical", "spacing": 14,
             "constraints": {"region": "centre"}, "components": body},
            {"type": "view", "layout": "inline", "direction": "horizontal", "constraints": {"region": "bottom", "position": "right"},
             "components": [{"type": "button", "text": "Done", "shortcut": "↩", "focus": True,
                             "events": [{"triggers": "actioned", "action": {"type": "json", "src": {"sheet": {"close": "help"}}}}]}]}]}}})


# ---------------------------------------------------------------- Script Filter

def cmd_history():
    items, out = [], {}
    if running_lock():
        live = read_json(LIVE) or {}
        items.append({"title": f"Testing… ↓ {fmt_num(live.get('dl'))}  ↑ {fmt_num(live.get('ul'))} Mbps",
                      "subtitle": "Live result from the running test", "valid": False})
        out["rerun"] = 0.5
    runs = history()
    for r in reversed(runs[-50:]):
        title = f"↓ {fmt_num(r.get('dl'))}  ↑ {fmt_num(r.get('ul'))} Mbps"
        if r.get("rpm"):
            title += f"  ·  {grade('rpm', r['rpm'])[0]} responsiveness"
        items.append({
            "title": title,
            "subtitle": f"{when(r['ts'])} · {net_label(r)}"
                        + (f" · {r['idle_ms']:.0f} ms" if r.get("idle_ms") else "") + " · ↩ copy · ⌘↩ open window",
            "match": f"{net_label(r)} {when(r['ts'])}",
            "arg": summary(r),
            "valid": True,
        })
    if not items:
        items.append({"title": "No tests yet", "subtitle": "⌘↩ open the window to run one", "arg": "", "valid": True})
    out["items"] = items
    emit(out)


ARG = (sys.argv + ["", ""])[2]  # network filter passed as {var:net_filter}, or select's new value

if __name__ == "__main__":
    cmd, arg = (sys.argv + ["", ""])[1:3]
    handlers = {"load": cmd_load, "toggle": cmd_toggle, "start": cmd_start,
                "stop": cmd_stop, "pulse": cmd_pulse, "copy": cmd_copy, "history": cmd_history, "help": cmd_help,
                "_run": cmd_run, "select": lambda: emit(render(arg))}
    handlers.get(cmd, lambda: sys.exit(f"unknown command {cmd!r}"))()
