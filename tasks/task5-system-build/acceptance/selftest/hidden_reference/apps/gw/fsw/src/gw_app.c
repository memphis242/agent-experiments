/* Scratch reference GW (spec S5), only to exercise the pass path of the hidden GW test. */
#include "cfe.h"
#include "cmdval_lib.h"
#include <stdio.h>
#include <string.h>

#define GW_CMD_MID     0x18D8u
#define GW_SEND_HK_MID 0x18D9u
#define GW_WAKEUP_MID  0x18DAu
#define GW_HK_MID      0x08D8u
#define GW_CONN_MID    0x08D9u
#define SFI_HB_MID     0x18DFu
#define NCONN          8
#define NSUB           16
#define MAXPKT         1024
#define ATTEMPT_WAKES  50 /* GW_TCP_ATTEMPT_SEC at 10 Hz */
#define MAX_ATTEMPTS   4  /* first attempt plus GW_TCP_MAX_RETRIES */

typedef struct
{
    uint8          Interface;
    uint8          Enabled;
    uint16         LocalPort;
    char           RemoteAddr[16];
    uint16         RemotePort;
    uint16         Spare;
    char           DevicePath[64];
    CFE_SB_MsgId_t Subs[NSUB];
} GW_ConnDef_t;

typedef struct { GW_ConnDef_t Conns[NCONN]; } GW_ConnTable_t;
typedef struct { CMDVAL_Config_t Validation; } GW_Val_t;

typedef struct
{
    GW_ConnDef_t def;
    bool         used, open, egr_rep, ing_rep, pending;
    uint8        state, attempts;
    osal_id_t    id;
    uint32       seq, tx, rx, txerr, rxerr, attempt_wake;
    uint8        buf[4400];
    size_t       blen;
} Conn_t;

static struct
{
    CFE_SB_PipeId_t  pipe;
    CFE_TBL_Handle_t th, vh;
    bool             shared, have_vh, valerr_rep, mapped;
    CMDVAL_Config_t *cfg;
    Conn_t           c[NCONN];
    uint8            cc, cec;
    uint32           egr, ingr, egerr, ingerr, loop, frames, rej[6];
    uint32           wake, hbseq;
    uint32           subs[NCONN * NSUB];
    int              nsubs;
} G;

static const uint8 MARK[4] = {0x1A, 0xCF, 0xFC, 0x1D};
static const char *REASON[6] = {"OK", "FORMAT", "CRC", "HMAC", "SOURCE", "AGE"};

static uint32 now(void) { return CFE_TIME_GetTime().Seconds; }
static uint16 be16(const uint8 *p) { return (uint16)((p[0] << 8) | p[1]); }
static void wbe16(uint8 *p, uint16 v) { p[0] = (uint8)(v >> 8); p[1] = (uint8)v; }
static void wbe32(uint8 *p, uint32 v)
{
    p[0] = (uint8)(v >> 24);
    p[1] = (uint8)(v >> 16);
    p[2] = (uint8)(v >> 8);
    p[3] = (uint8)v;
}

static bool valid_ip(const char *s)
{
    int part = 0, digits = 0, n = 0;
    for (;; s++)
    {
        if (*s >= '0' && *s <= '9')
        {
            n = n * 10 + (*s - '0');
            if (++digits > 3 || n > 255) return false;
        }
        else if (*s == '.' || *s == 0)
        {
            if (digits == 0 || part > 3) return false;
            part++;
            n = 0;
            digits = 0;
            if (*s == 0) break;
        }
        else
            return false;
    }
    return part == 4;
}

static bool def_valid(const GW_ConnDef_t *d)
{
    int i, k;
    if (d->Interface < 1 || d->Interface > 3 || d->Enabled > 1) return false;
    if (d->Interface != 3)
    {
        if (memchr(d->RemoteAddr, 0, sizeof(d->RemoteAddr)) == NULL || !valid_ip(d->RemoteAddr) || d->RemotePort == 0)
            return false;
    }
    if (d->Interface != 1 && d->LocalPort != 0) return false;
    if (d->Interface == 3 && (memchr(d->DevicePath, 0, sizeof(d->DevicePath)) == NULL || d->DevicePath[0] != '/'))
        return false;
    for (i = 0; i < NSUB; i++)
        for (k = i + 1; k < NSUB; k++)
            if (CFE_SB_MsgIdToValue(d->Subs[i]) != 0 && CFE_SB_MsgIdToValue(d->Subs[i]) == CFE_SB_MsgIdToValue(d->Subs[k]))
                return false;
    return true;
}

static int32 ValidateTbl(void *p)
{
    const GW_ConnTable_t *t = p;
    int i, k;
    for (i = 0; i < NCONN; i++)
    {
        if (t->Conns[i].Interface == 0) continue;
        if (!def_valid(&t->Conns[i])) goto bad;
        for (k = i + 1; k < NCONN; k++)
            if (t->Conns[i].Interface == 1 && t->Conns[k].Interface == 1 && t->Conns[i].LocalPort != 0 &&
                t->Conns[i].LocalPort == t->Conns[k].LocalPort)
                goto bad;
    }
    return CFE_SUCCESS;
bad:
    CFE_EVS_SendEvent(10, CFE_EVS_EventType_ERROR, "GW: ConnTable invalid");
    return -1;
}

static bool listed(const Conn_t *c, uint32 mid)
{
    int i;
    for (i = 0; i < NSUB; i++)
        if (CFE_SB_MsgIdToValue(c->def.Subs[i]) == mid) return true;
    return false;
}

static bool in_union(uint32 mid)
{
    int i;
    for (i = 0; i < NCONN; i++)
        if (G.c[i].used && G.c[i].def.Enabled && mid != 0 && listed(&G.c[i], mid)) return true;
    return false;
}

static void resub(void)
{
    uint32 want[NCONN * NSUB];
    int nw = 0, i, j, k;
    for (i = 0; i < NCONN; i++)
    {
        if (!G.c[i].used || !G.c[i].def.Enabled) continue;
        for (j = 0; j < NSUB; j++)
        {
            uint32 m = CFE_SB_MsgIdToValue(G.c[i].def.Subs[j]);
            bool dup = false;
            if (m == 0) continue;
            for (k = 0; k < nw; k++) dup |= want[k] == m;
            if (!dup) want[nw++] = m;
        }
    }
    for (k = 0; k < G.nsubs; k++)
    {
        bool keep = false;
        for (j = 0; j < nw; j++) keep |= want[j] == G.subs[k];
        if (!keep) CFE_SB_Unsubscribe(CFE_SB_ValueToMsgId(G.subs[k]), G.pipe);
    }
    for (j = 0; j < nw; j++)
    {
        bool have = false;
        for (k = 0; k < G.nsubs; k++) have |= want[j] == G.subs[k];
        if (!have) CFE_SB_SubscribeEx(CFE_SB_ValueToMsgId(want[j]), G.pipe, CFE_SB_DEFAULT_QOS, 16);
    }
    memcpy(G.subs, want, sizeof(uint32) * (size_t)nw);
    G.nsubs = nw;
}

static void set_up(int i)
{
    G.c[i].state = 1;
    G.c[i].seq = 0;
    G.c[i].blen = 0;
    CFE_EVS_SendEvent(20, CFE_EVS_EventType_INFORMATION, "GW: conn %d up", i);
}

static void conn_close(int i)
{
    if (G.c[i].open) OS_close(G.c[i].id);
    G.c[i].open = false;
    G.c[i].pending = false;
}

/* One connect attempt, started now: false only when no socket can be opened. */
static bool tcp_attempt(int i)
{
    Conn_t *c = &G.c[i];
    OS_SockAddr_t a;
    int32 rc;
    c->attempts++;
    c->attempt_wake = G.wake;
    if (OS_SocketOpen(&c->id, OS_SocketDomain_INET, OS_SocketType_STREAM) != OS_SUCCESS) return false;
    c->open = true;
    OS_SocketAddrInit(&a, OS_SocketDomain_INET);
    OS_SocketAddrFromString(&a, c->def.RemoteAddr);
    OS_SocketAddrSetPort(&a, c->def.RemotePort);
    rc = OS_SocketConnect(c->id, &a, 0);
    if (rc == OS_SUCCESS) set_up(i);
    else if (rc == OS_ERROR_TIMEOUT) c->pending = true;
    else conn_close(i);
    return true;
}

/* A connect cycle with its first attempt at once. */
static bool tcp_cycle(int i)
{
    G.c[i].state = 2;
    G.c[i].attempts = 0;
    return tcp_attempt(i);
}

/* On WAKEUP while CONNECTING: decide an in-progress attempt, then start the next one or enter DOWN. */
static void tcp_poll(int i)
{
    Conn_t *c = &G.c[i];
    uint32 fl = OS_STREAM_STATE_READABLE | OS_STREAM_STATE_WRITABLE;
    uint8 tmp[1100];
    int32 n;
    if (c->pending)
    {
        if (OS_SelectSingle(c->id, &fl, 0) == OS_SUCCESS)
        {
            if (fl & OS_STREAM_STATE_READABLE)
            {
                n = OS_TimedRead(c->id, tmp, sizeof(tmp), 0);
                if (n > 0)
                {
                    c->pending = false;
                    set_up(i);
                    memcpy(c->buf, tmp, (size_t)n);
                    c->blen = (size_t)n;
                    return;
                }
                conn_close(i);
            }
            else if (fl & OS_STREAM_STATE_WRITABLE)
            {
                c->pending = false;
                set_up(i);
                return;
            }
        }
        if (c->pending && G.wake - c->attempt_wake >= ATTEMPT_WAKES) conn_close(i);
    }
    if (c->pending || G.wake - c->attempt_wake < ATTEMPT_WAKES) return;
    if (c->attempts < MAX_ATTEMPTS)
    {
        tcp_attempt(i);
        return;
    }
    conn_close(i);
    c->state = 3;
    CFE_EVS_SendEvent(28, CFE_EVS_EventType_ERROR, "GW: conn %d to %s:%u timed out", i, c->def.RemoteAddr,
                      (unsigned)c->def.RemotePort);
}

static bool conn_open(int i)
{
    Conn_t *c = &G.c[i];
    OS_SockAddr_t a;
    char path[80];
    osal_id_t fs;
    int32 rc;
    if (c->def.Interface == 1)
    {
        if (OS_SocketOpen(&c->id, OS_SocketDomain_INET, OS_SocketType_DATAGRAM) != OS_SUCCESS) return false;
        c->open = true;
        if (c->def.LocalPort != 0)
        {
            OS_SocketAddrInit(&a, OS_SocketDomain_INET);
            OS_SocketAddrSetPort(&a, c->def.LocalPort);
            if (OS_SocketBind(c->id, &a) != OS_SUCCESS)
            {
                conn_close(i);
                return false;
            }
        }
        set_up(i);
        return true;
    }
    if (c->def.Interface == 2) return tcp_cycle(i);
    if (strncmp(c->def.DevicePath, "/dev/pts/", 9) == 0)
    {
        if (!G.mapped)
        {
            rc = OS_FileSysAddFixedMap(&fs, "/dev/pts", "/devpts");
            if (rc != OS_SUCCESS && rc != OS_ERR_NAME_TAKEN) return false;
            G.mapped = true;
        }
        snprintf(path, sizeof(path), "/devpts/%s", c->def.DevicePath + 9);
    }
    else
        snprintf(path, sizeof(path), "%s", c->def.DevicePath);
    if (OS_OpenCreate(&c->id, path, OS_FILE_FLAG_NONE, OS_READ_WRITE) != OS_SUCCESS) return false;
    c->open = true;
    set_up(i);
    return true;
}

static void conn_down(int i, bool disable)
{
    conn_close(i);
    if (disable)
    {
        G.c[i].def.Enabled = 0;
        G.c[i].state = 0;
        CFE_EVS_SendEvent(21, CFE_EVS_EventType_ERROR, "GW: conn %d down", i);
        resub();
    }
    else
        tcp_cycle(i);
}

static void apply_table(void)
{
    GW_ConnTable_t *t = NULL;
    int32 st;
    int i;
    for (i = 0; i < NCONN; i++) conn_close(i);
    memset(G.c, 0, sizeof(G.c));
    st = CFE_TBL_GetAddress((void **)&t, G.th);
    if ((st == CFE_SUCCESS || st == CFE_TBL_INFO_UPDATED) && t != NULL)
    {
        for (i = 0; i < NCONN; i++)
        {
            if (t->Conns[i].Interface == 0) continue;
            G.c[i].def = t->Conns[i];
            G.c[i].used = true;
            if (G.c[i].def.Enabled && !conn_open(i)) conn_down(i, true);
        }
        CFE_TBL_ReleaseAddress(G.th);
    }
    resub();
}

static void egress_err(int i)
{
    G.egerr++;
    G.c[i].txerr++;
    if (!G.c[i].egr_rep) CFE_EVS_SendEvent(22, CFE_EVS_EventType_ERROR, "GW: conn %d egress error", i);
    G.c[i].egr_rep = true;
}

static void egress(const uint8 *pkt, size_t n, uint32 mid)
{
    uint8 f[4 + 50 + MAXPKT];
    uint8 *fr = f + 4;
    OS_SockAddr_t a;
    int i;
    int32 rc;
    for (i = 0; i < NCONN; i++)
    {
        Conn_t *c = &G.c[i];
        if (!c->used || !c->def.Enabled || !listed(c, mid)) continue;
        if (!G.shared || c->state != 1 || n > MAXPKT)
        {
            egress_err(i);
            continue;
        }
        c->seq++;
        wbe16(fr, 0x4356);
        fr[2] = 1;
        fr[3] = 0;
        wbe16(fr + 4, (uint16)CFE_PSP_GetProcessorId());
        wbe16(fr + 6, (uint16)n);
        wbe32(fr + 8, now());
        wbe32(fr + 12, c->seq);
        memcpy(fr + 16, pkt, n);
        CMDVAL_HmacSha256(G.cfg->HmacKey, 32, fr, 16 + n, fr + 16 + n);
        wbe16(fr + 48 + n, CMDVAL_Crc16(fr, 48 + n));
        if (c->def.Interface == 1)
        {
            OS_SocketAddrInit(&a, OS_SocketDomain_INET);
            OS_SocketAddrFromString(&a, c->def.RemoteAddr);
            OS_SocketAddrSetPort(&a, c->def.RemotePort);
            rc = OS_SocketSendTo(c->id, fr, 50 + n, &a);
        }
        else
        {
            memcpy(f, MARK, 4);
            rc = OS_write(c->id, f, 54 + n);
        }
        if (rc < 0)
        {
            G.egerr++;
            c->txerr++;
            conn_down(i, c->def.Interface != 2);
            continue;
        }
        G.egr++;
        c->tx++;
        c->egr_rep = false;
    }
}

static void handle_frame(int i, const uint8 *f, size_t n)
{
    CMDVAL_FrameInfo_t info;
    CMDVAL_Status_t st;
    CFE_SB_MsgId_t mid;
    G.frames++;
    st = CMDVAL_ValidateFrame(f, n, G.cfg, now(), &info);
    if (st != CMDVAL_OK)
    {
        G.rej[st]++;
        G.c[i].rxerr++;
        if (st == CMDVAL_ERR_FORMAT)
            CFE_EVS_SendEvent(25, CFE_EVS_EventType_ERROR, "GW: conn %d frame rejected: FORMAT", i);
        else
            CFE_EVS_SendEvent(25, CFE_EVS_EventType_ERROR, "GW: conn %d frame rejected: %s, source %u", i, REASON[st],
                              (unsigned)be16(f + 4));
        return;
    }
    CFE_MSG_GetMsgId((const CFE_MSG_Message_t *)info.Command, &mid);
    if (in_union(CFE_SB_MsgIdToValue(mid)))
    {
        G.loop++;
        return;
    }
    CFE_SB_TransmitMsg((const CFE_MSG_Message_t *)info.Command, false);
    G.ingr++;
    G.c[i].rx++;
}

static void read_stream(int i)
{
    Conn_t *c = &G.c[i];
    uint32 fl = OS_STREAM_STATE_READABLE;
    int32 n;
    int frames = 0;
    size_t k;
    if (OS_SelectSingle(c->id, &fl, 0) == OS_SUCCESS && (fl & OS_STREAM_STATE_READABLE))
    {
        n = OS_read(c->id, c->buf + c->blen, sizeof(c->buf) - c->blen);
        if (n <= 0)
        {
            if (n < 0)
            {
                G.ingerr++;
                c->rxerr++;
                if (!c->ing_rep) CFE_EVS_SendEvent(23, CFE_EVS_EventType_ERROR, "GW: conn %d ingress error", i);
                c->ing_rep = true;
            }
            conn_down(i, c->def.Interface != 2);
            return;
        }
        c->ing_rep = false;
        c->blen += (size_t)n;
    }
    while (frames < 16)
    {
        uint16 l;
        for (k = 0; k + 4 <= c->blen && memcmp(c->buf + k, MARK, 4) != 0; k++) {}
        if (k + 4 > c->blen)
        {
            size_t keep = c->blen < 3 ? c->blen : 3;
            memmove(c->buf, c->buf + c->blen - keep, keep);
            c->blen = keep;
            break;
        }
        memmove(c->buf, c->buf + k, c->blen - k);
        c->blen -= k;
        if (c->blen < 20) break;
        l = be16(c->buf + 10);
        if (l < 8 || l > MAXPKT)
        {
            G.frames++;
            G.rej[1]++;
            c->rxerr++;
            CFE_EVS_SendEvent(25, CFE_EVS_EventType_ERROR, "GW: conn %d frame rejected: FORMAT", i);
            memmove(c->buf, c->buf + 4, c->blen - 4);
            c->blen -= 4;
            frames++;
            continue;
        }
        if (c->blen < 54u + l) break;
        handle_frame(i, c->buf + 4, 50u + l);
        memmove(c->buf, c->buf + 54 + l, c->blen - 54 - l);
        c->blen -= 54u + l;
        frames++;
    }
}

static void read_udp(int i)
{
    uint8 b[1200];
    OS_SockAddr_t from;
    int32 n;
    int k;
    for (k = 0; k < 16; k++)
    {
        n = OS_SocketRecvFrom(G.c[i].id, b, sizeof(b), &from, OS_CHECK);
        if (n <= 0) break;
        handle_frame(i, b, (size_t)n);
    }
}

static void share(void)
{
    GW_Val_t *v = NULL;
    int32 st;
    if (!G.have_vh)
    {
        if (CFE_TBL_Share(&G.vh, "CMDI.ValTable") != CFE_SUCCESS)
        {
            if (!G.valerr_rep) CFE_EVS_SendEvent(26, CFE_EVS_EventType_ERROR, "GW: cannot share CMDI.ValTable");
            G.valerr_rep = true;
            G.shared = false;
            G.cfg = NULL;
            return;
        }
        G.have_vh = true;
    }
    st = CFE_TBL_GetAddress((void **)&v, G.vh);
    if ((st == CFE_SUCCESS || st == CFE_TBL_INFO_UPDATED) && v != NULL)
    {
        if (!G.shared) CFE_EVS_SendEvent(27, CFE_EVS_EventType_INFORMATION, "GW: CMDI.ValTable shared");
        G.shared = true;
        G.valerr_rep = false;
        G.cfg = &v->Validation;
        return;
    }
    G.shared = false;
    G.have_vh = false;
    G.cfg = NULL;
    share();
}

static void wakeup(void)
{
    int i;
    struct { CFE_MSG_CommandHeader_t h; char Name[20]; uint32 Seq; } hb;
    G.wake++;
    share();
    if (CFE_TBL_Manage(G.th) == CFE_TBL_INFO_UPDATED) apply_table();
    for (i = 0; i < NCONN; i++)
    {
        Conn_t *c = &G.c[i];
        if (!c->used || !c->def.Enabled) continue;
        if (c->def.Interface == 2 && c->state == 2) tcp_poll(i);
        if (!G.shared || c->state != 1) continue;
        if (c->def.Interface == 1)
        {
            if (c->def.LocalPort) read_udp(i);
        }
        else
            read_stream(i);
    }
    if (G.wake % 10 == 0)
    {
        CFE_MSG_Init(CFE_MSG_PTR(hb.h), CFE_SB_ValueToMsgId(SFI_HB_MID), sizeof(hb));
        memset(hb.Name, 0, sizeof(hb.Name));
        strcpy(hb.Name, "GW");
        hb.Seq = ++G.hbseq;
        CFE_SB_TransmitMsg(CFE_MSG_PTR(hb.h), true);
    }
}

static void send_hk(void)
{
    struct { CFE_MSG_TelemetryHeader_t h; uint8 p[52]; } hk;
    int i;
    uint8 up = 0, cfg = 0;
    uint16 s = (uint16)G.nsubs;
    uint8 sh = G.shared ? 1 : 0;
    if (CFE_TBL_Manage(G.th) == CFE_TBL_INFO_UPDATED) apply_table();
    for (i = 0; i < NCONN; i++)
    {
        cfg += G.c[i].used;
        up += G.c[i].used && G.c[i].state == 1;
    }
    CFE_MSG_Init(CFE_MSG_PTR(hk.h), CFE_SB_ValueToMsgId(GW_HK_MID), sizeof(hk));
    memset(hk.p, 0, sizeof(hk.p));
    hk.p[0] = G.cc;
    hk.p[1] = G.cec;
    hk.p[2] = cfg;
    hk.p[3] = up;
    memcpy(hk.p + 4, &G.egr, 4);
    memcpy(hk.p + 8, &G.ingr, 4);
    memcpy(hk.p + 12, &G.egerr, 4);
    memcpy(hk.p + 16, &G.ingerr, 4);
    memcpy(hk.p + 20, &G.loop, 4);
    memcpy(hk.p + 24, &s, 2);
    hk.p[26] = sh;
    memcpy(hk.p + 28, &G.frames, 4);
    for (i = 1; i <= 5; i++) memcpy(hk.p + 28 + 4 * i, &G.rej[i], 4);
    CFE_SB_TimeStampMsg(CFE_MSG_PTR(hk.h));
    CFE_SB_TransmitMsg(CFE_MSG_PTR(hk.h), true);
}

static void send_status(void)
{
    struct { CFE_MSG_TelemetryHeader_t h; uint8 p[160]; } st;
    int i, j;
    CFE_MSG_Init(CFE_MSG_PTR(st.h), CFE_SB_ValueToMsgId(GW_CONN_MID), sizeof(st));
    memset(st.p, 0, sizeof(st.p));
    for (i = 0; i < NCONN; i++)
    {
        uint8 *p = st.p + 20 * i;
        uint8 sc = 0;
        if (!G.c[i].used) continue;
        for (j = 0; j < NSUB; j++) sc += CFE_SB_MsgIdToValue(G.c[i].def.Subs[j]) != 0;
        p[0] = G.c[i].def.Interface;
        p[1] = G.c[i].def.Enabled;
        p[2] = G.c[i].state;
        p[3] = sc;
        memcpy(p + 4, &G.c[i].tx, 4);
        memcpy(p + 8, &G.c[i].rx, 4);
        memcpy(p + 12, &G.c[i].txerr, 4);
        memcpy(p + 16, &G.c[i].rxerr, 4);
    }
    CFE_SB_TimeStampMsg(CFE_MSG_PTR(st.h));
    CFE_SB_TransmitMsg(CFE_MSG_PTR(st.h), true);
    CFE_EVS_SendEvent(24, CFE_EVS_EventType_INFORMATION, "GW: connection status sent");
}

static bool err(uint16 eid, const char *what)
{
    CFE_EVS_SendEvent(eid, CFE_EVS_EventType_ERROR, "GW: %s", what);
    return false;
}

static bool command(const CFE_SB_Buffer_t *b, size_t size)
{
    CFE_MSG_FcnCode_t cc;
    const uint8 *p = (const uint8 *)b + 8;
    uint8 idx = p[0];
    int i;
    static const size_t want[8] = {8, 8, 8 + 156, 12, 12, 12, 8, 8};
    CFE_MSG_GetFcnCode(&b->Msg, &cc);
    if (cc > 7) return err(2, "bad function code");
    if (size != want[cc]) return err(6, "bad length");
    switch (cc)
    {
        case 0:
            CFE_EVS_SendEvent(3, CFE_EVS_EventType_INFORMATION, "GW: NOOP");
            return true;
        case 1:
            G.cc = 255; /* the caller's increment wraps it to 0 */
            G.cec = 0;
            G.egr = G.ingr = G.egerr = G.ingerr = G.loop = G.frames = 0;
            memset(G.rej, 0, sizeof(G.rej));
            for (i = 0; i < NCONN; i++) G.c[i].tx = G.c[i].rx = G.c[i].txerr = G.c[i].rxerr = 0;
            CFE_EVS_SendEvent(4, CFE_EVS_EventType_INFORMATION, "GW: reset");
            return true;
        case 2:
        {
            GW_ConnDef_t d;
            memcpy(&d, p + 4, sizeof(d));
            if (idx >= NCONN || G.c[idx].used || !def_valid(&d)) return err(12, "ADD_CONN rejected");
            for (i = 0; i < NCONN; i++)
                if (G.c[i].used && d.Interface == 1 && G.c[i].def.Interface == 1 && d.LocalPort != 0 &&
                    G.c[i].def.LocalPort == d.LocalPort)
                    return err(12, "ADD_CONN rejected: LocalPort in use");
            memset(&G.c[idx], 0, sizeof(G.c[idx]));
            G.c[idx].def = d;
            if (d.Enabled && !conn_open(idx))
            {
                memset(&G.c[idx], 0, sizeof(G.c[idx]));
                return err(12, "ADD_CONN open failed");
            }
            G.c[idx].used = true;
            resub();
            CFE_EVS_SendEvent(11, CFE_EVS_EventType_INFORMATION, "GW: conn %u added", idx);
            return true;
        }
        case 3:
            if (idx >= NCONN || !G.c[idx].used) return err(14, "REMOVE_CONN rejected");
            conn_close(idx);
            memset(&G.c[idx], 0, sizeof(G.c[idx]));
            resub();
            CFE_EVS_SendEvent(13, CFE_EVS_EventType_INFORMATION, "GW: conn %u removed", idx);
            return true;
        case 4:
            if (idx >= NCONN || !G.c[idx].used || G.c[idx].def.Enabled || !conn_open(idx))
                return err(16, "ENABLE_CONN rejected");
            G.c[idx].def.Enabled = 1;
            resub();
            CFE_EVS_SendEvent(15, CFE_EVS_EventType_INFORMATION, "GW: conn %u enabled", idx);
            return true;
        case 5:
            if (idx >= NCONN || !G.c[idx].used || !G.c[idx].def.Enabled) return err(18, "DISABLE_CONN rejected");
            conn_close(idx);
            G.c[idx].def.Enabled = 0;
            G.c[idx].state = 0;
            resub();
            CFE_EVS_SendEvent(17, CFE_EVS_EventType_INFORMATION, "GW: conn %u disabled", idx);
            return true;
        case 6:
            apply_table();
            CFE_EVS_SendEvent(19, CFE_EVS_EventType_INFORMATION, "GW: reloaded");
            return true;
        default:
            send_status();
            return true;
    }
}

void GW_AppMain(void)
{
    uint32 rs = CFE_ES_RunStatus_APP_RUN;
    CFE_SB_Buffer_t *b;
    CFE_SB_MsgId_t mid;
    CFE_MSG_Size_t size;
    uint32 v;
    int i;
    memset(&G, 0, sizeof(G));
    CFE_EVS_Register(NULL, 0, CFE_EVS_EventFilter_BINARY);
    CFE_SB_CreatePipe(&G.pipe, 64, "GW_PIPE");
    CFE_SB_Subscribe(CFE_SB_ValueToMsgId(GW_CMD_MID), G.pipe);
    CFE_SB_Subscribe(CFE_SB_ValueToMsgId(GW_SEND_HK_MID), G.pipe);
    CFE_SB_Subscribe(CFE_SB_ValueToMsgId(GW_WAKEUP_MID), G.pipe);
    CFE_TBL_Register(&G.th, "ConnTable", sizeof(GW_ConnTable_t), CFE_TBL_OPT_DEFAULT, ValidateTbl);
    CFE_TBL_Load(G.th, CFE_TBL_SRC_FILE, "/cf/gw_tbl.tbl");
    share();
    apply_table();
    CFE_EVS_SendEvent(1, CFE_EVS_EventType_INFORMATION, "GW initialized");
    while (CFE_ES_RunLoop(&rs))
    {
        if (CFE_SB_ReceiveBuffer(&b, G.pipe, CFE_SB_PEND_FOREVER) != CFE_SUCCESS)
        {
            rs = CFE_ES_RunStatus_APP_ERROR;
            break;
        }
        CFE_MSG_GetMsgId(&b->Msg, &mid);
        CFE_MSG_GetSize(&b->Msg, &size);
        v = CFE_SB_MsgIdToValue(mid);
        if (v == GW_CMD_MID)
        {
            if (command(b, size)) G.cc++;
            else G.cec++;
        }
        else if (v == GW_SEND_HK_MID) send_hk();
        else if (v == GW_WAKEUP_MID) wakeup();
        else if (in_union(v)) egress((const uint8 *)b, size, v);
        else
        {
            CFE_EVS_SendEvent(5, CFE_EVS_EventType_ERROR, "GW: bad MID 0x%04X", (unsigned)v);
            G.cec++;
        }
    }
    for (i = 0; i < NCONN; i++) conn_close(i);
    CFE_ES_ExitApp(rs);
}
