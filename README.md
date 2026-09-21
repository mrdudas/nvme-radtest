# radtest – post-irradiation examination of NVMe drives

Purpose: decide whether an irradiated drive is damaged **physically** (NAND, controller) or only
**logically** (firmware, FTL, metadata).

## Usage

```bash
make                           # build bin/nvblk (once)
./radtest.py watch             # hot-plug: insert a drive -> test -> "can be removed" message
./radtest.py watch --yes       # same, without asking for confirmation
./radtest.py status            # from another terminal: where the test currently is
./radtest.py report            # once every drive is done: results/REPORT.{txt,md,csv}
./radtest.py run nvme0         # test a drive that is already attached
```

The operator is told about every step on the console, and during read/write work a progress line
is printed every 30 seconds with percentage, throughput, remaining time and the errors found so far.
A drive that has already been tested successfully is recognised and skipped (`--retest` overrides).
Only PCIe NVMe drives are touched, and never one that is in use (mounted, part of LVM, etc.).

Useful options: `--no-extended` (skip the extended self-test), `--no-erase`,
`--limit-lbas N` (trial run over the first N blocks), `--slow-factor 10` / `--slow-min-ms 20`
(when a command counts as slow), `--report-every 30`.

## Web UI

```bash
./web.py                       # http://<host-ip>:8090/  (default: all interfaces, port 8090)
./web.py --bind 127.0.0.1      # local only / through an SSH tunnel
```

Read only: it never starts or stops a test. There is no password, so anyone who can reach the port
can read the results.
- **Live test:** drive data, state of the 11 steps, progress bar, throughput, remaining time,
  error counters, live latency chart, log.
- **Drives:** every run in a table; click for details (SMART before/after, steps, charts,
  slow commands, summary.txt, downloadable files).
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

`radtest-watch` runs with `--yes`, because as a service there is nobody to confirm the erase with:
**it automatically erases and tests every inserted NVMe drive that has not been tested yet.**
On stop it receives SIGINT, so it closes the running test and restores the kernel parameters.

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
