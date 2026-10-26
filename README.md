# radtest – post-irradiation examination of NVMe drives

Purpose: decide whether an irradiated drive is damaged **physically** (NAND, controller) or only
**logically** (firmware, FTL, metadata).

## Usage

```bash
make                           # build bin/nvblk (once)
./radtest.py watch             # hot-plug watcher; each test waits for an explicit Start
./radtest.py drives            # list the detected drives
./radtest.py start nvme0       # start the test on that drive (same as the Start button)
./radtest.py status            # from another terminal: where the test currently is
./radtest.py report            # once every drive is done: results/REPORT.{txt,md,csv}
./radtest.py unlock nvme0      # read-only Micron drive: unlock it (ERASES THE DRIVE)
./radtest.py namespace nvme0   # drive without a namespace: attach the one it has, or create one
./radtest.py run nvme0         # test one drive directly, without the watcher
./radtest.py watch --auto      # old behaviour: test every inserted drive immediately
```

**Nothing is erased until someone presses Start.** The watcher only detects the drives and
publishes them; the test begins when a Start request arrives from the web UI or from
`radtest.py start`. With several drives attached, each one has its own Start button, so the
operator picks which drive to test. A drive that was already tested successfully shows
"Test again" instead (that is the only way to re-run it; `--retest` does the same on the CLI).

The operator is told about every step on the console, and during read/write work a progress line
is printed every 30 seconds with percentage, throughput, remaining time and the errors found so far.
Only PCIe NVMe drives are touched, and never one that is in use (mounted, part of LVM, etc.).

Useful options: `--no-extended` (skip the extended self-test), `--no-erase`,
`--limit-lbas N` (trial run over the first N blocks), `--slow-factor 10` / `--slow-min-ms 20`
(when a command counts as slow), `--report-every 30`.

## Web UI

```bash
./web.py                       # http://<host-ip>:8090/  (default: all interfaces, port 8090)
./web.py --bind 127.0.0.1      # local only / through an SSH tunnel
```

There is no password, so anyone who can reach the port can read the results **and press Start,
which erases a drive**. Bind it to localhost (`--bind 127.0.0.1`, reach it over an SSH tunnel) or
run it with `--read-only` if that is not acceptable.
- **Live test:** when idle, the detected drives with their Start buttons (model, serial, size, and
  whether they were tested before); while a test runs, drive data, state of the 11 steps, progress
  bar, throughput, remaining time, error counters, live latency chart, log. A drive that came back
  locked gets **Unlock…** and **Check only** instead of Start, one without a usable namespace gets
  **Attach namespace** / **Create namespace**, and both report their progress on the page, phase by
  phase.
  Nothing on the page is redrawn on a whole: the status is polled every two seconds, but only the
  fields that actually changed are rewritten, so selection, hover and scrolling are never lost.
- **Drives:** the runs are grouped by serial number, one row per physical drive, showing its latest
  completed run. Earlier runs are listed under it with their verdicts, so it is visible when a test
  misran and had to be repeated; a drive with an unfinished earlier run is expanded by default.
  Click a row for the details (SMART before/after, steps, charts, slow commands, summary.txt,
  downloadable files). The bin icon removes a run: it is moved to `results/.trash/` (so it can be
  restored by moving the directory back) and disappears from the list and from the report.
- **Report:** regenerate and download the summary (TXT / MD / CSV).

## Running as a service (systemd)

Both parts run as services and start on their own after a reboot:

```bash
cp radtest-watch.service radtest-web.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now radtest-watch radtest-web
systemctl status radtest-watch        # state
systemctl stop radtest-watch          # stop (restores the kernel parameters)
journalctl -u radtest-watch -f        # or: tail -f results_watch.out
```

`radtest-watch` runs in manual mode: it detects drives but starts nothing on its own, so a drive
is only erased after an explicit Start. Append `--auto` to the ExecStart line if you want every
inserted drive tested immediately. On stop the service receives SIGINT, so it closes the running
test and restores the kernel parameters.

## Write protection and the Micron read-only state

A drive can come back from irradiation unable to accept writes. There are two different cases and
radtest treats them differently.

**1. NVMe namespace write protection** (feature 0x84). Writes are refused with
`SCT0/SC0x20 Namespace Is Write Protected`, NSATTR reports read-only and the kernel marks the block
device read-only. `nvblk` stops such a write pass at the first refusal instead of grinding through
the whole drive. On **Micron drives only** (PCI VID 0x1344) and **only when the namespace really is
locked**, radtest clears it with `set-feature 0x84 = 0` plus `ns-rescan`, re-reads every signal and
repeats the write pass once (the blocked attempt is kept as `<label>_wp_blocked_*`). It never sets a
protection, and it does not try to clear "write protect until power cycle" (only unplugging helps)
or "permanent write protect" (irreversible) - those are reported and the test moves on.

**2. Firmware level media read-only** - this is the state an irradiated Micron drive actually comes
back in (see `docs/MICRON_RO_RECOVERY.md` of the Micron recovery work, verified on
Micron_4150AT / A1MU002). Here feature 0x84 reads "No Write Protect" and is misleading; the real
signals are SMART critical warning bit 3 (media placed in read-only mode) and NSATTR bit 0, and
usually there is **no namespace block device at all** (namespace commands answer 0x2006). radtest
detects this after identify - and also when a drive has no namespace, so the test stops with a clear
reason instead of a bare "no namespace".

Clearing this one **erases the whole drive**: the only chain that works starts with a block erase
and needs a reset deeper than a controller reset. radtest can do all of it, so no shell is needed:

```bash
./radtest.py recover nvme0     # check only: save the evidence, print the steps, erase nothing
./radtest.py unlock nvme0      # the whole chain, start to finish - ERASES THE DRIVE
```

In the web UI a locked drive shows **Unlock…** and **Check only** instead of Start. Unlock asks for
a confirmation and then runs:

| # | Phase | What happens |
|---|---|---|
| 1 | check | Micron drive, really locked, and an erase is the right answer. It **stops** when another critical warning bit is set, when there are media errors, or when the available spare is below its threshold - the drive is then failing and an erase would only destroy the evidence. A plain namespace write protection (feature 0x84) is cleared here directly, with no erase. |
| 2 | save evidence | Everything the erase would wipe: controller telemetry, persistent event log, SMART, vendor logs, `msecli -P` if present → `results/<serial>/<timestamp>-recovery/` |
| 3 | erase | `nvme sanitize -a 2 --ause`, polling the sanitize log with a progress bar. If the log says "failed", it leaves failure mode (`sanitize -a 1`) and stops instead of repeating the erase. |
| 4 | power cycle | The firmware only hands the namespace back after a deep reset. It tries the least invasive first: **sbr** = detach the endpoint and hot-reset its own PCIe port; then **switch** = `switchtec hard-reset` of the whole switch (every drive behind it disappears for a moment); then **manual** = it asks on the page for the drive to be unplugged and plugged back in, and waits for it. `--power-cycle sbr|switch|manual` forces one, `--no-switch-reset` keeps the switch out of it. |
| 5 | attach namespace | `nvme attach-ns -n 1 -c 0` + `ns-rescan`; this is what decides whether the reset was deep enough. |
| 6 | verify | critical warning 0, NSATTR 0, the block device is back with `ro=0`, and one block is actually written. |

The drive's controller name may change along the way (the reset re-enumerates it); the drive is
followed by serial number. The progress, every message and the result appear on the page while it
runs, and are saved into `unlock.json` next to the evidence.

### A drive without a namespace

An interrupted unlock leaves the drive in a half-recovered state: the erase is done and the lock is
gone, but the namespace is still detached, so there is no block device and nothing can be tested.
Such a drive shows **no namespace** in the list with an **Attach namespace** button (or **Create
namespace** when the drive has none allocated at all):

```bash
./radtest.py namespace nvme0            # attach what is there, or create one over the free capacity
./radtest.py namespace nvme0 --nsid 1   # a particular namespace
./radtest.py namespace nvme0 --block-size 4096 --nsze 107216896
```

It first reads what the drive actually has (`list-ns`, `list-ns -a`, `list-ctrl`, `id-ctrl`):
a namespace that is allocated but detached is simply attached (`attach-ns` + `ns-rescan`) - nothing
is created and nothing is erased. Only when the controller reports no allocated namespace at all
does it create one, over the free capacity, with 4 KiB blocks. If the drive answers `0x2006`, it
says so and asks for a power cycle, because that is what the firmware wants at that point.

It never runs the irreversible commands (`msecli -M -L`, `msecli -X -P`, permanent write protect,
firmware download/commit) - those are listed on the page as the ones never to use.

## Steps per drive

| # | Step | What it gives |
|---|---|---|
| 1 | Identify, logs | vendor (VID/OUI), model, S/N, FW, size, EUI64/NGUID; SMART: power-on hours, power cycles, data written/read, percentage used, available spare, media errors; full error log (status + LBA); self-test log, firmware log, persistent event log, vendor logs (OCP / Intel / Solidigm / Micron / WDC / SanDisk / Kioxia), every vendor log page as raw data, features, registers, lspci, AER counters |
| 2–3 | Short and extended self-test | the drive's own diagnostics, failing segment / LBA |
| 4 | Pass 0: read the original content | blocks left unreadable by the irradiation, **before** anything is overwritten |
| 5–6 | Pass 1: full write + read back | write / read errors down to the exact block, content mismatches |
| 7 | Logs | what changed during pass 1 |
| 8 | Erase (sanitize block erase → format) + erase check | whether old content survived (FTL/firmware fault) |
| 9–10 | Pass 2: full write + read back | whether the errors survive an erase |
| 11 | Final logs, short self-test, telemetry, evaluation | SMART differences, summary; telemetry runs last, because it once hung an irradiated drive completely (that too is recorded as a result) |

Reading and writing is done by `bin/nvblk` with **NVMe passthrough** commands (bypassing the kernel
block layer, the page cache and kernel-level retries), with the drive's own write cache (VWC)
disabled. Every block gets a unique pattern (LBA + seed + verifiable data), so the check can tell
apart: `io_error` (NVMe error status), `corrupt` (bit errors, with the bit count), `misdirected`
(content of a different LBA, with the offset), `stale_*` (content from an earlier pass or surviving
an erase), `zeroed`, `all_ff`, `ctrl_error` (timeout / reset). On an error the chunk is bisected
down to single blocks. The latency of every command is logged; a command counts as an outlier when
it is above 10× the running average (and above 20 ms), which usually means the drive is busy with
background work.

## Evaluation

| Verdict | Condition (any of them) |
|---|---|
| **PHYSICAL DAMAGE** | errors in pass 2 as well, i.e. after the erase; self-test reports a failing segment or a fatal error; the SMART media error counter increased; available spare dropped or is below the threshold; critical warning (reliability degraded, read-only) |
| **SOFTWARE/LOGICAL DAMAGE** | the pass 1 errors are gone after the erase; content survived the erase; the original content was unreadable but everything is fine after a write; broken identification data; changed namespace size |
| **SUSPECT** | the tests are clean, but there were slow commands, controller resets, AER errors or a failed erase |
| **NOT WORKING** | does not come up (visible on PCIe, but the driver cannot initialise it), or it disappeared during the test |
| **HEALTHY** | none of the above |

`report` always re-evaluates from the raw data, so the rules can still be refined afterwards.

## Results

```
results/
  radtest.log                      every operator message
  REPORT.txt / REPORT.md / REPORT.csv
  <S/N>/<timestamp>/
    summary.txt, summary.json      per-drive summary
    run.log                        messages of that test
    logs/01_pre, 02_after_pass1, 03_post   raw logs (json/txt/bin)
    pass0/ pass1/ erase/ pass2/    <step>_errors.csv, _slow.csv, _latency.csv, _summary.json
    kernel.log, kernel_drive.log
  NOT_ENUMERATED_<PCI address>/... drives that never came up
  .requests/                       queued start requests (handled within seconds)
  .trash/                          runs removed from the UI; delete by hand when no longer needed
```

## Kernel settings

The driver is the Linux in-tree `nvme` driver. While `radtest` runs, the following `nvme_core`
parameters are changed (restored on exit; they only affect drives attached **afterwards**):
`default_ps_max_latency_us=0` (APST off), `max_retries=0`, `io_timeout=60`, `admin_timeout=120`.
nvblk gives each command a 60 s timeout; after a controller reset it waits for the drive to come
back, retries once, and if the drive disappears the test ends with the "NOT WORKING" verdict.

The values set on the boot command line (`/proc/cmdline`), such as `nvme_core.io_timeout=3` and
`admin_timeout=5`, are left untouched.

## Trying it without a real drive

```bash
tests/loopdisk.sh up        # software NVMe drive (nvme-loop) with two bad ranges
./radtest.py run nvmeX --transport loop --yes
tests/loopdisk.sh down
```
