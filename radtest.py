#!/usr/bin/env python3
"""
radtest - examination of NVMe drives after proton irradiation.

Commands:
  radtest.py watch   [--yes]       hot-plug watcher: tests every drive that gets plugged in
  radtest.py run nvmeX [--yes]     test a controller that is already present
  radtest.py status                state of the currently running test (from another terminal)
  radtest.py report                summary report (CSV + Markdown + text)

Steps per drive:
   1  identify and save every available log (read-only)
   2  short self-test          3  extended self-test
   4  pass 0: read through the original content (read errors after irradiation)
   5  pass 1: full write       6  pass 1: read back + verify
   7  save logs
   8  erase (sanitize block erase / format) + erase-check read
   9  pass 2: full write      10  pass 2: read back + verify
  11  final logs, short self-test, evaluation
"""
import argparse
import csv
import datetime as dt
import glob
import json
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NVBLK = ROOT / "bin" / "nvblk"
NVME = shutil.which("nvme") or "nvme"
SMARTCTL = shutil.which("smartctl")

# for the duration of the test (only affects controllers attached afterwards)
MODPARAMS = {
    "default_ps_max_latency_us": "0",  # APST off: do not skew the latency measurement
    "max_retries": "0",  # do not let the kernel silently retry
    "io_timeout": "60",
    "admin_timeout": "120",
}

VERDICTS = {
    "OK": "HEALTHY",
    "SUSPECT": "SUSPECT (tests clean, but warning signs present)",
    "LOGICAL": "SOFTWARE/LOGICAL DAMAGE (gone after erase)",
    "PHYSICAL": "PHYSICAL DAMAGE",
    "DEAD": "NOT WORKING / LOST DURING TEST",
    "INCOMPLETE": "INCOMPLETE TEST",
}

SELFTEST_RESULTS = {
    0: "completed without error",
    1: "aborted by self-test command",
    2: "aborted by controller reset",
    3: "aborted by namespace removal",
    4: "aborted by format",
    5: "FATAL ERROR",
    6: "unknown segment failed",
    7: "one or more segments FAILED",
    8: "aborted for unknown reason",
    9: "aborted by sanitize",
    15: "entry not used",
}

VENDOR_PLUGINS = {
    0x8086: [["intel", "smart-log-add"], ["intel", "lat-stats"]],
    0x025E: [["solidigm", "smart-log-add"], ["solidigm", "vs-smart-add-log"]],
    0x1344: [["micron", "vs-smart-add-log"], ["micron", "vs-nand-stats"], ["micron", "vs-smart-ext-log"]],
    0x1B96: [["wdc", "vs-smart-add-log"], ["wdc", "vs-nand-stats"], ["sndk", "vs-smart-add-log"]],
    0x1C58: [["wdc", "vs-smart-add-log"], ["wdc", "vs-nand-stats"]],
    0x15B7: [["sndk", "vs-smart-add-log"], ["sndk", "vs-nand-stats"], ["wdc", "vs-smart-add-log"]],
    0x1179: [["toshiba", "vs-smart-add-log"]],
    0x1E0F: [["toshiba", "vs-smart-add-log"]],
}

# fields with names like these are collected from vendor logs (bad block, spare, ECC, ...)
VENDOR_KEY_RE = re.compile(
    r"bad|retir|spare|uncorrect|ecc|xor|refresh|erase.?fail|program.?fail|grown|remap|"
    r"reallocat|nand|wear|crc|end.?to.?end|pcie.*err|thermal.?throttl",
    re.I,
)


# ---------------------------------------------------------------------------
# logging, status
# ---------------------------------------------------------------------------


class Log:
    def __init__(self, results: Path):
        self.results = results
        self.global_f = open(results / "radtest.log", "a", buffering=1)
        self.drive_f = None
        self.prefix = ""

    def set_drive(self, path, prefix):
        if self.drive_f:
            self.drive_f.close()
        self.drive_f = open(path, "a", buffering=1) if path else None
        self.prefix = prefix

    def __call__(self, msg, level="INFO"):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        mark = {"INFO": "", "WARN": "WARNING: ", "ERR": "ERROR: ", "OK": ""}[level]
        line = f"[{ts}] {self.prefix}{mark}{msg}"
        if sys.stdout.isatty():
            color = {"WARN": "\033[33m", "ERR": "\033[31m", "OK": "\033[32m"}.get(level)
            print(f"{color}{line}\033[0m" if color else line, flush=True)
        else:
            print(line, flush=True)
        full = f"{dt.datetime.now().isoformat(timespec='seconds')} {line[11:]}\n"
        self.global_f.write(full)
        if self.drive_f:
            self.drive_f.write(full)

    def banner(self, msg):
        bar = "=" * 72
        for m in (bar, msg, bar):
            self(m)


REQ_DIR = ".requests"


def enqueue_start(results: Path, ctrl, serial=None, retest=False, source="cli"):
    """Queue a start request for the watcher. Returns the request file path."""
    d = results / REQ_DIR
    d.mkdir(parents=True, exist_ok=True)
    req = {"action": "start", "ctrl": ctrl, "serial": serial, "retest": bool(retest),
           "source": source, "created": dt.datetime.now().isoformat(timespec="seconds")}
    f = d / f"{time.time():.6f}-{safe_name(ctrl)}.json"
    f.write_text(json.dumps(req, ensure_ascii=False))
    return f


def take_requests(results: Path):
    """Read and remove queued requests (oldest first)."""
    d = results / REQ_DIR
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            pass
        try:
            f.unlink()
        except OSError:
            pass
    return out


def ns_size_bytes(ctrl):
    total = 0
    for ns in namespaces(ctrl):
        sectors = as_int(rd(f"/sys/block/{ns}/size"), 0)
        total += (sectors or 0) * 512
    return total


def drive_info(results: Path, ci):
    """Everything the operator needs to decide whether to start a test on this drive."""
    nss = namespaces(ci["ctrl"])
    busy = next((in_use(ns) for ns in nss if in_use(ns)), None)
    prev = previous_runs(results, ci["serial"]) if ci["serial"] else []
    done = [r for r in prev if r.get("status") == "completed"]
    return dict(ci, namespaces=nss, size_bytes=ns_size_bytes(ci["ctrl"]), in_use=busy,
                runs=len(prev), tested=bool(done),
                last_verdict=(done[-1] if done else (prev[-1] if prev else {})).get("verdict"),
                last_run=(done[-1] if done else (prev[-1] if prev else {})).get("started"))


def write_status(results: Path, **kw):
    kw["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    tmp = results / ".status.json.tmp"
    tmp.write_text(json.dumps(kw, ensure_ascii=False, indent=1))
    tmp.replace(results / ".status.json")


def fmt_dur(s):
    if s is None or s < 0:
        return "?"
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def gb(nbytes):
    return nbytes / 1e9


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def rd(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def run(cmd, timeout=120, out=None, binary=False):
    """Run a command; save the output to a file. Never raises."""
    t0 = time.time()
    res = {"cmd": [str(c) for c in cmd], "rc": None, "timed_out": False}
    try:
        p = subprocess.run([str(c) for c in cmd], capture_output=True, timeout=timeout)
        res["rc"] = p.returncode
        stdout, stderr = p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        res["timed_out"] = True
        res["rc"] = -1
        stdout, stderr = e.stdout or b"", (e.stderr or b"") + b"\n[TIMEOUT]"
    except OSError as e:
        res["rc"] = -2
        stdout, stderr = b"", str(e).encode()
    res["dur_s"] = round(time.time() - t0, 3)
    res["stderr"] = stderr.decode(errors="replace")
    if binary:
        res["data"] = stdout
    else:
        res["stdout"] = stdout.decode(errors="replace")
    if out:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            out.write_bytes(stdout)
        else:
            out.write_text(res["stdout"])
        if stderr.strip() or res["rc"]:
            out.with_suffix(out.suffix + ".err").write_text(
                f"$ {' '.join(res['cmd'])}\nrc={res['rc']} dur={res['dur_s']}s\n{res['stderr']}"
            )
    return res


def nvme(args, **kw):
    return run([NVME] + args, **kw)


def as_int(v, default=None):
    if v is None:
        return default
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict):
        for k in ("value", "raw", "Value"):
            if k in v:
                return as_int(v[k], default)
        return default
    try:
        s = str(v).strip().replace(",", "")
        m = re.match(r"^(0x[0-9a-fA-F]+|-?\d+)", s)
        return int(m.group(1), 0) if m else default
    except ValueError:
        return default


def load_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def printable(s):
    return bool(s) and all(32 <= ord(ch) < 127 for ch in s)


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", s.strip()) or "UNKNOWN"


# ---------------------------------------------------------------------------
# device discovery
# ---------------------------------------------------------------------------


def controllers(transports=("pcie",)):
    out = []
    for p in sorted(glob.glob("/sys/class/nvme/nvme*")):
        name = os.path.basename(p)
        if not re.fullmatch(r"nvme\d+", name):
            continue
        tr = rd(f"{p}/transport")
        if tr not in transports:
            continue
        out.append(
            {
                "ctrl": name,
                "transport": tr,
                "state": rd(f"{p}/state"),
                "serial": rd(f"{p}/serial"),
                "model": rd(f"{p}/model"),
                "firmware": rd(f"{p}/firmware_rev"),
                "address": rd(f"{p}/address"),
            }
        )
    return out


def namespaces(ctrl):
    """List of namespace block devices (/dev/nvmeXnY) belonging to the controller."""
    heads = set()
    for sub in glob.glob("/sys/class/nvme-subsystem/nvme-subsys*"):
        if os.path.exists(f"{sub}/{ctrl}"):
            for h in glob.glob(f"{sub}/nvme*n*"):
                b = os.path.basename(h)
                if re.fullmatch(r"nvme\d+n\d+", b):
                    heads.add(b)
    for h in glob.glob(f"/sys/class/nvme/{ctrl}/nvme*n*"):
        b = os.path.basename(h)
        if re.fullmatch(r"nvme\d+n\d+", b):
            heads.add(b)
    return sorted(heads, key=lambda s: int(s.rsplit("n", 1)[1]))


def in_use(ns):
    """Is the namespace or its partition mounted/in use. Returns a reason or None."""
    names = [ns] + [os.path.basename(p) for p in glob.glob(f"/sys/block/{ns}/{ns}p*")]
    mounts = rd("/proc/mounts") + "\n" + rd("/proc/swaps")
    for n in names:
        if re.search(rf"^/dev/{n}\s", mounts, re.M):
            return f"/dev/{n} mounted/swap"
        holders = glob.glob(f"/sys/block/{ns}/holders/*") + glob.glob(f"/sys/block/{ns}/{n}/holders/*")
        if holders:
            return f"/dev/{n} in use: {', '.join(os.path.basename(h) for h in holders)}"
    return None


def pci_dev_path(ctrl):
    p = os.path.realpath(f"/sys/class/nvme/{ctrl}/device")
    return p if os.path.exists(f"{p}/class") else None


def pcie_info(ctrl):
    p = pci_dev_path(ctrl)
    if not p:
        return {}
    info = {"bdf": os.path.basename(p)}
    for k in ("current_link_speed", "current_link_width", "max_link_speed", "max_link_width", "vendor", "device",
              "subsystem_vendor", "subsystem_device"):
        info[k] = rd(f"{p}/{k}")
    for k in ("aer_dev_correctable", "aer_dev_nonfatal", "aer_dev_fatal"):
        txt = rd(f"{p}/{k}")
        m = re.search(r"TOTAL_\w+\s+(\d+)", txt)
        info[k] = int(m.group(1)) if m else None
    return info


def nvme_pci_devices():
    """PCIe NVMe devices (class 0x0108xx) that are not assigned to a VM."""
    out = {}
    for p in glob.glob("/sys/bus/pci/devices/*"):
        if not rd(f"{p}/class").startswith("0x0108"):
            continue
        drv = os.path.basename(os.path.realpath(f"{p}/driver")) if os.path.exists(f"{p}/driver") else ""
        if drv and drv != "nvme":
            continue  # e.g. vfio-pci: handed to a VM, not our business
        ctrls = [os.path.basename(c) for c in glob.glob(f"{p}/nvme/nvme*")]
        out[os.path.basename(p)] = {"driver": drv, "ctrls": ctrls,
                                    "states": [rd(f"/sys/class/nvme/{c}/state") for c in ctrls]}
    return out


# ---------------------------------------------------------------------------
# kernel module parameters
# ---------------------------------------------------------------------------


class ModParams:
    def __init__(self, log):
        self.log = log
        self.saved = {}

    def apply(self):
        base = Path("/sys/module/nvme_core/parameters")
        for k, v in MODPARAMS.items():
            f = base / k
            try:
                old = f.read_text().strip()
                if old != v:
                    f.write_text(v)
                    self.saved[k] = old
                    self.log(f"nvme_core.{k}: {old} -> {v} (for the duration of the test)")
            except OSError as e:
                self.log(f"nvme_core.{k} cannot be set: {e}", "WARN")

    def restore(self):
        for k, v in self.saved.items():
            try:
                Path(f"/sys/module/nvme_core/parameters/{k}").write_text(v)
            except OSError:
                pass
        if self.saved:
            self.log("nvme_core parameters restored")
        self.saved = {}


# ---------------------------------------------------------------------------
# log collection and parsing
# ---------------------------------------------------------------------------


def get_log_raw(ctrl, lid, length, timeout=60):
    r = nvme(["get-log", f"/dev/{ctrl}", "-i", str(lid), "-l", str(length), "-b", "--no-retries"],
             timeout=timeout, binary=True)
    return r["data"] if r["rc"] == 0 else None


def supported_lids(ctrl):
    data = get_log_raw(ctrl, 0x00, 1024)
    if not data or len(data) < 1024:
        return None
    return [i for i in range(256) if struct.unpack_from("<I", data, i * 4)[0] & 1]


def collect_logs(ctrl, ns, d: Path, vid, first=False, log=None, telemetry=False):
    """Save every available piece of information into directory d. Returns the parsed data."""
    d.mkdir(parents=True, exist_ok=True)
    C, N = f"/dev/{ctrl}", f"/dev/{ns}" if ns else None
    nr = ["--no-retries"]
    parsed = {}

    idc = nvme(["id-ctrl", C, "-o", "json"] + nr, out=d / "id-ctrl.json")
    parsed["id_ctrl"] = load_json(idc["stdout"]) or {}
    nvme(["id-ctrl", C, "-H"] + nr, out=d / "id-ctrl.txt")
    nvme(["id-ctrl", C, "-b"] + nr, out=d / "id-ctrl.bin", binary=True)
    elpe = as_int(parsed["id_ctrl"].get("elpe"), 63)
    lpa = as_int(parsed["id_ctrl"].get("lpa"), 0)

    if N:
        idn = nvme(["id-ns", N, "-o", "json"] + nr, out=d / "id-ns.json")
        parsed["id_ns"] = load_json(idn["stdout"]) or {}
        nvme(["id-ns", N, "-H"] + nr, out=d / "id-ns.txt")
        nvme(["ns-descs", N, "-o", "json"] + nr, out=d / "ns-descs.json")
    nvme(["list-ns", C] + nr, out=d / "list-ns.txt")

    sl = nvme(["smart-log", C, "-o", "json"] + nr, out=d / "smart-log.json")
    parsed["smart"] = load_json(sl["stdout"]) or {}
    nvme(["smart-log", C, "-H"] + nr, out=d / "smart-log.txt")
    el = nvme(["error-log", C, "-e", str(elpe + 1), "-o", "json"] + nr, out=d / "error-log.json")
    parsed["error_log"] = load_json(el["stdout"]) or {}
    nvme(["fw-log", C, "-o", "json"] + nr, out=d / "fw-log.json")
    nvme(["self-test-log", C, "-o", "json"] + nr, out=d / "self-test-log.json")
    nvme(["sanitize-log", C, "-o", "json"] + nr, out=d / "sanitize-log.json")
    nvme(["effects-log", C, "-o", "json"] + nr, out=d / "effects-log.json")
    nvme(["endurance-log", C, "-g", "1", "-o", "json"] + nr, out=d / "endurance-log.json")
    for sel, name in ((0, "current"), (1, "default"), (2, "saved"), (3, "capabilities")):
        if sel and not first:
            break
        dump_features(C, sel, d / f"features-{name}.txt")
    if pci_dev_path(ctrl):
        nvme(["show-regs", C, "-H"] + nr, out=d / "registers.txt")

    lids = supported_lids(ctrl)
    parsed["supported_lids"] = [f"0x{x:02x}" for x in lids] if lids is not None else None
    raw_lids = {0x01: 4096, 0x02: 512, 0x03: 512, 0x05: 4096, 0x06: 564, 0x81: 512}
    if lids is not None:
        for lid in lids:
            if lid >= 0xC0:
                raw_lids[lid] = 4096  # vendor log: raw dump for later analysis
    raw = d / "raw-logs"
    for lid, ln in sorted(raw_lids.items()):
        data = get_log_raw(ctrl, lid, ln)
        if data:
            raw.mkdir(exist_ok=True)
            (raw / f"lid_{lid:02x}.bin").write_bytes(data)

    # persistent event log (0x0d): open context + read, then release
    if lids is None or 0x0D in lids:
        nvme(["persistent-event-log", C, "-a", "1", "-o", "json"] + nr, out=d / "persistent-event-log.json",
             timeout=180)
        nvme(["persistent-event-log", C, "-a", "2"] + nr, timeout=30)

    # telemetry (lpa bit3): it froze one irradiated drive, so only on request (at the end of the test)
    if telemetry:
        parsed["telemetry"] = collect_telemetry(ctrl, d / "telemetry", lpa)

    # vendor plugins
    vend = {}
    vdir = d / "vendor"
    cmds = [["ocp", "smart-add-log"]] + VENDOR_PLUGINS.get(vid or -1, [])
    for c in cmds:
        r = nvme(c + [C], out=vdir / f"{'_'.join(c)}.txt", timeout=120)
        if r["rc"] == 0:
            vend.update(extract_vendor_fields(r["stdout"], prefix=c[0]))
    parsed["vendor_health"] = vend

    if SMARTCTL:
        s = run([SMARTCTL, "-x", "-j", C], out=d / "smartctl.json", timeout=120)
        parsed["smartctl"] = load_json(s["stdout"]) or {}

    parsed["pcie"] = pcie_info(ctrl)
    if parsed["pcie"].get("bdf") and first:
        run(["lspci", "-vvv", "-s", parsed["pcie"]["bdf"]], out=d / "lspci.txt")
    (d / "pcie.json").write_text(json.dumps(parsed["pcie"], indent=1))
    parsed["self_test"] = parse_selftest_log(get_log_raw(ctrl, 0x06, 564))
    return parsed


FEATURE_IDS = list(range(0x01, 0x21)) + [0x7D, 0x7E, 0x7F, 0x80, 0x81, 0x82, 0x83, 0x84]


def dump_features(C, sel, path):
    """Standard features one by one (the nvme-cli 'all' mode stops at the first error)."""
    parts = []
    for fid in FEATURE_IDS:
        r = nvme(["get-feature", C, "-f", hex(fid), "-s", str(sel), "-H", "--no-retries"], timeout=30)
        if r["rc"] == 0:
            parts.append(r["stdout"].rstrip())
        else:
            parts.append(f"get-feature 0x{fid:02x}: {(r['stderr'].strip() or r['stdout'].strip())[:160]}")
    path.write_text("\n".join(parts) + "\n")


def collect_telemetry(ctrl, tdir, lpa):
    """Controller- and host-initiated telemetry. Returns the result and whether the drive hung."""
    res = {"supported": bool(lpa & 0x08), "items": {}}
    if not res["supported"]:
        return res
    tdir.mkdir(parents=True, exist_ok=True)
    C = f"/dev/{ctrl}"
    for name, args in (("controller-initiated", ["-c"]), ("host-initiated", ["-g", "1"])):
        if rd(f"/sys/class/nvme/{ctrl}/state") != "live":
            res["items"][name] = {"skipped": "the controller is not alive"}
            continue
        r = nvme(["telemetry-log", C] + args + ["-d", "3", "-O", tdir / f"{name}.bin", "--no-retries"],
                 out=tdir / f"{name}.log", timeout=900)
        size = (tdir / f"{name}.bin").stat().st_size if (tdir / f"{name}.bin").exists() else 0
        state = rd(f"/sys/class/nvme/{ctrl}/state", "disappeared")
        res["items"][name] = {"rc": r["rc"], "dur_s": r["dur_s"], "bytes": size, "ctrl_state_after": state,
                              "error": r["stderr"].strip()[-200:]}
        if state != "live" or r["rc"] != 0 and r["dur_s"] > 30:
            res["hang"] = f"controller after {name} telemetry fetch: {state} ({r['dur_s']:.0f} s, rc={r['rc']})"
        elif r["dur_s"] > 30:
            res.setdefault("slow", []).append(f"{name} telemetry fetch took {r['dur_s']:.0f} s (successful)")
    return res


def extract_vendor_fields(text, prefix):
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z][^:]{2,80}?)\s*[:=]\s*(\S.*)$", line)
        if not m:
            continue
        k, v = m.group(1).strip(), m.group(2).strip()
        if VENDOR_KEY_RE.search(k):
            out[f"{prefix}: {k}"] = v
    return out


def parse_selftest_log(data):
    if not data or len(data) < 8:
        return None
    res = {"current_op": data[0], "progress_pct": data[1], "entries": []}
    for i in range(20):
        e = data[4 + i * 28: 4 + (i + 1) * 28]  # 4-byte header, 28-byte entries
        if len(e) < 28:
            break
        code, result = e[0] >> 4, e[0] & 0x0F
        if result == 0x0F:
            continue
        valid = e[2]
        res["entries"].append(
            {
                "type": {1: "short", 2: "extended", 7: "host-initiated", 0xE: "vendor"}.get(code, f"0x{code:x}"),
                "result": result,
                "result_desc": SELFTEST_RESULTS.get(result, f"0x{result:x}"),
                "segment": e[1] if result == 7 else None,
                "power_on_hours": struct.unpack_from("<Q", e, 4)[0],
                "failing_lba": struct.unpack_from("<Q", e, 16)[0] if valid & 2 else None,
                "sct": e[24] & 7 if valid & 4 else None,
                "sc": e[25] if valid & 8 else None,
            }
        )
    return res


SMART_KEYS = [
    "critical_warning", "temperature", "avail_spare", "spare_thresh", "percent_used",
    "endurance_grp_critical_warning_summary", "data_units_read", "data_units_written",
    "host_read_commands", "host_write_commands", "controller_busy_time", "power_cycles",
    "power_on_hours", "unsafe_shutdowns", "media_errors", "num_err_log_entries",
    "warning_temp_time", "critical_comp_time",
]


def smart_values(parsed):
    s = parsed.get("smart") or {}
    out = {k: as_int(s.get(k)) for k in SMART_KEYS}
    if out["temperature"] is not None and out["temperature"] > 200:
        out["temperature_c"] = out["temperature"] - 273
    else:
        out["temperature_c"] = out["temperature"]
    return out


def critical_warning_desc(cw):
    if not cw:
        return []
    bits = {
        0: "available spare below threshold",
        1: "temperature past threshold",
        2: "reliability degraded (media/internal error)",
        3: "media in READ-ONLY mode",
        4: "volatile memory backup failed",
        5: "persistent memory region read-only",
    }
    return [t for b, t in bits.items() if cw & (1 << b)]


def error_log_summary(parsed):
    el = parsed.get("error_log") or {}
    entries = el.get("errors") if isinstance(el, dict) else None
    entries = entries or []
    by_status, lbas, total = {}, [], 0
    for e in entries:
        if not as_int(e.get("error_count")):
            continue
        total += 1
        st = as_int(e.get("status_field"), 0)  # nvme-cli already reports it without the phase bit
        key = f"SCT{(st >> 8) & 7}/SC0x{st & 0xff:02x}"
        by_status[key] = by_status.get(key, 0) + 1
        lba = as_int(e.get("lba"))
        if lba is not None and lba not in (0, 0xFFFFFFFFFFFFFFFF):
            lbas.append(lba)
    return {"entries_read": total, "by_status": by_status, "lbas": sorted(set(lbas))[:200]}


def identity(parsed):
    ic, ns = parsed.get("id_ctrl") or {}, parsed.get("id_ns") or {}
    lbaf = []
    flbas = as_int(ns.get("flbas"), 0)
    for f in ns.get("lbafs") or []:
        lbaf.append(f)
    ds = None
    for f in lbaf:
        if as_int(f.get("lbaf"), -1) == (flbas & 0x0F):
            ds = as_int(f.get("ds"))
    lbs = 1 << ds if ds is not None else None
    nsze = as_int(ns.get("nsze"))
    return {
        "vid": as_int(ic.get("vid")),
        "ssvid": as_int(ic.get("ssvid")),
        "serial": str(ic.get("sn", "")).strip(),
        "model": str(ic.get("mn", "")).strip(),
        "firmware": str(ic.get("fr", "")).strip(),
        "ieee_oui": as_int(ic.get("ieee")),
        "cntlid": as_int(ic.get("cntlid")),
        "nvme_version": as_int(ic.get("ver")),
        "fguid": ic.get("fguid"),
        "subnqn": ic.get("subnqn"),
        "tnvmcap": as_int(ic.get("tnvmcap")),
        "eui64": ns.get("eui64"),
        "nguid": ns.get("nguid"),
        "nsze": nsze,
        "ncap": as_int(ns.get("ncap")),
        "nuse": as_int(ns.get("nuse")),
        "lba_size": lbs,
        "capacity_bytes": nsze * lbs if nsze is not None and lbs else None,
        "flbas_lbaf": flbas & 0x0F,
        "oacs": as_int(ic.get("oacs"), 0),
        "sanicap": as_int(ic.get("sanicap"), 0),
        "fna": as_int(ic.get("fna"), 0),
        "vwc": as_int(ic.get("vwc"), 0),
        "edstt_min": as_int(ic.get("edstt"), 0),
        "lpa": as_int(ic.get("lpa"), 0),
    }


# ---------------------------------------------------------------------------
# test of a single drive
# ---------------------------------------------------------------------------


class Aborted(Exception):
    pass


class DriveTest:
    STEPS = 11

    def __init__(self, args, log, ctrl_info):
        self.a = args
        self.log = log
        self.ci = ctrl_info
        self.ctrl = ctrl_info["ctrl"]
        self.results = Path(args.results)
        self.S = {"tool": "radtest", "status": "running", "steps": {}}
        self.step_no = 0
        self.stop = False

    # -- state --
    def step(self, title):
        self.step_no += 1
        self.cur_step = title
        self.log.banner(f"step {self.step_no}/{self.STEPS}: {title}")
        self.status(progress=None)
        self._t_step = time.time()

    def step_done(self, key, info):
        info = dict(info or {})
        info["duration_s"] = round(time.time() - self._t_step, 1)
        self.S["steps"][key] = info
        self.save()

    def status(self, **kw):
        write_status(self.results, running=True, ctrl=self.ctrl,
                     serial=self.S.get("identity", {}).get("serial") or self.ci["serial"],
                     model=self.S.get("identity", {}).get("model") or self.ci["model"], dir=str(self.dir) if hasattr(self, "dir") else None,
                     step=f"{self.step_no}/{self.STEPS} {getattr(self, 'cur_step', '')}", started=self.S.get("started"),
                     **kw)

    def save(self):
        (self.dir / "summary.json").write_text(json.dumps(self.S, indent=1, ensure_ascii=False, default=str))

    def alive(self):
        return rd(f"/sys/class/nvme/{self.ctrl}/state") == "live" and self.ctrl_serial_matches()

    def ctrl_serial_matches(self):
        return rd(f"/sys/class/nvme/{self.ctrl}/serial") == self.ci["serial"]

    def require_alive(self, wait=180):
        t0 = time.time()
        while time.time() - t0 < wait:
            if not os.path.exists(f"/sys/class/nvme/{self.ctrl}"):
                break
            if self.alive():
                return
            time.sleep(1)
        self.S["device_lost"] = True
        raise Aborted("the controller disappeared or is not in 'live' state")

    # -- main flow --
    def run(self):
        self.t_start = time.time()
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        serial = safe_name(self.ci["serial"])
        self.dir = self.results / serial / stamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log.set_drive(self.dir / "run.log", f"[{self.ctrl} {self.ci['serial']}] ")
        self.S["started"] = dt.datetime.now().isoformat(timespec="seconds")
        self.S["ctrl_sysfs"] = self.ci
        self.S["options"] = {k: v for k, v in vars(self.a).items() if k != "func"}
        self.S["modparams_active"] = {k: rd(f"/sys/module/nvme_core/parameters/{k}") for k in MODPARAMS}
        self.S["previous_runs"] = previous_runs(self.results, self.ci["serial"], exclude=self.dir)
        self.save()
        try:
            self._pipeline()
            self.S["status"] = "completed"
        except Aborted as e:
            self.S["status"] = "aborted"
            self.S["abort_reason"] = str(e)
            self.log(f"The test was aborted: {e}", "ERR")
            try:
                if self.alive():
                    self.log("Saving final logs after the abort...")
                    p = collect_logs(self.ctrl, self.ns, self.dir / "logs" / "after_abort", self.S["identity"].get("vid"))
                    self.S["smart_post"] = smart_values(p)
                    self.S["error_log_post"] = error_log_summary(p)
            except Exception as e2:  # noqa: BLE001
                self.log(f"saving logs failed: {e2}", "WARN")
        except KeyboardInterrupt:
            self.S["status"] = "interrupted"
            self.log("Interrupted (Ctrl+C)", "WARN")
            raise
        finally:
            self.S["finished"] = dt.datetime.now().isoformat(timespec="seconds")
            self.S["duration_s"] = round(time.time() - self.t_start)
            self.kernel_log()
            evaluate(self.S)
            self.save()
            (self.dir / "summary.txt").write_text(drive_text(self.S))
            write_status(self.results, running=False, last_dir=str(self.dir), last_verdict=self.S.get("verdict"))
        v = self.S["verdict"]
        self.log.banner(f"DONE ({fmt_dur(self.S['duration_s'])}) - verdict: {VERDICTS[v]}")
        lv = {"abort": "ERR", "physical": "ERR", "logical": "ERR", "suspect": "WARN", "note": "INFO"}
        groups = self.S.get("reason_groups") or {}
        if not any(groups.values()):
            self.log("  - every test completed without error", "OK")
        for g, items in groups.items():
            for r in items:
                self.log(f"  - {r}", lv.get(g, "INFO"))
        self.log(f"Details: {self.dir}/summary.txt")
        return self.S

    def _pipeline(self):
        a = self.a
        # 1. identify
        self.step("identify and save logs (read-only)")
        nss = namespaces(self.ctrl)
        if not nss:
            raise Aborted("no namespace on the controller")
        self.ns = nss[0]
        if len(nss) > 1:
            self.log(f"multiple namespaces ({', '.join(nss)}); only {self.ns} will be tested", "WARN")
        p = collect_logs(self.ctrl, self.ns, self.dir / "logs" / "01_pre", None, first=True)
        idn = identity(p)
        self.S["identity"] = idn
        # vendor logs now that the VID is known
        if VENDOR_PLUGINS.get(idn["vid"]):
            p2 = collect_logs(self.ctrl, self.ns, self.dir / "logs" / "01_pre", idn["vid"])
            p["vendor_health"] = p2["vendor_health"]
        self.S["namespace"] = self.ns
        self.S["namespaces_all"] = nss
        self.S["smart_pre"] = smart_values(p)
        self.S["error_log_pre"] = error_log_summary(p)
        self.S["vendor_health_pre"] = p.get("vendor_health")
        self.S["pcie_pre"] = p.get("pcie")
        self.S["selftest_log_pre"] = p.get("self_test")
        self.S["supported_lids"] = p.get("supported_lids")
        self.S["identity_anomalies"] = identity_anomalies(idn, self.ci)
        self.print_identity()
        self.step_done("identify", {})

        # 2-3. self-tests
        self.step("short self-test")
        self.selftest(1, "selftest_short_pre")
        self.step("extended self-test")
        if a.no_extended:
            self.log("skipped (--no-extended)")
            self.step_done("selftest_extended", {"skipped": True})
        else:
            self.selftest(2, "selftest_extended")

        # 4. survey of the original content
        self.step("pass 0: read through the original content (read-only)")
        self.S["pass0"] = {"readscan": self.nvblk("readscan", "pass0", "readscan", 0)}
        self.step_done("pass0", {})

        # 5-6. first pass
        self.write_cache_off()
        seed1 = secrets.randbits(62)
        self.step("pass 1: full write")
        self.S["pass1"] = {"seed": seed1, "write": self.nvblk("write", "pass1", "write", seed1)}
        self.step_done("pass1_write", {})
        self.step("pass 1: read back and verify")
        self.S["pass1"]["verify"] = self.nvblk("verify", "pass1", "verify", seed1)
        self.step_done("pass1_verify", {})

        # 7. logs
        self.step("save logs after pass 1")
        p = collect_logs(self.ctrl, self.ns, self.dir / "logs" / "02_after_pass1", idn["vid"])
        self.S["smart_mid"] = smart_values(p)
        self.S["error_log_mid"] = error_log_summary(p)
        self.step_done("logs_mid", {})

        # 8. erase
        self.step("erase (sanitize/format) and erase check")
        if a.no_erase:
            self.log("skipped (--no-erase)", "WARN")
            self.S["erase"] = {"method": "skipped"}
        else:
            self.S["erase"] = self.erase(idn)
            self.require_alive()
            if self.S["erase"].get("ok"):
                self.S["erase"]["check"] = self.nvblk("erasecheck", "erase", "erasecheck", 0)
        self.step_done("erase", {})

        # 9-10. second pass
        self.write_cache_off()
        seed2 = secrets.randbits(62)
        self.step("pass 2: full write")
        self.S["pass2"] = {"seed": seed2, "write": self.nvblk("write", "pass2", "write", seed2)}
        self.step_done("pass2_write", {})
        self.step("pass 2: read back and verify")
        self.S["pass2"]["verify"] = self.nvblk("verify", "pass2", "verify", seed2)
        self.step_done("pass2_verify", {})

        # 11. closing
        self.step("final logs, short self-test, evaluation")
        self.selftest(1, "selftest_short_post")
        p = collect_logs(self.ctrl, self.ns, self.dir / "logs" / "03_post", idn["vid"])
        # telemetry at the very end: it froze one irradiated drive
        self.log("fetching telemetry (the drive may hang from this - that is a result too)...")
        tel = collect_telemetry(self.ctrl, self.dir / "logs" / "03_post" / "telemetry",
                                idn.get("lpa", 0))
        self.S["telemetry"] = tel
        if tel.get("hang"):
            self.log(tel["hang"], "ERR")
        for m in tel.get("slow", []):
            self.log(m, "WARN")
        self.S["smart_post"] = smart_values(p)
        self.S["error_log_post"] = error_log_summary(p)
        self.S["vendor_health_post"] = p.get("vendor_health")
        self.S["pcie_post"] = p.get("pcie")
        self.S["identity_post"] = identity(p)
        self.step_done("final", {})

    def print_identity(self):
        i, s = self.S["identity"], self.S["smart_pre"]
        cap = f"{gb(i['capacity_bytes']):.1f} GB" if i["capacity_bytes"] else "?"
        self.log(f"Vendor VID: 0x{(i['vid'] or 0):04x}  Model: {i['model']}  S/N: {i['serial']}  FW: {i['firmware']}")
        self.log(f"Size: {cap} ({i['nsze']} x {i['lba_size']} B)  EUI64: {i['eui64']}  NGUID: {i['nguid']}")
        pc = self.S.get("pcie_pre") or {}
        if pc:
            self.log(f"PCIe: {pc.get('bdf')} {pc.get('current_link_speed')} x{pc.get('current_link_width')}")
        if s.get("power_on_hours") is not None:
            self.log(
                f"power-on hours: {s['power_on_hours']}  power cycles: {s['power_cycles']}  "
                f"unsafe shutdowns: {s['unsafe_shutdowns']}  percentage used: {s['percent_used']}%  "
                f"available spare: {s['avail_spare']}% (threshold {s['spare_thresh']}%)"
            )
            self.log(
                f"written: {gb((s['data_units_written'] or 0) * 512000):.1f} GB  "
                f"read: {gb((s['data_units_read'] or 0) * 512000):.1f} GB  "
                f"media errors: {s['media_errors']}  error log entries: {s['num_err_log_entries']}"
            )
            for w in critical_warning_desc(s.get("critical_warning")):
                self.log(f"CRITICAL WARNING: {w}", "ERR")
        else:
            self.log("SMART log cannot be read!", "ERR")
        for k, v in (self.S.get("vendor_health_pre") or {}).items():
            self.log(f"  {k}: {v}")
        for a in self.S["identity_anomalies"]:
            self.log(f"identify anomaly: {a}", "WARN")

    def write_cache_off(self):
        idn = self.S["identity"]
        if not idn.get("vwc", 0) & 1:
            self.S["vwc"] = "no volatile write cache"
            return
        r = nvme(["set-feature", f"/dev/{self.ctrl}", "-f", "6", "-V", "0", "--no-retries"])
        g = nvme(["get-feature", f"/dev/{self.ctrl}", "-f", "6", "-s", "0", "--no-retries"])
        m = re.search(r"Current value:\s*(?:0x)?([0-9a-fA-F]+)", g["stdout"])
        cur = int(m.group(1), 16) if m else None
        self.S["vwc"] = "disabled" if cur == 0 else f"FAILED to disable (rc={r['rc']}, value={cur})"
        self.log(f"Drive write cache (VWC): {self.S['vwc']}", "INFO" if cur == 0 else "WARN")

    # -- self-test --
    def selftest(self, code, key):
        idn = self.S["identity"]
        name = {1: "short", 2: "extended"}[code]
        if not idn["oacs"] & 0x10:
            self.log("the drive does not support self-test")
            self.S[key] = {"supported": False}
            self.step_done(key, self.S[key])
            return
        if self.a.no_selftest:
            self.S[key] = {"skipped": True}
            self.step_done(key, self.S[key])
            return
        self.require_alive()
        r = nvme(["device-self-test", f"/dev/{self.ctrl}", "-n", "0xffffffff", "-s", str(code), "--no-retries"])
        if r["rc"] != 0:
            self.log(f"{name} self-test did not start: {r['stderr'].strip() or r['stdout'].strip()}", "WARN")
            self.S[key] = {"supported": True, "started": False, "error": r["stderr"].strip()}
            self.step_done(key, self.S[key])
            return
        limit = 15 * 60 if code == 1 else max(idn["edstt_min"] * 3 * 60, 3600) + 1800
        t0, last, info = time.time(), 0, None
        time.sleep(2)
        while True:
            if not self.alive():
                raise Aborted(f"the controller was lost during the {name} self-test")
            st = parse_selftest_log(get_log_raw(self.ctrl, 0x06, 564))
            if st and st["current_op"] == 0:
                info = st["entries"][0] if st["entries"] else None
                break
            if time.time() - t0 > limit:
                nvme(["device-self-test", f"/dev/{self.ctrl}", "-s", "0xf", "--no-retries"])
                self.log(f"{name} self-test exceeded the time limit ({fmt_dur(limit)}), stopped", "ERR")
                info = {"result": None, "result_desc": "timed out, stopped"}
                break
            if time.time() - last > 30 and st:
                last = time.time()
                self.log(f"{name} self-test: {st['progress_pct']}%  ({fmt_dur(time.time() - t0)})")
                self.status(progress={"pct": st["progress_pct"], "elapsed_s": int(time.time() - t0)})
            time.sleep(5)
        self.S[key] = {"supported": True, "started": True, "duration_s": round(time.time() - t0), "result": info}
        if info:
            ok = info.get("result") == 0
            extra = ""
            if info.get("segment"):
                extra += f" segment: {info['segment']}"
            if info.get("failing_lba") is not None:
                extra += f" failing LBA: {info['failing_lba']}"
            self.log(f"{name} self-test result: {info.get('result_desc')}{extra}", "OK" if ok else "ERR")
        self.step_done(key, self.S[key])

    # -- erase --
    def erase(self, idn):
        C = f"/dev/{self.ctrl}"
        out = {"ok": False}
        t0 = time.time()
        attempts = []
        if idn["sanicap"] & 0x2:
            attempts.append("sanitize-block")
        if idn["oacs"] & 0x2:
            attempts.append("format-ses1")
            attempts.append("format-ses0")
        if idn["sanicap"] & 0x1:
            attempts.append("sanitize-crypto")
        if not attempts:
            self.log("the drive supports neither sanitize nor format: erase skipped", "WARN")
            out["method"] = "unsupported"
            return out
        for m in attempts:
            self.require_alive()
            self.log(f"erase: {m} ...")
            if m.startswith("sanitize"):
                act = "2" if m == "sanitize-block" else "4"
                r = nvme(["sanitize", C, "-a", act, "--no-retries"], out=self.dir / "erase" / f"{m}.txt")
                if r["rc"] != 0:
                    attempts_msg = r["stderr"].strip() or r["stdout"].strip()
                    self.log(f"{m} did not start: {attempts_msg}", "WARN")
                    out.setdefault("failed", []).append({"method": m, "error": attempts_msg})
                    continue
                res = self.wait_sanitize()
                if res == "ok":
                    out.update(ok=True, method=m)
                    break
                out.setdefault("failed", []).append({"method": m, "error": res})
                self.log(f"{m} failed: {res}", "ERR")
            else:
                ses = m[-1]
                r = nvme(["format", f"/dev/{self.ns}", "-l", str(idn["flbas_lbaf"]), "-s", ses, "--force",
                          "-t", "3600000", "--no-retries"], timeout=3700, out=self.dir / "erase" / f"{m}.txt")
                if r["rc"] == 0:
                    out.update(ok=True, method=m)
                    break
                msg = (r["stderr"].strip() or r["stdout"].strip())[-300:]
                out.setdefault("failed", []).append({"method": m, "error": msg})
                self.log(f"{m} failed: {msg}", "WARN")
        out["duration_s"] = round(time.time() - t0)
        nvme(["ns-rescan", C])
        time.sleep(3)
        # capacity check after erase
        idn2 = identity({"id_ns": load_json(nvme(["id-ns", f"/dev/{self.ns}", "-o", "json"])["stdout"]) or {}})
        out["nsze_after"] = idn2["nsze"]
        if idn2["nsze"] != idn["nsze"]:
            self.log(f"the namespace size changed after erase: {idn['nsze']} -> {idn2['nsze']}", "ERR")
        self.log(f"erase {'done' if out['ok'] else 'FAILED'}: {out.get('method')} ({fmt_dur(out['duration_s'])})",
                 "OK" if out["ok"] else "ERR")
        return out

    def wait_sanitize(self):
        t0, last = time.time(), 0
        limit = 4 * 3600
        time.sleep(2)
        while time.time() - t0 < limit:
            if not os.path.exists(f"/sys/class/nvme/{self.ctrl}"):
                return "controller disappeared"
            data = get_log_raw(self.ctrl, 0x81, 512)
            if data and len(data) >= 16:
                sprog, sstat = struct.unpack_from("<HH", data, 0)
                s = sstat & 0x7
                if s in (1, 4):
                    return "ok"
                if s == 3:
                    return "FAILED according to the sanitize log"
                if time.time() - last > 30:
                    last = time.time()
                    self.log(f"sanitize in progress: {sprog * 100 / 65536:.1f}% ({fmt_dur(time.time() - t0)})")
                    self.status(progress={"pct": round(sprog * 100 / 65536, 1)})
            time.sleep(5)
        return "timed out"

    # -- nvblk --
    def nvblk(self, mode, sub, label, seed):
        self.require_alive()
        out = self.dir / sub
        out.mkdir(parents=True, exist_ok=True)
        cmd = [NVBLK, "--mode", mode, "--dev", f"/dev/{self.ns}", "--out", out, "--label", label,
               "--seed", str(seed), "--ctrl-state", f"/sys/class/nvme/{self.ctrl}/state",
               "--timeout-ms", str(self.a.io_timeout_ms), "--slow-factor", str(self.a.slow_factor),
               "--slow-min-ms", str(self.a.slow_min_ms)]
        if self.a.limit_lbas:
            cmd += ["--count", str(self.a.limit_lbas)]
        if self.a.posix:
            cmd += ["--posix"]
        cmd = [str(c) for c in cmd]
        with open(out / f"{label}_stderr.log", "w") as ef:
            proc = subprocess.Popen(cmd, stdout=ef, stderr=ef, start_new_session=True)
            last = 0
            pfile = out / f"{label}_progress.json"
            try:
                while proc.poll() is None:
                    time.sleep(1)
                    if time.time() - last >= self.a.report_every:
                        last = time.time()
                        pr = load_json(rd(pfile)) if pfile.exists() else None
                        if pr:
                            self.progress_line(label, pr)
                            self.status(progress=pr)
            except KeyboardInterrupt:
                proc.send_signal(signal.SIGINT)
                proc.wait()
                raise
        summ = load_json(rd(out / f"{label}_summary.json"))
        if not summ:
            raise Aborted(f"nvblk {mode} produced no result (rc={proc.returncode}), see {out}/{label}_stderr.log")
        summ["rc"] = proc.returncode
        c, l = summ["counts"], summ["latency_us"]
        bad = c["io_err_lbas"] + c["unbisected_err_lbas"]
        lvl = "OK" if summ["result"] == "clean" else "ERR"
        msg = (f"{mode} done: {summ['result']}  {summ['mb_s']:.0f} MB/s  {fmt_dur(summ['elapsed_s'])}  "
               f"bad blocks: {bad}  mismatched content: {c['mismatch_lbas']}  controller errors: {c['ctrl_err_events']}  "
               f"slow commands: {l['slow_cmds']}  latency p50/p99/max: "
               f"{l['p50'] / 1000:.1f}/{l['p99'] / 1000:.1f}/{l['max'] / 1000:.1f} ms")
        if mode in ("readscan", "erasecheck") and summ["result"] == "errors" and not bad and not c["ctrl_err_events"]:
            lvl = "WARN"
        self.log(msg, lvl)
        for st in summ.get("statuses", []):
            self.log(f"   status {st['desc']}: {st['events']} events, {st['lbas']} blocks", "WARN")
        if c["mismatch_lbas"]:
            self.log(f"   bit errors: {c['corrupt_lbas']} ({c['bitflips']} bits)  misdirected: {c['misdirected_lbas']}  "
                     f"stale content: {c['stale_lbas']}  zeroed: {c['zeroed_lbas']}  all FF: {c['all_ff_lbas']}",
                     "ERR")
        if summ["device_lost"] or summ["result"] == "device_lost":
            self.S["device_lost"] = True
        if summ["device_lost"] or summ["result"] in ("device_lost", "interrupted"):
            raise Aborted(f"nvblk {mode}: {summ['abort_reason'] or summ['result']}")
        return summ

    def progress_line(self, label, pr):
        tot = pr["total_lbas"] * pr["lba_size"]
        done = pr["done_lbas"] * pr["lba_size"]
        bad = pr["io_err_lbas"] + pr["unbisected_lbas"]
        self.log(
            f"{label}: {pr['pct']:5.1f}%  ({gb(done):.1f}/{gb(tot):.1f} GB)  {pr['mb_s']:.0f} MB/s  "
            f"eta: {fmt_dur(pr['eta_s'])}  | bad blocks: {bad}  mismatch: {pr['mismatch_lbas']}  "
            f"ctrl errors: {pr['ctrl_err_events']}  slow: {pr['slow_cmds']}  (base: {pr['last_lat_us'] / 1000:.2f} ms)"
        )

    def kernel_log(self):
        since = self.S.get("started")
        r = run(["journalctl", "-k", "--since", since.replace("T", " "), "--no-pager", "-o", "short-iso-precise"],
                timeout=60)
        text = r.get("stdout", "")
        if r["rc"] != 0 or not text.strip():
            r = run(["dmesg", "-T"], timeout=30)
            text = r.get("stdout", "")
        (self.dir / "kernel.log").write_text(text)
        pat = re.compile(rf"\b{self.ctrl}\b|{self.ctrl}c\d+n|{self.S.get('namespace') or '@@'}\b|"
                         rf"{(self.S.get('pcie_pre') or {}).get('bdf') or '@@'}")
        rel = [ln for ln in text.splitlines() if pat.search(ln)]
        (self.dir / "kernel_drive.log").write_text("\n".join(rel) + "\n")
        self.S["kernel"] = {
            "lines": len(rel),
            "resets": sum(1 for ln in rel if re.search(r"reset", ln, re.I)),
            "timeouts": sum(1 for ln in rel if re.search(r"timeout", ln, re.I)),
            "io_errors": sum(1 for ln in rel if re.search(r"I/O Error|I/O error", ln)),
            "aer": sum(1 for ln in rel if re.search(r"AER|PCIe Bus Error", ln)),
            "removed": sum(1 for ln in rel if re.search(r"remov|disabl|dead", ln, re.I)),
        }


def identity_anomalies(idn, ci):
    out = []
    if not printable(idn["serial"]):
        out.append(f"the serial number is not printable/empty: {idn['serial']!r}")
    if not printable(idn["model"]):
        out.append(f"the model name is not printable/empty: {idn['model']!r}")
    if not printable(idn["firmware"]):
        out.append(f"the firmware version is not printable/empty: {idn['firmware']!r}")
    if not idn["nsze"]:
        out.append("the namespace size is 0 or not readable")
    if idn["ncap"] is not None and idn["nsze"] and idn["ncap"] != idn["nsze"]:
        out.append(f"NCAP ({idn['ncap']}) != NSZE ({idn['nsze']})")
    if not idn["vid"] and ci.get("transport") == "pcie":
        out.append("the PCI vendor ID is 0 in the identify data")
    if ci.get("serial") and ci["serial"] != idn["serial"]:
        out.append(f"sysfs S/N ({ci['serial']}) != identify S/N ({idn['serial']})")
    return out


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


def pass_errors(p):
    """Errors of one pass: bad blocks on write, bad blocks on read, mismatched content, controller errors."""
    r = {"w": 0, "r": 0, "mis": 0, "ctrl": 0}
    for k in ("write", "verify", "readscan", "check"):
        s = (p or {}).get(k)
        if not s:
            continue
        c = s["counts"]
        r["w" if k == "write" else "r"] += c["io_err_lbas"] + c["unbisected_err_lbas"]
        r["mis"] += c["mismatch_lbas"]
        r["ctrl"] += c["ctrl_err_events"]
    r["any"] = r["w"] + r["r"] + r["mis"]
    return r


def slow_stats(S):
    slow, mx = 0, 0
    for p in ("pass0", "pass1", "pass2", "erase"):
        for k in ("write", "verify", "readscan", "check"):
            s = (S.get(p) or {}).get(k)
            if s:
                slow += s["latency_us"]["slow_cmds"]
                mx = max(mx, s["latency_us"]["max"])
    return slow, mx


def selftests(S):
    out = []
    for k, name in (("selftest_short_pre", "short (start)"), ("selftest_extended", "extended"),
                    ("selftest_short_post", "short (end)")):
        v = S.get(k)
        if v and v.get("result"):
            r = dict(v["result"])
            # older summary files stored the description in Hungarian: re-derive it from the code
            if r.get("result") in SELFTEST_RESULTS:
                r["result_desc"] = SELFTEST_RESULTS[r["result"]]
            out.append((name, r))
    return out


def evaluate(S):
    phys, logical, susp, notes = [], [], [], []
    if S.get("status") != "completed" and not S.get("identity"):
        S["verdict"], S["reasons"] = "DEAD", [f"even the identification failed: {S.get('abort_reason')}"]
        return
    if S.get("identity"):
        S["identity_anomalies"] = identity_anomalies(S["identity"], S.get("ctrl_sysfs") or {})
    pre, post = S.get("smart_pre") or {}, S.get("smart_post") or S.get("smart_mid") or {}
    e0, e1, e2 = (pass_errors(S.get(k)) for k in ("pass0", "pass1", "pass2"))
    er = S.get("erase") or {}
    ec = er.get("check")
    erased = bool(er.get("ok"))

    if e2["any"]:
        phys.append(f"errors in pass 2 as well{' (after erase)' if erased else ''}: write errors on {e2['w']} blocks, "
                    f"read errors on {e2['r']} blocks, mismatched content on {e2['mis']} blocks")
    for name, r in selftests(S):
        if r.get("result") in (5, 6, 7):
            phys.append(f"{name} self-test: {r['result_desc']}"
                        + (f" (LBA {r['failing_lba']})" if r.get("failing_lba") is not None else ""))
    cw = post.get("critical_warning") or pre.get("critical_warning")
    for w in critical_warning_desc(cw):
        (phys if "temperature" not in w else susp).append(f"SMART critical warning: {w}")
    if pre.get("media_errors") is not None and post.get("media_errors") is not None:
        d = post["media_errors"] - pre["media_errors"]
        if d > 0:
            phys.append(f"the SMART media error counter grew by {d} during the test ({pre['media_errors']} -> "
                        f"{post['media_errors']})")
    for src in (post, pre):
        if src.get("avail_spare") is not None and src.get("spare_thresh") and src["avail_spare"] < src["spare_thresh"]:
            phys.append(f"the available spare ({src['avail_spare']}%) is below the threshold ({src['spare_thresh']}%)")
            break
    if pre.get("avail_spare") is not None and post.get("avail_spare") is not None \
            and post["avail_spare"] < pre["avail_spare"]:
        phys.append(f"the available spare dropped during the test: {pre['avail_spare']}% -> {post['avail_spare']}%")

    if e1["any"] and not e2["any"] and S.get("pass2"):
        logical.append(f"the errors of pass 1 (write {e1['w']}, read {e1['r']}, mismatch {e1['mis']} blocks) "
                       f"disappeared during{' the erase and' if erased else ''} pass 2")
    if ec and ec["counts"]["stale_lbas"]:
        logical.append(f"after the erase the previous content remained in {ec['counts']['stale_lbas']} blocks "
                       "(the erase/FTL does not work correctly)")
    if er.get("method") == "unsupported":
        notes.append("the drive does not support erase (sanitize/format), pass 2 ran without erase")
    elif er.get("method") == "skipped":
        notes.append("the erase was skipped (--no-erase)")
    elif er and not erased:
        susp.append(f"the erase failed: {er.get('failed')}")
    if e0["r"]:
        (logical if not phys else notes).append(
            f"the original content had {e0['r']} unreadable blocks (state after irradiation)")
    for a in S.get("identity_anomalies") or []:
        logical.append(f"identify anomaly: {a}")
    ip = S.get("identity_post")
    if ip and S.get("identity") and ip.get("nsze") != S["identity"].get("nsze"):
        logical.append("the namespace size changed during the test")

    if pre.get("media_errors"):
        notes.append(f"{pre['media_errors']} media errors were already recorded before the test")
    elpre = (S.get("error_log_pre") or {}).get("entries_read")
    if pre.get("num_err_log_entries"):
        notes.append(f"there were {pre['num_err_log_entries']} error log entries before the test ({elpre} readable)")
    if pre.get("unsafe_shutdowns"):
        notes.append(f"number of unsafe shutdowns: {pre['unsafe_shutdowns']}")
    nctrl = e0["ctrl"] + e1["ctrl"] + e2["ctrl"] + (ec["counts"]["ctrl_err_events"] if ec else 0)
    if nctrl:
        susp.append(f"controller-level errors (timeout/reset) during the test: {nctrl}")
    slow, mx = slow_stats(S)
    if slow:
        susp.append(f"{slow} outlier-slow commands (max {mx / 1000:.0f} ms): the drive was busy in the background")
    tel = S.get("telemetry") or {}
    if tel.get("hang"):
        susp.append(f"the controller hung on the standard telemetry fetch: {tel['hang']}")
    for m in tel.get("slow", []):
        notes.append(m)
    for pr in S.get("previous_runs") or []:
        if pr.get("status") != "completed":
            susp.append(f"an earlier run ({pr.get('started')}) did not finish: {pr.get('abort_reason')}"
                        + (f"; {pr['telemetry_hang']}" if pr.get("telemetry_hang") else ""))
    k = S.get("kernel") or {}
    if k.get("resets"):
        susp.append(f"the kernel logged {k['resets']} reset messages")
    pc0, pc1 = S.get("pcie_pre") or {}, S.get("pcie_post") or {}
    for key in ("aer_dev_correctable", "aer_dev_nonfatal", "aer_dev_fatal"):
        if pc0.get(key) is not None and pc1.get(key) is not None and pc1[key] > pc0[key]:
            susp.append(f"PCIe {key}: {pc0[key]} -> {pc1[key]}")
    if S.get("vwc", "").startswith("FAILED"):
        notes.append("the write cache of the drive could not be disabled")

    if S.get("status") != "completed":
        ar = str(S.get("abort_reason"))
        # the last two markers are the Hungarian words for "lost" / "disappeared": they keep older
        # (untranslated) summary files and nvblk messages classified exactly as before
        base = "DEAD" if S.get("device_lost") or any(
            w in ar for w in ("lost", "disappeared", "elveszett", "elt\u0171nt")) else "INCOMPLETE"
        if S.get("status") == "interrupted":
            base = "INCOMPLETE"
        reasons = [f"the test did not finish: {S.get('abort_reason') or S.get('status')}"]
        S["verdict"], S["reasons"] = base, reasons + phys + logical + susp + notes
        S["reason_groups"] = {"abort": reasons, "physical": phys, "logical": logical, "suspect": susp,
                              "note": notes}
        return
    if phys:
        v = "PHYSICAL"
    elif logical:
        v = "LOGICAL"
    elif susp:
        v = "SUSPECT"
    else:
        v = "OK"
    S["verdict"] = v
    S["reasons"] = phys + logical + susp + notes or ["every test completed without error"]
    S["reason_groups"] = {"physical": phys, "logical": logical, "suspect": susp, "note": notes}


# ---------------------------------------------------------------------------
# text summary
# ---------------------------------------------------------------------------


def pass_line(name, s):
    if not s:
        return f"  {name:<34} -"
    c, l = s["counts"], s["latency_us"]
    return (f"  {name:<34} {s['result']:<11} {s['mb_s']:>6.0f} MB/s {fmt_dur(s['elapsed_s']):>9}  "
            f"bad:{c['io_err_lbas'] + c['unbisected_err_lbas']:>7}  mismatch:{c['mismatch_lbas']:>7}  "
            f"ctrlerr:{c['ctrl_err_events']:>4}  slow:{l['slow_cmds']:>5}  "
            f"p50/p99/max: {l['p50'] / 1000:.2f}/{l['p99'] / 1000:.2f}/{l['max'] / 1000:.1f} ms")


def drive_text(S):
    i = S.get("identity") or {}
    pre, post = S.get("smart_pre") or {}, S.get("smart_post") or {}
    L = []
    L.append(f"NVMe post-irradiation examination - {i.get('model')}  S/N {i.get('serial')}")
    L.append("=" * 78)
    L.append(f"Verdict: {VERDICTS.get(S.get('verdict'), S.get('verdict'))}")
    for r in S.get("reasons", []):
        L.append(f"  - {r}")
    L.append("")
    L.append(f"Test: {S.get('started')} - {S.get('finished')}  ({fmt_dur(S.get('duration_s'))}), "
             f"status: {S.get('status')}")
    L.append("")
    L.append("Identification")
    cap = i.get("capacity_bytes")
    for k, v in [("Vendor (PCI VID)", f"0x{i['vid']:04x}" if i.get("vid") is not None else "?"),
                 ("Subsystem VID", f"0x{i['ssvid']:04x}" if i.get("ssvid") is not None else "?"),
                 ("IEEE OUI", f"0x{i['ieee_oui']:06x}" if i.get("ieee_oui") is not None else "?"),
                 ("Model", i.get("model")), ("Serial number", i.get("serial")), ("Firmware", i.get("firmware")),
                 ("Size", f"{gb(cap):.2f} GB ({i.get('nsze')} x {i.get('lba_size')} B)" if cap else "?"),
                 ("EUI64", i.get("eui64")), ("NGUID", i.get("nguid")), ("FGUID", i.get("fguid")),
                 ("SubNQN", i.get("subnqn")),
                 ("PCIe", " ".join(str((S.get("pcie_pre") or {}).get(x, "")) for x in
                                   ("bdf", "current_link_speed", "current_link_width")))]:
        L.append(f"  {k:<22} {v}")
    L.append("")
    L.append('SMART / lifetime                      before test      after test')
    labels = [("power_on_hours", "Power-on hours"), ("power_cycles", "Power cycles"),
              ("unsafe_shutdowns", "Unsafe shutdowns"), ("data_units_written", "Data written (GB)"),
              ("data_units_read", "Data read (GB)"), ("host_write_commands", "Write commands"),
              ("host_read_commands", "Read commands"), ("percent_used", "Percentage used (%)"),
              ("avail_spare", "Available spare (%)"), ("spare_thresh", "Available spare threshold (%)"),
              ("media_errors", "Media errors"), ("num_err_log_entries", "Error log entries"),
              ("critical_warning", "Critical warning"), ("temperature_c", "Temperature (C)"),
              ("controller_busy_time", "Controller busy (min)")]
    for k, name in labels:
        a, b = pre.get(k), post.get(k)
        if k in ("data_units_written", "data_units_read"):
            a = f"{gb(a * 512000):.1f}" if a is not None else None
            b = f"{gb(b * 512000):.1f}" if b is not None else None
        L.append(f"  {name:<32} {'-' if a is None else str(a):>14}  {'-' if b is None else str(b):>14}")
    for w in critical_warning_desc(pre.get("critical_warning")):
        L.append(f"  ! {w}")
    L.append("")
    L.append("Error log")
    for key, name in (("error_log_pre", "before test"), ("error_log_post", "after test")):
        e = S.get(key) or {}
        L.append(f"  {name}: {e.get('entries_read')} readable entries; statuses: {e.get('by_status')}")
        if e.get("lbas"):
            L.append(f"    affected LBAs: {', '.join(map(str, e['lbas'][:40]))}{' ...' if len(e['lbas']) > 40 else ''}")
    vh = S.get("vendor_health_post") or S.get("vendor_health_pre") or {}
    if vh:
        L.append("")
        L.append("Vendor health data (bad blocks, available spare, ECC ...)")
        pre_vh = S.get("vendor_health_pre") or {}
        for k, v in vh.items():
            before = pre_vh.get(k)
            L.append(f"  {k:<60} {v}" + (f"   (before: {before})" if before is not None and before != v else ""))
    L.append("")
    L.append("Self-tests")
    for name, r in selftests(S) or [("-", {"result_desc": "did not run / not supported"})]:
        extra = ""
        if r.get("segment"):
            extra += f" segment {r['segment']}"
        if r.get("failing_lba") is not None:
            extra += f" LBA {r['failing_lba']}"
        L.append(f"  {name:<18} {r.get('result_desc')}{extra}")
    L.append("")
    L.append(f"Write/read tests (drive write cache: {S.get('vwc', '?')})")
    L.append(pass_line("pass 0: read original content", (S.get("pass0") or {}).get("readscan")))
    L.append(pass_line("pass 1: write", (S.get("pass1") or {}).get("write")))
    L.append(pass_line("pass 1: read back", (S.get("pass1") or {}).get("verify")))
    er = S.get("erase") or {}
    L.append(f"  Erase: {er.get('method')}  {'successful' if er.get('ok') else 'FAILED/skipped'}"
             f"  {fmt_dur(er.get('duration_s'))}" + (f"  errors: {er.get('failed')}" if er.get("failed") else ""))
    L.append(pass_line("   erase-check read", er.get("check")))
    if er.get("check"):
        c = er["check"]["counts"]
        L.append(f"   erased/empty blocks: {c['erased_ok_lbas']}  (of these 'unwritten' status: {c['unwritten_lbas']})"
                 f"  stale content left: {c['stale_lbas']}")
    L.append(pass_line("pass 2: write", (S.get("pass2") or {}).get("write")))
    L.append(pass_line("pass 2: read back", (S.get("pass2") or {}).get("verify")))
    for p in ("pass0", "pass1", "erase", "pass2"):
        for k in ("readscan", "write", "verify", "check"):
            s = (S.get(p) or {}).get(k)
            if s and s.get("statuses"):
                L.append(f"    {p}/{k} NVMe statuses: " +
                         "; ".join(f"{x['desc']} x{x['events']} ({x['lbas']} blocks)" for x in s["statuses"]))
            if s and s["counts"]["mismatch_lbas"]:
                c = s["counts"]
                L.append(f"    {p}/{k} mismatches: bit errors {c['corrupt_lbas']} ({c['bitflips']} bits), "
                         f"misdirected {c['misdirected_lbas']}, stale content {c['stale_lbas']}, "
                         f"zeroed {c['zeroed_lbas']}, all FF {c['all_ff_lbas']}")
    k = S.get("kernel") or {}
    L.append("")
    L.append(f"Kernel log: {k.get('lines')} lines about the drive; resets: {k.get('resets')}, "
             f"timeouts: {k.get('timeouts')}, I/O errors: {k.get('io_errors')}, AER: {k.get('aer')}")
    L.append("")
    L.append("Files: pass*/<step>_errors.csv (bad blocks), *_slow.csv (slow commands), "
             "*_latency.csv (every command), logs/ (raw logs), kernel_drive.log")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# summary report
# ---------------------------------------------------------------------------

REPORT_COLS = [
    ("serial", "S/N"), ("model", "Model"), ("fw", "FW"), ("cap_gb", "GB"), ("verdict", "Verdict"),
    ("poh", "Power-on h"), ("pwr_cycles", "Pwr cycles"), ("unsafe", "Unsafe sd"), ("written_gb", "Written GB"),
    ("read_gb", "Read GB"), ("used_pct", "Used%"), ("spare", "Spare%"), ("spare_thr", "Thresh%"),
    ("media_err", "Media errors (before>after)"), ("errlog", "Error log (before>after)"), ("crit", "Crit.warn"),
    ("selftest", "Self-test"), ("p0_bad", "P0 read err"), ("p1_w", "P1 write err"), ("p1_r", "P1 read err"), ("p1_mis", "P1 mismatch"),
    ("erase", "Erase"), ("stale", "Stale after erase"), ("p2_w", "P2 write err"), ("p2_r", "P2 read err"), ("p2_mis", "P2 mismatch"),
    ("ctrl_err", "Ctrl err"), ("slow", "Slow"), ("max_ms", "Max lat ms"), ("resets", "Kernel resets"),
    ("date", "Date"), ("dir", "Directory"),
]


def report_row(S, d):
    i = S.get("identity") or {}
    pre, post = S.get("smart_pre") or {}, S.get("smart_post") or S.get("smart_mid") or {}
    e0, e1, e2 = (pass_errors(S.get(k)) for k in ("pass0", "pass1", "pass2"))
    er = S.get("erase") or {}
    slow, mx = slow_stats(S)
    st = selftests(S)
    stxt = "; ".join(f"{n}: {r.get('result_desc')}" for n, r in st) if st else "-"

    def pa(a, b):
        return f"{'-' if a is None else a}>{'-' if b is None else b}"

    if not er:
        erase = "-"
    elif er.get("ok"):
        erase = er.get("method")
    else:
        erase = {"unsupported": "not supported", "skipped": "skipped"}.get(er.get("method"), "FAILED")

    return {
        "serial": i.get("serial") or S.get("ctrl_sysfs", {}).get("serial") or S.get("bdf"),
        "model": i.get("model") or S.get("ctrl_sysfs", {}).get("model", ""),
        "fw": i.get("firmware", ""),
        "cap_gb": f"{gb(i['capacity_bytes']):.1f}" if i.get("capacity_bytes") else "",
        "verdict": S.get("verdict"),
        "poh": pre.get("power_on_hours"), "pwr_cycles": pre.get("power_cycles"),
        "unsafe": pre.get("unsafe_shutdowns"),
        "written_gb": f"{gb((pre.get('data_units_written') or 0) * 512000):.1f}",
        "read_gb": f"{gb((pre.get('data_units_read') or 0) * 512000):.1f}",
        "used_pct": pre.get("percent_used"), "spare": pa(pre.get("avail_spare"), post.get("avail_spare")),
        "spare_thr": pre.get("spare_thresh"),
        "media_err": pa(pre.get("media_errors"), post.get("media_errors")),
        "errlog": pa(pre.get("num_err_log_entries"), post.get("num_err_log_entries")),
        "crit": pre.get("critical_warning"), "selftest": stxt,
        "p0_bad": e0["r"] if S.get("pass0") else "-",
        "p1_w": e1["w"] if S.get("pass1") else "-", "p1_r": e1["r"] if S.get("pass1") else "-",
        "p1_mis": e1["mis"] if S.get("pass1") else "-",
        "erase": erase,
        "stale": er["check"]["counts"]["stale_lbas"] if er.get("check") else "-",
        "p2_w": e2["w"] if S.get("pass2") else "-", "p2_r": e2["r"] if S.get("pass2") else "-",
        "p2_mis": e2["mis"] if S.get("pass2") else "-",
        "ctrl_err": e0["ctrl"] + e1["ctrl"] + e2["ctrl"], "slow": slow, "max_ms": f"{mx / 1000:.0f}",
        "resets": (S.get("kernel") or {}).get("resets"),
        "date": (S.get("started") or "")[:16], "dir": str(d),
    }


def cmd_report(args):
    results = Path(args.results)
    runs = {}
    for f in sorted(results.glob("*/*/summary.json")):
        S = load_json(f.read_text())
        if not S:
            continue
        evaluate(S)  # always from the raw data, with the current rules
        key = f.parent.parent.name
        prev = runs.get(key)
        # by default the latest completed run counts; if there is none, the latest one
        if args.all_runs:
            runs[str(f.parent)] = (S, f.parent)
        elif not prev or S.get("status") == "completed" or prev[0].get("status") != "completed":
            runs[key] = (S, f.parent)
    if not runs:
        print("no results yet under", results)
        return 1
    rows = [report_row(S, d.relative_to(results)) for S, d in runs.values()]
    order = {"DEAD": 0, "PHYSICAL": 1, "LOGICAL": 2, "SUSPECT": 3, "INCOMPLETE": 4, "OK": 5}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), str(r["serial"])))
    keys = [k for k, _ in REPORT_COLS]
    heads = [h for _, h in REPORT_COLS]
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    with open(results / "REPORT.csv", "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(heads)
        for r in rows:
            w.writerow([r[k] if r[k] is not None else "" for k in keys])

    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    md = [f"# NVMe post-irradiation examination - summary", "", f"Generated: {stamp}, drives: {len(rows)}", ""]
    md += ["| Verdict | count |", "|---|---|"]
    md += [f"| {VERDICTS.get(k, k)} | {v} |" for k, v in sorted(counts.items(), key=lambda x: order.get(x[0], 9))]
    md += ["", "## Table", "", "| " + " | ".join(heads[:-1]) + " |", "|" + "---|" * (len(heads) - 1)]
    for r in rows:
        md.append("| " + " | ".join(str(r[k] if r[k] is not None else "").replace("|", "/") for k in keys[:-1]) + " |")
    md += ["", "## Per drive", ""]
    txt = [f"NVMe POST-IRRADIATION EXAMINATION - SUMMARY ({stamp})", "=" * 78, ""]
    for k, v in sorted(counts.items(), key=lambda x: order.get(x[0], 9)):
        txt.append(f"  {VERDICTS.get(k, k):<60} {v:>3} pcs")
    txt += ["", "Short table:", ""]
    short = ["serial", "model", "cap_gb", "poh", "media_err", "p0_bad", "p1_w", "p1_r", "p1_mis", "p2_w", "p2_r",
             "p2_mis", "slow", "verdict"]
    shead = {k: h for k, h in REPORT_COLS}
    widths = {k: max(len(shead[k]), *(len(str(r[k])) for r in rows)) for k in short}
    txt.append("  ".join(shead[k].ljust(widths[k]) for k in short))
    txt.append("  ".join("-" * widths[k] for k in short))
    for r in rows:
        txt.append("  ".join(str(r[k]).ljust(widths[k]) for k in short))
    txt += ["", "(P0 = read of the original content, P1 = write/read pass 1, P2 = pass after the erase;",
            " error counts are in blocks (LBA))", ""]
    for S, d in sorted(runs.values(), key=lambda x: order.get(x[0].get("verdict"), 9)):
        i = S.get("identity") or {}
        md.append(f"### {i.get('model') or S.get('bdf')} - {i.get('serial') or '?'}")
        md.append(f"**{VERDICTS.get(S.get('verdict'), S.get('verdict'))}**  ")
        md += [f"- {r}" for r in S.get("reasons", [])]
        md.append(f"- details: `{d.relative_to(results)}/summary.txt`")
        md.append("")
        txt.append("#" * 78)
        txt.append(drive_text(S))
    (results / "REPORT.md").write_text("\n".join(md) + "\n")
    (results / "REPORT.txt").write_text("\n".join(txt) + "\n")
    print("\n".join(txt[: 8 + len(counts) + len(rows)]))
    print(f"\nReport: {results}/REPORT.txt, REPORT.md, REPORT.csv")
    return 0


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------


def previous_runs(results, serial, exclude=None):
    out = []
    for f in sorted((results / safe_name(serial)).glob("*/summary.json")):
        if exclude and f.parent == exclude:
            continue
        S = load_json(f.read_text()) or {}
        out.append({"dir": f.parent.name, "started": S.get("started"), "status": S.get("status"),
                    "verdict": S.get("verdict"), "abort_reason": S.get("abort_reason"),
                    "telemetry_hang": (S.get("telemetry") or {}).get("hang")})
    return out


def already_tested(results, serial):
    for f in sorted((results / safe_name(serial)).glob("*/summary.json"), reverse=True):
        S = load_json(f.read_text()) or {}
        if S.get("status") == "completed":
            return f.parent
    return None


def confirm(log, ci, args):
    if args.yes:
        return True
    print(f"\n  Drive to test: {ci['ctrl']}  {ci['model']}  S/N {ci['serial']}  FW {ci['firmware']}  "
          f"({ci['address']})\n  The test ERASES THE ENTIRE CONTENT of the drive and overwrites it several times.")
    try:
        ans = input("  Proceed? [y/N] ").strip().lower()
    except EOFError:
        ans = ""
    if ans not in ("i", "igen", "y", "yes"):
        log("the operator did not approve it, skipped", "WARN")
        return False
    return True


def preflight(args, log):
    if os.geteuid() != 0:
        sys.exit("root privileges are required")
    if not NVBLK.exists():
        sys.exit(f"{NVBLK} not found - run: make -C {ROOT}")
    if not shutil.which(NVME):
        sys.exit("nvme-cli not found")


def test_one(args, log, ci, consent=False, retest=False):
    results = Path(args.results)
    if ci["state"] != "live":
        log(f"{ci['ctrl']} state: {ci['state']} - cannot be tested", "ERR")
        return None
    prev = already_tested(results, ci["serial"])
    if prev and not (args.retest or retest):
        log(f"{ci['ctrl']} S/N {ci['serial']} already tested ({prev}); skipping (retest with --retest)", "WARN")
        return None
    for ns in namespaces(ci["ctrl"]):
        why = in_use(ns)
        if why:
            log(f"{ci['ctrl']}: {why} - NOT testing it!", "ERR")
            return None
    if not consent and not confirm(log, ci, args):
        return None
    t = DriveTest(args, log, ci)
    try:
        return t.run()
    finally:
        log.set_drive(None, "")


def dead_device_record(args, log, bdf, info):
    results = Path(args.results)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    d = results / f"NOT_ENUMERATED_{safe_name(bdf)}" / stamp
    d.mkdir(parents=True, exist_ok=True)
    p = f"/sys/bus/pci/devices/{bdf}"
    run(["lspci", "-vvv", "-s", bdf], out=d / "lspci.txt")
    run(["dmesg", "-T"], out=d / "dmesg.txt")
    S = {"tool": "radtest", "status": "not_enumerated", "bdf": bdf, "pci": info,
         "pci_ids": {k: rd(f"{p}/{k}") for k in ("vendor", "device", "subsystem_vendor", "subsystem_device")},
         "started": dt.datetime.now().isoformat(timespec="seconds"),
         "abort_reason": f"visible on PCIe ({bdf}), but the NVMe driver could not initialize it: {info}"}
    evaluate(S)
    (d / "summary.json").write_text(json.dumps(S, indent=1, ensure_ascii=False))
    (d / "summary.txt").write_text(drive_text(S))
    log(f"{bdf}: NVMe device on the PCIe bus, but it does not come up (state: {info}). Recorded: {d}", "ERR")
    log("You can remove the drive.", "WARN")


def cmd_watch(args):
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)
    log = Log(results)
    preflight(args, log)
    mp = ModParams(log)
    mp.apply()
    transports = tuple(args.transport)
    log.banner(f"radtest hot-plug watcher started ({', '.join(transports)}); results: {results}")
    if args.auto:
        log("AUTO mode: every inserted drive is tested without asking. Exit: Ctrl+C", "WARN")
    else:
        log("Plug in an NVMe drive, then press Start in the web UI (or run: radtest.py start <ctrl>). Exit: Ctrl+C")
    present = {c["ctrl"]: c for c in controllers(transports)}
    if present:
        names = ", ".join(f"{c} ({v['serial']})" for c, v in present.items())
        log(f"Controllers already attached: {names}. "
            "The kernel parameters do not apply to these (re-plugging is recommended).", "WARN")
    pci_baseline = set(nvme_pci_devices()) if "pcie" in transports else set()
    handled, waiting_since, dead_done, announced = set(), {}, set(), set()
    for f in (results / REQ_DIR).glob("*.json") if (results / REQ_DIR).is_dir() else []:
        f.unlink(missing_ok=True)  # stale requests from an earlier run
    for c in present.values():
        if c["state"] != "live":
            handled.add((c["ctrl"], c["serial"]))
            log(f"{c['ctrl']} (S/N {c['serial']}) state is '{c['state']}' - skipped; remove it and plug it in again", "WARN")
    idle_msg = 0
    try:
        while True:
            now = {c["ctrl"]: c for c in controllers(transports)}
            # removed drives
            for key in list(handled):
                if key[0] not in now or now[key[0]]["serial"] != key[1]:
                    handled.discard(key)
                    announced.discard(key)
                    log(f"{key[0]} (S/N {key[1]}) removed. The next drive can come.", "OK")
                    write_status(results, running=False, waiting=True)
            for c in now.values():
                key = (c["ctrl"], c["serial"])
                if key in handled:
                    continue
                if c["state"] != "live":
                    waiting_since.setdefault(key, time.time())
                    if time.time() - waiting_since[key] < 60:
                        continue
                if c["state"] == "live" and not args.auto:
                    if key not in announced:
                        announced.add(key)
                        waiting_since.pop(key, None)
                        if not namespaces(c["ctrl"]):
                            time.sleep(3)
                        info = drive_info(results, c)
                        log(f"New drive: {c['ctrl']}  {c['model']}  S/N {c['serial']}  FW {c['firmware']}  "
                            f"({c['address']})  {gb(info['size_bytes']):.1f} GB")
                        if info["in_use"]:
                            log(f"{info['in_use']} - this drive cannot be tested", "ERR")
                        elif info["tested"]:
                            log(f"already tested ({info['last_run']} -> {info['last_verdict']}); "
                                f"press Start in the web UI to test it again", "WARN")
                        else:
                            log(f"waiting for Start (web UI, or: radtest.py start {c['ctrl']})")
                    continue
                handled.add(key)
                waiting_since.pop(key, None)
                log(f"New drive: {c['ctrl']}  {c['model']}  S/N {c['serial']}  FW {c['firmware']}  ({c['address']})")
                if c["state"] != "live":
                    log(f"the controller is still not 'live' after 60 s ({c['state']})", "ERR")
                    dead_device_record(args, log, c["address"] or c["ctrl"], {"ctrl": c})
                    continue
                if not namespaces(c["ctrl"]):
                    time.sleep(3)
                if test_one(args, log, c) is None:
                    log(f">>> {c['ctrl']} (S/N {c['serial']}) was not tested - it can be removed. <<<", "WARN")
                    continue
                log(f">>> {c['ctrl']} (S/N {c['serial']}) DONE - the drive can be removed. <<<", "OK")
                sys.stdout.write("\a")
                idle_msg = time.time()
            # start requests (web UI button / radtest.py start)
            for req in take_requests(results):
                c = now.get(req.get("ctrl"))
                who = req.get("source", "?")
                if not c:
                    log(f"start request for {req.get('ctrl')} ignored: not present ({who})", "WARN")
                    continue
                if req.get("serial") and c["serial"] != req["serial"]:
                    log(f"start request for {c['ctrl']} ignored: serial changed "
                        f"({req['serial']} != {c['serial']})", "WARN")
                    continue
                key = (c["ctrl"], c["serial"])
                handled.add(key)
                announced.add(key)
                log(f"START requested for {c['ctrl']} (S/N {c['serial']}) by {who}")
                if test_one(args, log, c, consent=True, retest=bool(req.get("retest"))) is None:
                    log(f">>> {c['ctrl']} (S/N {c['serial']}) was not tested - it can be removed. <<<", "WARN")
                else:
                    log(f">>> {c['ctrl']} (S/N {c['serial']}) DONE - the drive can be removed. <<<", "OK")
                    sys.stdout.write("\a")
                idle_msg = time.time()

            # visible on PCIe, but there is no live controller
            if "pcie" in transports:
                pci = nvme_pci_devices()
                for bdf in list(pci_baseline):
                    if bdf not in pci:
                        pci_baseline.discard(bdf)
                for bdf in list(dead_done):
                    if bdf not in pci:
                        dead_done.discard(bdf)
                tested = {now[k[0]]["address"] for k in handled if k[0] in now}
                for bdf, info in pci.items():
                    if bdf in pci_baseline or bdf in dead_done or bdf in tested:
                        continue
                    live = any(s == "live" for s in info["states"])
                    if live:
                        waiting_since.pop(("pci", bdf), None)
                        continue
                    t0 = waiting_since.setdefault(("pci", bdf), time.time())
                    if time.time() - t0 > 90:
                        dead_done.add(bdf)
                        waiting_since.pop(("pci", bdf), None)
                        dead_device_record(args, log, bdf, info)
            if time.time() - idle_msg > 600:
                idle_msg = time.time()
                log("waiting for a drive ... (summary report: radtest.py report)")
            write_status(results, running=False, waiting=True, mode="auto" if args.auto else "manual",
                         drives=[drive_info(results, c) for c in now.values()])
            time.sleep(2)
    except KeyboardInterrupt:
        log("watcher stopped")
    finally:
        mp.restore()
        write_status(results, running=False, waiting=False)


def cmd_run(args):
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)
    log = Log(results)
    preflight(args, log)
    ci = next((c for c in controllers(tuple(args.transport)) if c["ctrl"] == args.ctrl), None)
    if not ci:
        sys.exit(f"{args.ctrl} not found (allowed transport: {args.transport})")
    mp = ModParams(log)
    mp.apply()
    log("note: the kernel parameters only affect a re-attached controller", "WARN")
    try:
        S = test_one(args, log, ci)
    except KeyboardInterrupt:
        S = None
    finally:
        mp.restore()
    return 0 if S and S.get("verdict") == "OK" else 1


def cmd_start(args):
    results = Path(args.results)
    if not results.is_dir():
        sys.exit(f"{results} does not exist - is the watcher running?")
    ci = next((c for c in controllers(("pcie", "loop", "tcp", "rdma", "fc")) if c["ctrl"] == args.ctrl), None)
    if not ci:
        sys.exit(f"{args.ctrl} not found")
    st = load_json(rd(results / ".status.json")) or {}
    if st.get("running"):
        sys.exit(f"a test is already running: {st.get('ctrl')} {st.get('serial')}")
    f = enqueue_start(results, ci["ctrl"], ci["serial"], args.retest, source="cli")
    print(f"start requested: {ci['ctrl']}  {ci['model']}  S/N {ci['serial']}"
          f"{' (retest)' if args.retest else ''}")
    print(f"the watcher picks it up within a few seconds ({f.name})")
    return 0


def cmd_drives(args):
    results = Path(args.results)
    rows = [drive_info(results, c) for c in controllers(tuple(args.transport))]
    if not rows:
        print("no NVMe drive detected")
        return 1
    print(f"{'CTRL':<8} {'MODEL':<28} {'SERIAL':<24} {'GB':>7}  {'STATE':<10} NOTE")
    for d in rows:
        note = d["in_use"] or (f"already tested -> {d['last_verdict']}" if d["tested"] else "not tested yet")
        print(f"{d['ctrl']:<8} {d['model'][:28]:<28} {d['serial'][:24]:<24} "
              f"{gb(d['size_bytes']):>7.1f}  {d['state']:<10} {note}")
    return 0


def cmd_status(args):
    f = Path(args.results) / ".status.json"
    if not f.exists():
        print("no status information")
        return 1
    s = load_json(f.read_text()) or {}
    if not s.get("running"):
        print(f"No running test ({'waiting for a drive' if s.get('waiting') else 'watcher not running'}), "
              f"updated: {s.get('updated')}")
        if s.get("last_dir"):
            print(f"Last: {s['last_dir']} -> {VERDICTS.get(s.get('last_verdict'), s.get('last_verdict'))}")
        return 0
    print(f"Drive: {s.get('ctrl')} {s.get('model')} S/N {s.get('serial')}  (started: {s.get('started')})")
    print(f"Step: {s.get('step')}")
    p = s.get("progress")
    if p and "done_lbas" in p:
        print(f"Progress: {p['pct']:.1f}%  {p['mb_s']:.0f} MB/s  eta {fmt_dur(p['eta_s'])}  "
              f"bad blocks {p['io_err_lbas'] + p['unbisected_lbas']}  mismatch {p['mismatch_lbas']}  "
              f"slow {p['slow_cmds']}")
    elif p:
        print(f"Progress: {p}")
    print(f"Updated: {s.get('updated')}   Directory: {s.get('dir')}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Examination of NVMe drives after irradiation")
    ap.add_argument("--results", default=str(ROOT / "results"), help="results directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def test_opts(p):
        p.add_argument("--yes", action="store_true", help="do not ask for confirmation before the erase")
        p.add_argument("--retest", action="store_true", help="test an already tested drive again")
        p.add_argument("--transport", nargs="+", default=["pcie"], help="allowed transport (for testing: loop)")
        p.add_argument("--no-extended", action="store_true", help="skip the extended self-test")
        p.add_argument("--no-selftest", action="store_true", help="skip every self-test")
        p.add_argument("--no-erase", action="store_true", help="skip the erase")
        p.add_argument("--limit-lbas", type=int, default=0, help="only the first N blocks (for a trial run)")
        p.add_argument("--io-timeout-ms", type=int, default=60000)
        p.add_argument("--slow-factor", type=float, default=10.0, help="slow if > X * baseline")
        p.add_argument("--slow-min-ms", type=int, default=20, help="anything faster than this is never slow")
        p.add_argument("--report-every", type=int, default=30, help="progress report interval in seconds")
        p.add_argument("--posix", action="store_true", help=argparse.SUPPRESS)

    p = sub.add_parser("watch", help="hot-plug watcher; tests start on request (Start button)")
    test_opts(p)
    p.add_argument("--auto", action="store_true",
                   help="test every inserted drive immediately, without waiting for Start")
    p.set_defaults(func=cmd_watch)
    p = sub.add_parser("run", help="test one controller")
    p.add_argument("ctrl", help="e.g. nvme0")
    test_opts(p)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("start", help="ask the running watcher to start testing a drive")
    p.add_argument("ctrl", help="e.g. nvme0")
    p.add_argument("--retest", action="store_true", help="test it again even if it was tested before")
    p.set_defaults(func=cmd_start)
    p = sub.add_parser("drives", help="list the detected NVMe drives")
    p.add_argument("--transport", nargs="+", default=["pcie"])
    p.set_defaults(func=cmd_drives)
    p = sub.add_parser("status", help="state of the running test")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("report", help="summary report")
    p.add_argument("--all-runs", action="store_true", help="every run, not just the latest one per drive")
    p.set_defaults(func=cmd_report)
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
