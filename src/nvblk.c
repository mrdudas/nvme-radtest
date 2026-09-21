/*
 * nvblk - full-disk write / read-back / verify via NVMe passthrough
 *
 * Every block gets a unique, deterministic pattern:
 *   0..7   LBA (LE)          8..15  seed (LE)        16..23 "RADTEST1"
 *   24..   splitmix64 stream (based on seed, LBA)
 * This lets verification tell apart: read error, bit error, write that landed in the
 * wrong place (misdirected), leftover from an earlier pass (stale), zeroed block.
 *
 * Modes:
 *   write       full write (on error, bisection narrows it down to block level)
 *   verify      read back and compare
 *   erasecheck  read after erase: no RADTEST pattern may remain
 *   readscan    read only, without content verification (survey of the original state)
 *
 * Output (--out directory, prefixed with --label):
 *   <label>_errors.csv   failed ranges (merged)
 *   <label>_latency.csv  latency of every command
 *   <label>_slow.csv     outlier latencies
 *   <label>_progress.json, <label>_summary.json
 *
 * Exit code: 0 no errors, 1 errors occurred, 2 usage/startup error,
 *            3 device lost / interrupted
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <linux/fs.h>
#include <linux/nvme_ioctl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>

#define MAGIC "RADTEST1"
#define HBUCKETS 180
#define MAX_STATUS 128
#define MAX_CONSEC_FAILED_CHUNKS 8
#define MAX_CONSEC_CTRL_ERRORS 20

enum mode { M_WRITE, M_VERIFY, M_ERASECHECK, M_READSCAN };

static volatile sig_atomic_t g_stop;
static void on_signal(int s) { (void)s; g_stop = 1; }

struct pend {
	int active;
	char kind[32];
	uint64_t lba, n;
	int st;
	int64_t extra;
	uint64_t bits;
};

struct stcount { int st; uint64_t events, lbas; };

struct ctx {
	/* options */
	const char *dev, *outdir, *label, *ctrl_state;
	enum mode mode;
	uint64_t seed, start, count;
	unsigned chunk_kb, timeout_ms, ctrl_wait_s;
	double lat_factor;
	uint64_t lat_min_us, lat_abs_us;
	int posix;
	uint64_t max_err_rows;

	int fd;
	uint32_t nsid, lbs;
	uint64_t nlba, end;
	uint32_t chunk;
	unsigned char *buf, *exp, *failmap;
	FILE *f_err, *f_lat, *f_slow;

	/* counters (in LBAs unless noted otherwise) */
	uint64_t io_err_lbas, io_err_events, unbisected_lbas, ctrl_err_events;
	uint64_t mismatch_lbas, corrupt, bitflips, misdirected, stale, zeroed, ones;
	uint64_t unwritten_lbas; /* 0x287: deallocated/unwritten (not an error in erasecheck) */
	uint64_t erased_ok, foreign;
	uint64_t slow_cmds, slow_time_us, cmds, bytes_ok, err_rows, rows_dropped;
	struct stcount stc[MAX_STATUS];
	int nstc;
	struct pend pend;

	double ewma;
	uint64_t ewma_n, lat_max, lat_sum;
	uint64_t flush_us, flush_cmds;
	uint64_t hist[HBUCKETS];
	int consec_failed_chunks, consec_ctrl, bisect_off;
	int device_lost;
	char abort_reason[128];
	struct timespec t0;
	double last_progress;
	uint64_t done_lbas;
};

static uint64_t now_us(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (uint64_t)t.tv_sec * 1000000ULL + t.tv_nsec / 1000;
}

static double elapsed_s(struct ctx *c)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (t.tv_sec - c->t0.tv_sec) + (t.tv_nsec - c->t0.tv_nsec) / 1e9;
}

/* ---------- pattern ---------- */

static inline uint64_t splitmix64(uint64_t *s)
{
	uint64_t z = (*s += 0x9E3779B97F4A7C15ULL);
	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
	z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
	return z ^ (z >> 31);
}

static inline void put64(unsigned char *p, uint64_t v) { memcpy(p, &v, 8); }
static inline uint64_t get64(const unsigned char *p) { uint64_t v; memcpy(&v, p, 8); return v; }

static void fill_block(struct ctx *c, unsigned char *p, uint64_t lba)
{
	uint64_t s = c->seed ^ (lba * 0xD1342543DE82EF95ULL) ^ 0x5DEECE66DULL;
	put64(p, lba);
	put64(p + 8, c->seed);
	memcpy(p + 16, MAGIC, 8);
	for (uint32_t off = 24; off < c->lbs; off += 8)
		put64(p + off, splitmix64(&s));
}

/* ---------- NVMe status ---------- */

static const char *status_desc(int st)
{
	static char b[96];
	if (st == 0)
		return "OK";
	if (st < 0) {
		snprintf(b, sizeof b, "errno %d (%s)", -st, strerror(-st));
		return b;
	}
	int sct = (st >> 8) & 7, sc = st & 0xff;
	const char *d = NULL;
	if (sct == 0) {
		switch (sc) {
		case 0x01: d = "Invalid Command Opcode"; break;
		case 0x02: d = "Invalid Field in Command"; break;
		case 0x04: d = "Data Transfer Error"; break;
		case 0x05: d = "Aborted: Power Loss"; break;
		case 0x06: d = "Internal Error"; break;
		case 0x07: d = "Command Abort Requested"; break;
		case 0x08: d = "Aborted: SQ Deletion"; break;
		case 0x0b: d = "Invalid Namespace or Format"; break;
		case 0x0d: d = "Invalid Field in Command"; break;
		case 0x1c: d = "Sanitize In Progress"; break;
		case 0x80: d = "LBA Out of Range"; break;
		case 0x81: d = "Capacity Exceeded"; break;
		case 0x82: d = "Namespace Not Ready"; break;
		case 0x83: d = "Reservation Conflict"; break;
		case 0x84: d = "Format In Progress"; break;
		}
	} else if (sct == 1) {
		switch (sc) {
		case 0x80: d = "Conflicting Attributes"; break;
		case 0x82: d = "Attempted Write to Read Only Range"; break;
		}
	} else if (sct == 2) {
		switch (sc) {
		case 0x80: d = "Write Fault"; break;
		case 0x81: d = "Unrecovered Read Error"; break;
		case 0x82: d = "End-to-end Guard Check Error"; break;
		case 0x83: d = "End-to-end Application Tag Check Error"; break;
		case 0x84: d = "End-to-end Reference Tag Check Error"; break;
		case 0x85: d = "Compare Failure"; break;
		case 0x86: d = "Access Denied"; break;
		case 0x87: d = "Deallocated or Unwritten Logical Block"; break;
		}
	} else if (sct == 3) {
		switch (sc) {
		case 0x00: d = "Internal Path Error"; break;
		case 0x70: d = "Internal Host Path Error"; break;
		case 0x71: d = "Host Aborted Command (timeout/reset)"; break;
		case 0x72: d = "Controller Pathing Error"; break;
		}
	}
	snprintf(b, sizeof b, "SCT%d/SC0x%02x %s%s%s", sct, sc, d ? d : "(unknown)",
		 (st & 0x4000) ? " DNR" : "", (st & 0x2000) ? " MORE" : "");
	return b;
}

static int st_key(int st) { return st > 0 ? (st & 0x7ff) : st; } /* without DNR/MORE */

static int is_lost(int st) { return st == -ENODEV || st == -ENXIO || st == -ESHUTDOWN; }

/* controller level error: no point in bisecting block by block */
static int is_ctrl_err(struct ctx *c, int st)
{
	(void)c;
	if (st < 0)
		return st != -EIO; /* -EIO: block level error, can be bisected */
	int sct = (st >> 8) & 7, sc = st & 0xff;
	if (sct == 3)
		return 1;
	if (sct == 0 && (sc == 0x05 || sc == 0x07 || sc == 0x08 || sc == 0x82 || sc == 0x84 || sc == 0x1c))
		return 1;
	return 0;
}

static void count_status(struct ctx *c, int st, uint64_t lbas)
{
	int k = st_key(st);
	for (int i = 0; i < c->nstc; i++)
		if (c->stc[i].st == k) {
			c->stc[i].events++;
			c->stc[i].lbas += lbas;
			return;
		}
	if (c->nstc < MAX_STATUS)
		c->stc[c->nstc++] = (struct stcount){ k, 1, lbas };
}

/* ---------- merging error rows ---------- */

static const char *op_name(struct ctx *c)
{
	return c->mode == M_WRITE ? "write" : "read";
}

static void flush_pend(struct ctx *c)
{
	struct pend *p = &c->pend;
	if (!p->active)
		return;
	if (c->err_rows < c->max_err_rows) {
		fprintf(c->f_err, "%s,%s,%" PRIu64 ",%" PRIu64 ",0x%04x,\"%s\",%" PRId64 ",%" PRIu64 "\n",
			op_name(c), p->kind, p->lba, p->n, p->st > 0 ? p->st : 0, status_desc(p->st),
			p->extra, p->bits);
		c->err_rows++;
	} else {
		c->rows_dropped++;
	}
	p->active = 0;
}

static void record(struct ctx *c, const char *kind, uint64_t lba, uint64_t n, int st, int64_t extra,
		   uint64_t bits)
{
	struct pend *p = &c->pend;
	if (p->active && p->lba + p->n == lba && p->st == st && p->extra == extra &&
	    !strcmp(p->kind, kind)) {
		p->n += n;
		p->bits += bits;
		return;
	}
	flush_pend(c);
	p->active = 1;
	snprintf(p->kind, sizeof p->kind, "%s", kind);
	p->lba = lba;
	p->n = n;
	p->st = st;
	p->extra = extra;
	p->bits = bits;
}

/* ---------- latency ---------- */

static int hbucket(uint64_t us)
{
	if (us < 4)
		return (int)us;
	int msb = 63 - __builtin_clzll(us);
	int sub = (int)((us >> (msb - 2)) & 3);
	int i = msb * 4 + sub;
	return i < HBUCKETS ? i : HBUCKETS - 1;
}

static uint64_t hbucket_upper(int i)
{
	if (i < 4)
		return (uint64_t)i;
	int msb = i / 4, sub = i % 4;
	return (uint64_t)(5 + sub) << (msb - 2);
}

static uint64_t percentile(struct ctx *c, double q)
{
	uint64_t tot = 0, acc = 0;
	for (int i = 0; i < HBUCKETS; i++)
		tot += c->hist[i];
	if (!tot)
		return 0;
	uint64_t want = (uint64_t)(q * tot);
	for (int i = 0; i < HBUCKETS; i++) {
		acc += c->hist[i];
		if (acc > want)
			return hbucket_upper(i) < c->lat_max ? hbucket_upper(i) : c->lat_max;
	}
	return c->lat_max;
}

static void note_latency(struct ctx *c, const char *op, uint64_t lba, uint32_t n, uint64_t lat, int st)
{
	double t = elapsed_s(c);
	fprintf(c->f_lat, "%.3f,%s,%" PRIu64 ",%u,%" PRIu64 ",0x%04x\n", t, op, lba, n, lat,
		st > 0 ? st : (st < 0 ? 0xffff : 0));
	if (!n) { /* flush: counted separately, not part of the slow I/O statistics */
		c->flush_cmds++;
		if (lat > c->flush_us)
			c->flush_us = lat;
		return;
	}
	c->cmds++;
	c->hist[hbucket(lat)]++;
	c->lat_sum += lat;
	if (lat > c->lat_max)
		c->lat_max = lat;

	int slow = 0;
	if (lat >= c->lat_min_us) {
		if (c->ewma_n < 32)
			slow = lat >= c->lat_abs_us;
		else
			slow = lat >= c->lat_factor * c->ewma;
	}
	if (slow) {
		c->slow_cmds++;
		c->slow_time_us += lat;
		fprintf(c->f_slow, "%.3f,%s,%" PRIu64 ",%u,%" PRIu64 ",%.0f,0x%04x,\"%s\"\n", t, op, lba, n, lat,
			c->ewma, st > 0 ? st : 0, status_desc(st));
		fflush(c->f_slow);
	} else if (st == 0 && n == c->chunk) {
		c->ewma = c->ewma_n ? 0.98 * c->ewma + 0.02 * lat : (double)lat;
		c->ewma_n++;
	}
}

/* ---------- I/O ---------- */

static int ctrl_live(struct ctx *c)
{
	if (!c->ctrl_state)
		return 1;
	char s[32] = "";
	FILE *f = fopen(c->ctrl_state, "r");
	if (!f)
		return -1; /* gone */
	if (!fgets(s, sizeof s, f))
		s[0] = 0;
	fclose(f);
	if (!strncmp(s, "live", 4))
		return 1;
	if (!strncmp(s, "dead", 4) || !strncmp(s, "deleting", 8))
		return -1;
	return 0;
}

/* after a controller error we wait until it is alive again; 0 if alive, -1 if lost */
static int wait_ctrl(struct ctx *c)
{
	uint64_t t0 = now_us();
	while (now_us() - t0 < (uint64_t)c->ctrl_wait_s * 1000000ULL) {
		int l = ctrl_live(c);
		if (l < 0)
			return -1;
		if (l > 0) {
			usleep(200000);
			return 0;
		}
		usleep(250000);
	}
	return -1;
}

static int do_io(struct ctx *c, int wr, uint64_t lba, uint32_t n, unsigned char *buf)
{
	int r;
	size_t len = (size_t)n * c->lbs;
	uint64_t t0 = now_us();
	if (c->posix) {
		ssize_t x = wr ? pwrite(c->fd, buf, len, (off_t)(lba * c->lbs))
			       : pread(c->fd, buf, len, (off_t)(lba * c->lbs));
		r = x == (ssize_t)len ? 0 : (x < 0 ? -errno : -EIO);
	} else {
		struct nvme_passthru_cmd64 cmd;
		memset(&cmd, 0, sizeof cmd);
		cmd.opcode = wr ? 0x01 : 0x02;
		cmd.nsid = c->nsid;
		cmd.addr = (uint64_t)(uintptr_t)buf;
		cmd.data_len = (uint32_t)len;
		cmd.cdw10 = (uint32_t)lba;
		cmd.cdw11 = (uint32_t)(lba >> 32);
		cmd.cdw12 = n - 1;
		cmd.timeout_ms = c->timeout_ms;
		r = ioctl(c->fd, NVME_IOCTL_IO64_CMD, &cmd);
		if (r < 0)
			r = -errno;
	}
	note_latency(c, wr ? "write" : "read", lba, n, now_us() - t0, r);
	return r;
}

static int do_flush(struct ctx *c)
{
	uint64_t t0 = now_us();
	int r;
	if (c->posix) {
		r = fsync(c->fd) ? -errno : 0;
	} else {
		struct nvme_passthru_cmd64 cmd;
		memset(&cmd, 0, sizeof cmd);
		cmd.opcode = 0x00;
		cmd.nsid = c->nsid;
		cmd.timeout_ms = c->timeout_ms;
		r = ioctl(c->fd, NVME_IOCTL_IO64_CMD, &cmd);
		if (r < 0)
			r = -errno;
	}
	note_latency(c, "flush", 0, 0, now_us() - t0, r);
	return r;
}

/*
 * Write/read a range; on error, bisect down to block level.
 * base: the starting LBA of the chunk (buf is aligned to it). Returns: number of failed LBAs.
 */
static uint64_t io_range(struct ctx *c, int wr, uint64_t base, uint64_t lba, uint32_t n)
{
	unsigned char *b = c->buf + (lba - base) * c->lbs;
	int st = do_io(c, wr, lba, n, b);
	if (st == 0) {
		c->consec_ctrl = 0;
		return 0;
	}
	if (is_lost(st) || g_stop) {
		if (is_lost(st)) {
			c->device_lost = 1;
			snprintf(c->abort_reason, sizeof c->abort_reason, "device lost: %s", status_desc(st));
		}
		return 0;
	}
	int sct = st > 0 ? (st >> 8) & 7 : -1, sc = st > 0 ? st & 0xff : -1;
	if ((c->mode == M_ERASECHECK || c->mode == M_READSCAN) && sct == 2 && sc == 0x87) {
		/* deallocated/unwritten: after an erase / on an empty disk this is expected behavior */
		c->unwritten_lbas += n;
		c->erased_ok += n;
		count_status(c, st, n);
		memset(c->failmap + (lba - base), 2, n);
		return 0;
	}
	if (is_ctrl_err(c, st)) {
		c->ctrl_err_events++;
		c->consec_ctrl++;
		count_status(c, st, n);
		if (wait_ctrl(c) < 0) {
			c->device_lost = 1;
			snprintf(c->abort_reason, sizeof c->abort_reason,
				 "controller did not come back after error: %s", status_desc(st));
			return 0;
		}
		if (c->consec_ctrl > MAX_CONSEC_CTRL_ERRORS) {
			c->device_lost = 1;
			snprintf(c->abort_reason, sizeof c->abort_reason,
				 "too many consecutive controller errors (%d)", c->consec_ctrl);
			return 0;
		}
		/* one retry; if that is a controller error too, we record the whole range */
		st = do_io(c, wr, lba, n, b);
		if (st == 0) {
			c->consec_ctrl = 0;
			record(c, "ctrl_error_recovered", lba, n, 0, 0, 0);
			return 0;
		}
		if (is_lost(st)) {
			c->device_lost = 1;
			snprintf(c->abort_reason, sizeof c->abort_reason, "device lost: %s", status_desc(st));
			return 0;
		}
		if (is_ctrl_err(c, st)) {
			c->ctrl_err_events++;
			c->consec_ctrl++;
			count_status(c, st, n);
			if (wait_ctrl(c) < 0) {
				c->device_lost = 1;
				snprintf(c->abort_reason, sizeof c->abort_reason,
					 "controller did not come back after error: %s", status_desc(st));
				return 0;
			}
			record(c, "ctrl_error", lba, n, st, 0, 0);
			c->unbisected_lbas += n;
			memset(c->failmap + (lba - base), 1, n);
			return n;
		}
		/* it turned into a non-controller error: continue on the normal path */
	}
	if (n == 1 || c->bisect_off) {
		c->io_err_events++;
		count_status(c, st, n);
		record(c, n == 1 ? "io_error" : "io_error_unbisected", lba, n, st, 0, 0);
		if (n == 1)
			c->io_err_lbas++;
		else
			c->unbisected_lbas += n;
		memset(c->failmap + (lba - base), 1, n);
		return n;
	}
	uint32_t h = n / 2;
	uint64_t f = io_range(c, wr, base, lba, h);
	if (c->device_lost || g_stop)
		return f;
	return f + io_range(c, wr, base, lba + h, n - h);
}

static int all_bytes(const unsigned char *p, uint32_t len, unsigned char v)
{
	for (uint32_t i = 0; i < len; i++)
		if (p[i] != v)
			return 0;
	return 1;
}

static uint64_t count_bitflips(const unsigned char *a, const unsigned char *b, uint32_t len)
{
	uint64_t bits = 0;
	for (uint32_t off = 0; off < len; off += 8)
		bits += __builtin_popcountll(get64(a + off) ^ get64(b + off));
	return bits;
}

static void check_chunk(struct ctx *c, uint64_t base, uint32_t n)
{
	for (uint32_t i = 0; i < n; i++) {
		if (c->failmap[i])
			continue; /* read error or unwritten: already counted */
		uint64_t lba = base + i;
		unsigned char *got = c->buf + (uint64_t)i * c->lbs;
		int has_magic = !memcmp(got + 16, MAGIC, 8);
		uint64_t glba = get64(got), gseed = get64(got + 8);

		if (c->mode == M_ERASECHECK) {
			if (!has_magic) {
				c->erased_ok++;
				continue;
			}
			/* the pattern of an earlier pass survived the erase */
			c->stale++;
			c->mismatch_lbas++;
			record(c, glba == lba ? "stale_after_erase" : "stale_after_erase_misplaced", lba, 1, 0,
			       (int64_t)(glba - lba), 0);
			continue;
		}

		fill_block(c, c->exp, lba);
		if (!memcmp(got, c->exp, c->lbs))
			continue;
		c->mismatch_lbas++;
		if (all_bytes(got, c->lbs, 0)) {
			c->zeroed++;
			record(c, "zeroed", lba, 1, 0, 0, 0);
		} else if (all_bytes(got, c->lbs, 0xff)) {
			c->ones++;
			record(c, "all_ff", lba, 1, 0, 0, 0);
		} else if (has_magic && gseed == c->seed && glba != lba) {
			c->misdirected++;
			record(c, "misdirected", lba, 1, 0, (int64_t)(glba - lba), 0);
		} else if (has_magic && gseed != c->seed) {
			c->stale++;
			record(c, "stale_old_pass", lba, 1, 0, 0, 0);
		} else {
			uint64_t b = count_bitflips(got, c->exp, c->lbs);
			c->corrupt++;
			c->bitflips += b;
			record(c, "corrupt", lba, 1, 0, 0, b);
		}
	}
}

/* ---------- report ---------- */

static FILE *open_out(struct ctx *c, const char *suffix, const char *mode)
{
	char p[4096];
	snprintf(p, sizeof p, "%s/%s_%s", c->outdir, c->label, suffix);
	FILE *f = fopen(p, mode);
	if (!f) {
		fprintf(stderr, "cannot open: %s: %s\n", p, strerror(errno));
		exit(2);
	}
	return f;
}

static void write_json_atomic(struct ctx *c, const char *suffix, const char *text)
{
	char p[4096], tmp[4200];
	snprintf(p, sizeof p, "%s/%s_%s", c->outdir, c->label, suffix);
	snprintf(tmp, sizeof tmp, "%s.tmp", p);
	FILE *f = fopen(tmp, "w");
	if (!f)
		return;
	fputs(text, f);
	fclose(f);
	rename(tmp, p);
}

static const char *mode_name(enum mode m)
{
	return m == M_WRITE ? "write" : m == M_VERIFY ? "verify" : m == M_ERASECHECK ? "erasecheck" : "readscan";
}

static void progress(struct ctx *c, int force)
{
	double t = elapsed_s(c);
	if (!force && t - c->last_progress < 2.0)
		return;
	c->last_progress = t;
	uint64_t total = c->end - c->start;
	double pct = total ? 100.0 * c->done_lbas / total : 100.0;
	double mbps = t > 0 ? (double)c->done_lbas * c->lbs / 1048576.0 / t : 0;
	double eta = c->done_lbas ? t * (total - c->done_lbas) / c->done_lbas : -1;
	char s[1024];
	snprintf(s, sizeof s,
		 "{\"mode\":\"%s\",\"done_lbas\":%" PRIu64 ",\"total_lbas\":%" PRIu64
		 ",\"lba_size\":%u,\"pct\":%.2f,\"mb_s\":%.1f,\"elapsed_s\":%.1f,\"eta_s\":%.0f,"
		 "\"io_err_lbas\":%" PRIu64 ",\"unbisected_lbas\":%" PRIu64 ",\"mismatch_lbas\":%" PRIu64
		 ",\"ctrl_err_events\":%" PRIu64 ",\"slow_cmds\":%" PRIu64 ",\"last_lat_us\":%.0f}\n",
		 mode_name(c->mode), c->done_lbas, total, c->lbs, pct, mbps, t, eta, c->io_err_lbas,
		 c->unbisected_lbas, c->mismatch_lbas, c->ctrl_err_events, c->slow_cmds, c->ewma);
	write_json_atomic(c, "progress.json", s);
	fflush(c->f_err);
	fflush(c->f_lat);
}

static void summary(struct ctx *c, const char *result)
{
	double t = elapsed_s(c);
	char *s = NULL;
	size_t sl = 0;
	FILE *f = open_memstream(&s, &sl);
	fprintf(f, "{\n  \"tool\": \"nvblk\",\n  \"mode\": \"%s\",\n  \"label\": \"%s\",\n", mode_name(c->mode),
		c->label);
	fprintf(f, "  \"device\": \"%s\",\n  \"access\": \"%s\",\n  \"nsid\": %u,\n", c->dev,
		c->posix ? "posix_odirect" : "nvme_passthru", c->nsid);
	fprintf(f, "  \"seed\": %" PRIu64 ",\n  \"lba_size\": %u,\n  \"ns_lbas\": %" PRIu64 ",\n", c->seed,
		c->lbs, c->nlba);
	fprintf(f, "  \"start_lba\": %" PRIu64 ",\n  \"end_lba\": %" PRIu64 ",\n  \"done_lbas\": %" PRIu64 ",\n",
		c->start, c->end, c->done_lbas);
	fprintf(f, "  \"chunk_lbas\": %u,\n  \"result\": \"%s\",\n  \"abort_reason\": \"%s\",\n", c->chunk,
		result, c->abort_reason);
	fprintf(f, "  \"device_lost\": %s,\n  \"interrupted\": %s,\n", c->device_lost ? "true" : "false",
		g_stop ? "true" : "false");
	fprintf(f, "  \"elapsed_s\": %.1f,\n  \"mb_s\": %.1f,\n", t,
		t > 0 ? (double)c->done_lbas * c->lbs / 1048576.0 / t : 0.0);
	fprintf(f,
		"  \"counts\": {\n    \"io_err_lbas\": %" PRIu64 ",\n    \"io_err_events\": %" PRIu64
		",\n    \"unbisected_err_lbas\": %" PRIu64 ",\n    \"ctrl_err_events\": %" PRIu64
		",\n    \"mismatch_lbas\": %" PRIu64 ",\n    \"corrupt_lbas\": %" PRIu64
		",\n    \"bitflips\": %" PRIu64 ",\n    \"misdirected_lbas\": %" PRIu64
		",\n    \"stale_lbas\": %" PRIu64 ",\n    \"zeroed_lbas\": %" PRIu64
		",\n    \"all_ff_lbas\": %" PRIu64 ",\n    \"unwritten_lbas\": %" PRIu64
		",\n    \"erased_ok_lbas\": %" PRIu64 ",\n    \"error_rows_written\": %" PRIu64
		",\n    \"error_rows_dropped\": %" PRIu64 "\n  },\n",
		c->io_err_lbas, c->io_err_events, c->unbisected_lbas, c->ctrl_err_events, c->mismatch_lbas,
		c->corrupt, c->bitflips, c->misdirected, c->stale, c->zeroed, c->ones, c->unwritten_lbas,
		c->erased_ok, c->err_rows, c->rows_dropped);
	fprintf(f,
		"  \"latency_us\": {\"commands\": %" PRIu64 ", \"mean\": %.0f, \"p50\": %" PRIu64
		", \"p90\": %" PRIu64 ", \"p99\": %" PRIu64 ", \"p999\": %" PRIu64 ", \"max\": %" PRIu64
		", \"baseline_ewma\": %.0f, \"slow_cmds\": %" PRIu64 ", \"slow_total_us\": %" PRIu64
		", \"slow_factor\": %.1f, \"slow_min_us\": %" PRIu64 ", \"flush_max\": %" PRIu64 "},\n",
		c->cmds, c->cmds ? (double)c->lat_sum / c->cmds : 0.0, percentile(c, 0.5), percentile(c, 0.9),
		percentile(c, 0.99), percentile(c, 0.999), c->lat_max, c->ewma, c->slow_cmds, c->slow_time_us,
		c->lat_factor, c->lat_min_us, c->flush_us);
	fprintf(f, "  \"statuses\": [");
	for (int i = 0; i < c->nstc; i++)
		fprintf(f, "%s\n    {\"status\": %d, \"desc\": \"%s\", \"events\": %" PRIu64 ", \"lbas\": %" PRIu64 "}",
			i ? "," : "", c->stc[i].st, status_desc(c->stc[i].st), c->stc[i].events, c->stc[i].lbas);
	fprintf(f, "%s]\n}\n", c->nstc ? "\n  " : "");
	fclose(f);
	write_json_atomic(c, "summary.json", s);
	free(s);
}

/* ---------- setup ---------- */

static unsigned long read_sysfs_ul(dev_t rdev, const char *attr)
{
	char p[256];
	snprintf(p, sizeof p, "/sys/dev/block/%u:%u/queue/%s", major(rdev), minor(rdev), attr);
	FILE *f = fopen(p, "r");
	unsigned long v = 0;
	if (f) {
		if (fscanf(f, "%lu", &v) != 1)
			v = 0;
		fclose(f);
	}
	return v;
}

static void usage(void)
{
	fprintf(stderr,
		"usage: nvblk --mode write|verify|erasecheck|readscan --dev /dev/nvmeXnY --out DIR --label NAME\n"
		"  --seed N          pattern seed (verify needs the same one as write)\n"
		"  --ctrl-state F    e.g. /sys/class/nvme/nvme0/state (watch for controller reset)\n"
		"  --chunk-kb N      chunk size (default 1024, clamped to the device limit)\n"
		"  --timeout-ms N    command timeout (default 60000)\n"
		"  --ctrl-wait N     seconds to wait for the controller to come back (default 180)\n"
		"  --slow-factor X   counts as slow if > X * baseline (default 10)\n"
		"  --slow-min-ms N   anything shorter is never slow (default 20)\n"
		"  --slow-abs-ms N   absolute threshold during warm-up (default 500)\n"
		"  --start LBA --count N   partial run\n"
		"  --max-err-rows N  errors.csv row limit (default 2000000)\n"
		"  --posix           O_DIRECT pread/pwrite instead of passthrough (for testing)\n");
	exit(2);
}

int main(int argc, char **argv)
{
	struct ctx c;
	memset(&c, 0, sizeof c);
	c.chunk_kb = 1024;
	c.timeout_ms = 60000;
	c.ctrl_wait_s = 180;
	c.lat_factor = 10;
	c.lat_min_us = 20000;
	c.lat_abs_us = 500000;
	c.max_err_rows = 2000000;
	c.seed = 1;
	c.label = "run";
	int have_mode = 0;

	static const struct option lo[] = {
		{ "mode", 1, 0, 'm' },	      { "dev", 1, 0, 'd' },	    { "out", 1, 0, 'o' },
		{ "label", 1, 0, 'l' },	      { "seed", 1, 0, 's' },	    { "ctrl-state", 1, 0, 'S' },
		{ "chunk-kb", 1, 0, 'c' },    { "timeout-ms", 1, 0, 't' },  { "ctrl-wait", 1, 0, 'W' },
		{ "slow-factor", 1, 0, 'F' }, { "slow-min-ms", 1, 0, 'M' }, { "slow-abs-ms", 1, 0, 'A' },
		{ "start", 1, 0, 'a' },	      { "count", 1, 0, 'n' },	    { "max-err-rows", 1, 0, 'R' },
		{ "posix", 0, 0, 'P' },	      { 0, 0, 0, 0 }
	};
	int o;
	while ((o = getopt_long(argc, argv, "", lo, NULL)) != -1) {
		switch (o) {
		case 'm':
			have_mode = 1;
			if (!strcmp(optarg, "write")) c.mode = M_WRITE;
			else if (!strcmp(optarg, "verify")) c.mode = M_VERIFY;
			else if (!strcmp(optarg, "erasecheck")) c.mode = M_ERASECHECK;
			else if (!strcmp(optarg, "readscan")) c.mode = M_READSCAN;
			else usage();
			break;
		case 'd': c.dev = optarg; break;
		case 'o': c.outdir = optarg; break;
		case 'l': c.label = optarg; break;
		case 's': c.seed = strtoull(optarg, NULL, 0); break;
		case 'S': c.ctrl_state = optarg; break;
		case 'c': c.chunk_kb = strtoul(optarg, NULL, 0); break;
		case 't': c.timeout_ms = strtoul(optarg, NULL, 0); break;
		case 'W': c.ctrl_wait_s = strtoul(optarg, NULL, 0); break;
		case 'F': c.lat_factor = strtod(optarg, NULL); break;
		case 'M': c.lat_min_us = strtoull(optarg, NULL, 0) * 1000; break;
		case 'A': c.lat_abs_us = strtoull(optarg, NULL, 0) * 1000; break;
		case 'a': c.start = strtoull(optarg, NULL, 0); break;
		case 'n': c.count = strtoull(optarg, NULL, 0); break;
		case 'R': c.max_err_rows = strtoull(optarg, NULL, 0); break;
		case 'P': c.posix = 1; break;
		default: usage();
		}
	}
	if (!have_mode || !c.dev || !c.outdir)
		usage();

	c.fd = open(c.dev, (c.mode == M_WRITE ? O_RDWR : O_RDONLY) | (c.posix ? O_DIRECT : 0));
	if (c.fd < 0) {
		fprintf(stderr, "open %s: %s\n", c.dev, strerror(errno));
		return 3;
	}
	struct stat sb;
	if (fstat(c.fd, &sb) || !S_ISBLK(sb.st_mode)) {
		fprintf(stderr, "%s is not a block device\n", c.dev);
		return 2;
	}
	uint64_t bytes;
	int ssz;
	if (ioctl(c.fd, BLKGETSIZE64, &bytes) || ioctl(c.fd, BLKSSZGET, &ssz)) {
		fprintf(stderr, "size query failed: %s\n", strerror(errno));
		return 2;
	}
	c.lbs = (uint32_t)ssz;
	c.nlba = bytes / c.lbs;
	if (c.lbs < 512 || c.lbs % 8) {
		fprintf(stderr, "unsupported block size: %u\n", c.lbs);
		return 2;
	}
	if (!c.posix) {
		int ns = ioctl(c.fd, NVME_IOCTL_ID);
		if (ns <= 0) {
			fprintf(stderr, "NVME_IOCTL_ID failed (%s) - not NVMe? (--posix)\n", strerror(errno));
			return 2;
		}
		c.nsid = (uint32_t)ns;
	}

	/* chunk size: the smallest of request, max_hw_sectors_kb, max_segments*4K, 65536 LBA */
	uint64_t ckb = c.chunk_kb ? c.chunk_kb : 1024;
	unsigned long mhw = read_sysfs_ul(sb.st_rdev, "max_hw_sectors_kb");
	unsigned long mseg = read_sysfs_ul(sb.st_rdev, "max_segments");
	if (mhw && mhw < ckb)
		ckb = mhw;
	if (!c.posix && mseg && mseg * 4 < ckb)
		ckb = mseg * 4;
	uint64_t cl = ckb * 1024 / c.lbs;
	if (cl < 1)
		cl = 1;
	if (cl > 65536)
		cl = 65536;
	c.chunk = (uint32_t)cl;

	if (c.start >= c.nlba) {
		fprintf(stderr, "start is outside the namespace\n");
		return 2;
	}
	c.end = c.count ? c.start + c.count : c.nlba;
	if (c.end > c.nlba)
		c.end = c.nlba;

	if (posix_memalign((void **)&c.buf, 4096, (size_t)c.chunk * c.lbs) ||
	    posix_memalign((void **)&c.exp, 4096, c.lbs) || !(c.failmap = malloc(c.chunk))) {
		fprintf(stderr, "out of memory\n");
		return 2;
	}
	memset(c.buf, 0, (size_t)c.chunk * c.lbs);

	c.f_err = open_out(&c, "errors.csv", "w");
	c.f_lat = open_out(&c, "latency.csv", "w");
	c.f_slow = open_out(&c, "slow.csv", "w");
	fprintf(c.f_err, "op,kind,lba,count,status,status_desc,extra,bitflips\n");
	fprintf(c.f_lat, "t_s,op,lba,nlb,lat_us,status\n");
	fprintf(c.f_slow, "t_s,op,lba,nlb,lat_us,baseline_us,status,status_desc\n");

	struct sigaction sa;
	memset(&sa, 0, sizeof sa);
	sa.sa_handler = on_signal;
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);

	fprintf(stderr, "nvblk %s: %s nsid=%u lbs=%u lbas=%" PRIu64 " [%" PRIu64 "..%" PRIu64 ") chunk=%u LBA (%s)\n",
		mode_name(c.mode), c.dev, c.nsid, c.lbs, c.nlba, c.start, c.end, c.chunk,
		c.posix ? "O_DIRECT" : "passthrough");

	clock_gettime(CLOCK_MONOTONIC, &c.t0);
	int wr = c.mode == M_WRITE;
	for (uint64_t lba = c.start; lba < c.end && !g_stop && !c.device_lost;) {
		uint32_t n = (uint32_t)((c.end - lba) < c.chunk ? (c.end - lba) : c.chunk);
		memset(c.failmap, 0, n);
		if (wr)
			for (uint32_t i = 0; i < n; i++)
				fill_block(&c, c.buf + (uint64_t)i * c.lbs, lba + i);
		uint64_t failed = io_range(&c, wr, lba, lba, n);
		if (c.device_lost || g_stop)
			break;
		if (failed == n) {
			if (++c.consec_failed_chunks >= MAX_CONSEC_FAILED_CHUNKS && !c.bisect_off) {
				c.bisect_off = 1;
				fprintf(stderr, "\n%d consecutive fully failed chunks: bisection paused\n",
					c.consec_failed_chunks);
			}
		} else if (failed == 0) {
			c.consec_failed_chunks = 0;
			c.bisect_off = 0;
		} else {
			c.consec_failed_chunks = 0;
		}
		if (!wr && c.mode != M_READSCAN)
			check_chunk(&c, lba, n);
		c.bytes_ok += (uint64_t)(n - failed) * c.lbs;
		lba += n;
		c.done_lbas = lba - c.start;
		progress(&c, 0);
	}
	if (wr && !c.device_lost) {
		int st = do_flush(&c);
		if (st) {
			count_status(&c, st, 0);
			record(&c, "flush_error", 0, 0, st, 0, 0);
		}
	}
	flush_pend(&c);
	progress(&c, 1);

	const char *result;
	int rc;
	if (c.device_lost) {
		result = "device_lost";
		rc = 3;
	} else if (g_stop) {
		result = "interrupted";
		snprintf(c.abort_reason, sizeof c.abort_reason, "interrupted (signal)");
		rc = 3;
	} else if (c.io_err_lbas || c.unbisected_lbas || c.mismatch_lbas || c.ctrl_err_events || c.rows_dropped) {
		result = "errors";
		rc = 1;
	} else {
		result = "clean";
		rc = 0;
	}
	summary(&c, result);
	fclose(c.f_err);
	fclose(c.f_lat);
	fclose(c.f_slow);
	fprintf(stderr, "\nnvblk %s done: %s (bad LBAs: %" PRIu64 ", mismatches: %" PRIu64 ", controller errors: %" PRIu64
		", slow: %" PRIu64 ")\n", mode_name(c.mode), result, c.io_err_lbas + c.unbisected_lbas,
		c.mismatch_lbas, c.ctrl_err_events, c.slow_cmds);
	return rc;
}
