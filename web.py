#!/usr/bin/env python3
"""
radtest web UI - read only (never starts or stops a test).

  ./web.py [--port 8090] [--bind 0.0.0.0] [--results results]

API:
  GET  /api/status                 state of the running test, steps, progress
  GET  /api/drives                 summary of every run
  GET  /api/drive?dir=S/N/time     details of one run
  GET  /api/latency?dir=..&label=pass1/write   latency time series (bucketed)
  GET  /api/log?offset=N           new lines of radtest.log
  POST /api/report                 regenerate the summary report
  POST /api/start                  ask the watcher to start a test on a drive
  GET  /files/<path>               download a file from under results
"""
import argparse
import csv
import io
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import radtest  # noqa: E402

STEP_KEYS = [
    ("identify", "Identify, collect logs"),
    ("selftest_short_pre", "Short self-test"),
    ("selftest_extended", "Extended self-test"),
    ("pass0", "Pass 0: read original content"),
    ("pass1_write", "Pass 1: write"),
    ("pass1_verify", "Pass 1: read back & verify"),
    ("logs_mid", "Collect logs"),
    ("erase", "Erase + erase check"),
    ("pass2_write", "Pass 2: write"),
    ("pass2_verify", "Pass 2: read back & verify"),
    ("final", "Final logs, self-test, evaluation"),
]
# nvblk steps: (directory/label, display name)
IO_LABELS = [
    ("pass0/readscan", "Pass 0 read"),
    ("pass1/write", "Pass 1 write"),
    ("pass1/verify", "Pass 1 verify"),
    ("erase/erasecheck", "Erase check"),
    ("pass2/write", "Pass 2 write"),
    ("pass2/verify", "Pass 2 verify"),
]

RESULTS = ROOT / "results"
ALLOW_START = True  # a Start gomb kikapcsolható: --read-only


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def safe_rel(rel):
    """Path under results, without escaping it."""
    p = (RESULTS / rel).resolve()
    if RESULTS.resolve() not in p.parents and p != RESULTS.resolve():
        return None
    return p


def watch_running():
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(c.endswith(b"radtest.py") for c in cmd) and b"watch" in cmd:
            return True
    return False


# ---------------------------------------------------------------------------
# latency CSV: incremental read with a cache
# ---------------------------------------------------------------------------


class LatCache:
    def __init__(self, maxfiles=6):
        self.lock = threading.Lock()
        self.files = {}  # path -> dict(offset, t, lat, lba, st, used)
        self.maxfiles = maxfiles

    def get(self, path):
        with self.lock:
            e = self.files.get(path)
            if e is None:
                if len(self.files) >= self.maxfiles:
                    old = min(self.files, key=lambda k: self.files[k]["used"])
                    del self.files[old]
                e = self.files[path] = {"offset": 0, "t": [], "lat": [], "lba": [], "st": [], "rest": b""}
            e["used"] = time.time()
            try:
                size = os.path.getsize(path)
            except OSError:
                return e
            if size < e["offset"]:
                e.update(offset=0, t=[], lat=[], lba=[], st=[], rest=b"")
            if size > e["offset"]:
                with open(path, "rb") as f:
                    f.seek(e["offset"])
                    data = e["rest"] + f.read(size - e["offset"])
                    e["offset"] = size
                lines = data.split(b"\n")
                e["rest"] = lines.pop()
                t, lat, lba, st = e["t"], e["lat"], e["lba"], e["st"]
                for ln in lines:
                    parts = ln.split(b",")
                    if len(parts) < 6 or parts[0] == b"t_s" or parts[3] == b"0":
                        continue  # header / flush
                    try:
                        t.append(float(parts[0]))
                        lba.append(int(parts[2]))
                        lat.append(int(parts[4]))
                        st.append(parts[5] != b"0x0000")
                    except ValueError:
                        pass
            return e


LAT = LatCache()


def latency_series(run_dir, label, points=600):
    path = str(run_dir / f"{label}_latency.csv")
    e = LAT.get(path)
    n = len(e["t"])
    out = {"label": label, "count": n, "buckets": []}
    if not n:
        return out
    t0, t1 = e["t"][0], e["t"][-1]
    span = max(t1 - t0, 1e-6)
    nb = min(points, n)
    buckets = [[0, 0, 0, 0, None] for _ in range(nb)]  # n, sum, max, err, first_lba
    t, lat, lba, st = e["t"], e["lat"], e["lba"], e["st"]
    for i in range(n):
        b = min(int((t[i] - t0) / span * nb), nb - 1)
        k = buckets[b]
        k[0] += 1
        k[1] += lat[i]
        if lat[i] > k[2]:
            k[2] = lat[i]
        if st[i]:
            k[3] += 1
        if k[4] is None:
            k[4] = lba[i]
    for i, k in enumerate(buckets):
        if k[0]:
            out["buckets"].append({"t": round(t0 + (i + 0.5) * span / nb, 2), "n": k[0],
                                   "mean": round(k[1] / k[0]), "max": k[2], "err": k[3], "lba": k[4]})
    out["t_end"] = t1
    slow = []
    sp = run_dir / f"{label}_slow.csv"
    if sp.exists():
        with open(sp, newline="") as f:
            for i, row in enumerate(csv.DictReader(f)):
                if i >= 500:
                    break
                slow.append(row)
    out["slow"] = slow
    return out


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


def all_runs():
    runs = []
    for f in RESULTS.glob("*/*/summary.json"):
        S = load(f)
        if not S:
            continue
        try:
            radtest.evaluate(S)
            row = radtest.report_row(S, f.parent.relative_to(RESULTS))
        except Exception as ex:  # noqa: BLE001
            row = {"serial": f.parent.parent.name, "verdict": "INCOMPLETE", "error": str(ex),
                   "dir": str(f.parent.relative_to(RESULTS))}
        row["status"] = S.get("status")
        row["started"] = S.get("started")
        row["duration_s"] = S.get("duration_s")
        row["verdict_text"] = radtest.VERDICTS.get(row.get("verdict"), row.get("verdict"))
        row["reasons"] = S.get("reasons", [])
        row["reason_groups"] = S.get("reason_groups", {})
        runs.append(row)
    runs.sort(key=lambda r: r.get("started") or "", reverse=True)
    return runs


def run_detail(rel):
    d = safe_rel(rel)
    if not d or not (d / "summary.json").exists():
        return None
    S = load(d / "summary.json") or {}
    radtest.evaluate(S)
    files = []
    for p in sorted(d.rglob("*")):
        if p.is_file() and not p.name.endswith((".tmp",)):
            files.append({"path": str(p.relative_to(RESULTS)), "name": str(p.relative_to(d)),
                          "size": p.stat().st_size})
    io_steps = []
    for label, name in IO_LABELS:
        s = load(d / f"{label}_summary.json")
        p = load(d / f"{label}_progress.json")
        if s or p or (d / f"{label}_latency.csv").exists():
            io_steps.append({"label": label, "name": name, "summary": s, "progress": p})
    return {"dir": rel, "summary": S, "text": radtest.drive_text(S), "files": files, "io": io_steps,
            "verdict_text": radtest.VERDICTS.get(S.get("verdict"), S.get("verdict"))}


def current_status():
    st = load(RESULTS / ".status.json") or {}
    out = {"status": st, "watch_running": watch_running(), "now": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "steps": None, "allow_start": ALLOW_START}
    if st.get("running") and st.get("dir"):
        d = Path(st["dir"])
        S = load(d / "summary.json") or {}
        m = re.match(r"(\d+)/(\d+)\s*(.*)", st.get("step") or "")
        cur = int(m.group(1)) if m else 0
        steps = []
        for i, (key, name) in enumerate(STEP_KEYS, 1):
            info = S.get("steps", {}).get(key)
            state = "done" if info is not None or i < cur else ("running" if i == cur else "pending")
            steps.append({"n": i, "key": key, "name": name, "state": state,
                          "duration_s": (info or {}).get("duration_s")})
        out["steps"] = steps
        try:
            out["dir_rel"] = str(d.relative_to(RESULTS))
        except ValueError:
            out["dir_rel"] = None
        # active nvblk step: the most recently modified latency file
        newest, lab = 0, None
        for label, _ in IO_LABELS:
            p = d / f"{label}_latency.csv"
            if p.exists() and p.stat().st_mtime > newest:
                newest, lab = p.stat().st_mtime, label
        io_active = lab and time.time() - newest < 15 and not (d / f"{lab}_summary.json").exists()
        out["io_label"] = lab if io_active else None
        out["io_name"] = dict(IO_LABELS).get(lab) if io_active else None
        ident = S.get("identity") or {}
        out["identity"] = {k: ident.get(k) for k in ("model", "serial", "firmware", "capacity_bytes", "vid")}
        out["smart_pre"] = S.get("smart_pre")
        out["pcie"] = S.get("pcie_pre")
        out["io_done"] = []
        for label, name in IO_LABELS:
            s = load(d / f"{label}_summary.json")
            if s:
                out["io_done"].append({"label": label, "name": name, "result": s["result"],
                                       "counts": s["counts"], "latency": s["latency_us"],
                                       "mb_s": s["mb_s"], "elapsed_s": s["elapsed_s"]})
        out["selftests"] = {k: (S.get(k) or {}).get("result") for k in
                            ("selftest_short_pre", "selftest_extended", "selftest_short_post")}
        out["erase"] = {k: v for k, v in (S.get("erase") or {}).items() if k != "check"}
    return out


class LogTail:
    path = None

    @classmethod
    def read(cls, offset):
        p = RESULTS / "radtest.log"
        try:
            size = p.stat().st_size
        except OSError:
            return {"offset": 0, "lines": []}
        if offset < 0 or offset > size:
            offset = max(0, size - 30000)
            skip_first = offset > 0
        else:
            skip_first = False
        with open(p, "rb") as f:
            f.seek(offset)
            data = f.read(min(size - offset, 2_000_000))
        end = offset + len(data)
        text = data.decode(errors="replace")
        if not text.endswith("\n"):
            cut = text.rfind("\n") + 1
            end = offset + len(text[:cut].encode())
            text = text[:cut]
        lines = text.splitlines()
        if skip_first and lines:
            lines = lines[1:]
        lines = [ln for ln in lines if not re.match(r"^\S+\s+(\[[^\]]*\]\s*)?=+\s*$", ln)]
        return {"offset": end, "lines": lines[-2000:]}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "radtest-web"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, (ROOT / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self.send(200, current_status())
            if u.path == "/api/drives":
                return self.send(200, all_runs())
            if u.path == "/api/drive":
                d = run_detail(q.get("dir", ""))
                return self.send(200, d) if d else self.send(404, {"error": "no such run"})
            if u.path == "/api/latency":
                d = safe_rel(q.get("dir", ""))
                label = q.get("label", "")
                if not d or label not in dict(IO_LABELS):
                    return self.send(404, {"error": "bad parameter"})
                return self.send(200, latency_series(d, label, int(q.get("points", 600))))
            if u.path == "/api/log":
                return self.send(200, LogTail.read(int(q.get("offset", -1))))
            if u.path.startswith("/files/"):
                p = safe_rel(urllib.parse.unquote(u.path[len("/files/"):]))
                if not p or not p.is_file():
                    return self.send(404, {"error": "no such file"})
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                if p.suffix in (".log", ".txt", ".csv", ".md", ".json", ".err"):
                    ctype = ("text/plain" if p.suffix != ".json" else "application/json") + "; charset=utf-8"
                extra = {}
                if q.get("download"):
                    extra["Content-Disposition"] = f'attachment; filename="{p.name}"'
                return self.send(200, p.read_bytes(), ctype, extra)
            return self.send(404, {"error": "unknown path"})
        except BrokenPipeError:
            pass
        except Exception as ex:  # noqa: BLE001
            try:
                self.send(500, {"error": f"{type(ex).__name__}: {ex}"})
            except OSError:
                pass

    def read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not 0 < n <= 65536:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode()) or {}
        except (ValueError, OSError):
            return {}

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/start":
            if not ALLOW_START:
                return self.send(403, {"error": "this UI runs read-only (--read-only)"})
            body = self.read_body()
            ctrl, serial = str(body.get("ctrl", "")), body.get("serial")
            if not re.fullmatch(r"nvme\d+", ctrl):
                return self.send(400, {"error": "bad controller name"})
            st = load(RESULTS / ".status.json") or {}
            if st.get("running"):
                return self.send(409, {"error": f"a test is already running on {st.get('ctrl')}"})
            if not st.get("mode"):
                return self.send(409, {"error": "the watcher is not running"})
            ci = next((c for c in radtest.controllers(("pcie", "loop", "tcp", "rdma", "fc"))
                       if c["ctrl"] == ctrl), None)
            if not ci:
                return self.send(404, {"error": f"{ctrl} is not present"})
            if serial and ci["serial"] != serial:
                return self.send(409, {"error": "the drive changed, reload the page"})
            info = radtest.drive_info(RESULTS, ci)
            if info["in_use"]:
                return self.send(409, {"error": info["in_use"]})
            who = f"web {self.client_address[0]}"
            radtest.enqueue_start(RESULTS, ctrl, ci["serial"], bool(body.get("retest")), source=who)
            return self.send(200, {"ok": True, "ctrl": ctrl, "serial": ci["serial"]})
        if u.path == "/api/report":
            buf = io.StringIO()
            old = sys.stdout
            try:
                sys.stdout = buf
                rc = radtest.cmd_report(argparse.Namespace(results=str(RESULTS), all_runs=False))
            except Exception as ex:  # noqa: BLE001
                return self.send(500, {"error": str(ex)})
            finally:
                sys.stdout = old
            return self.send(200, {"rc": rc, "output": buf.getvalue()})
        self.send(404, {"error": "unknown path"})


def main():
    global RESULTS, ALLOW_START
    ap = argparse.ArgumentParser(description="radtest web UI")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--results", default=str(ROOT / "results"))
    ap.add_argument("--read-only", action="store_true",
                    help="disable the Start button (display only)")
    a = ap.parse_args()
    RESULTS = Path(a.results).resolve()
    ALLOW_START = not a.read_only
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.daemon_threads = True
    print(f"radtest web: http://{a.bind}:{a.port}/  (results: {RESULTS})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
