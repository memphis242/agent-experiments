/*
 * Black-box behaviour test of the STOR application (spec S1 storage manager base, S2 transfer).
 *
 * STOR_AppMain runs on its own thread against the behaviour model in t5_model.h: the software bus
 * delivers the messages this test queues to the pipes STOR subscribed them on, tables come from test
 * images, files live in the model's in-memory file system, and every transmitted message is captured.
 * Only names, layouts, message IDs, function codes and event IDs from the specification are used;
 * structures are decoded by offset. Case names start with the subsystem they check ("S1 " or "S2 ").
 */
#include "t5_model.h"

extern void STOR_AppMain(void);

/* ---------------------------------------------------------------- constants (spec C.3, S1, S2) */

#define T_CMD_MID      0x18C4u
#define T_SEND_HK_MID  0x18C5u
#define T_WAKEUP_MID   0x18C6u
#define T_HK_MID       0x08C4u
#define T_LIST_MID     0x08C5u
#define T_STATS_MID    0x08C6u
#define T_XFER_TLM_MID 0x08C7u
#define T_SEG_MID      0x1878u
#define T_ACK_MID      0x1879u
#define T_HB_MID       0x18DFu
#define T_MID_A        0x18E0u
#define T_MID_B        0x18E1u
#define T_MID_C        0x18E2u
#define T_MID_D        0x18E3u
#define T_MID_UNKNOWN  0x18EFu

#define CC_NOOP     0
#define CC_RESET    1
#define CC_SET_PRI  2
#define CC_SET_DEC  3
#define CC_SET_THR  4
#define CC_ADD_FILE 5
#define CC_DEL_FILE 6
#define CC_LIST_DIR 7
#define CC_LIST_ALL 8
#define CC_STATS    9
#define CC_CLOSE    10
#define CC_TRANSFER 11
#define CC_ABORT    12

#define PATH_LEN     64
#define MAX_ENT      32
#define TBL_SIZE     400
#define TLM_HDR      16
#define CMD_HDR      8
#define FS_HDR       64
#define HK_PAYLOAD   72
#define SEG_PAYLOAD  604
#define SEG_CMD_SIZE (CMD_HDR + SEG_PAYLOAD)
#define SEG_TLM_SIZE (TLM_HDR + SEG_PAYLOAD)
#define ACK_SIZE     (CMD_HDR + 12)
#define XFER_PAYLOAD 132
#define DATA_LEN     512
#define HB_SIZE      32

#define EID_INIT       1
#define EID_CC_ERR     2
#define EID_NOOP       3
#define EID_RESET      4
#define EID_MID_ERR    5
#define EID_LEN_ERR    6
#define EID_PIPE_ERR   7
#define EID_TBLREG     8
#define EID_TBLLOAD    9
#define EID_TBLVAL     10
#define EID_SUB_ERR    11
#define EID_DIR_ERR    12
#define EID_QUOTA      13
#define EID_FILE_WRITE 15
#define EID_PARAM_OK   16
#define EID_PARAM_ERR  17
#define EID_PATH_ERR   18
#define EID_SRC_ERR    19
#define EID_EXISTS     20
#define EID_FQUOTA     21
#define EID_ADD_OK     22
#define EID_DEL_ERR    23
#define EID_DEL_OK     24
#define EID_LIST_ERR   25
#define EID_LIST_OK    26
#define EID_CLOSE_OK   27
#define EID_XFER_START 29
#define EID_XFER_ERR   30
#define EID_XFER_DONE  31
#define EID_XFER_FAIL  32
#define EID_XFER_ABORT 33
#define EID_XFER_RX    34
#define EID_XFER_RXERR 35
#define EID_XFER_ACT   36

#define AREA_FILE   0
#define AREA_TLM    1
#define ACT_KEEP    0
#define ACT_DELETE  1
#define ACT_ARCHIVE 2
#define ST_OK       0
#define ST_EXISTS   1
#define ST_QUOTA    2
#define ST_SEQUENCE 3
#define ST_CRC      4
#define ST_IO       5
#define ST_PATH     6
#define ST_TIMEOUT  7
#define ST_ABORTED  8
#define ST_ACTFAIL  9

/* ------------------------------------------------------------- table images */

typedef struct { uint8 b[TBL_SIZE]; } tbl_t;

T5_VAR tbl_t tbl_dflt;

T5_FN void tbl_default(tbl_t *t)
{
    memset(t, 0, sizeof(*t));
    strcpy((char *)t->b, "/cf/stor/tlm");
    strcpy((char *)t->b + PATH_LEN, "/cf/stor/files");
    t5_put32(t->b + 128, 262144);
    t5_put32(t->b + 132, 1048576);
    t5_put32(t->b + 136, 16384);
    t->b[140] = 80;
}

T5_FN void tbl_entry(tbl_t *t, int i, uint32 mid, uint8 pri, uint8 dec)
{
    uint8 *e = t->b + 144 + i * 8;
    t5_put32(e, mid);
    e[4] = pri;
    e[5] = dec;
    e[6] = e[7] = 0;
}

T5_FN void tbl_sizes(tbl_t *t, uint32 tq, uint32 fq, uint32 maxf, uint8 thr)
{
    t5_put32(t->b + 128, tq);
    t5_put32(t->b + 132, fq);
    t5_put32(t->b + 136, maxf);
    t->b[140] = thr;
}

/* A quiet table: big telemetry quota, file quota `fq`, 1024-byte packet files, no purging, entries A and B. */
T5_FN void tbl_quiet(tbl_t *t, uint32 fq)
{
    tbl_default(t);
    tbl_sizes(t, 262144, fq, 1024, 100);
    tbl_entry(t, 0, T_MID_A, 10, 1);
    tbl_entry(t, 1, T_MID_B, 20, 1);
}

/* ---------------------------------------------------------------- application, bus */

T5_FN bool boot(const tbl_t *t)
{
    t5_tbl_set_file("StorageTable", t->b, TBL_SIZE);
    return t5_app_start();
}

T5_FN bool send_cmd(uint32 mid, int cc, const void *payload, size_t plen) { return t5_send_cmd(mid, cc, payload, plen); }

T5_FN bool send_simple(int cc) { return send_cmd(T_CMD_MID, cc, NULL, 0); }

T5_FN bool send_wakeup(void)
{
    t5_now_sec += 1;
    return send_cmd(T_WAKEUP_MID, 0, NULL, 0);
}

T5_FN void wakeups(int n)
{
    int i;
    for (i = 0; i < n; i++) send_wakeup();
}

/* A packet of `size` bytes on `mid` carrying `index` at byte 16 and a pattern elsewhere after byte 8. */
T5_FN size_t fill_pkt(uint8 *b, uint32 mid, size_t size, uint32 index)
{
    size_t k;
    memset(b, 0, size);
    t5_wbe16(b, (uint16)mid);
    b[2] = 0xC0;
    t5_wbe16(b + 4, (uint16)(size - 7));
    for (k = 8; k < size; k++) b[k] = (uint8)(index * 3 + k);
    t5_put32(b + 16, index);
    return size;
}

T5_FN bool send_pkt(uint32 mid, size_t size, uint32 index)
{
    fill_pkt(t5_scratch, mid, size, index);
    return t5_deliver(t5_scratch, size);
}

/* Deliver a message to STOR's first pipe whether or not it subscribed it (for the unknown-MID case). */
T5_FN bool deliver_unsubscribed(const uint8 *msg, size_t size)
{
    int p, slot;
    for (p = 0; p < T5_MAX_PIPES && !t5_pipes[p].used; p++) {}
    if (p == T5_MAX_PIPES) { UtAssert_Failed("STOR created no software bus pipe"); return false; }
    slot = t5_pool_next++ % T5_POOL;
    memset(t5_pool[slot], 0, T5_MSG_MAX);
    memcpy(t5_pool[slot], msg, size);
    t5_pipes[p].q[(t5_pipes[p].head + t5_pipes[p].count) % T5_PIPE_Q] = slot;
    t5_pipes[p].count++;
    return t5_kick();
}

T5_FN void pstr(uint8 *dst, const char *s)
{
    memset(dst, 0, PATH_LEN);
    snprintf((char *)dst, PATH_LEN, "%s", s);
}

T5_FN bool cmd_param(int cc, uint32 mid, uint8 value)
{
    uint8 p[8] = {0};
    t5_put32(p, mid);
    p[4] = value;
    return send_cmd(T_CMD_MID, cc, p, sizeof(p));
}

T5_FN bool cmd_thr(uint8 pct)
{
    uint8 p[4] = {0, 0, 0, 0};
    p[0] = pct;
    return send_cmd(T_CMD_MID, CC_SET_THR, p, sizeof(p));
}

T5_FN bool cmd_add(const char *src, const char *dst)
{
    uint8 p[2 * PATH_LEN];
    pstr(p, src);
    pstr(p + PATH_LEN, dst);
    return send_cmd(T_CMD_MID, CC_ADD_FILE, p, sizeof(p));
}

T5_FN bool cmd_path(int cc, const char *path)
{
    uint8 p[PATH_LEN];
    pstr(p, path);
    return send_cmd(T_CMD_MID, cc, p, sizeof(p));
}

T5_FN bool cmd_transfer(uint8 area, uint8 dest, uint8 action, const char *path, const char *destpath)
{
    uint8 p[XFER_PAYLOAD];
    memset(p, 0, sizeof(p));
    p[0] = area;
    p[1] = dest;
    p[2] = action;
    pstr(p + 4, path);
    pstr(p + 4 + PATH_LEN, destpath);
    return send_cmd(T_CMD_MID, CC_TRANSFER, p, sizeof(p));
}

/* ----------------------------------------------------------- telemetry readers */

typedef struct
{
    uint8  cc, cec, thr;
    uint32 used, quota, fused, fquota, stored, decimated, dropped, purged, cycles;
    uint16 fadds, fcount, active, open;
    uint8  xstate, xlast, rxstate;
    uint32 xid;
    uint16 xdone, xfail;
    uint32 xsent, xrecv;
    uint16 frecv, rxerr;
} hk_t;

T5_FN bool get_hk(hk_t *h)
{
    const t5_tx_t *t;
    int before = t5_ntx;
    const uint8 *p;
    memset(h, 0, sizeof(*h));
    if (!send_cmd(T_SEND_HK_MID, 0, NULL, 0)) return false;
    t = t5_last_tx(T_HK_MID, before);
    if (t == NULL)
    {
        UtAssert_Failed("SEND_HK did not transmit a housekeeping packet on STOR_HK_TLM_MID");
        return false;
    }
    UtAssert_True(t->size == TLM_HDR + HK_PAYLOAD, "housekeeping packet is %u bytes, expected %u", (unsigned)t->size,
                  (unsigned)(TLM_HDR + HK_PAYLOAD));
    if (t->size < TLM_HDR + HK_PAYLOAD) return false;
    p = t->bytes + TLM_HDR;
    h->cc = p[0]; h->cec = p[1]; h->thr = p[2];
    h->used = t5_get32(p + 4); h->quota = t5_get32(p + 8); h->fused = t5_get32(p + 12); h->fquota = t5_get32(p + 16);
    h->stored = t5_get32(p + 20); h->decimated = t5_get32(p + 24); h->dropped = t5_get32(p + 28);
    h->purged = t5_get32(p + 32); h->cycles = t5_get32(p + 36);
    h->fadds = t5_get16(p + 40); h->fcount = t5_get16(p + 42); h->active = t5_get16(p + 44); h->open = t5_get16(p + 46);
    h->xstate = p[48]; h->xlast = p[49]; h->rxstate = p[50];
    h->xid = t5_get32(p + 52); h->xdone = t5_get16(p + 56); h->xfail = t5_get16(p + 58);
    h->xsent = t5_get32(p + 60); h->xrecv = t5_get32(p + 64); h->frecv = t5_get16(p + 68); h->rxerr = t5_get16(p + 70);
    return true;
}

typedef struct { uint32 mid; uint8 pri, dec; uint32 rx, st, de, dr; } est_t;

T5_FN bool get_stats(est_t out[MAX_ENT], int *n)
{
    const t5_tx_t *t;
    int i, m = 0;
    int before = t5_ntx;
    if (!send_simple(CC_STATS)) return false;
    t = t5_last_tx(T_STATS_MID, before);
    if (t == NULL)
    {
        UtAssert_Failed("SEND_STATS did not transmit a packet on STOR_STATS_TLM_MID");
        return false;
    }
    UtAssert_True(t->size == TLM_HDR + MAX_ENT * 24, "statistics packet is %u bytes, expected %u", (unsigned)t->size,
                  (unsigned)(TLM_HDR + MAX_ENT * 24));
    if (t->size < TLM_HDR + MAX_ENT * 24) return false;
    for (i = 0; i < MAX_ENT; i++)
    {
        const uint8 *e = t->bytes + TLM_HDR + i * 24;
        if (t5_get32(e) == 0) continue;
        out[m].mid = t5_get32(e); out[m].pri = e[4]; out[m].dec = e[5];
        out[m].rx = t5_get32(e + 8); out[m].st = t5_get32(e + 12); out[m].de = t5_get32(e + 16); out[m].dr = t5_get32(e + 20);
        m++;
    }
    *n = m;
    return true;
}

T5_FN const est_t *est_find(const est_t *e, int n, uint32 mid)
{
    int i;
    for (i = 0; i < n; i++) if (e[i].mid == mid) return &e[i];
    return NULL;
}

typedef struct { uint16 total, index, count; char name[4][PATH_LEN + 1]; uint32 size[4]; } lpkt_t;

T5_FN int get_list(int first_tx, lpkt_t *out, int max)
{
    int i, n = 0, k;
    for (i = first_tx; i < t5_ntx && n < max; i++)
    {
        const uint8 *p;
        if (t5_tx[i].mid != T_LIST_MID) continue;
        UtAssert_True(t5_tx[i].size == TLM_HDR + 8 + 4 * 72, "listing packet is %u bytes, expected %u", (unsigned)t5_tx[i].size,
                      (unsigned)(TLM_HDR + 8 + 4 * 72));
        if (t5_tx[i].size < TLM_HDR + 8 + 4 * 72) continue;
        p = t5_tx[i].bytes + TLM_HDR;
        out[n].total = t5_get16(p); out[n].index = t5_get16(p + 2); out[n].count = t5_get16(p + 4);
        for (k = 0; k < 4; k++)
        {
            memcpy(out[n].name[k], p + 8 + k * 72, PATH_LEN);
            out[n].name[k][PATH_LEN] = 0;
            out[n].size[k] = t5_get32(p + 8 + k * 72 + PATH_LEN);
        }
        n++;
    }
    return n;
}

/* ------------------------------------------------------------- transfer messages (S2.1) */

typedef struct
{
    uint32 tid;
    uint8  src, dst, area;
    uint16 idx, count;
    uint32 fsize, fcrc, offset;
    uint16 dlen;
    char   dest[PATH_LEN + 1];
    const uint8 *data;   /* STOR_XFER_DATA_LEN bytes */
    size_t size;         /* whole packet */
    bool   zero_tail;    /* Data bytes after DataLength are 0 */
} seg_t;

T5_FN uint32 crc32_iso_hdlc(const uint8 *d, size_t n)
{
    uint32 c = 0xFFFFFFFFu;
    size_t i;
    int b;
    for (i = 0; i < n; i++)
    {
        c ^= d[i];
        for (b = 0; b < 8; b++) c = (c & 1u) ? (c >> 1) ^ 0xEDB88320u : (c >> 1);
    }
    return c ^ 0xFFFFFFFFu;
}

T5_FN void parse_seg(const t5_tx_t *t, size_t hdr, seg_t *s)
{
    const uint8 *p = t->bytes + hdr;
    size_t k;
    memset(s, 0, sizeof(*s));
    s->size = t->size;
    if (t->size < hdr + SEG_PAYLOAD) return;
    s->tid = t5_get32(p);
    s->src = p[4]; s->dst = p[5]; s->area = p[6];
    s->idx = t5_get16(p + 8); s->count = t5_get16(p + 10);
    s->fsize = t5_get32(p + 12); s->fcrc = t5_get32(p + 16); s->offset = t5_get32(p + 20);
    s->dlen = t5_get16(p + 24);
    memcpy(s->dest, p + 28, PATH_LEN);
    s->dest[PATH_LEN] = 0;
    s->data = p + 92;
    s->zero_tail = true;
    for (k = s->dlen < DATA_LEN ? s->dlen : DATA_LEN; k < DATA_LEN; k++)
        if (s->data[k] != 0) s->zero_tail = false;
}

/* Segments transmitted on `mid` since `from` (ground: telemetry header, peer: command header). */
T5_FN int collect_segs(uint32 mid, int from, seg_t *out, int max)
{
    int i, n = 0;
    size_t hdr = mid == T_XFER_TLM_MID ? TLM_HDR : CMD_HDR;
    for (i = from; i < t5_ntx && n < max; i++)
    {
        if (t5_tx[i].mid != mid) continue;
        parse_seg(&t5_tx[i], hdr, &out[n]);
        n++;
    }
    return n;
}

typedef struct { uint32 tid; uint8 src, dst, status; uint32 bytes; size_t size; } ack_t;

T5_FN int collect_acks(int from, ack_t *out, int max)
{
    int i, n = 0;
    for (i = from; i < t5_ntx && n < max; i++)
    {
        const uint8 *p;
        if (t5_tx[i].mid != T_ACK_MID) continue;
        p = t5_tx[i].bytes + CMD_HDR;
        memset(&out[n], 0, sizeof(out[n]));
        out[n].size = t5_tx[i].size;
        if (t5_tx[i].size >= ACK_SIZE)
        {
            out[n].tid = t5_get32(p); out[n].src = p[4]; out[n].dst = p[5]; out[n].status = p[6];
            out[n].bytes = t5_get32(p + 8);
        }
        n++;
    }
    return n;
}

/* A segment command from a peer (STOR_XferSegCmd_t, 612 bytes). `data` holds `dlen` bytes. */
T5_FN size_t build_seg(uint8 *b, uint32 tid, uint8 src, uint8 dst, uint16 idx, uint16 count, uint32 fsize, uint32 fcrc,
                       const char *dest, const uint8 *data, uint16 dlen)
{
    static const uint8 zeros[SEG_PAYLOAD];
    uint8 *p = b + CMD_HDR;
    t5_build_cmd(b, T_SEG_MID, 0, zeros, SEG_PAYLOAD);
    t5_put32(p, tid);
    p[4] = src; p[5] = dst; p[6] = AREA_FILE;
    t5_put16(p + 8, idx); t5_put16(p + 10, count);
    t5_put32(p + 12, fsize); t5_put32(p + 16, fcrc); t5_put32(p + 20, (uint32)idx * DATA_LEN);
    t5_put16(p + 24, dlen);
    snprintf((char *)p + 28, PATH_LEN, "%s", dest);
    if (dlen) memcpy(p + 92, data, dlen);
    t5_fill_checksum(b);
    return SEG_CMD_SIZE;
}

T5_FN bool send_seg(uint32 tid, uint8 src, uint8 dst, uint16 idx, uint16 count, uint32 fsize, uint32 fcrc, const char *dest,
                    const uint8 *file)
{
    uint32 off = (uint32)idx * DATA_LEN;
    uint16 dlen = (uint16)(fsize > off ? (fsize - off < DATA_LEN ? fsize - off : DATA_LEN) : 0);
    uint8 buf[SEG_CMD_SIZE];
    build_seg(buf, tid, src, dst, idx, count, fsize, fcrc, dest, dlen ? file + off : NULL, dlen);
    return t5_deliver(buf, SEG_CMD_SIZE);
}

/* Send a whole file to node 1 as a peer would (one segment per delivery). */
T5_FN void send_file(uint32 tid, uint8 src, const char *dest, const uint8 *file, uint32 fsize, uint32 fcrc)
{
    uint16 count = (uint16)(fsize == 0 ? 1 : (fsize + DATA_LEN - 1) / DATA_LEN), i;
    for (i = 0; i < count; i++) send_seg(tid, src, 1, i, count, fsize, fcrc, dest, file);
}

T5_FN bool send_ack(uint32 tid, uint8 src, uint8 dst, uint8 status, uint32 bytes)
{
    uint8 p[12] = {0};
    t5_put32(p, tid);
    p[4] = src; p[5] = dst; p[6] = status;
    t5_put32(p + 8, bytes);
    return send_cmd(T_ACK_MID, 0, p, sizeof(p));
}

/* ------------------------------------------------------------- file helpers */

T5_FN bool exists_file(const char *path)
{
    int i = mf_find(path);
    return i >= 0 && !mf[i].dir;
}

T5_FN size_t size_of_file(const char *path)
{
    int i = mf_find(path);
    return i >= 0 ? mf[i].size : (size_t)-1;
}

T5_FN const uint8 *data_of(const char *path)
{
    static const uint8 none[1] = {0};
    int i = mf_find(path);
    return (i >= 0 && mf[i].data != NULL) ? mf[i].data : none;
}

T5_FN uint32 tlm_bytes_on_disk(void)
{
    int i;
    uint32 s = 0;
    for (i = 0; i < MF_MAX; i++)
        if (mf[i].used && !mf[i].dir && strncmp(mf[i].path, "/cf/stor/tlm/", 13) == 0) s += (uint32)mf[i].size;
    return s;
}

T5_FN int tlm_file_count(void)
{
    int i, n = 0;
    for (i = 0; i < MF_MAX; i++)
        if (mf[i].used && !mf[i].dir && strncmp(mf[i].path, "/cf/stor/tlm/", 13) == 0) n++;
    return n;
}

T5_FN int part_files(void)
{
    int i, n = 0;
    size_t l;
    for (i = 0; i < MF_MAX; i++)
    {
        if (!mf[i].used || mf[i].dir) continue;
        l = strlen(mf[i].path);
        if (l > 5 && strcmp(mf[i].path + l - 5, ".part") == 0) n++;
    }
    return n;
}

/* Packet files (.bin and .arc) of one MID, ordered by name; returns how many. */
T5_FN int mid_files(uint32 mid, int *idx, int max)
{
    char prefix[32];
    int i, k, n = 0;
    snprintf(prefix, sizeof(prefix), "/cf/stor/tlm/tlm_%04X_", (unsigned)mid);
    for (i = 0; i < MF_MAX && n < max; i++)
        if (mf[i].used && !mf[i].dir && strncmp(mf[i].path, prefix, strlen(prefix)) == 0) idx[n++] = i;
    for (i = 0; i < n; i++)
        for (k = i + 1; k < n; k++)
            if (strcmp(mf[idx[k]].path, mf[idx[i]].path) < 0)
            {
                int t = idx[i];
                idx[i] = idx[k];
                idx[k] = t;
            }
    return n;
}

#define SEEN_MAX 128
T5_VAR char seen[SEEN_MAX][NAME_MAX_TEST];
T5_VAR int  nseen;

T5_FN void snap(void)
{
    int i, k;
    for (i = 0; i < MF_MAX; i++)
    {
        if (!mf[i].used || mf[i].dir || strncmp(mf[i].path, "/cf/stor/tlm/", 13) != 0) continue;
        for (k = 0; k < nseen; k++) if (strcmp(seen[k], mf[i].path) == 0) break;
        if (k == nseen && nseen < SEEN_MAX) snprintf(seen[nseen++], NAME_MAX_TEST, "%s", mf[i].path);
    }
}

T5_FN bool send_pkt_snap(uint32 mid, size_t size, uint32 index)
{
    bool ok = send_pkt(mid, size, index);
    snap();
    return ok;
}

T5_FN int count_ev(uint16 id, int from) { return t5_count_ev(id, from); }
T5_FN int last_ev_type(uint16 id) { return t5_last_ev_type(id); }

/* --------------------------------------------------------------- fixture */

static void Setup(void)
{
    t5_setup();
    t5_app_entry = STOR_AppMain;
    t5_node_id = 1;
    nseen = 0;
    tbl_default(&tbl_dflt);
    tbl_entry(&tbl_dflt, 0, 0x0801, 10, 1);
    tbl_entry(&tbl_dflt, 1, 0x0800, 20, 1);
    tbl_entry(&tbl_dflt, 2, 0x0883, 30, 2);
    tbl_entry(&tbl_dflt, 3, 0x0805, 40, 1);
}

static void Teardown(void)
{
    t5_teardown();
}

/* ===================================================================== S1 */

static void Test_StartupAndHousekeeping(void)
{
    hk_t h;
    int k;
    UtAssert_True(boot(&tbl_dflt), "STOR_AppMain starts and waits on its pipe");
    UtAssert_True(count_ev(EID_INIT, 0) == 1, "STOR_INIT_INF_EID (1) emitted once, saw %d", count_ev(EID_INIT, 0));
    UtAssert_True(last_ev_type(EID_INIT) == CFE_EVS_EventType_INFORMATION, "event 1 is INFORMATION");
    k = t5_tbl_find("StorageTable");
    UtAssert_True(t5_n_tbl_register == 1 && k >= 0, "table registered as StorageTable");
    if (k >= 0)
    {
        UtAssert_True(t5_tbl[k].size == TBL_SIZE, "table size %u, expected %u", (unsigned)t5_tbl[k].size, TBL_SIZE);
        UtAssert_True(t5_tbl[k].validate != NULL, "a table validation function is registered");
    }
    UtAssert_True(t5_subscribed(T_CMD_MID) && t5_subscribed(T_SEND_HK_MID) && t5_subscribed(T_WAKEUP_MID),
                  "subscribed to the command, SEND_HK and WAKEUP message IDs");
    UtAssert_True(t5_subscribed(0x0801) && t5_subscribed(0x0800) && t5_subscribed(0x0883) && t5_subscribed(0x0805),
                  "subscribed to each of the four table entries");
    UtAssert_True(t5_subscribed(T_SEG_MID) && t5_subscribed(T_ACK_MID),
                  "subscribed at initialization to the global STOR_XFER_SEG_MID (0x1878) and STOR_XFER_ACK_MID (0x1879)");
    UtAssert_True(mf_find("/cf/stor/tlm") >= 0 && mf_find("/cf/stor/files") >= 0, "both roots created, one level at a time");
    if (!get_hk(&h)) return;
    UtAssert_True(h.quota == 262144 && h.fquota == 1048576 && h.thr == 80 && h.active == 4, "quotas %u/%u threshold %u active %u",
                  h.quota, h.fquota, h.thr, h.active);
    UtAssert_True(h.cc == 0 && h.cec == 0 && h.used == 0 && h.fused == 0 && h.open == 0, "initial housekeeping is zero");
    UtAssert_True(h.xstate == 0 && h.rxstate == 0 && h.xid == 0 && h.xdone == 0 && h.xfail == 0 && h.xsent == 0 && h.xrecv == 0 &&
                      h.frecv == 0 && h.rxerr == 0,
                  "transfer housekeeping is idle and zero before any transfer (XferActiveId %u)", h.xid);
    UtAssert_True(send_simple(CC_NOOP) && count_ev(EID_NOOP, 0) == 1, "NOOP emits event 3");
    UtAssert_True(last_ev_type(EID_NOOP) == CFE_EVS_EventType_INFORMATION, "event 3 is INFORMATION");
    if (get_hk(&h)) UtAssert_UINT32_EQ(h.cc, 1);
}

static void Test_Heartbeat(void)
{
    int before, i, n = 0;
    if (!boot(&tbl_dflt)) return;
    before = t5_ntx;
    wakeups(3);
    for (i = before; i < t5_ntx; i++)
    {
        const t5_tx_t *t = &t5_tx[i];
        if (t->mid != T_HB_MID) continue;
        n++;
        UtAssert_True(t->size == HB_SIZE, "heartbeat is %u bytes, expected %u", (unsigned)t->size, HB_SIZE);
        if (t->size < HB_SIZE) continue;
        UtAssert_True(t5_msg_fcn(t->bytes) == 0, "heartbeat function code %u is 0", (unsigned)t5_msg_fcn(t->bytes));
        UtAssert_True(strncmp((const char *)t->bytes + 8, "STOR", 20) == 0, "heartbeat AppName is STOR ('%.20s')",
                      (const char *)t->bytes + 8);
        UtAssert_True(t5_get32(t->bytes + 28) == (uint32)n, "heartbeat Sequence %u, expected %d", t5_get32(t->bytes + 28), n);
    }
    UtAssert_True(n == 3, "one heartbeat on SFI_HEARTBEAT_MID per 1 Hz WAKEUP: %d for 3 wakeups", n);
}

static void Test_StartupErrors(void)
{
    t5_tbl_set_file("StorageTable", tbl_dflt.b, TBL_SIZE);
    t5_tbl_reg_rc = CFE_TBL_ERR_INVALID_NAME;
    t5_app_start();
    UtAssert_True(count_ev(EID_TBLREG, 0) >= 1, "table registration failure emits event 8");
    t5_app_end();

    Setup();
    t5_tbl_set_file("StorageTable", tbl_dflt.b, TBL_SIZE);
    t5_tbl_load_rc = CFE_TBL_ERR_NO_ACCESS;
    t5_app_start();
    UtAssert_True(count_ev(EID_TBLLOAD, 0) >= 1, "table load failure emits event 9");
    t5_app_end();

    Setup();
    t5_tbl_set_file("StorageTable", tbl_dflt.b, TBL_SIZE);
    t5_sub_rc = CFE_SB_BAD_ARGUMENT;
    t5_app_start();
    UtAssert_True(count_ev(EID_SUB_ERR, 0) >= 1, "subscription failure emits event 11");
    t5_app_end();

    Setup();
    t5_tbl_set_file("StorageTable", tbl_dflt.b, TBL_SIZE);
    fi_mkdir_fail = true;
    t5_app_start();
    UtAssert_True(count_ev(EID_DIR_ERR, 0) >= 1, "root directory creation failure emits event 12");
    t5_app_end();

    Setup();
    t5_tbl_set_file("StorageTable", tbl_dflt.b, TBL_SIZE);
    t5_recv_rc = CFE_SB_PIPE_RD_ERR;
    t5_app_start();
    UtAssert_True(count_ev(EID_PIPE_ERR, 0) >= 1, "pipe read failure emits event 7");
}

T5_FN bool vtbl(const tbl_t *t)
{
    return t5_tbl_validate("StorageTable", t->b) == CFE_SUCCESS;
}

static void Test_TableValidation(void)
{
    tbl_t t, good;
    int i, ev0, k;
    if (!boot(&tbl_dflt)) return;
    k = t5_tbl_find("StorageTable");
    if (k < 0 || t5_tbl[k].validate == NULL)
    {
        UtAssert_Failed("no table validation function registered for StorageTable");
        return;
    }
    tbl_default(&good);
    tbl_entry(&good, 0, T_MID_A, 5, 1);
    UtAssert_True(vtbl(&good), "a valid table is accepted");

    ev0 = t5_nev;
    t = good; t.b[0] = 0;
    UtAssert_True(!vtbl(&t), "empty TelemetryRoot is rejected");
    UtAssert_True(count_ev(EID_TBLVAL, ev0) >= 1, "a rejected table emits STOR_TBL_VAL_ERR_EID (10)");
    UtAssert_True(last_ev_type(EID_TBLVAL) == CFE_EVS_EventType_ERROR, "event 10 is ERROR");
    t = good; t.b[PATH_LEN] = 0;
    UtAssert_True(!vtbl(&t), "empty FileRoot is rejected");
    t = good; memset(t.b, 'x', PATH_LEN);
    UtAssert_True(!vtbl(&t), "unterminated TelemetryRoot is rejected");
    t = good; memset(t.b + PATH_LEN, 'x', PATH_LEN);
    UtAssert_True(!vtbl(&t), "unterminated FileRoot is rejected");
    t = good; tbl_sizes(&t, 0, 1048576, 16384, 80);
    UtAssert_True(!vtbl(&t), "zero TelemetryQuotaBytes is rejected");
    t = good; tbl_sizes(&t, 262144, 0, 16384, 80);
    UtAssert_True(!vtbl(&t), "zero FileQuotaBytes is rejected");
    t = good; tbl_sizes(&t, 262144, 1048576, 1023, 80);
    UtAssert_True(!vtbl(&t), "MaxPacketFileBytes 1023 is rejected");
    t = good; tbl_sizes(&t, 262144, 1048576, 262145, 80);
    UtAssert_True(!vtbl(&t), "MaxPacketFileBytes above TelemetryQuotaBytes is rejected");
    t = good; tbl_sizes(&t, 262144, 1048576, 16384, 0);
    UtAssert_True(!vtbl(&t), "threshold 0 is rejected");
    t = good; tbl_sizes(&t, 262144, 1048576, 16384, 101);
    UtAssert_True(!vtbl(&t), "threshold 101 is rejected");
    t = good; tbl_entry(&t, 1, T_MID_B, 0, 1);
    UtAssert_True(!vtbl(&t), "a used entry with Priority 0 is rejected");
    t = good; tbl_entry(&t, 1, T_MID_B, 5, 0);
    UtAssert_True(!vtbl(&t), "a used entry with Decimation 0 is rejected");
    t = good; tbl_entry(&t, 1, T_MID_A, 7, 2);
    UtAssert_True(!vtbl(&t), "duplicate MsgIds are rejected");
    t = good; tbl_entry(&t, 31, T_MID_A, 7, 2);
    UtAssert_True(!vtbl(&t), "duplicate MsgIds far apart are rejected");

    t = good; tbl_sizes(&t, 1024, 1, 1024, 1);
    UtAssert_True(vtbl(&t), "MaxPacketFileBytes == TelemetryQuotaBytes == 1024 and threshold 1 are accepted");
    t = good; tbl_sizes(&t, 262144, 1048576, 262144, 100);
    UtAssert_True(vtbl(&t), "MaxPacketFileBytes == TelemetryQuotaBytes and threshold 100 are accepted");
    t = good; tbl_entry(&t, 5, 0, 0, 0);
    UtAssert_True(vtbl(&t), "an unused entry (invalid MsgId) with Priority 0 is accepted");
    tbl_default(&t);
    for (i = 0; i < MAX_ENT; i++) tbl_entry(&t, i, 0x0900u + (uint32)i, 255, 255);
    UtAssert_True(vtbl(&t), "32 used entries with priority and decimation 255 are accepted");
    t = good; memset(t.b + 144, 0, 8 * MAX_ENT);
    UtAssert_True(vtbl(&t), "a table with no used entries is accepted");
}

static void Test_DecimationAndStats(void)
{
    tbl_t t;
    est_t e[MAX_ENT];
    hk_t h;
    int n = 0, i, idx[16], nf, found;
    uint32 expect_idx[4] = {0, 3, 6, 9};
    const est_t *a, *b, *c;
    tbl_default(&t);
    tbl_sizes(&t, 262144, 1048576, 16384, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 20, 3);
    tbl_entry(&t, 2, T_MID_C, 30, 255);
    if (!boot(&t)) return;
    for (i = 0; i < 4; i++) send_pkt(T_MID_A, 100, (uint32)i);
    for (i = 0; i < 10; i++) send_pkt(T_MID_B, 100, (uint32)i);
    for (i = 0; i < 10; i++) send_pkt(T_MID_C, 100, (uint32)i);
    if (!get_stats(e, &n)) return;
    a = est_find(e, n, T_MID_A); b = est_find(e, n, T_MID_B); c = est_find(e, n, T_MID_C);
    UtAssert_True(a && b && c, "statistics list all three entries (%d entries)", n);
    if (!(a && b && c)) return;
    UtAssert_True(a->rx == 4 && a->st == 4 && a->de == 0 && a->dr == 0, "decimation 1: %u/%u/%u/%u", a->rx, a->st, a->de, a->dr);
    UtAssert_True(b->rx == 10 && b->st == 4 && b->de == 6, "decimation 3: received %u stored %u decimated %u", b->rx, b->st, b->de);
    UtAssert_True(c->rx == 10 && c->st == 1 && c->de == 9, "decimation 255: received %u stored %u decimated %u", c->rx, c->st, c->de);
    UtAssert_True(a->pri == 10 && b->pri == 20 && c->pri == 30 && a->dec == 1 && b->dec == 3 && c->dec == 255,
                  "priority and decimation reported per entry");
    if (get_hk(&h))
        UtAssert_True(h.stored == 9 && h.decimated == 15, "PacketsStored %u, PacketsDecimated %u", h.stored, h.decimated);
    send_simple(CC_CLOSE);
    nf = mid_files(T_MID_B, idx, 16);
    found = 0;
    for (i = 0; i < nf; i++)
    {
        size_t pos = FS_HDR;
        while (pos + 100 <= mf[idx[i]].size)
        {
            uint32 got = t5_get32(mf[idx[i]].data + pos + 16);
            if (found < 4)
                UtAssert_True(got == expect_idx[found], "decimation 3 stored packet #%u, expected #%u", got, expect_idx[found]);
            found++;
            pos += 100;
        }
    }
    UtAssert_UINT32_EQ((uint32)found, 4);

    UtAssert_True(cmd_param(CC_SET_DEC, T_MID_B, 1) && count_ev(EID_PARAM_OK, 0) == 1, "SET_DECIMATION emits event 16");
    UtAssert_True(last_ev_type(EID_PARAM_OK) == CFE_EVS_EventType_INFORMATION, "event 16 is INFORMATION");
    UtAssert_True(cmd_param(CC_SET_PRI, T_MID_B, 77) && count_ev(EID_PARAM_OK, 0) == 2, "SET_PRIORITY emits event 16");
    send_pkt(T_MID_B, 100, 10);
    send_pkt(T_MID_B, 100, 11);
    if (get_stats(e, &n))
    {
        b = est_find(e, n, T_MID_B);
        UtAssert_True(b && b->pri == 77 && b->dec == 1 && b->rx == 12 && b->st == 6,
                      "after SET_*: priority %u decimation %u received %u stored %u", b ? b->pri : 0, b ? b->dec : 0,
                      b ? b->rx : 0, b ? b->st : 0);
    }
}

T5_FN void check_packet_file(const char *path, uint32 mid, size_t pkt, uint32 first_idx, uint32 count)
{
    int i = mf_find(path);
    uint32 k;
    const CFE_FS_Header_t *h;
    uint8 want[T5_MSG_MAX];
    UtAssert_True(i >= 0, "file %s exists", path);
    if (i < 0) return;
    UtAssert_True(mf[i].size == FS_HDR + (size_t)count * pkt, "%s is %u bytes, expected %u", path, (unsigned)mf[i].size,
                  (unsigned)(FS_HDR + count * pkt));
    if (mf[i].size < FS_HDR) return;
    h = (const CFE_FS_Header_t *)mf[i].data;
    UtAssert_True(h->ContentType == 0x63464531, "%s starts with a cFE file header", path);
    UtAssert_True(h->SubType == 0x5354, "%s sub type 0x%X is STOR_FILE_SUBTYPE 0x5354", path, (unsigned)h->SubType);
    UtAssert_True(strncmp(h->Description, "STOR packet file", sizeof(h->Description)) == 0, "%s description '%.32s'", path,
                  h->Description);
    for (k = 0; k < count && FS_HDR + (k + 1) * pkt <= mf[i].size; k++)
    {
        fill_pkt(want, mid, pkt, first_idx + k);
        UtAssert_True(memcmp(mf[i].data + FS_HDR + k * pkt, want, pkt) == 0, "%s packet %u is stored exactly as received", path, k);
    }
}

static void Test_RolloverAndNaming(void)
{
    tbl_t t;
    hk_t h;
    int i, idx[8], nb;
    uint32 before;
    tbl_default(&t);
    tbl_sizes(&t, 65536, 1048576, 1024, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 20, 1);
    if (!boot(&t)) return;
    for (i = 0; i < 10; i++) send_pkt(T_MID_A, 300, (uint32)i);
    check_packet_file("/cf/stor/tlm/tlm_18E0_00000000.bin", T_MID_A, 300, 0, 3);
    check_packet_file("/cf/stor/tlm/tlm_18E0_00000001.bin", T_MID_A, 300, 3, 3);
    check_packet_file("/cf/stor/tlm/tlm_18E0_00000002.bin", T_MID_A, 300, 6, 3);
    check_packet_file("/cf/stor/tlm/tlm_18E0_00000003.bin", T_MID_A, 300, 9, 1);
    UtAssert_True(!exists_file("/cf/stor/tlm/tlm_18E0_00000004.bin"), "no fifth file");
    if (get_hk(&h))
    {
        UtAssert_True(h.used == 3 * 964 + 364 && h.used == tlm_bytes_on_disk(), "TelemetryBytesUsed %u, expected %u (disk %u)", h.used,
                      3 * 964 + 364, tlm_bytes_on_disk());
        UtAssert_True(h.open == 1 && h.stored == 10, "OpenPacketFiles %u, PacketsStored %u", h.open, h.stored);
        UtAssert_True(n_open_total() == 1, "exactly one OSAL file is still open (%u)", n_open_total());
    }
    for (i = 0; i < 9; i++) send_pkt(T_MID_B, 240, (uint32)i);
    nb = mid_files(T_MID_B, idx, 8);
    UtAssert_True(nb == 3, "%d files for the second MID, expected 3", nb);
    UtAssert_True(exists_file("/cf/stor/tlm/tlm_18E1_00000000.bin") && exists_file("/cf/stor/tlm/tlm_18E1_00000001.bin") &&
                      exists_file("/cf/stor/tlm/tlm_18E1_00000002.bin"),
                  "sequence numbers are per MID: the second MID starts at 0");
    if (nb == 3)
        UtAssert_True(mf[idx[0]].size == 1024 && mf[idx[1]].size == 1024 && mf[idx[2]].size == 304, "exact-fit sizes %u/%u/%u",
                      (unsigned)mf[idx[0]].size, (unsigned)mf[idx[1]].size, (unsigned)mf[idx[2]].size);
    before = n_close;
    UtAssert_True(send_simple(CC_CLOSE) && count_ev(EID_CLOSE_OK, 0) == 1, "CLOSE_FILES emits event 27");
    UtAssert_True(last_ev_type(EID_CLOSE_OK) == CFE_EVS_EventType_INFORMATION, "event 27 is INFORMATION");
    UtAssert_True(n_close - before == 2, "CLOSE_FILES closed %u files, expected 2", n_close - before);
    UtAssert_True(n_open_total() == 0, "no OSAL file remains open");
    if (get_hk(&h)) UtAssert_UINT32_EQ(h.open, 0);
    send_pkt(T_MID_A, 300, 20);
    nb = mid_files(T_MID_A, idx, 8);
    UtAssert_True(nb == 5 && exists_file("/cf/stor/tlm/tlm_18E0_00000004.bin"),
                  "a packet after CLOSE_FILES opens the next sequence number (%d files for the first MID)", nb);
}

static void Test_SequenceAndScan(void)
{
    tbl_t t;
    hk_t h;
    tbl_default(&t);
    tbl_sizes(&t, 65536, 1048576, 1024, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 10, 1);
    mf_mkdir_p("/cf/stor/tlm");
    mf_mkdir_p("/cf/stor/files/x");
    mf_put("/cf/stor/tlm/tlm_18E0_00000007.bin", 1000, 1);
    mf_put("/cf/stor/tlm/tlm_18E0_00000009.arc", 200, 1);
    mf_put("/cf/stor/tlm/tlm_18E1_00000003.bin", 500, 2);
    mf_put("/cf/stor/files/x/y.bin", 700, 3);
    mf_put("/cf/stor/files/z.bin", 300, 4);
    mf_put("/cf/stor/files/top.bin.part", 100, 5);
    mf_put("/cf/stor/files/x/half.bin.part", 400, 6);
    if (!boot(&t)) return;
    UtAssert_True(count_ev(EID_DIR_ERR, 0) == 0, "existing roots are not an error");
    UtAssert_True(!exists_file("/cf/stor/files/top.bin.part") && !exists_file("/cf/stor/files/x/half.bin.part"),
                  "*.part files left in the file area are deleted at startup");
    UtAssert_True(exists_file("/cf/stor/files/z.bin") && exists_file("/cf/stor/files/x/y.bin"), "other files of the file area are kept");
    if (get_hk(&h))
    {
        UtAssert_True(h.used == 1700, "TelemetryBytesUsed %u initialized from the existing .bin and .arc packet files (1700)", h.used);
        UtAssert_True(h.fused == 1000 && h.fcount == 2, "FileBytesUsed %u FileCount %u initialized from the regular files (1000, 2)",
                      h.fused, h.fcount);
    }
    send_pkt(T_MID_A, 300, 0);
    UtAssert_True(exists_file("/cf/stor/tlm/tlm_18E0_00000010.bin"),
                  "sequence continues from the highest existing sequence of the MID, archived .arc files included, plus one");
    send_pkt(T_MID_B, 300, 0);
    UtAssert_True(exists_file("/cf/stor/tlm/tlm_18E1_00000004.bin"), "sequence numbers are per MID");
    if (get_hk(&h)) UtAssert_True(h.used == 1700 + 2 * (64 + 300), "TelemetryBytesUsed %u after two packets", h.used);
}

/* Purge: A (priority 5), B (200), C (100); quota 16384, threshold 50 percent (8192), files of 964 bytes. */
static void Test_PurgeOrder(void)
{
    tbl_t t;
    hk_t h;
    int i, k, nb, nc, na, idx[64];
    bool lowest_first = true, none_lost_high = true;
    int open_after, present_seen = 0;
    tbl_default(&t);
    tbl_sizes(&t, 16384, 1048576, 1024, 50);
    tbl_entry(&t, 0, T_MID_A, 5, 1);
    tbl_entry(&t, 1, T_MID_B, 200, 1);
    tbl_entry(&t, 2, T_MID_C, 100, 1);
    if (!boot(&t)) return;
    for (i = 0; i < 6; i++) send_pkt_snap(T_MID_B, 300, (uint32)i);
    for (i = 0; i < 3; i++) send_pkt_snap(T_MID_C, 300, (uint32)i);
    send_simple(CC_CLOSE);
    UtAssert_UINT32_EQ(n_remove, 0);
    nb = mid_files(T_MID_B, idx, 8);
    nc = mid_files(T_MID_C, idx, 8);
    UtAssert_True(nb == 2 && nc == 1, "setup: %d files for B, %d for C", nb, nc);
    for (i = 0; i < 33; i++) send_pkt_snap(T_MID_A, 300, (uint32)i);
    if (!get_hk(&h)) return;
    UtAssert_True(h.purged > 0 && h.cycles > 0, "FilesPurged %u, PurgeCycles %u", h.purged, h.cycles);
    UtAssert_True(h.used <= 8192, "TelemetryBytesUsed %u above the 8192-byte threshold", h.used);
    UtAssert_True(h.used == tlm_bytes_on_disk(), "TelemetryBytesUsed %u matches the files on disk %u", h.used, tlm_bytes_on_disk());
    UtAssert_True(h.dropped == 0, "nothing dropped while closed files could be purged");
    UtAssert_True(h.purged == n_remove, "FilesPurged %u equals files removed %u", h.purged, n_remove);
    nb = mid_files(T_MID_B, idx, 8);
    nc = mid_files(T_MID_C, idx, 8);
    none_lost_high = nb == 2 && nc == 1;
    UtAssert_True(none_lost_high, "higher-priority files survive while lowest-priority files are purged (B %d, C %d)", nb, nc);
    na = mid_files(T_MID_A, idx, 64);
    UtAssert_True(na < 11 && na >= 1, "%d of 11 A files remain", na);
    for (k = 0; k < nseen; k++)
    {
        int j;
        if (strncmp(seen[k], "/cf/stor/tlm/tlm_18E0_", 22) != 0) continue;
        if (!exists_file(seen[k]))
        {
            for (j = 0; j < nseen; j++)
                if (strncmp(seen[j], "/cf/stor/tlm/tlm_18E0_", 22) == 0 && exists_file(seen[j]) && strcmp(seen[j], seen[k]) < 0)
                    lowest_first = false;
        }
        else
        {
            present_seen++;
        }
    }
    UtAssert_True(lowest_first, "purged files are the lowest sequence numbers of the lowest priority");
    UtAssert_True(present_seen == na, "every remaining A file was seen");
    UtAssert_True(cmd_thr(1), "SET_PURGE_THRESHOLD 1 accepted");
    if (get_hk(&h))
    {
        open_after = h.open;
        UtAssert_True(open_after >= 1, "an open file exists (%d)", open_after);
        UtAssert_True(tlm_file_count() == open_after, "only open files remain after a 1 percent threshold (%d files, %d open)",
                      tlm_file_count(), open_after);
        UtAssert_True(h.used == tlm_bytes_on_disk(), "TelemetryBytesUsed %u matches the files on disk", h.used);
    }
    send_simple(CC_CLOSE);
    send_wakeup();
    UtAssert_True(tlm_file_count() == 0, "WAKEUP purges the files closed since (%d left)", tlm_file_count());
}

/* Archived (.arc) packet files go first, whatever the priority of their MID; threshold 8192, files of 964 bytes. */
static void Test_PurgeArchived(void)
{
    tbl_t t;
    hk_t h;
    int i, idx[64], na;
    tbl_default(&t);
    tbl_sizes(&t, 16384, 1048576, 1024, 50);
    tbl_entry(&t, 0, T_MID_A, 5, 1);
    tbl_entry(&t, 1, T_MID_B, 200, 1);
    mf_mkdir_p("/cf/stor/tlm");
    mf_put("/cf/stor/tlm/tlm_18E1_00000000.arc", 964, 1);
    mf_put("/cf/stor/tlm/tlm_18E1_00000001.arc", 964, 2);
    mf_put("/cf/stor/tlm/tlm_18EE_00000000.arc", 500, 3);
    if (!boot(&t)) return;
    if (!get_hk(&h)) return;
    UtAssert_True(h.used == 2428, "archived files count in TelemetryBytesUsed (%u, expected 2428)", h.used);
    for (i = 0; i < 21; i++) send_pkt(T_MID_A, 300, (uint32)i);
    UtAssert_True(!exists_file("/cf/stor/tlm/tlm_18EE_00000000.arc"),
                  "the archived file of a MID no longer in the table (priority 0) is purged first");
    UtAssert_True(!exists_file("/cf/stor/tlm/tlm_18E1_00000000.arc"), "then the archived file of the lowest sequence");
    UtAssert_True(exists_file("/cf/stor/tlm/tlm_18E1_00000001.arc"), "purging stops once below the threshold");
    na = mid_files(T_MID_A, idx, 64);
    UtAssert_True(na == 7, "no .bin file is purged while archived files can be purged (%d A files, expected 7)", na);
    for (i = 21; i < 24; i++) send_pkt(T_MID_A, 300, (uint32)i);
    UtAssert_True(!exists_file("/cf/stor/tlm/tlm_18E1_00000001.arc"), "the remaining archived file is purged before .bin files");
    na = mid_files(T_MID_A, idx, 64);
    UtAssert_True(na == 8, "every A .bin file is kept while an archived file remained (%d, expected 8)", na);
    for (i = 24; i < 27; i++) send_pkt(T_MID_A, 300, (uint32)i);
    UtAssert_True(!exists_file("/cf/stor/tlm/tlm_18E0_00000000.bin"), "with no archived file left, the lowest .bin goes");
    if (get_hk(&h))
        UtAssert_True(h.used <= 8192 && h.used == tlm_bytes_on_disk() && h.purged == 4,
                      "TelemetryBytesUsed %u (disk %u), FilesPurged %u (expected 4)", h.used, tlm_bytes_on_disk(), h.purged);
}

static void Test_PurgePriorityChange(void)
{
    tbl_t t;
    hk_t h;
    int i, idx[16], nb, na;
    tbl_default(&t);
    tbl_sizes(&t, 16384, 1048576, 1024, 50);
    tbl_entry(&t, 0, T_MID_A, 5, 1);
    tbl_entry(&t, 1, T_MID_B, 200, 1);
    if (!boot(&t)) return;
    for (i = 0; i < 6; i++) send_pkt(T_MID_B, 300, (uint32)i);
    for (i = 0; i < 9; i++) send_pkt(T_MID_A, 300, (uint32)i);
    send_simple(CC_CLOSE);
    UtAssert_UINT32_EQ(n_remove, 0);
    UtAssert_True(cmd_param(CC_SET_PRI, T_MID_B, 1) && cmd_param(CC_SET_PRI, T_MID_A, 250), "priorities reversed at run time");
    UtAssert_True(cmd_thr(20), "threshold lowered to 20 percent (3276 bytes)");
    send_wakeup();
    if (!get_hk(&h)) return;
    nb = mid_files(T_MID_B, idx, 16);
    na = mid_files(T_MID_A, idx, 16);
    UtAssert_True(nb == 0 && na == 3, "after the priority change the old B files (now lowest) are purged first (B %d, A %d)", nb, na);
    UtAssert_True(h.used <= 3276 && h.used == tlm_bytes_on_disk(), "TelemetryBytesUsed %u after the purge", h.used);
}

static void Test_QuotaDrop(void)
{
    tbl_t t;
    hk_t h;
    est_t e[MAX_ENT];
    int i, n = 0, ev0;
    uint32 dropped0;
    tbl_default(&t);
    tbl_sizes(&t, 4096, 1048576, 1024, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 20, 1);
    if (!boot(&t)) return;
    for (i = 0; i < 12; i++) send_pkt(T_MID_A, 300, (uint32)i);
    UtAssert_UINT32_EQ((uint32)count_ev(EID_QUOTA, 0), 0);
    for (i = 12; i < 20; i++) send_pkt(T_MID_A, 300, (uint32)i);
    if (!get_hk(&h)) return;
    UtAssert_True(h.dropped > 0, "PacketsDropped %u after the quota was reached", h.dropped);
    UtAssert_True(h.used <= 4096 && 4096 - h.used < 64 + 300, "TelemetryBytesUsed %u at the quota", h.used);
    UtAssert_True(h.stored + h.dropped == 20, "stored %u + dropped %u == 20", h.stored, h.dropped);
    UtAssert_True(h.stored == 12, "the new file's header counts against the quota: stored %u, expected 12", h.stored);
    UtAssert_True(h.purged == 0, "nothing purged with a 100 percent threshold");
    UtAssert_True(count_ev(EID_QUOTA, 0) == 1, "event 13 emitted once for many drops (%d)", count_ev(EID_QUOTA, 0));
    UtAssert_True(last_ev_type(EID_QUOTA) == CFE_EVS_EventType_ERROR, "event 13 is ERROR");
    if (get_stats(e, &n))
    {
        const est_t *a = est_find(e, n, T_MID_A);
        UtAssert_True(a && a->dr == h.dropped && a->st == h.stored, "per-entry Dropped %u Stored %u", a ? a->dr : 0, a ? a->st : 0);
    }
    for (i = 0; i < 2; i++) send_pkt(T_MID_B, 300, (uint32)i);
    UtAssert_True(count_ev(EID_QUOTA, 0) == 2, "event 13 is reported once per entry (%d)", count_ev(EID_QUOTA, 0));
    UtAssert_True(cmd_thr(30), "SET_PURGE_THRESHOLD 30");
    if (!get_hk(&h)) return;
    UtAssert_True(h.purged > 0 && h.used <= 4096 * 30 / 100, "purge after SET_PURGE_THRESHOLD: FilesPurged %u used %u", h.purged, h.used);
    dropped0 = h.dropped;
    ev0 = count_ev(EID_QUOTA, 0);
    send_pkt(T_MID_A, 300, 100);
    if (get_hk(&h)) UtAssert_True(h.dropped == dropped0, "stores again after space was freed (dropped %u -> %u)", dropped0, h.dropped);
    UtAssert_UINT32_EQ((uint32)count_ev(EID_QUOTA, 0), (uint32)ev0);
    cmd_thr(100);
    for (i = 0; i < 20; i++) send_pkt(T_MID_A, 300, (uint32)(200 + i));
    UtAssert_True(count_ev(EID_QUOTA, 0) == ev0 + 1, "a new drop episode reports event 13 again (%d -> %d)", ev0, count_ev(EID_QUOTA, 0));
}

static void Test_SetParamCommands(void)
{
    hk_t a, b;
    est_t e[MAX_ENT];
    int n = 0;
    const est_t *x;
    if (!boot(&tbl_dflt)) return;
    if (!get_hk(&a)) return;
    UtAssert_True(cmd_param(CC_SET_PRI, 0x0FFF, 5) && cmd_param(CC_SET_DEC, 0x0883, 0) && cmd_param(CC_SET_PRI, 0x0883, 0),
                  "invalid SET_PRIORITY/SET_DECIMATION sent");
    UtAssert_UINT32_EQ((uint32)count_ev(EID_PARAM_ERR, 0), 3);
    UtAssert_True(last_ev_type(EID_PARAM_ERR) == CFE_EVS_EventType_ERROR, "event 17 is ERROR");
    if (get_hk(&b)) UtAssert_True(b.cec == a.cec + 3 && b.cc == a.cc, "error counter %u -> %u, command counter %u -> %u", a.cec, b.cec, a.cc, b.cc);
    UtAssert_True(cmd_thr(0) && cmd_thr(101), "invalid thresholds sent");
    UtAssert_UINT32_EQ((uint32)count_ev(EID_PARAM_ERR, 0), 5);
    if (get_hk(&b)) UtAssert_True(b.thr == 80 && b.cec == a.cec + 5, "threshold stays 80 (%u), errors %u", b.thr, b.cec);
    UtAssert_True(cmd_thr(50), "threshold 50 sent");
    UtAssert_UINT32_EQ((uint32)count_ev(EID_PARAM_OK, 0), 1);
    if (get_hk(&b)) UtAssert_UINT32_EQ(b.thr, 50);
    cmd_thr(100);
    if (get_hk(&b)) UtAssert_UINT32_EQ(b.thr, 100);
    cmd_thr(1);
    if (get_hk(&b)) UtAssert_UINT32_EQ(b.thr, 1);
    cmd_param(CC_SET_PRI, 0x0801, 255);
    cmd_param(CC_SET_DEC, 0x0801, 255);
    if (get_stats(e, &n))
    {
        x = est_find(e, n, 0x0801);
        UtAssert_True(x && x->pri == 255 && x->dec == 255, "priority and decimation 255 are accepted");
    }
}

static void Test_CommandErrors(void)
{
    hk_t a, b;
    uint8 extra[4] = {0};
    uint8 unknown[100];
    const int cc_with_payload[] = {CC_SET_PRI, CC_SET_DEC, CC_SET_THR, CC_ADD_FILE, CC_DEL_FILE, CC_LIST_DIR};
    int i;
    if (!boot(&tbl_dflt)) return;
    get_hk(&a);
    UtAssert_True(send_simple(77) && count_ev(EID_CC_ERR, 0) == 1, "an invalid function code emits event 2");
    UtAssert_True(last_ev_type(EID_CC_ERR) == CFE_EVS_EventType_ERROR, "event 2 is ERROR");
    send_cmd(T_CMD_MID, CC_NOOP, extra, 4);
    send_cmd(T_CMD_MID, CC_RESET, extra, 4);
    send_cmd(T_CMD_MID, CC_LIST_ALL, extra, 4);
    send_cmd(T_CMD_MID, CC_STATS, extra, 4);
    send_cmd(T_CMD_MID, CC_CLOSE, extra, 4);
    for (i = 0; i < 6; i++) send_cmd(T_CMD_MID, cc_with_payload[i], extra, 1);
    UtAssert_True(count_ev(EID_LEN_ERR, 0) == 11, "event 6 emitted %d times for 11 wrongly sized commands", count_ev(EID_LEN_ERR, 0));
    UtAssert_True(last_ev_type(EID_LEN_ERR) == CFE_EVS_EventType_ERROR, "event 6 is ERROR");
    UtAssert_True(count_ev(EID_NOOP, 0) == 0 && count_ev(EID_RESET, 0) == 0, "wrongly sized commands do nothing");
    if (get_hk(&b))
        UtAssert_True(b.cec == (uint8)(a.cec + 12) && b.cc == a.cc,
                      "twelve rejected commands: CommandErrorCounter %u -> %u, CommandCounter %u -> %u", a.cec, b.cec, a.cc, b.cc);
    fill_pkt(unknown, T_MID_UNKNOWN, sizeof(unknown), 0);
    deliver_unsubscribed(unknown, sizeof(unknown));
    UtAssert_True(count_ev(EID_MID_ERR, 0) == 1, "a packet with an unknown message ID emits event 5 (%d)", count_ev(EID_MID_ERR, 0));
    UtAssert_True(last_ev_type(EID_MID_ERR) == CFE_EVS_EventType_ERROR, "event 5 is ERROR");
    if (get_hk(&a))
        UtAssert_True(a.cec == (uint8)(b.cec + 1) && a.cc == b.cc,
                      "the unknown message ID counts as one rejected command (CommandErrorCounter %u -> %u)", b.cec, a.cec);
}

static void Test_CommandCounters(void)
{
    hk_t a, b;
    est_t e[MAX_ENT];
    int n = 0;
    uint8 extra[4] = {0};
    tbl_t t;
    tbl_default(&t);
    tbl_sizes(&t, 262144, 4096, 16384, 80);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    if (!boot(&t)) return;
    mf_put("/cf/src.bin", 100, 5);
    mf_put("/cf/big.bin", 6000, 5);
    get_hk(&a);
    send_simple(CC_NOOP);
    cmd_param(CC_SET_PRI, T_MID_A, 7);
    cmd_param(CC_SET_DEC, T_MID_A, 2);
    cmd_thr(60);
    cmd_add("/cf/src.bin", "x.bin");
    cmd_path(CC_LIST_DIR, "");
    send_simple(CC_LIST_ALL);
    get_stats(e, &n);
    send_simple(CC_CLOSE);
    cmd_path(CC_DEL_FILE, "x.bin");
    get_hk(&b);
    UtAssert_True((uint8)(b.cc - a.cc) == 10 && b.cec == a.cec, "ten accepted commands: CommandCounter %u -> %u, errors %u -> %u", a.cc,
                  b.cc, a.cec, b.cec);
    a = b;
    send_simple(88);
    send_cmd(T_CMD_MID, CC_NOOP, extra, 4);
    cmd_param(CC_SET_PRI, 0x0FFF, 1);
    cmd_thr(0);
    cmd_add("/cf/missing.bin", "y.bin");
    cmd_add("/cf/src.bin", "../y.bin");
    cmd_add("/cf/big.bin", "z.bin");
    cmd_path(CC_DEL_FILE, "nothing");
    cmd_path(CC_LIST_DIR, "nothing");
    get_hk(&b);
    UtAssert_True(b.cc == a.cc && (uint8)(b.cec - a.cec) == 9, "nine rejected commands: CommandCounter %u -> %u, errors %u -> %u", a.cc, b.cc,
                  a.cec, b.cec);
}

static void Test_ResetCounters(void)
{
    tbl_t t;
    hk_t a, b;
    est_t e[MAX_ENT];
    int n = 0, i;
    tbl_default(&t);
    tbl_sizes(&t, 65536, 4096, 1024, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 20, 2);
    if (!boot(&t)) return;
    mf_put("/cf/small.bin", 1000, 9);
    mf_put("/cf/big.bin", 5000, 9);
    for (i = 0; i < 3; i++) send_pkt(T_MID_A, 300, (uint32)i);
    for (i = 0; i < 4; i++) send_pkt(T_MID_B, 300, (uint32)i);
    send_simple(CC_NOOP);
    cmd_param(CC_SET_PRI, T_MID_A, 42);
    cmd_param(CC_SET_PRI, 0x0FFE, 2);
    cmd_add("/cf/small.bin", "keep.bin");
    cmd_add("/cf/big.bin", "toobig.bin");
    get_hk(&a);
    UtAssert_True(a.stored == 5 && a.decimated == 2 && a.fadds == 1 && a.fcount == 1 && a.cec >= 2,
                  "setup counters stored %u decimated %u rejected %u files %u errors %u", a.stored, a.decimated, a.fadds, a.fcount, a.cec);
    UtAssert_True(send_simple(CC_RESET) && count_ev(EID_RESET, 0) == 1, "RESET_COUNTERS emits event 4");
    UtAssert_True(last_ev_type(EID_RESET) == CFE_EVS_EventType_INFORMATION, "event 4 is INFORMATION");
    if (!get_hk(&b)) return;
    UtAssert_True(b.cc == 0 && b.cec == 0 && b.stored == 0 && b.decimated == 0 && b.dropped == 0 && b.purged == 0 && b.cycles == 0 &&
                      b.fadds == 0,
                  "counters are zero after RESET_COUNTERS");
    UtAssert_True(b.used == a.used && b.fused == a.fused && b.fcount == a.fcount && b.open == a.open && b.thr == a.thr &&
                      b.active == a.active,
                  "byte accounting, FileCount, open files and parameters are unchanged");
    if (get_stats(e, &n))
    {
        const est_t *x = est_find(e, n, T_MID_A), *y = est_find(e, n, T_MID_B);
        UtAssert_True(x && y && x->rx == 0 && x->st == 0 && y->rx == 0 && y->st == 0 && y->de == 0 && y->dr == 0,
                      "per-entry counters are zero after RESET_COUNTERS");
        UtAssert_True(x && x->pri == 42 && y && y->dec == 2, "run-time parameters are unchanged");
    }
}

static void Test_FileAddDelete(void)
{
    hk_t h, h2;
    tbl_t t;
    tbl_default(&t);
    tbl_sizes(&t, 262144, 8192, 16384, 80);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    if (!boot(&t)) return;
    mf_put("/cf/src.bin", 3000, 5);
    mf_put("/cf/big.bin", 6000, 6);

    UtAssert_True(cmd_add("/cf/src.bin", "a/b/copy.bin") && count_ev(EID_ADD_OK, 0) == 1, "ADD_FILE emits event 22");
    UtAssert_True(last_ev_type(EID_ADD_OK) == CFE_EVS_EventType_INFORMATION, "event 22 is INFORMATION");
    UtAssert_True(exists_file("/cf/stor/files/a/b/copy.bin") && size_of_file("/cf/stor/files/a/b/copy.bin") == 3000 &&
                      memcmp(data_of("/cf/stor/files/a/b/copy.bin"), data_of("/cf/src.bin"), 3000) == 0,
                  "the copy exists in the file area, creating a and a/b, with identical contents");
    if (!get_hk(&h)) return;
    UtAssert_True(h.fcount == 1 && h.fused == 3000, "FileCount %u FileBytesUsed %u", h.fcount, h.fused);

    UtAssert_True(cmd_add("/cf/src.bin", "a/b/copy.bin") && count_ev(EID_EXISTS, 0) == 1, "existing destination emits event 20");
    UtAssert_True(cmd_add("/cf/missing.bin", "m.bin") && count_ev(EID_SRC_ERR, 0) == 1, "missing source emits event 19");
    UtAssert_True(!exists_file("/cf/stor/files/m.bin"), "no destination for a missing source");
    UtAssert_True(cmd_add("/cf/big.bin", "big.bin") && count_ev(EID_FQUOTA, 0) == 1, "file quota exceeded emits event 21");
    UtAssert_True(!exists_file("/cf/stor/files/big.bin"), "no partial file after the quota rejection");
    UtAssert_True(last_ev_type(EID_FQUOTA) == CFE_EVS_EventType_ERROR, "event 21 is ERROR");
    if (get_hk(&h2))
        UtAssert_True(h2.fadds == 1 && h2.fcount == 1 && h2.fused == 3000 && h2.cec == h.cec + 3,
                      "FileAddsRejected %u, FileCount %u, errors %u -> %u", h2.fadds, h2.fcount, h.cec, h2.cec);
    mf_put("/cf/fit.bin", 5192, 1);
    UtAssert_True(cmd_add("/cf/fit.bin", "fit.bin") && count_ev(EID_ADD_OK, 0) == 2, "a file that exactly fills the quota is accepted");
    mf_put("/cf/one.bin", 1, 1);
    UtAssert_True(cmd_add("/cf/one.bin", "one.bin") && count_ev(EID_FQUOTA, 0) == 2, "one more byte is rejected");

    UtAssert_True(cmd_path(CC_DEL_FILE, "fit.bin") && count_ev(EID_DEL_OK, 0) == 1, "DELETE_FILE emits event 24");
    UtAssert_True(!exists_file("/cf/stor/files/fit.bin"), "the file is removed");
    UtAssert_True(cmd_path(CC_DEL_FILE, "fit.bin") && count_ev(EID_DEL_ERR, 0) == 1, "deleting a missing file emits event 23");
    UtAssert_True(cmd_path(CC_DEL_FILE, "a") && count_ev(EID_DEL_ERR, 0) == 2 && mf_find("/cf/stor/files/a") >= 0,
                  "deleting a directory emits event 23 and keeps it");
    UtAssert_True(last_ev_type(EID_DEL_ERR) == CFE_EVS_EventType_ERROR && last_ev_type(EID_DEL_OK) == CFE_EVS_EventType_INFORMATION,
                  "event types of 23 and 24");
    if (get_hk(&h)) UtAssert_True(h.fcount == 1 && h.fused == 3000, "FileCount %u FileBytesUsed %u after the delete", h.fcount, h.fused);
}

static void Test_PathRejection(void)
{
    hk_t a, b;
    int before_files = 0, now_files = 0, i, pk = 0;
    if (!boot(&tbl_dflt)) return;
    mf_put("/cf/src.bin", 100, 5);
    get_hk(&a);
    for (i = 0; i < MF_MAX; i++) before_files += mf[i].used;
    UtAssert_True(cmd_add("/cf/src.bin", "../x") && cmd_add("/cf/src.bin", "/abs") && cmd_add("/cf/src.bin", "a/../b") &&
                      cmd_add("/cf/src.bin", "..") && cmd_add("/cf/src.bin", "a/b/.."),
                  "bad ADD_FILE destinations sent");
    UtAssert_UINT32_EQ((uint32)count_ev(EID_PATH_ERR, 0), 5);
    UtAssert_True(last_ev_type(EID_PATH_ERR) == CFE_EVS_EventType_ERROR, "event 18 is ERROR");
    UtAssert_True(cmd_path(CC_DEL_FILE, "../x") && cmd_path(CC_DEL_FILE, "/cf/src.bin") && cmd_path(CC_LIST_DIR, "..") &&
                      cmd_path(CC_LIST_DIR, "/cf") && cmd_path(CC_LIST_DIR, "a/../.."),
                  "bad DELETE_FILE and LIST_DIR paths sent");
    UtAssert_UINT32_EQ((uint32)count_ev(EID_PATH_ERR, 0), 10);
    UtAssert_True(exists_file("/cf/src.bin"), "a path starting with / did not delete the real file");
    for (i = 0; i < MF_MAX; i++) now_files += mf[i].used;
    UtAssert_True(now_files == before_files, "no file or directory was created (%d -> %d)", before_files, now_files);
    if (get_hk(&b)) UtAssert_True(b.cec >= a.cec + 5, "ADD_FILE path rejections count as command errors (%u -> %u)", a.cec, b.cec);
    for (i = 0; i < t5_ntx; i++) pk += t5_tx[i].mid == T_LIST_MID;
    UtAssert_UINT32_EQ((uint32)pk, 0);
}

static void Test_Listings(void)
{
    lpkt_t p[8];
    int first, n, i, ev0;
    if (!boot(&tbl_dflt)) return;

    first = t5_ntx;
    UtAssert_True(send_simple(CC_LIST_ALL), "LIST_ALL of an empty file area");
    n = get_list(first, p, 8);
    UtAssert_True(n == 1 && p[0].total == 0 && p[0].count == 0 && p[0].index == 0, "an empty listing is one packet with TotalEntries 0 (%d packets)", n);
    UtAssert_True(count_ev(EID_LIST_OK, 0) == 1 && last_ev_type(EID_LIST_OK) == CFE_EVS_EventType_INFORMATION, "event 26 is INFORMATION");

    mf_put("/cf/stor/files/z.txt", 11, 1);
    mf_mkdir_p("/cf/stor/files/d1");
    mf_put("/cf/stor/files/d1/a.bin", 33, 3);
    mf_mkdir_p("/cf/stor/files/d1/sub");
    mf_put("/cf/stor/files/d1/sub/deep.bin", 44, 4);
    mf_put("/cf/stor/files/d1/b.bin", 55, 5);
    mf_put("/cf/stor/files/y.txt", 22, 2);
    mf_mkdir_p("/cf/stor/files/empty");

    first = t5_ntx;
    send_simple(CC_LIST_ALL);
    n = get_list(first, p, 8);
    UtAssert_True(n == 2, "LIST_ALL of 5 files is %d packets, expected 2", n);
    if (n == 2)
    {
        UtAssert_True(p[0].total == 5 && p[1].total == 5 && p[0].index == 0 && p[1].index == 1 && p[0].count == 4 && p[1].count == 1,
                      "TotalEntries/PacketIndex/EntriesInPacket %u/%u/%u and %u/%u/%u", p[0].total, p[0].index, p[0].count, p[1].total,
                      p[1].index, p[1].count);
        UtAssert_True(strcmp(p[0].name[0], "z.txt") == 0 && p[0].size[0] == 11, "entry 0 is z.txt (%s)", p[0].name[0]);
        UtAssert_True(strcmp(p[0].name[1], "d1/a.bin") == 0 && p[0].size[1] == 33, "entry 1 is d1/a.bin (%s)", p[0].name[1]);
        UtAssert_True(strcmp(p[0].name[2], "d1/sub/deep.bin") == 0 && p[0].size[2] == 44, "entry 2 is d1/sub/deep.bin (%s)", p[0].name[2]);
        UtAssert_True(strcmp(p[0].name[3], "d1/b.bin") == 0 && p[0].size[3] == 55, "entry 3 is d1/b.bin (%s)", p[0].name[3]);
        UtAssert_True(strcmp(p[1].name[0], "y.txt") == 0 && p[1].size[0] == 22, "entry 4 is y.txt (%s)", p[1].name[0]);
    }

    first = t5_ntx;
    cmd_path(CC_LIST_DIR, "");
    n = get_list(first, p, 8);
    UtAssert_True(n == 1 && p[0].total == 4 && p[0].count == 4, "LIST_DIR of the root: %d packets, total %u", n, n ? p[0].total : 0);
    if (n == 1)
        UtAssert_True(strcmp(p[0].name[0], "z.txt") == 0 && strcmp(p[0].name[1], "d1/") == 0 && p[0].size[1] == 0 &&
                          strcmp(p[0].name[2], "y.txt") == 0 && strcmp(p[0].name[3], "empty/") == 0,
                      "root entries in directory order, directories with / and size 0: %s %s %s %s", p[0].name[0], p[0].name[1],
                      p[0].name[2], p[0].name[3]);
    first = t5_ntx;
    cmd_path(CC_LIST_DIR, "d1");
    n = get_list(first, p, 8);
    UtAssert_True(n == 1 && p[0].total == 3 && p[0].count == 3 && strcmp(p[0].name[0], "a.bin") == 0 && strcmp(p[0].name[1], "sub/") == 0 &&
                      strcmp(p[0].name[2], "b.bin") == 0,
                  "LIST_DIR d1 has a.bin, sub/, b.bin");
    first = t5_ntx;
    cmd_path(CC_LIST_DIR, "empty");
    n = get_list(first, p, 8);
    UtAssert_True(n == 1 && p[0].total == 0 && p[0].count == 0, "an empty directory is one packet with TotalEntries 0");

    ev0 = t5_nev;
    first = t5_ntx;
    cmd_path(CC_LIST_DIR, "nosuchdir");
    n = get_list(first, p, 8);
    UtAssert_True(n == 0 && count_ev(EID_LIST_ERR, ev0) == 1, "a missing directory gives event 25 and no packet");
    UtAssert_True(last_ev_type(EID_LIST_ERR) == CFE_EVS_EventType_ERROR, "event 25 is ERROR");

    for (i = 0; i < 9; i++)
    {
        char path[96];
        snprintf(path, sizeof(path), "/cf/stor/files/empty/e%d.bin", i);
        mf_put(path, 100 + (size_t)i, 1);
    }
    first = t5_ntx;
    cmd_path(CC_LIST_DIR, "empty");
    n = get_list(first, p, 8);
    UtAssert_True(n == 3 && p[0].total == 9 && p[2].total == 9 && p[0].count == 4 && p[1].count == 4 && p[2].count == 1 && p[2].index == 2,
                  "nine entries are three packets of 4, 4, 1 (%d packets)", n);
}

static void Test_TableUpdate(void)
{
    tbl_t t;
    hk_t h;
    est_t e[MAX_ENT];
    int n = 0;
    tbl_default(&t);
    tbl_sizes(&t, 262144, 1048576, 16384, 80);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 20, 1);
    if (!boot(&t)) return;
    cmd_param(CC_SET_PRI, T_MID_A, 99);
    cmd_param(CC_SET_DEC, T_MID_A, 9);
    cmd_thr(33);
    tbl_default(&t);
    tbl_sizes(&t, 262144, 1048576, 16384, 70);
    tbl_entry(&t, 0, T_MID_B, 20, 1);
    tbl_entry(&t, 1, T_MID_D, 15, 4);
    UtAssert_True(t5_tbl_submit("StorageTable", t.b) == CFE_SUCCESS, "the new table passes the application's validation");
    send_wakeup();
    send_wakeup();
    if (!get_hk(&h)) return;
    UtAssert_True(t5_subscribed(T_MID_A) == 0, "an entry no longer in the table is unsubscribed");
    UtAssert_True(t5_subscribed(T_MID_D) > 0, "a new table entry is subscribed");
    UtAssert_True(t5_subscribed(T_SEG_MID) > 0 && t5_subscribed(T_ACK_MID) > 0, "the transfer subscriptions stay after a table update");
    UtAssert_True(h.thr == 70, "the table's threshold 70 replaces the run-time value (%u)", h.thr);
    UtAssert_True(h.active == 2, "ActiveEntries %u after the table update", h.active);
    if (get_stats(e, &n))
    {
        const est_t *d = est_find(e, n, T_MID_D);
        UtAssert_True(d && d->pri == 15 && d->dec == 4, "the new entry carries the table's priority and decimation");
        UtAssert_True(est_find(e, n, T_MID_A) == NULL, "the removed entry is gone from the statistics");
    }
    cmd_param(CC_SET_PRI, T_MID_B, 123);
    tbl_entry(&t, 0, T_MID_B, 21, 2);
    UtAssert_True(t5_tbl_submit("StorageTable", t.b) == CFE_SUCCESS, "second table accepted");
    send_wakeup();
    send_wakeup();
    if (get_stats(e, &n))
    {
        const est_t *b = est_find(e, n, T_MID_B);
        UtAssert_True(b && b->pri == 21 && b->dec == 2, "a table update overrides run-time priority (%u) and decimation (%u)",
                      b ? b->pri : 0, b ? b->dec : 0);
    }
}

static void Test_IdleClose(void)
{
    tbl_t t;
    hk_t h;
    int i, idx[8];
    tbl_default(&t);
    tbl_sizes(&t, 65536, 1048576, 1024, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 20, 1);
    if (!boot(&t)) return;
    send_pkt(T_MID_A, 300, 0);
    for (i = 0; i < 4; i++) send_wakeup();
    send_pkt(T_MID_B, 300, 0);
    for (i = 0; i < 5; i++) send_wakeup();
    if (get_hk(&h)) UtAssert_True(h.open == 2, "both files stay open for less than 10 idle seconds (%u open at 9 s)", h.open);
    send_wakeup();
    send_wakeup();
    if (get_hk(&h)) UtAssert_True(h.open == 1, "the file idle for 10 s is closed on WAKEUP, the other stays open (%u open)", h.open);
    for (i = 0; i < 4; i++) send_wakeup();
    if (get_hk(&h)) UtAssert_True(h.open == 0, "all idle files are closed (%u open)", h.open);
    UtAssert_True(n_open_total() == 0, "OSAL files are closed");
    send_pkt(T_MID_A, 300, 1);
    UtAssert_True(mid_files(T_MID_A, idx, 8) == 2, "a packet after the idle close goes to a new file");
}

static void Test_WriteFailures(void)
{
    tbl_t t;
    hk_t h;
    int ev0;
    tbl_default(&t);
    tbl_sizes(&t, 65536, 1048576, 1024, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    if (!boot(&t)) return;
    fi_create_fail = 1;
    send_pkt(T_MID_A, 300, 0);
    UtAssert_True(count_ev(EID_FILE_WRITE, 0) >= 1, "a packet file that cannot be created emits event 15");
    UtAssert_True(last_ev_type(EID_FILE_WRITE) == CFE_EVS_EventType_ERROR, "event 15 is ERROR");
    send_pkt(T_MID_A, 300, 1);
    if (get_hk(&h))
        UtAssert_True(h.used == tlm_bytes_on_disk(), "TelemetryBytesUsed %u matches the files on disk %u after the failure", h.used,
                      tlm_bytes_on_disk());
    fi_write_short = true;
    ev0 = count_ev(EID_FILE_WRITE, 0);
    send_pkt(T_MID_A, 300, 2);
    UtAssert_True(count_ev(EID_FILE_WRITE, 0) > ev0, "a failed packet write emits event 15");
    fi_write_short = false;
    UtAssert_True(get_hk(&h), "STOR still answers SEND_HK after write failures");
}

/* ===================================================================== S2 */

/* Checks every field of the segments of a whole file against the source bytes. */
T5_FN void check_segments(const seg_t *s, int n, uint32 tid, uint8 dst, uint8 area, const uint8 *file, uint32 fsize,
                          const char *dest, size_t pkt_size)
{
    int count = fsize == 0 ? 1 : (int)((fsize + DATA_LEN - 1) / DATA_LEN), i;
    uint32 crc = crc32_iso_hdlc(file, fsize);
    UtAssert_True(n == count, "%d segments for a %u-byte file, expected %d", n, fsize, count);
    for (i = 0; i < n && i < count; i++)
    {
        uint32 off = (uint32)i * DATA_LEN;
        uint16 dlen = (uint16)(fsize == 0 ? 0 : (fsize - off < DATA_LEN ? fsize - off : DATA_LEN));
        UtAssert_True(s[i].size == pkt_size, "segment %d is %u bytes, expected the full structure size %u", i, (unsigned)s[i].size,
                      (unsigned)pkt_size);
        if (s[i].size < pkt_size) continue;
        UtAssert_True(s[i].tid == tid && s[i].src == 1 && s[i].dst == dst && s[i].area == area,
                      "segment %d TransferId %u SrcNode %u DstNode %u Area %u, expected %u/1/%u/%u", i, s[i].tid, s[i].src, s[i].dst,
                      s[i].area, tid, dst, area);
        UtAssert_True(s[i].idx == (uint16)i && s[i].count == (uint16)count, "segment %d SegmentIndex %u SegmentCount %u", i, s[i].idx,
                      s[i].count);
        UtAssert_True(s[i].fsize == fsize && s[i].fcrc == crc,
                      "segment %d FileSize %u FileCrc 0x%08X, expected %u 0x%08X (CRC-32/ISO-HDLC)", i, s[i].fsize, s[i].fcrc, fsize, crc);
        UtAssert_True(s[i].offset == off && s[i].dlen == dlen, "segment %d Offset %u DataLength %u, expected %u %u", i, s[i].offset,
                      s[i].dlen, off, dlen);
        UtAssert_True(strcmp(s[i].dest, dest) == 0, "segment %d DestPath '%s', expected '%s'", i, s[i].dest, dest);
        UtAssert_True(dlen == 0 || memcmp(s[i].data, file + off, dlen) == 0, "segment %d carries the file bytes at its offset", i);
        UtAssert_True(s[i].zero_tail, "segment %d: unused Data bytes are 0", i);
    }
}

static void Test_XferRejections(void)
{
    tbl_t t;
    hk_t a, b;
    uint8 p[XFER_PAYLOAD];
    uint8 extra[4] = {0};
    int ev0, before;
    tbl_quiet(&t, 1048576);
    mf_mkdir_p("/cf/stor/files/dir");
    mf_put("/cf/stor/files/ok.bin", 700, 1);
    if (!boot(&t)) return;
    send_pkt(T_MID_A, 300, 0); /* opens tlm_18E0_00000000.bin */
    if (!get_hk(&a)) return;
    ev0 = t5_nev;
    before = t5_ntx;
    cmd_transfer(2, 0, ACT_KEEP, "ok.bin", "");                       /* Area out of range */
    cmd_transfer(AREA_FILE, 0, 3, "ok.bin", "");                      /* SuccessAction out of range */
    cmd_transfer(AREA_FILE, 1, ACT_KEEP, "ok.bin", "in/x.bin");       /* Destination is the own node */
    cmd_transfer(AREA_FILE, 0, ACT_KEEP, "../ok.bin", "");            /* path rules */
    cmd_transfer(AREA_FILE, 0, ACT_KEEP, "/cf/stor/files/ok.bin", "");
    cmd_transfer(AREA_FILE, 0, ACT_KEEP, "missing.bin", "");          /* source missing */
    cmd_transfer(AREA_FILE, 0, ACT_KEEP, "dir", "");                  /* source a directory */
    cmd_transfer(AREA_TLM, 0, ACT_KEEP, "notes.bin", "");             /* not a packet file name */
    cmd_transfer(AREA_TLM, 0, ACT_KEEP, "tlm_18E0_0000000.bin", "");
    cmd_transfer(AREA_TLM, 0, ACT_KEEP, "tlm_18E0_00000005.bin", ""); /* packet file missing */
    cmd_transfer(AREA_TLM, 0, ACT_KEEP, "tlm_18E0_00000000.bin", ""); /* open packet file */
    cmd_transfer(AREA_FILE, 2, ACT_KEEP, "ok.bin", "");               /* peer: empty DestPath */
    cmd_transfer(AREA_FILE, 2, ACT_KEEP, "ok.bin", "../in.bin");      /* peer: DestPath breaks the path rules */
    cmd_transfer(AREA_FILE, 2, ACT_KEEP, "ok.bin", "/in.bin");
    memset(p, 0, sizeof(p));
    p[0] = AREA_FILE;
    p[1] = 2;
    p[2] = ACT_KEEP;
    pstr(p + 4, "ok.bin");
    memset(p + 4 + PATH_LEN, 'q', PATH_LEN);                          /* peer: DestPath unterminated */
    send_cmd(T_CMD_MID, CC_TRANSFER, p, sizeof(p));
    UtAssert_True(count_ev(EID_XFER_ERR, ev0) == 15, "each of 15 invalid TRANSFER commands emits STOR_XFER_ERR_EID (30): %d",
                  count_ev(EID_XFER_ERR, ev0));
    UtAssert_True(last_ev_type(EID_XFER_ERR) == CFE_EVS_EventType_ERROR, "event 30 is ERROR");
    UtAssert_True(count_ev(EID_XFER_START, ev0) == 0, "no rejected TRANSFER starts a transfer");
    if (get_hk(&b))
        UtAssert_True((uint8)(b.cec - a.cec) == 15 && b.cc == a.cc && b.xstate == 0 && b.xid == 0,
                      "rejections: CommandErrorCounter +%u (15), CommandCounter %u -> %u, XferState %u, XferActiveId %u",
                      (unsigned)(uint8)(b.cec - a.cec), a.cc, b.cc, b.xstate, b.xid);
    wakeups(2);
    UtAssert_True(t5_tx_count(T_XFER_TLM_MID, before) == 0 && t5_tx_count(T_SEG_MID, before) == 0, "rejected transfers send no segment");

    ev0 = t5_nev;
    send_cmd(T_CMD_MID, CC_TRANSFER, p, sizeof(p) - 1);
    send_cmd(T_CMD_MID, CC_ABORT, extra, 4);
    UtAssert_True(count_ev(EID_LEN_ERR, ev0) == 2, "a wrongly sized TRANSFER or ABORT_TRANSFER emits event 6 (%d)", count_ev(EID_LEN_ERR, ev0));

    mf_put("/cf/stor/files/big.bin", 20 * DATA_LEN, 2);
    ev0 = t5_nev;
    UtAssert_True(cmd_transfer(AREA_FILE, 0, ACT_KEEP, "big.bin", "") && count_ev(EID_XFER_START, ev0) == 1,
                  "a valid TRANSFER emits STOR_XFER_START_INF_EID (29)");
    UtAssert_True(last_ev_type(EID_XFER_START) == CFE_EVS_EventType_INFORMATION, "event 29 is INFORMATION");
    if (!get_hk(&a)) return;
    UtAssert_True(a.xstate == 1 && a.xid != 0, "accepted: XferState %u (SENDING 1), XferActiveId %u", a.xstate, a.xid);
    cmd_transfer(AREA_FILE, 0, ACT_KEEP, "ok.bin", "");
    UtAssert_True(count_ev(EID_XFER_ERR, ev0) == 1, "TRANSFER while a transfer is active emits event 30");
    if (get_hk(&b)) UtAssert_True(b.xid == a.xid && b.xstate == 1 && b.cec == a.cec + 1, "the active transfer is unchanged (id %u -> %u)", a.xid, b.xid);
}

static void Test_XferGround(void)
{
    tbl_t t;
    hk_t h;
    seg_t s[8];
    int before, n, ev0;
    uint8 copy[1300];
    tbl_quiet(&t, 1048576);
    mf_mkdir_p("/cf/stor/files/g");
    mf_put("/cf/stor/files/g/data.bin", 1300, 7);
    if (!boot(&t)) return;
    memcpy(copy, data_of("/cf/stor/files/g/data.bin"), sizeof(copy));
    before = t5_ntx;
    ev0 = t5_nev;
    UtAssert_True(cmd_transfer(AREA_FILE, 0, ACT_KEEP, "g/data.bin", "") && count_ev(EID_XFER_START, ev0) == 1,
                  "TRANSFER to the ground accepted");
    UtAssert_True(t5_tx_count(T_XFER_TLM_MID, before) == 0, "no segment before the first WAKEUP after acceptance");
    if (get_hk(&h)) UtAssert_True(h.xstate == 1 && h.xid == 1, "XferState %u (1), XferActiveId %u (1 for the first TRANSFER)", h.xstate, h.xid);
    send_wakeup();
    send_wakeup();
    n = collect_segs(T_XFER_TLM_MID, before, s, 8);
    check_segments(s, n, 1, 0, AREA_FILE, copy, 1300, "g/data.bin", SEG_TLM_SIZE);
    UtAssert_True(t5_tx_count(T_SEG_MID, before) == 0, "a ground transfer sends nothing on STOR_XFER_SEG_MID");
    if (!get_hk(&h)) return;
    UtAssert_True(h.xdone == 1 && h.xfail == 0 && h.xlast == ST_OK && h.xstate == 0,
                  "completed after the last segment: XfersCompleted %u XfersFailed %u XferLastStatus %u XferState %u", h.xdone, h.xfail,
                  h.xlast, h.xstate);
    UtAssert_True(h.xsent == 3 && h.xid == 1, "XferSegmentsSent %u (3), XferActiveId %u keeps the last id (1)", h.xsent, h.xid);
    UtAssert_True(count_ev(EID_XFER_DONE, ev0) == 1 && last_ev_type(EID_XFER_DONE) == CFE_EVS_EventType_INFORMATION,
                  "STOR_XFER_DONE_INF_EID (31) once, INFORMATION");
    UtAssert_True(exists_file("/cf/stor/files/g/data.bin") && memcmp(data_of("/cf/stor/files/g/data.bin"), copy, 1300) == 0 &&
                      h.fcount == 1 && h.fused == 1300,
                  "KEEP leaves the source and accounting unchanged");
    before = t5_ntx;
    cmd_transfer(AREA_FILE, 0, ACT_KEEP, "g/data.bin", "");
    wakeups(2);
    n = collect_segs(T_XFER_TLM_MID, before, s, 8);
    UtAssert_True(n == 3 && s[0].tid == 2, "the second TRANSFER uses TransferId 2 (%u)", n ? s[0].tid : 0);
}

static void Test_XferPacing(void)
{
    tbl_t t;
    hk_t h;
    seg_t *s = calloc(32, sizeof(seg_t));
    uint8 *copy = malloc(21 * DATA_LEN);
    int before, n1, n2, n3, n, i;
    uint32 size = 20 * DATA_LEN + 100;
    tbl_quiet(&t, 1048576);
    mf_put("/cf/stor/files/long.bin", size, 3);
    mf_put("/cf/stor/files/empty.bin", 0, 0);
    if (boot(&t))
    {
        memcpy(copy, data_of("/cf/stor/files/long.bin"), size);
        before = t5_ntx;
        cmd_transfer(AREA_FILE, 0, ACT_KEEP, "long.bin", "");
        send_wakeup();
        n1 = t5_tx_count(T_XFER_TLM_MID, before);
        send_wakeup();
        n2 = t5_tx_count(T_XFER_TLM_MID, before);
        send_wakeup();
        n3 = t5_tx_count(T_XFER_TLM_MID, before);
        UtAssert_True(n1 == 8 && n2 == 16 && n3 == 21, "up to 8 segments per WAKEUP: %d, %d, %d after 1, 2, 3 wakeups (8, 16, 21)", n1,
                      n2, n3);
        n = collect_segs(T_XFER_TLM_MID, before, s, 32);
        for (i = 0; i < n; i++)
            if (s[i].idx != (uint16)i) { UtAssert_Failed("segment %d has SegmentIndex %u: not in index order", i, s[i].idx); break; }
        check_segments(s, n, 1, 0, AREA_FILE, copy, size, "long.bin", SEG_TLM_SIZE);
        if (get_hk(&h)) UtAssert_True(h.xsent == 21 && h.xdone == 1, "XferSegmentsSent %u (21), XfersCompleted %u", h.xsent, h.xdone);
        before = t5_ntx;
        cmd_transfer(AREA_FILE, 0, ACT_KEEP, "empty.bin", "");
        wakeups(2);
        n = collect_segs(T_XFER_TLM_MID, before, s, 32);
        check_segments(s, n, 2, 0, AREA_FILE, copy, 0, "empty.bin", SEG_TLM_SIZE);
        if (get_hk(&h)) UtAssert_True(h.xdone == 2 && h.xlast == ST_OK, "the empty-file transfer completes (XfersCompleted %u)", h.xdone);
    }
    free(s);
    free(copy);
}

static void Test_XferPeer(void)
{
    tbl_t t;
    hk_t h;
    seg_t s[8];
    uint8 copy[1100];
    int before, n, ev0;
    tbl_quiet(&t, 1048576);
    mf_put("/cf/stor/files/p.bin", 1100, 4);
    if (!boot(&t)) return;
    memcpy(copy, data_of("/cf/stor/files/p.bin"), sizeof(copy));
    before = t5_ntx;
    ev0 = t5_nev;
    cmd_transfer(AREA_FILE, 2, ACT_KEEP, "p.bin", "in/copy.bin");
    send_wakeup();
    n = collect_segs(T_SEG_MID, before, s, 8);
    check_segments(s, n, 1, 2, AREA_FILE, copy, 1100, "in/copy.bin", SEG_CMD_SIZE);
    UtAssert_True(t5_tx_count(T_XFER_TLM_MID, before) == 0, "a peer transfer sends nothing on STOR_XFER_TLM_MID");
    if (n > 0)
        UtAssert_True(t5_msg_is_cmd(t5_last_tx(T_SEG_MID, before)->bytes) && t5_msg_fcn(t5_last_tx(T_SEG_MID, before)->bytes) == 0,
                      "peer segments are commands with function code 0");
    if (!get_hk(&h)) return;
    UtAssert_True(h.xstate == 2, "after the last segment XferState is AWAITING_ACK 2 (%u)", h.xstate);
    UtAssert_True(h.xrecv == 0 && h.rxstate == 0 && part_files() == 0 && !exists_file("/cf/stor/files/in/copy.bin"),
                  "STOR's own segments on its local bus are ignored without counting (XferSegmentsReceived %u, RxState %u)", h.xrecv,
                  h.rxstate);
    send_ack(9, 2, 1, ST_OK, 1100); /* other TransferId */
    send_ack(1, 3, 1, ST_OK, 1100); /* SrcNode is not the destination */
    send_ack(1, 2, 3, ST_OK, 1100); /* DstNode is not this node */
    if (get_hk(&h)) UtAssert_True(h.xstate == 2 && h.xdone == 0 && h.xfail == 0, "mismatched acknowledgements are ignored (XferState %u)", h.xstate);
    send_ack(1, 2, 1, ST_OK, 1100);
    if (!get_hk(&h)) return;
    UtAssert_True(h.xdone == 1 && h.xstate == 0 && h.xlast == ST_OK, "an OK acknowledgement completes it: XfersCompleted %u XferState %u",
                  h.xdone, h.xstate);
    UtAssert_True(count_ev(EID_XFER_DONE, ev0) == 1, "STOR_XFER_DONE_INF_EID on completion");
}

static void Test_XferAckFailures(void)
{
    tbl_t t;
    hk_t h;
    int before, ev0, i;
    tbl_quiet(&t, 1048576);
    mf_put("/cf/stor/files/n.bin", 600, 4);
    if (!boot(&t)) return;
    ev0 = t5_nev;
    cmd_transfer(AREA_FILE, 2, ACT_DELETE, "n.bin", "in/n.bin");
    send_wakeup();
    send_ack(1, 2, 1, ST_EXISTS, 0);
    if (!get_hk(&h)) return;
    UtAssert_True(h.xfail == 1 && h.xdone == 0 && h.xlast == ST_EXISTS && h.xstate == 0,
                  "a NAK fails the transfer with its status: XfersFailed %u XferLastStatus %u XferState %u", h.xfail, h.xlast, h.xstate);
    UtAssert_True(count_ev(EID_XFER_FAIL, ev0) == 1 && last_ev_type(EID_XFER_FAIL) == CFE_EVS_EventType_ERROR,
                  "STOR_XFER_FAIL_ERR_EID (32) once, ERROR");
    UtAssert_True(exists_file("/cf/stor/files/n.bin") && h.fcount == 1, "no success action after a failure: the source stays");
    before = t5_ntx;
    cmd_transfer(AREA_FILE, 2, ACT_DELETE, "n.bin", "in/n.bin");
    send_wakeup();
    UtAssert_True(t5_tx_count(T_SEG_MID, before) == 2, "two segments sent");
    for (i = 0; i < 8; i++) send_wakeup();
    if (get_hk(&h)) UtAssert_True(h.xstate == 2 && h.xfail == 1, "still AWAITING_ACK 8 wakeups after the last segment (state %u)", h.xstate);
    for (i = 0; i < 3; i++) send_wakeup();
    if (!get_hk(&h)) return;
    UtAssert_True(h.xfail == 2 && h.xlast == ST_TIMEOUT && h.xstate == 0 && h.xid == 2,
                  "no acknowledgement within 10 wakeups fails it with TIMEOUT 7: XfersFailed %u XferLastStatus %u", h.xfail, h.xlast);
    UtAssert_True(exists_file("/cf/stor/files/n.bin"), "the source is kept after a timeout");
    UtAssert_True(t5_tx_count(T_SEG_MID, before) == 2, "segments are not resent while waiting");
}

static void Test_XferAbort(void)
{
    tbl_t t;
    hk_t a, h;
    int before, ev0, n;
    tbl_quiet(&t, 1048576);
    mf_put("/cf/stor/files/a.bin", 30 * DATA_LEN, 4);
    mf_put("/cf/stor/files/s.bin", 100, 5);
    if (!boot(&t)) return;
    if (!get_hk(&a)) return;
    ev0 = t5_nev;
    send_simple(CC_ABORT);
    UtAssert_True(count_ev(EID_XFER_ERR, ev0) == 1, "ABORT_TRANSFER with no transfer active is rejected with event 30");
    if (get_hk(&h)) UtAssert_True(h.cec == a.cec + 1, "and counted as a command error");
    before = t5_ntx;
    cmd_transfer(AREA_FILE, 0, ACT_DELETE, "a.bin", "");
    send_wakeup();
    ev0 = t5_nev;
    send_simple(CC_ABORT);
    UtAssert_True(count_ev(EID_XFER_ABORT, ev0) == 1 && count_ev(EID_XFER_FAIL, ev0) == 0,
                  "ABORT_TRANSFER while SENDING emits STOR_XFER_ABORT_INF_EID (33) instead of XFER_FAIL_ERR");
    n = t5_tx_count(T_XFER_TLM_MID, before);
    wakeups(3);
    UtAssert_True(t5_tx_count(T_XFER_TLM_MID, before) == n, "no segment after the abort (%d then %d)", n, t5_tx_count(T_XFER_TLM_MID, before));
    if (!get_hk(&h)) return;
    UtAssert_True(h.xfail == 1 && h.xlast == ST_ABORTED && h.xstate == 0, "aborted: XfersFailed %u XferLastStatus %u (8) XferState %u",
                  h.xfail, h.xlast, h.xstate);
    UtAssert_True(exists_file("/cf/stor/files/a.bin"), "no success action after an abort");
    cmd_transfer(AREA_FILE, 2, ACT_KEEP, "s.bin", "in/s.bin");
    send_wakeup();
    ev0 = t5_nev;
    send_simple(CC_ABORT);
    if (get_hk(&h))
        UtAssert_True(h.xfail == 2 && h.xlast == ST_ABORTED && h.xstate == 0 && count_ev(EID_XFER_ABORT, ev0) == 1,
                      "ABORT_TRANSFER while AWAITING_ACK aborts it (XfersFailed %u)", h.xfail);
}

static void Test_XferActionsFile(void)
{
    tbl_t t;
    hk_t h;
    int ev0;
    tbl_quiet(&t, 1048576);
    mf_mkdir_p("/cf/stor/files/d");
    mf_put("/cf/stor/files/d/del.bin", 1000, 1);
    mf_put("/cf/stor/files/d/arc.bin", 600, 2);
    mf_put("/cf/stor/files/d/twice.bin", 300, 3);
    mf_put("/cf/stor/files/d/twice.bin.arc", 50, 4);
    if (!boot(&t)) return;
    if (!get_hk(&h)) return;
    UtAssert_True(h.fcount == 4 && h.fused == 1950, "setup FileCount %u FileBytesUsed %u", h.fcount, h.fused);
    cmd_transfer(AREA_FILE, 0, ACT_DELETE, "d/del.bin", "");
    wakeups(2);
    if (!get_hk(&h)) return;
    UtAssert_True(!exists_file("/cf/stor/files/d/del.bin") && h.fcount == 3 && h.fused == 950 && h.xdone == 1,
                  "DELETE removes the source with DELETE_FILE accounting: FileCount %u FileBytesUsed %u", h.fcount, h.fused);
    cmd_transfer(AREA_FILE, 0, ACT_ARCHIVE, "d/arc.bin", "");
    wakeups(2);
    if (!get_hk(&h)) return;
    UtAssert_True(!exists_file("/cf/stor/files/d/arc.bin") && exists_file("/cf/stor/files/d/arc.bin.arc") &&
                      size_of_file("/cf/stor/files/d/arc.bin.arc") == 600,
                  "MARK_ARCHIVED renames <Path> to <Path>.arc in the file area");
    UtAssert_True(h.fcount == 3 && h.fused == 950 && h.xdone == 2 && h.xlast == ST_OK,
                  "renaming changes neither FileCount %u nor FileBytesUsed %u", h.fcount, h.fused);
    ev0 = t5_nev;
    cmd_transfer(AREA_FILE, 0, ACT_ARCHIVE, "d/twice.bin", "");
    wakeups(2);
    if (!get_hk(&h)) return;
    UtAssert_True(h.xdone == 3 && h.xlast == ST_ACTFAIL && h.xstate == 0,
                  "a failed success action still completes: XfersCompleted %u, XferLastStatus %u (ACTION_FAILED 9)", h.xdone, h.xlast);
    UtAssert_True(count_ev(EID_XFER_ACT, ev0) == 1 && last_ev_type(EID_XFER_ACT) == CFE_EVS_EventType_ERROR,
                  "STOR_XFER_ACTION_ERR_EID (36) once, ERROR");
    UtAssert_True(exists_file("/cf/stor/files/d/twice.bin") && size_of_file("/cf/stor/files/d/twice.bin.arc") == 50,
                  "MARK_ARCHIVED fails when <Path>.arc exists and leaves both files");
}

static void Test_XferActionsTlm(void)
{
    tbl_t t;
    hk_t a, h;
    seg_t s[8];
    int i, before, n;
    uint8 copy[1024];
    tbl_quiet(&t, 1048576);
    if (!boot(&t)) return;
    for (i = 0; i < 6; i++) send_pkt(T_MID_A, 300, (uint32)i); /* two files of 964 bytes */
    send_simple(CC_CLOSE);
    if (!get_hk(&a)) return;
    memset(copy, 0, sizeof(copy));
    if (size_of_file("/cf/stor/tlm/tlm_18E0_00000000.bin") == 964) memcpy(copy, data_of("/cf/stor/tlm/tlm_18E0_00000000.bin"), 964);
    before = t5_ntx;
    cmd_transfer(AREA_TLM, 0, ACT_ARCHIVE, "tlm_18E0_00000000.bin", "");
    wakeups(2);
    n = collect_segs(T_XFER_TLM_MID, before, s, 8);
    check_segments(s, n, 1, 0, AREA_TLM, copy, 964, "tlm_18E0_00000000.bin", SEG_TLM_SIZE);
    if (!get_hk(&h)) return;
    UtAssert_True(!exists_file("/cf/stor/tlm/tlm_18E0_00000000.bin") && exists_file("/cf/stor/tlm/tlm_18E0_00000000.arc"),
                  "MARK_ARCHIVED renames tlm_MMMM_NNNNNNNN.bin to tlm_MMMM_NNNNNNNN.arc");
    UtAssert_True(h.used == a.used && h.xdone == 1, "archiving keeps TelemetryBytesUsed (%u -> %u)", a.used, h.used);
    cmd_transfer(AREA_TLM, 0, ACT_ARCHIVE, "tlm_18E0_00000000.arc", "");
    wakeups(2);
    if (get_hk(&h))
        UtAssert_True(exists_file("/cf/stor/tlm/tlm_18E0_00000000.arc") && h.xdone == 2 && h.xlast == ST_OK,
                      "an archived file can be transferred, and MARK_ARCHIVED leaves a .arc name unchanged");
    cmd_transfer(AREA_TLM, 0, ACT_DELETE, "tlm_18E0_00000001.bin", "");
    wakeups(2);
    if (!get_hk(&h)) return;
    UtAssert_True(!exists_file("/cf/stor/tlm/tlm_18E0_00000001.bin") && h.used == a.used - 964 && h.used == tlm_bytes_on_disk(),
                  "DELETE of a packet file subtracts its bytes (%u -> %u)", a.used, h.used);
    UtAssert_True(h.purged == a.purged && h.cycles == a.cycles, "without the purge counters (FilesPurged %u -> %u)", a.purged, h.purged);
    send_pkt(T_MID_A, 300, 50);
    UtAssert_True(exists_file("/cf/stor/tlm/tlm_18E0_00000002.bin"), "packet file sequence numbers continue after archive and delete");
}

static void Test_XferReadError(void)
{
    tbl_t t;
    hk_t h;
    int ev0;
    tbl_quiet(&t, 1048576);
    mf_put("/cf/stor/files/r.bin", 3 * DATA_LEN, 4);
    if (!boot(&t)) return;
    ev0 = t5_nev;
    cmd_transfer(AREA_FILE, 0, ACT_DELETE, "r.bin", "");
    fi_read_fail = 50;
    wakeups(2);
    fi_read_fail = 0;
    if (!get_hk(&h)) return;
    UtAssert_True(h.xfail == 1 && h.xlast == ST_IO && h.xstate == 0 && h.xdone == 0,
                  "a source read error fails the transfer with IO 5: XfersFailed %u XferLastStatus %u", h.xfail, h.xlast);
    UtAssert_True(count_ev(EID_XFER_FAIL, ev0) == 1, "STOR_XFER_FAIL_ERR_EID for the read error");
    UtAssert_True(exists_file("/cf/stor/files/r.bin"), "no success action after the failure");
}

static void Test_XferReceive(void)
{
    tbl_t t;
    hk_t h;
    ack_t k[4];
    uint8 file[1300];
    uint32 crc, i;
    int before, n, ev0;
    tbl_quiet(&t, 1048576);
    if (!boot(&t)) return;
    for (i = 0; i < sizeof(file); i++) file[i] = (uint8)(i * 13 + 5);
    crc = crc32_iso_hdlc(file, sizeof(file));
    before = t5_ntx;
    ev0 = t5_nev;
    send_seg(5, 2, 1, 0, 3, sizeof(file), crc, "rx/new.bin", file);
    UtAssert_True(exists_file("/cf/stor/files/rx/new.bin.part") && !exists_file("/cf/stor/files/rx/new.bin"),
                  "index 0 creates <FileRoot>/<DestPath>.part, with intermediate directories");
    if (get_hk(&h))
        UtAssert_True(h.rxstate == 1 && h.xrecv == 1 && h.fused == 0 && h.fcount == 0,
                      "while receiving: RxState %u (1), XferSegmentsReceived %u, .part bytes not counted (FileBytesUsed %u)", h.rxstate,
                      h.xrecv, h.fused);
    send_seg(5, 2, 1, 1, 3, sizeof(file), crc, "rx/new.bin", file);
    send_seg(5, 2, 1, 2, 3, sizeof(file), crc, "rx/new.bin", file);
    UtAssert_True(exists_file("/cf/stor/files/rx/new.bin") && !exists_file("/cf/stor/files/rx/new.bin.part") &&
                      size_of_file("/cf/stor/files/rx/new.bin") == sizeof(file) &&
                      memcmp(data_of("/cf/stor/files/rx/new.bin"), file, sizeof(file)) == 0,
                  "after the last segment the .part file is renamed to DestPath with the file's bytes");
    n = collect_acks(before, k, 4);
    UtAssert_True(n == 1, "one acknowledgement on STOR_XFER_ACK_MID (%d)", n);
    if (n >= 1)
    {
        UtAssert_True(k[0].size == ACK_SIZE, "acknowledgement is %u bytes, expected %u", (unsigned)k[0].size, ACK_SIZE);
        UtAssert_True(k[0].tid == 5 && k[0].src == 1 && k[0].dst == 2 && k[0].status == ST_OK && k[0].bytes == sizeof(file),
                      "acknowledgement TransferId %u SrcNode %u DstNode %u Status %u BytesReceived %u, expected 5/1/2/OK/1300", k[0].tid,
                      k[0].src, k[0].dst, k[0].status, k[0].bytes);
    }
    UtAssert_True(count_ev(EID_XFER_RX, ev0) == 1 && last_ev_type(EID_XFER_RX) == CFE_EVS_EventType_INFORMATION,
                  "STOR_XFER_RX_INF_EID (34) once, INFORMATION");
    if (get_hk(&h))
        UtAssert_True(h.rxstate == 0 && h.frecv == 1 && h.xrecv == 3 && h.fcount == 1 && h.fused == sizeof(file) && h.rxerr == 0,
                      "received: RxState %u FilesReceived %u XferSegmentsReceived %u FileCount %u FileBytesUsed %u", h.rxstate, h.frecv,
                      h.xrecv, h.fcount, h.fused);
    before = t5_ntx;
    send_seg(6, 2, 1, 0, 1, 0, 0, "rx/zero.bin", file);
    n = collect_acks(before, k, 4);
    UtAssert_True(exists_file("/cf/stor/files/rx/zero.bin") && size_of_file("/cf/stor/files/rx/zero.bin") == 0 && n == 1 &&
                      k[0].status == ST_OK,
                  "a zero-length file is received from its single segment");
}

/* One NAK case: returns the acknowledgement status (or -1 without one) and checks the common NAK effects. */
T5_FN int nak_case(const char *what, int ev0, int before, uint16 rxerr_before)
{
    ack_t k[4];
    hk_t h;
    int n = collect_acks(before, k, 4);
    UtAssert_True(n == 1, "%s: one acknowledgement (%d)", what, n);
    UtAssert_True(count_ev(EID_XFER_RXERR, ev0) >= 1, "%s: STOR_XFER_RX_ERR_EID (35)", what);
    UtAssert_True(part_files() == 0, "%s: the partial file is deleted", what);
    if (get_hk(&h))
        UtAssert_True(h.rxerr == rxerr_before + 1 && h.rxstate == 0, "%s: RxErrors %u -> %u, RxState %u", what, rxerr_before, h.rxerr,
                      h.rxstate);
    return n >= 1 ? k[0].status : -1;
}

static void Test_XferReceiveNaks(void)
{
    tbl_t t;
    hk_t h;
    uint8 file[1200];
    uint32 crc, i;
    int before, ev0, st;
    uint16 rxe = 0;
    tbl_quiet(&t, 4096);
    mf_mkdir_p("/cf/stor/files/rx");
    mf_put("/cf/stor/files/rx/have.bin", 10, 1);
    if (!boot(&t)) return;
    for (i = 0; i < sizeof(file); i++) file[i] = (uint8)(i * 7 + 1);
    crc = crc32_iso_hdlc(file, sizeof(file));
#define NAK(label, expect, stmt)                                                           \
    do                                                                                     \
    {                                                                                      \
        if (get_hk(&h)) rxe = h.rxerr;                                                     \
        before = t5_ntx;                                                                   \
        ev0 = t5_nev;                                                                      \
        stmt;                                                                              \
        st = nak_case(label, ev0, before, rxe);                                            \
        UtAssert_True(st == (expect), "%s: NAK status %d, expected %d", label, st, expect); \
    } while (0)
    NAK("DestPath with ..", ST_PATH, send_seg(10, 2, 1, 0, 3, sizeof(file), crc, "../out.bin", file));
    NAK("DestPath starting with /", ST_PATH, send_seg(11, 2, 1, 0, 3, sizeof(file), crc, "/abs.bin", file));
    NAK("final component plus .part longer than 31 characters", ST_PATH,
        send_seg(12, 2, 1, 0, 3, sizeof(file), crc, "rx/abcdefghijklmnopqrstuvw.bin", file));
    NAK("existing destination", ST_EXISTS, send_seg(13, 2, 1, 0, 3, sizeof(file), crc, "rx/have.bin", file));
    NAK("file quota", ST_QUOTA, send_seg(14, 2, 1, 0, 9, 4096, crc, "rx/big.bin", file));
    NAK("out-of-sequence segment", ST_SEQUENCE,
        { send_seg(15, 2, 1, 0, 3, sizeof(file), crc, "rx/seq.bin", file); send_seg(15, 2, 1, 2, 3, sizeof(file), crc, "rx/seq.bin", file); });
    NAK("CRC mismatch", ST_CRC, send_file(16, 2, "rx/crc.bin", file, sizeof(file), crc ^ 1u));
    UtAssert_True(!exists_file("/cf/stor/files/rx/crc.bin") && !exists_file("/cf/stor/files/rx/seq.bin"), "no file after a NAK");
    fi_write_short = true;
    NAK("write failure", ST_IO, send_file(17, 2, "rx/io.bin", file, sizeof(file), crc));
    fi_write_short = false;
    fi_rename_fail = 1;
    NAK("rename failure", ST_IO, send_file(18, 2, "rx/io2.bin", file, sizeof(file), crc));
    fi_rename_fail = 0;
#undef NAK
    send_file(19, 2, "rx/abcdefghijklmnopqrstuv.bin", file, 100, crc32_iso_hdlc(file, 100));
    UtAssert_True(exists_file("/cf/stor/files/rx/abcdefghijklmnopqrstuv.bin"),
                  "a final component of 26 characters (31 with .part) is accepted");
    if (get_hk(&h)) UtAssert_True(h.frecv == 1 && h.fcount == 2, "FilesReceived %u FileCount %u after the accepted file", h.frecv, h.fcount);
}

static void Test_XferReceiveTimeoutRestart(void)
{
    tbl_t t;
    hk_t h;
    ack_t k[4];
    uint8 file[1500];
    uint32 crc, i;
    int before, ev0, n;
    tbl_quiet(&t, 1048576);
    if (!boot(&t)) return;
    for (i = 0; i < sizeof(file); i++) file[i] = (uint8)(i * 3 + 9);
    crc = crc32_iso_hdlc(file, sizeof(file));
    before = t5_ntx;
    ev0 = t5_nev;
    send_seg(30, 2, 1, 0, 3, sizeof(file), crc, "rx/t.bin", file);
    for (i = 0; i < 8; i++) send_wakeup();
    if (get_hk(&h))
        UtAssert_True(h.rxstate == 1 && exists_file("/cf/stor/files/rx/t.bin.part"),
                      "still receiving 8 wakeups after the last segment (RxState %u)", h.rxstate);
    for (i = 0; i < 3; i++) send_wakeup();
    if (!get_hk(&h)) return;
    UtAssert_True(h.rxstate == 0 && h.rxerr == 1 && part_files() == 0,
                  "no segment for 10 wakeups aborts the inbound transfer: RxState %u RxErrors %u, partial file deleted", h.rxstate, h.rxerr);
    UtAssert_True(count_ev(EID_XFER_RXERR, ev0) == 1, "the timeout emits STOR_XFER_RX_ERR_EID");
    UtAssert_True(collect_acks(before, k, 4) == 0, "the timeout sends no acknowledgement");
    before = t5_ntx;
    send_seg(31, 2, 1, 0, 3, sizeof(file), crc, "rx/old.bin", file);
    send_seg(31, 2, 1, 1, 3, sizeof(file), crc, "rx/old.bin", file);
    send_seg(32, 2, 1, 0, 3, sizeof(file), crc, "rx/new.bin", file);
    UtAssert_True(!exists_file("/cf/stor/files/rx/old.bin.part"), "index 0 of a new transfer deletes the incomplete partial file");
    send_seg(31, 2, 1, 2, 3, sizeof(file), crc, "rx/old.bin", file);
    send_seg(32, 3, 1, 1, 3, sizeof(file), crc, "rx/new.bin", file);
    send_seg(32, 2, 1, 1, 3, sizeof(file), crc, "rx/new.bin", file);
    send_seg(32, 2, 1, 2, 3, sizeof(file), crc, "rx/new.bin", file);
    n = collect_acks(before, k, 4);
    UtAssert_True(n == 1 && k[0].tid == 32 && k[0].status == ST_OK, "only the new transfer is acknowledged, OK (%d acknowledgements)", n);
    UtAssert_True(exists_file("/cf/stor/files/rx/new.bin") && memcmp(data_of("/cf/stor/files/rx/new.bin"), file, sizeof(file)) == 0 &&
                      !exists_file("/cf/stor/files/rx/old.bin"),
                  "the new file is complete; nothing of the discarded one remains");
    if (get_hk(&h))
        UtAssert_True(h.xrecv == 1 + 2 + 3 && h.frecv == 1, "XferSegmentsReceived %u (6: ignored segments are not counted), FilesReceived %u",
                      h.xrecv, h.frecv);
}

static void Test_XferAddressedElsewhere(void)
{
    tbl_t t;
    hk_t a, h;
    uint8 file[600];
    uint32 i;
    int before;
    tbl_quiet(&t, 1048576);
    if (!boot(&t)) return;
    for (i = 0; i < sizeof(file); i++) file[i] = (uint8)i;
    if (!get_hk(&a)) return;
    before = t5_ntx;
    for (i = 0; i < 2; i++) send_seg(40, 2, 3, (uint16)i, 2, sizeof(file), crc32_iso_hdlc(file, sizeof(file)), "rx/else.bin", file);
    send_ack(1, 2, 3, ST_OK, 600);
    if (!get_hk(&h)) return;
    UtAssert_True(h.xrecv == a.xrecv && h.rxstate == 0 && h.rxerr == a.rxerr && h.frecv == a.frecv && part_files() == 0 &&
                      !exists_file("/cf/stor/files/rx/else.bin"),
                  "segments whose DstNode is not this node are ignored without counting");
    UtAssert_True(t5_tx_count(T_ACK_MID, before) == 0, "and not acknowledged");
    UtAssert_True(h.cec == a.cec && h.cc == a.cc && count_ev(EID_MID_ERR, 0) == 0, "and are not command errors");
}

static void Test_XferReset(void)
{
    tbl_t t;
    hk_t a, b;
    uint8 file[700];
    uint32 i;
    tbl_quiet(&t, 1048576);
    mf_put("/cf/stor/files/x.bin", 900, 4);
    if (!boot(&t)) return;
    for (i = 0; i < sizeof(file); i++) file[i] = (uint8)(i + 3);
    cmd_transfer(AREA_FILE, 0, ACT_KEEP, "x.bin", "");
    wakeups(2);
    cmd_transfer(AREA_FILE, 2, ACT_KEEP, "x.bin", "in/x.bin");
    send_wakeup();
    send_ack(2, 2, 1, ST_EXISTS, 0);
    send_file(50, 2, "rx/r.bin", file, sizeof(file), crc32_iso_hdlc(file, sizeof(file)));
    send_file(51, 2, "rx/bad.bin", file, sizeof(file), crc32_iso_hdlc(file, sizeof(file)) ^ 1u);
    if (!get_hk(&a)) return;
    UtAssert_True(a.xdone == 1 && a.xfail == 1 && a.xsent == 4 && a.xrecv == 4 && a.frecv == 1 && a.rxerr == 1,
                  "setup transfer counters %u/%u/%u/%u/%u/%u", a.xdone, a.xfail, a.xsent, a.xrecv, a.frecv, a.rxerr);
    send_simple(CC_RESET);
    if (!get_hk(&b)) return;
    UtAssert_True(b.xdone == 0 && b.xfail == 0 && b.xsent == 0 && b.xrecv == 0 && b.frecv == 0 && b.rxerr == 0,
                  "RESET_COUNTERS zeroes XfersCompleted, XfersFailed, XferSegmentsSent, XferSegmentsReceived, FilesReceived, RxErrors");
    UtAssert_True(b.xid == a.xid && b.xid == 2 && b.xlast == a.xlast && b.xstate == 0 && b.fcount == a.fcount && b.fused == a.fused,
                  "but not XferActiveId (%u), XferLastStatus, transfer state or file accounting", b.xid);
}

void UtTest_Setup(void)
{
    UtAssert_True(sizeof(CFE_MSG_TelemetryHeader_t) == TLM_HDR, "telemetry header is %u bytes", (unsigned)sizeof(CFE_MSG_TelemetryHeader_t));
    UtAssert_True(sizeof(CFE_MSG_CommandHeader_t) == CMD_HDR, "command header is %u bytes", (unsigned)sizeof(CFE_MSG_CommandHeader_t));
    UtAssert_True(sizeof(CFE_FS_Header_t) == FS_HDR, "file header is %u bytes", (unsigned)sizeof(CFE_FS_Header_t));
    UtTest_Add(Test_StartupAndHousekeeping, Setup, Teardown, "S1 Startup and housekeeping");
    UtTest_Add(Test_Heartbeat, Setup, Teardown, "S1 Heartbeat");
    UtTest_Add(Test_StartupErrors, Setup, Teardown, "S1 Startup error events");
    UtTest_Add(Test_TableValidation, Setup, Teardown, "S1 Table validation");
    UtTest_Add(Test_DecimationAndStats, Setup, Teardown, "S1 Decimation and statistics");
    UtTest_Add(Test_RolloverAndNaming, Setup, Teardown, "S1 Packet files and roll-over");
    UtTest_Add(Test_SequenceAndScan, Setup, Teardown, "S1 Startup scan and sequence");
    UtTest_Add(Test_PurgeOrder, Setup, Teardown, "S1 Purge order");
    UtTest_Add(Test_PurgeArchived, Setup, Teardown, "S1 Purge of archived files");
    UtTest_Add(Test_PurgePriorityChange, Setup, Teardown, "S1 Purge after priority change");
    UtTest_Add(Test_QuotaDrop, Setup, Teardown, "S1 Telemetry quota drops");
    UtTest_Add(Test_SetParamCommands, Setup, Teardown, "S1 Parameter commands");
    UtTest_Add(Test_CommandErrors, Setup, Teardown, "S1 Command errors");
    UtTest_Add(Test_CommandCounters, Setup, Teardown, "S1 Command counters");
    UtTest_Add(Test_ResetCounters, Setup, Teardown, "S1 Reset counters");
    UtTest_Add(Test_FileAddDelete, Setup, Teardown, "S1 File add and delete");
    UtTest_Add(Test_PathRejection, Setup, Teardown, "S1 Path rejection");
    UtTest_Add(Test_Listings, Setup, Teardown, "S1 Listing packets");
    UtTest_Add(Test_TableUpdate, Setup, Teardown, "S1 Table update");
    UtTest_Add(Test_IdleClose, Setup, Teardown, "S1 Idle close");
    UtTest_Add(Test_WriteFailures, Setup, Teardown, "S1 File write failures");
    UtTest_Add(Test_XferRejections, Setup, Teardown, "S2 Transfer command rejections");
    UtTest_Add(Test_XferGround, Setup, Teardown, "S2 Ground transfer of a stored file");
    UtTest_Add(Test_XferPacing, Setup, Teardown, "S2 Segmentation pacing and empty files");
    UtTest_Add(Test_XferPeer, Setup, Teardown, "S2 Peer transfer and acknowledgement");
    UtTest_Add(Test_XferAckFailures, Setup, Teardown, "S2 Acknowledgement failure and timeout");
    UtTest_Add(Test_XferAbort, Setup, Teardown, "S2 Abort transfer");
    UtTest_Add(Test_XferActionsFile, Setup, Teardown, "S2 Success actions in the file area");
    UtTest_Add(Test_XferActionsTlm, Setup, Teardown, "S2 Success actions for packet files");
    UtTest_Add(Test_XferReadError, Setup, Teardown, "S2 Source read error");
    UtTest_Add(Test_XferReceive, Setup, Teardown, "S2 Receiving a file");
    UtTest_Add(Test_XferReceiveNaks, Setup, Teardown, "S2 Receiver rejections");
    UtTest_Add(Test_XferReceiveTimeoutRestart, Setup, Teardown, "S2 Receiver timeout and restart");
    UtTest_Add(Test_XferAddressedElsewhere, Setup, Teardown, "S2 Segments addressed elsewhere");
    UtTest_Add(Test_XferReset, Setup, Teardown, "S2 Transfer counters and reset");
}
