/*
 * Behaviour model of the cFE and OSAL services the Task 5 applications use, shared by the hidden
 * black-box tests. Each test includes this header once, sets t5_app_entry to the application's
 * entry point, and drives it only with software bus messages, table images, files, datagrams and
 * stream bytes. Everything is decoded by offset from the specification; no application header is
 * included.
 *
 * Models (all reset by t5_setup()):
 *   - software bus: pipes, subscriptions with message limits, per-pipe queues, byte-level CCSDS
 *     headers, a capture of every transmitted message; the application thread runs until it waits
 *     on an empty pipe, then control returns to the test (t5_deliver / t5_idle);
 *   - events with their formatted text; a clock (seconds, subseconds) the test advances;
 *   - tables owned by the application (register, load from a test image, validate, manage, update)
 *     and tables shared from another application (CFE_TBL_Share, available or not, updated or not);
 *   - an in-memory file system with OSAL name limits, fixed file system maps that survive an
 *     application restart, and PTY-like devices with a receive queue and a transmit log;
 *   - UDP and TCP sockets: bound ports with datagram queues, connections to test listeners with
 *     byte streams, captured transmissions;
 *   - optionally (T5_MODEL_CMDVAL) the cmdval_lib API as a reference implementation with
 *     fault injection, so CMDI and GW are tested apart from the agent's library.
 * Calls a test cannot answer faithfully are reported by name ("unmodelled call").
 *
 * The header is compiled with the application's flags (-std=c99 -pedantic -Wall -Werror), so every
 * helper is marked unused-tolerant.
 */
#ifndef T5_MODEL_H
#define T5_MODEL_H

#include <errno.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "utassert.h"
#include "uttest.h"
#include "utstubs.h"

#include "cfe.h"
#include "cfe_psp.h"

#define T5_FN  static __attribute__((unused))
#define T5_VAR static __attribute__((unused))

/* ------------------------------------------------------------------------- byte helpers */

T5_FN void t5_put16(uint8 *p, uint16 v) { memcpy(p, &v, 2); }
T5_FN void t5_put32(uint8 *p, uint32 v) { memcpy(p, &v, 4); }
T5_FN uint16 t5_get16(const uint8 *p) { uint16 v; memcpy(&v, p, 2); return v; }
T5_FN uint32 t5_get32(const uint8 *p) { uint32 v; memcpy(&v, p, 4); return v; }
T5_FN uint16 t5_be16(const uint8 *p) { return (uint16)((p[0] << 8) | p[1]); }
T5_FN uint32 t5_be32(const uint8 *p) { return ((uint32)p[0] << 24) | ((uint32)p[1] << 16) | ((uint32)p[2] << 8) | p[3]; }
T5_FN void t5_wbe16(uint8 *p, uint16 v) { p[0] = (uint8)(v >> 8); p[1] = (uint8)v; }
T5_FN void t5_wbe32(uint8 *p, uint32 v) { p[0] = (uint8)(v >> 24); p[1] = (uint8)(v >> 16); p[2] = (uint8)(v >> 8); p[3] = (uint8)v; }

T5_FN void t5_ret(UT_EntryKey_t key, int32 v) { UT_Stub_SetReturnValue(key, v); }

/* ------------------------------------------------------------------------- unmodelled calls */

T5_VAR char t5_unmodelled_seen[16][48];
T5_VAR int  t5_n_unmodelled;

T5_FN void H_Unmodelled(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *name = ud;
    int i;
    (void)c;
    for (i = 0; i < t5_n_unmodelled; i++)
        if (strcmp(t5_unmodelled_seen[i], name) == 0) break;
    if (i == t5_n_unmodelled && t5_n_unmodelled < 16) snprintf(t5_unmodelled_seen[t5_n_unmodelled++], 48, "%s", name);
    UtAssert_Failed("unmodelled call: %s (outside the OSAL/cFE calls this review environment can answer)", name);
    t5_ret(key, -1);
}
#define T5_UNMODELLED(fn) UT_SetHandlerFunction(UT_KEY(fn), H_Unmodelled, (void *)#fn)

/* ------------------------------------------------------------------------- clock */

T5_VAR uint32 t5_now_sec;
T5_VAR uint32 t5_now_sub;

T5_FN CFE_TIME_SysTime_t t5_now(void)
{
    CFE_TIME_SysTime_t t;
    memset(&t, 0, sizeof(t));
    t.Seconds = t5_now_sec;
    t.Subseconds = t5_now_sub;
    return t;
}

T5_FN void H_TIME_Now(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TIME_SysTime_t t = t5_now();
    (void)ud; (void)c;
    UT_Stub_SetReturnValue(key, t);
}

T5_FN void H_OS_GetLocalTime(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    OS_time_t *t = UT_Hook_GetArgValueByName(c, "time_struct", OS_time_t *);
    (void)ud;
    *t = OS_TimeAssembleFromNanoseconds((int64)t5_now_sec, 0);
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_PSP_GetTime(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    OS_time_t *t = UT_Hook_GetArgValueByName(c, "LocalTime", OS_time_t *);
    (void)ud; (void)key;
    *t = OS_TimeAssembleFromNanoseconds((int64)t5_now_sec, 0);
}

T5_FN void H_TIME_Add(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TIME_SysTime_t a = UT_Hook_GetArgValueByName(c, "Time1", CFE_TIME_SysTime_t);
    CFE_TIME_SysTime_t b = UT_Hook_GetArgValueByName(c, "Time2", CFE_TIME_SysTime_t);
    CFE_TIME_SysTime_t r;
    uint64 sub = (uint64)a.Subseconds + b.Subseconds;
    (void)ud;
    r.Seconds = a.Seconds + b.Seconds + (uint32)(sub >> 32);
    r.Subseconds = (uint32)sub;
    UT_Stub_SetReturnValue(key, r);
}

T5_FN void H_TIME_Subtract(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TIME_SysTime_t a = UT_Hook_GetArgValueByName(c, "Time1", CFE_TIME_SysTime_t);
    CFE_TIME_SysTime_t b = UT_Hook_GetArgValueByName(c, "Time2", CFE_TIME_SysTime_t);
    CFE_TIME_SysTime_t r;
    (void)ud;
    r.Seconds = a.Seconds - b.Seconds - (a.Subseconds < b.Subseconds ? 1u : 0u);
    r.Subseconds = a.Subseconds - b.Subseconds;
    UT_Stub_SetReturnValue(key, r);
}

T5_FN void H_TIME_Compare(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TIME_SysTime_t a = UT_Hook_GetArgValueByName(c, "TimeA", CFE_TIME_SysTime_t);
    CFE_TIME_SysTime_t b = UT_Hook_GetArgValueByName(c, "TimeB", CFE_TIME_SysTime_t);
    CFE_TIME_Compare_t r = CFE_TIME_EQUAL;
    (void)ud;
    if (a.Seconds < b.Seconds || (a.Seconds == b.Seconds && a.Subseconds < b.Subseconds)) r = CFE_TIME_A_LT_B;
    else if (a.Seconds > b.Seconds || a.Subseconds > b.Subseconds) r = CFE_TIME_A_GT_B;
    UT_Stub_SetReturnValue(key, r);
}

T5_FN void H_TIME_Sub2MicroSecs(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 sub = UT_Hook_GetArgValueByName(c, "SubSeconds", uint32);
    uint32 us = (uint32)(((uint64)sub * 1000000u) >> 32);
    (void)ud;
    UT_Stub_SetReturnValue(key, us);
}

T5_FN void H_TIME_Micro2SubSecs(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 us = UT_Hook_GetArgValueByName(c, "MicroSeconds", uint32);
    uint32 sub = us >= 1000000u ? 0xFFFFFFFFu : (uint32)(((uint64)us << 32) / 1000000u);
    (void)ud;
    UT_Stub_SetReturnValue(key, sub);
}

/* ------------------------------------------------------------------------- CCSDS messages (byte level) */

#define T5_MSG_MAX 2048

T5_FN uint32 t5_msg_mid(const uint8 *m) { return t5_be16(m); }
T5_FN size_t t5_msg_size(const uint8 *m) { return (size_t)t5_be16(m + 4) + 7; }
T5_FN bool t5_msg_is_cmd(const uint8 *m) { return (m[0] & 0x10) != 0; }
T5_FN uint16 t5_msg_fcn(const uint8 *m) { return (uint16)(m[6] & 0x7F); }
T5_FN size_t t5_hdr_size(const uint8 *m) { return t5_msg_is_cmd(m) ? sizeof(CFE_MSG_CommandHeader_t) : sizeof(CFE_MSG_TelemetryHeader_t); }

T5_FN void H_MSG_Init(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    uint32 mid = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    size_t sz = UT_Hook_GetArgValueByName(c, "Size", CFE_MSG_Size_t);
    (void)ud;
    memset(m, 0, sz);
    t5_wbe16(m, (uint16)(mid | 0x0800));
    m[2] = 0xC0;
    t5_wbe16(m + 4, (uint16)(sz - 7));
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_MSG_GetMsgId(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_SB_MsgId_t *out = UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t *);
    (void)ud;
    *out = CFE_SB_ValueToMsgId(t5_msg_mid(m));
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_MSG_SetMsgId(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    (void)ud;
    t5_wbe16(m, (uint16)CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t)));
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_MSG_GetSize(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_Size_t *out = UT_Hook_GetArgValueByName(c, "Size", CFE_MSG_Size_t *);
    (void)ud;
    *out = t5_msg_size(m);
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_MSG_SetSize(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    size_t sz = UT_Hook_GetArgValueByName(c, "Size", CFE_MSG_Size_t);
    (void)ud;
    t5_wbe16(m + 4, (uint16)(sz - 7));
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_MSG_GetType(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_Type_t *out = UT_Hook_GetArgValueByName(c, "Type", CFE_MSG_Type_t *);
    (void)ud;
    *out = t5_msg_is_cmd(m) ? CFE_MSG_Type_Cmd : CFE_MSG_Type_Tlm;
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_MSG_SetType(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    CFE_MSG_Type_t t = UT_Hook_GetArgValueByName(c, "Type", CFE_MSG_Type_t);
    (void)ud;
    if (t == CFE_MSG_Type_Cmd) m[0] |= 0x10; else m[0] &= (uint8)~0x10;
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_MSG_GetFcnCode(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_FcnCode_t *out = UT_Hook_GetArgValueByName(c, "FcnCode", CFE_MSG_FcnCode_t *);
    (void)ud;
    *out = t5_msg_fcn(m);
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_MSG_SetFcnCode(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    (void)ud;
    m[6] = (uint8)((m[6] & 0x80) | (UT_Hook_GetArgValueByName(c, "FcnCode", CFE_MSG_FcnCode_t) & 0x7F));
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void t5_fill_checksum(uint8 *m)
{
    size_t n = t5_msg_size(m), i;
    uint8 x = 0xFF;
    m[7] = 0;
    for (i = 0; i < n; i++) x ^= m[i];
    m[7] = x;
}

T5_FN void H_MSG_GenerateChecksum(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    (void)ud;
    if (t5_msg_is_cmd(m)) t5_fill_checksum(m);
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_MSG_ValidateChecksum(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    bool *ok = UT_Hook_GetArgValueByName(c, "IsValid", bool *);
    size_t n = t5_msg_size(m), i;
    uint8 x = 0xFF;
    (void)ud;
    for (i = 0; i < n; i++) x ^= m[i];
    *ok = (x == 0);
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void t5_stamp(uint8 *m, CFE_TIME_SysTime_t t)
{
    if (!t5_msg_is_cmd(m))
    {
        t5_wbe32(m + 6, t.Seconds);
        t5_wbe16(m + 10, (uint16)(t.Subseconds >> 16));
    }
}

T5_FN void H_SB_TimeStampMsg(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)key;
    t5_stamp((uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *), t5_now());
}

T5_FN void H_MSG_SetMsgTime(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    t5_stamp((uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *),
             UT_Hook_GetArgValueByName(c, "NewTime", CFE_TIME_SysTime_t));
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_MSG_GetMsgTime(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_TIME_SysTime_t *t = UT_Hook_GetArgValueByName(c, "Time", CFE_TIME_SysTime_t *);
    (void)ud;
    memset(t, 0, sizeof(*t));
    if (!t5_msg_is_cmd(m))
    {
        t->Seconds = t5_be32(m + 6);
        t->Subseconds = (uint32)t5_be16(m + 10) << 16;
    }
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_SB_GetUserData(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    void *p = m + t5_hdr_size(m);
    (void)ud;
    UT_Stub_SetReturnValue(key, p);
}

T5_FN void H_SB_GetUserDataLength(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    size_t n = t5_msg_size(m) - t5_hdr_size(m);
    (void)ud;
    UT_Stub_SetReturnValue(key, n);
}

T5_FN void H_SB_IsValidMsgId(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 v = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    bool ok = v != 0 && v <= 0xFFFF;
    (void)ud;
    UT_Stub_SetReturnValue(key, ok);
}

/* ------------------------------------------------------------------------- events */

#define T5_EV_MAX 4096
#define T5_EV_TEXT 160

typedef struct { uint16 id; uint16 type; char text[T5_EV_TEXT]; } t5_ev_t;
T5_VAR t5_ev_t *t5_ev;
T5_VAR int t5_nev;

T5_FN int32 t5_ev_hook(void *ud, int32 rc, uint32 cnt, const UT_StubContext_t *c, va_list va)
{
    const char *spec = UT_Hook_GetArgValueByName(c, "Spec", const char *);
    (void)ud; (void)cnt;
    if (t5_nev < T5_EV_MAX)
    {
        t5_ev[t5_nev].id = UT_Hook_GetArgValueByName(c, "EventID", uint16);
        t5_ev[t5_nev].type = (uint16)UT_Hook_GetArgValueByName(c, "EventType", CFE_EVS_EventType_Enum_t);
        t5_ev[t5_nev].text[0] = 0;
        if (spec != NULL) vsnprintf(t5_ev[t5_nev].text, T5_EV_TEXT, spec, va);
        t5_nev++;
    }
    return rc;
}

T5_FN int t5_count_ev(uint16 id, int from)
{
    int i, n = 0;
    for (i = from; i < t5_nev; i++) if (t5_ev[i].id == id) n++;
    return n;
}

T5_FN int t5_last_ev_type(uint16 id)
{
    int i;
    for (i = t5_nev - 1; i >= 0; i--) if (t5_ev[i].id == id) return t5_ev[i].type;
    return -1;
}

T5_FN const char *t5_last_ev_text(uint16 id, int from)
{
    int i;
    for (i = t5_nev - 1; i >= from; i--) if (t5_ev[i].id == id) return t5_ev[i].text;
    return NULL;
}

/* True when some event `id` at or after `from` contains every one of the (up to three) substrings. */
T5_FN bool t5_ev_has(uint16 id, int from, const char *s1, const char *s2, const char *s3)
{
    int i;
    for (i = from; i < t5_nev; i++)
    {
        if (t5_ev[i].id != id) continue;
        if (s1 != NULL && strstr(t5_ev[i].text, s1) == NULL) continue;
        if (s2 != NULL && strstr(t5_ev[i].text, s2) == NULL) continue;
        if (s3 != NULL && strstr(t5_ev[i].text, s3) == NULL) continue;
        return true;
    }
    return false;
}

/* ------------------------------------------------------------------------- software bus */

#define T5_MAX_PIPES 8
#define T5_PIPE_Q    256
#define T5_POOL      512
#define T5_SUBS_MAX  512
#define T5_TX_MAX    2048

typedef struct
{
    bool   used;
    char   name[32];
    uint16 depth;
    int    q[T5_PIPE_Q];
    int    head, count;
} t5_pipe_t;

typedef struct { bool used; uint32 mid; int pipe; uint16 msglim; } t5_sub_t;
typedef struct { uint32 mid; size_t size; uint8 bytes[T5_MSG_MAX]; } t5_tx_t;

T5_VAR t5_pipe_t t5_pipes[T5_MAX_PIPES];
T5_VAR t5_sub_t  t5_subs[T5_SUBS_MAX];
T5_VAR uint8     t5_pool[T5_POOL][T5_MSG_MAX] __attribute__((aligned(16)));
T5_VAR int       t5_pool_next;
T5_VAR t5_tx_t  *t5_tx;
T5_VAR int       t5_ntx;
T5_VAR int       t5_unrouted;     /* messages the test delivered that no pipe subscribes to */
T5_VAR int       t5_overflow;     /* deliveries refused by a full pipe or message limit */
T5_VAR bool      t5_self_delivery = true; /* transmitted messages also reach the app's own subscribed pipes */
T5_VAR int32     t5_sub_rc, t5_unsub_rc, t5_pipe_rc, t5_recv_rc, t5_transmit_rc;
T5_VAR int       t5_n_subscribe, t5_n_unsubscribe;

T5_FN int t5_pipe_index(CFE_SB_PipeId_t id)
{
    unsigned long v = CFE_RESOURCEID_TO_ULONG(id);
    if (v < 1 || v > T5_MAX_PIPES || !t5_pipes[v - 1].used) return -1;
    return (int)v - 1;
}

T5_FN void H_SB_CreatePipe(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_SB_PipeId_t *id = UT_Hook_GetArgValueByName(c, "PipeIdPtr", CFE_SB_PipeId_t *);
    const char *name = UT_Hook_GetArgValueByName(c, "PipeName", const char *);
    int k;
    (void)ud;
    if (t5_pipe_rc != CFE_SUCCESS) { t5_ret(key, t5_pipe_rc); return; }
    for (k = 0; k < T5_MAX_PIPES && t5_pipes[k].used; k++) {}
    if (k == T5_MAX_PIPES) { t5_ret(key, CFE_SB_MAX_PIPES_MET); return; }
    memset(&t5_pipes[k], 0, sizeof(t5_pipes[k]));
    t5_pipes[k].used = true;
    t5_pipes[k].depth = UT_Hook_GetArgValueByName(c, "Depth", uint16);
    snprintf(t5_pipes[k].name, sizeof(t5_pipes[k].name), "%s", name ? name : "");
    *id = CFE_SB_PIPEID_C(CFE_ResourceId_FromInteger((unsigned long)k + 1));
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_SB_DeletePipe(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int p = t5_pipe_index(UT_Hook_GetArgValueByName(c, "PipeId", CFE_SB_PipeId_t)), k;
    (void)ud;
    if (p < 0) { t5_ret(key, CFE_SB_BAD_ARGUMENT); return; }
    t5_pipes[p].used = false;
    for (k = 0; k < T5_SUBS_MAX; k++) if (t5_subs[k].used && t5_subs[k].pipe == p) t5_subs[k].used = false;
    t5_ret(key, CFE_SUCCESS);
}

T5_FN int t5_find_sub(uint32 mid, int pipe)
{
    int k;
    for (k = 0; k < T5_SUBS_MAX; k++)
        if (t5_subs[k].used && t5_subs[k].mid == mid && t5_subs[k].pipe == pipe) return k;
    return -1;
}

/* Number of pipes subscribed to `mid` (0 when nothing would receive it). */
T5_FN int t5_subscribed(uint32 mid)
{
    int k, n = 0;
    for (k = 0; k < T5_SUBS_MAX; k++) if (t5_subs[k].used && t5_subs[k].mid == mid) n++;
    return n;
}

T5_FN uint16 t5_sub_msglim(uint32 mid)
{
    int k;
    for (k = 0; k < T5_SUBS_MAX; k++) if (t5_subs[k].used && t5_subs[k].mid == mid) return t5_subs[k].msglim;
    return 0;
}

T5_FN int t5_sub_count(void)
{
    int k, n = 0;
    for (k = 0; k < T5_SUBS_MAX; k++) if (t5_subs[k].used) n++;
    return n;
}

T5_FN void t5_subscribe(UT_EntryKey_t key, const UT_StubContext_t *c, uint16 msglim)
{
    uint32 mid = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    int p = t5_pipe_index(UT_Hook_GetArgValueByName(c, "PipeId", CFE_SB_PipeId_t)), k;
    t5_n_subscribe++;
    if (t5_sub_rc != CFE_SUCCESS) { t5_ret(key, t5_sub_rc); return; }
    if (p < 0 || mid == 0) { t5_ret(key, CFE_SB_BAD_ARGUMENT); return; }
    k = t5_find_sub(mid, p);
    if (k < 0)
    {
        for (k = 0; k < T5_SUBS_MAX && t5_subs[k].used; k++) {}
        if (k == T5_SUBS_MAX) { t5_ret(key, CFE_SB_MAX_MSGS_MET); return; }
        t5_subs[k].used = true;
        t5_subs[k].mid = mid;
        t5_subs[k].pipe = p;
    }
    t5_subs[k].msglim = msglim;
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_SB_Subscribe(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    t5_subscribe(key, c, 4); /* the SB default message limit of this build */
}

T5_FN void H_SB_SubscribeEx(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    t5_subscribe(key, c, UT_Hook_GetArgValueByName(c, "MsgLim", uint16));
}

T5_FN void H_SB_Unsubscribe(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 mid = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    int p = t5_pipe_index(UT_Hook_GetArgValueByName(c, "PipeId", CFE_SB_PipeId_t));
    int k = p < 0 ? -1 : t5_find_sub(mid, p);
    (void)ud;
    t5_n_unsubscribe++;
    if (t5_unsub_rc != CFE_SUCCESS) { t5_ret(key, t5_unsub_rc); return; }
    if (k >= 0) t5_subs[k].used = false;
    t5_ret(key, CFE_SUCCESS);
}

/* Queue a copy of a message on every pipe subscribed to its MID; returns the number of pipes reached. */
T5_FN int t5_route(const uint8 *msg, size_t size)
{
    uint32 mid = t5_msg_mid(msg);
    int k, n = 0, slot, depth, inq, j;
    for (k = 0; k < T5_SUBS_MAX; k++)
    {
        t5_pipe_t *p;
        if (!t5_subs[k].used || t5_subs[k].mid != mid) continue;
        p = &t5_pipes[t5_subs[k].pipe];
        depth = p->depth > 0 && p->depth < T5_PIPE_Q ? p->depth : T5_PIPE_Q;
        inq = 0;
        for (j = 0; j < p->count; j++) if (t5_msg_mid(t5_pool[p->q[(p->head + j) % T5_PIPE_Q]]) == mid) inq++;
        if (p->count >= depth || (t5_subs[k].msglim > 0 && inq >= t5_subs[k].msglim))
        {
            t5_overflow++;
            continue;
        }
        slot = t5_pool_next++ % T5_POOL;
        memset(t5_pool[slot], 0, T5_MSG_MAX);
        memcpy(t5_pool[slot], msg, size < T5_MSG_MAX ? size : T5_MSG_MAX);
        p->q[(p->head + p->count) % T5_PIPE_Q] = slot;
        p->count++;
        n++;
    }
    return n;
}

T5_FN void t5_capture(const uint8 *m)
{
    size_t sz = t5_msg_size(m);
    if (t5_ntx < T5_TX_MAX)
    {
        t5_tx[t5_ntx].mid = t5_msg_mid(m);
        t5_tx[t5_ntx].size = sz;
        memcpy(t5_tx[t5_ntx].bytes, m, sz <= T5_MSG_MAX ? sz : T5_MSG_MAX);
        t5_ntx++;
    }
    if (t5_self_delivery) t5_route(m, sz);
}

T5_FN void H_SB_TransmitMsg(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    (void)ud;
    if (t5_transmit_rc == CFE_SUCCESS) t5_capture(m);
    t5_ret(key, t5_transmit_rc);
}

T5_VAR uint8 t5_alloc_pool[16][T5_MSG_MAX] __attribute__((aligned(16)));
T5_VAR int   t5_alloc_next;

T5_FN void H_SB_AllocateMessageBuffer(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    size_t n = UT_Hook_GetArgValueByName(c, "MsgSize", size_t);
    CFE_SB_Buffer_t *b = NULL;
    (void)ud;
    if (n <= T5_MSG_MAX)
    {
        b = (CFE_SB_Buffer_t *)t5_alloc_pool[t5_alloc_next++ % 16];
        memset(b, 0, n);
    }
    UT_Stub_SetReturnValue(key, b);
}

T5_FN void H_SB_TransmitBuffer(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "BufPtr", CFE_SB_Buffer_t *);
    (void)ud;
    if (t5_transmit_rc == CFE_SUCCESS) t5_capture(m);
    t5_ret(key, t5_transmit_rc);
}

T5_FN void H_SB_Ok(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    t5_ret(key, CFE_SUCCESS);
}

T5_FN int t5_tx_count(uint32 mid, int from)
{
    int i, n = 0;
    for (i = from; i < t5_ntx; i++) if (t5_tx[i].mid == mid) n++;
    return n;
}

T5_FN const t5_tx_t *t5_last_tx(uint32 mid, int from)
{
    int i;
    for (i = t5_ntx - 1; i >= from; i--) if (t5_tx[i].mid == mid) return &t5_tx[i];
    return NULL;
}

/* ------------------------------------------------------------------------- application thread */

T5_VAR void (*t5_app_entry)(void);
T5_VAR pthread_t t5_thread;
T5_VAR bool t5_running, t5_exited, t5_stop;
T5_VAR sem_t t5_sem_app, t5_sem_test;
T5_VAR int t5_spin;              /* consecutive time-outs returned without progress */
T5_VAR int t5_timeouts;          /* time-outs returned to timed receives */
T5_VAR uint32 t5_exit_status;
T5_VAR bool t5_exit_called;
T5_VAR bool t5_sems_ready;

T5_FN void *t5_main(void *arg)
{
    (void)arg;
    t5_app_entry();
    t5_exited = true;
    sem_post(&t5_sem_test);
    return NULL;
}

T5_FN void H_ES_RunLoop(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    bool run = !t5_stop;
    (void)ud; (void)c;
    UT_Stub_SetReturnValue(key, run);
}

T5_FN void H_ES_ExitApp(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)key;
    t5_exit_status = UT_Hook_GetArgValueByName(c, "ExitStatus", uint32);
    t5_exit_called = true;
}

T5_FN bool t5_any_pending(void)
{
    int k;
    for (k = 0; k < T5_MAX_PIPES; k++) if (t5_pipes[k].used && t5_pipes[k].count > 0) return true;
    return false;
}

/*
 * Receive: a queued message returns at once. On an empty pipe, a poll returns NO_MESSAGE; a timed
 * receive returns TIME_OUT while another pipe has messages waiting (so a loop over several pipes
 * makes progress, at most 4 times in a row); otherwise the application is idle and control passes to
 * the test until it delivers something or ends the application.
 */
T5_FN void H_SB_ReceiveBuffer(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_SB_Buffer_t **bp = UT_Hook_GetArgValueByName(c, "BufPtr", CFE_SB_Buffer_t **);
    int p = t5_pipe_index(UT_Hook_GetArgValueByName(c, "PipeId", CFE_SB_PipeId_t));
    int32 timeout = UT_Hook_GetArgValueByName(c, "TimeOut", int32);
    (void)ud;
    if (t5_recv_rc != CFE_SUCCESS)
    {
        t5_stop = true;
        t5_ret(key, t5_recv_rc);
        return;
    }
    if (p < 0) { t5_ret(key, CFE_SB_BAD_ARGUMENT); return; }
    for (;;)
    {
        if (t5_stop) { t5_ret(key, CFE_SB_PIPE_RD_ERR); return; }
        if (t5_pipes[p].count > 0)
        {
            int slot = t5_pipes[p].q[t5_pipes[p].head];
            t5_pipes[p].head = (t5_pipes[p].head + 1) % T5_PIPE_Q;
            t5_pipes[p].count--;
            t5_spin = 0;
            *bp = (CFE_SB_Buffer_t *)t5_pool[slot];
            t5_ret(key, CFE_SUCCESS);
            return;
        }
        if (timeout == CFE_SB_POLL) { t5_ret(key, CFE_SB_NO_MESSAGE); return; }
        if (timeout != CFE_SB_PEND_FOREVER && t5_any_pending() && t5_spin < 4)
        {
            t5_spin++;
            t5_timeouts++;
            t5_ret(key, CFE_SB_TIME_OUT);
            return;
        }
        t5_spin = 0;
        sem_post(&t5_sem_test);
        sem_wait(&t5_sem_app);
    }
}

T5_FN bool t5_wait_idle(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += 10;
    while (sem_timedwait(&t5_sem_test, &ts) != 0)
    {
        if (errno == EINTR) continue;
        UtAssert_Failed("the application did not return to its software bus pipe within 10 s");
        return false;
    }
    return !t5_exited;
}

T5_FN bool t5_app_start(void)
{
    if (!t5_sems_ready)
    {
        sem_init(&t5_sem_app, 0, 0);
        sem_init(&t5_sem_test, 0, 0);
        t5_sems_ready = true;
    }
    UtAssert_True(t5_app_entry != NULL, "the test names the entry point");
    /* posts left over from an application that exited before the test waited for it must not count here */
    while (sem_trywait(&t5_sem_test) == 0) {}
    while (sem_trywait(&t5_sem_app) == 0) {}
    t5_running = true;
    t5_exited = false;
    t5_stop = false;
    t5_spin = 0;
    if (pthread_create(&t5_thread, NULL, t5_main, NULL) != 0)
    {
        UtAssert_Failed("cannot create the application thread");
        t5_running = false;
        return false;
    }
    return t5_wait_idle();
}

T5_FN void t5_close_app_resources(void);

T5_FN void t5_app_end(void)
{
    struct timespec ts;
    if (!t5_running) return;
    t5_stop = true;
    if (!t5_exited)
    {
        sem_post(&t5_sem_app);
        clock_gettime(CLOCK_REALTIME, &ts);
        ts.tv_sec += 5;
        while (!t5_exited && sem_timedwait(&t5_sem_test, &ts) != 0 && errno == EINTR) {}
    }
    if (t5_exited) pthread_join(t5_thread, NULL);
    while (sem_trywait(&t5_sem_test) == 0) {}
    while (sem_trywait(&t5_sem_app) == 0) {}
    t5_running = false;
    t5_close_app_resources();
}

/* Wake the application and wait until it is idle again; false if it is not running. */
T5_FN bool t5_kick(void)
{
    if (!t5_running || t5_exited)
    {
        UtAssert_Failed("the application is not running");
        return false;
    }
    sem_post(&t5_sem_app);
    return t5_wait_idle();
}

/* Deliver one message (copied) to every subscribed pipe, then run the application until idle. */
T5_FN bool t5_deliver(const uint8 *msg, size_t size)
{
    if (t5_route(msg, size) == 0) t5_unrouted++;
    return t5_kick();
}

/* Deliver several messages at once (as a burst on the bus), then run until idle. */
T5_FN bool t5_deliver_burst(const uint8 *const *msgs, const size_t *sizes, int n)
{
    int i;
    for (i = 0; i < n; i++) if (t5_route(msgs[i], sizes[i]) == 0) t5_unrouted++;
    return t5_kick();
}

T5_FN size_t t5_build_cmd(uint8 *b, uint32 mid, int cc, const void *payload, size_t plen)
{
    size_t sz = sizeof(CFE_MSG_CommandHeader_t) + plen;
    memset(b, 0, sz);
    t5_wbe16(b, (uint16)(mid | 0x1800));
    b[2] = 0xC0;
    t5_wbe16(b + 4, (uint16)(sz - 7));
    b[6] = (uint8)(cc & 0x7F);
    if (plen && payload != NULL) memcpy(b + sizeof(CFE_MSG_CommandHeader_t), payload, plen); /* NULL payload: zeros */
    t5_fill_checksum(b);
    return sz;
}

/* A telemetry packet of `size` bytes carrying `index` as its first payload word. */
T5_FN size_t t5_build_tlm(uint8 *b, uint32 mid, size_t size, uint32 index)
{
    size_t k;
    memset(b, 0, size);
    t5_wbe16(b, (uint16)mid);
    b[2] = 0xC0;
    t5_wbe16(b + 4, (uint16)(size - 7));
    t5_wbe32(b + 6, t5_now_sec);
    t5_put32(b + 16, index);
    for (k = 20; k < size; k++) b[k] = (uint8)(index * 3 + k);
    return size;
}

/* A verification packet (spec C.3): a command of `size` bytes with `value` (width 1, 2 or 4) at byte 16. */
T5_FN size_t t5_build_verif(uint8 *b, uint32 mid, size_t size, uint32 value, int width)
{
    size_t k;
    memset(b, 0, size);
    t5_wbe16(b, (uint16)mid);
    b[2] = 0xC0;
    t5_wbe16(b + 4, (uint16)(size - 7));
    for (k = 8; k < size; k++) b[k] = (uint8)(k * 5);
    if (size >= 16 + (size_t)width)
    {
        if (width == 1) b[16] = (uint8)value;
        else if (width == 2) t5_put16(b + 16, (uint16)value);
        else t5_put32(b + 16, value);
    }
    t5_fill_checksum(b);
    return size;
}

T5_VAR uint8 t5_scratch[T5_MSG_MAX] __attribute__((aligned(16)));

T5_FN bool t5_send_cmd(uint32 mid, int cc, const void *payload, size_t plen)
{
    size_t n = t5_build_cmd(t5_scratch, mid, cc, payload, plen);
    return t5_deliver(t5_scratch, n);
}

/* ------------------------------------------------------------------------- tables */

#define T5_TBL_MAX  6
#define T5_TBL_SIZE 4096

typedef struct
{
    bool   used;
    bool   owned;          /* registered by the application (else shared from another application) */
    char   name[48];
    size_t size;
    CFE_TBL_CallbackFuncPtr_t validate;
    uint8  active[T5_TBL_SIZE] __attribute__((aligned(16)));
    uint8  pending[T5_TBL_SIZE] __attribute__((aligned(16)));
    bool   has_pending;
    bool   updated;
    bool   loaded;
    bool   unregistered;   /* the owning application went away (shared tables) */
} t5_tbl_t;

typedef struct { char name[48]; uint8 image[T5_TBL_SIZE]; size_t size; bool set; } t5_tblfile_t;

T5_VAR t5_tbl_t     t5_tbl[T5_TBL_MAX];
T5_VAR t5_tblfile_t t5_tblfile[T5_TBL_MAX];
T5_VAR int32        t5_tbl_reg_rc, t5_tbl_load_rc;
T5_VAR int          t5_n_tbl_register, t5_n_share_calls, t5_n_getaddress;
T5_VAR bool         t5_share_ok[T5_TBL_MAX];

T5_FN int t5_tbl_find(const char *name)
{
    int k;
    for (k = 0; k < T5_TBL_MAX; k++) if (t5_tbl[k].used && strcmp(t5_tbl[k].name, name) == 0) return k;
    return -1;
}

/* The image CFE_TBL_Load(..., CFE_TBL_SRC_FILE, ...) returns for table `name` (registered name). */
T5_FN void t5_tbl_set_file(const char *name, const void *image, size_t size)
{
    int k;
    for (k = 0; k < T5_TBL_MAX && t5_tblfile[k].set && strcmp(t5_tblfile[k].name, name) != 0; k++) {}
    if (k == T5_TBL_MAX) { UtAssert_Failed("table file model full"); return; }
    snprintf(t5_tblfile[k].name, sizeof(t5_tblfile[k].name), "%s", name);
    memcpy(t5_tblfile[k].image, image, size);
    t5_tblfile[k].size = size;
    t5_tblfile[k].set = true;
}

T5_FN int t5_handle_index(CFE_TBL_Handle_t h)
{
    int v = (int)h;
    if (v < 1 || v > T5_TBL_MAX || !t5_tbl[v - 1].used) return -1;
    return v - 1;
}

T5_FN void H_TBL_Register(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TBL_Handle_t *h = UT_Hook_GetArgValueByName(c, "TblHandlePtr", CFE_TBL_Handle_t *);
    const char *name = UT_Hook_GetArgValueByName(c, "Name", const char *);
    size_t size = UT_Hook_GetArgValueByName(c, "Size", size_t);
    int k;
    (void)ud;
    t5_n_tbl_register++;
    if (t5_tbl_reg_rc != CFE_SUCCESS) { t5_ret(key, t5_tbl_reg_rc); return; }
    k = t5_tbl_find(name);
    if (k < 0) for (k = 0; k < T5_TBL_MAX && t5_tbl[k].used; k++) {}
    if (k == T5_TBL_MAX || size > T5_TBL_SIZE) { t5_ret(key, CFE_TBL_ERR_REGISTRY_FULL); return; }
    memset(&t5_tbl[k], 0, sizeof(t5_tbl[k]));
    t5_tbl[k].used = true;
    t5_tbl[k].owned = true;
    snprintf(t5_tbl[k].name, sizeof(t5_tbl[k].name), "%s", name);
    t5_tbl[k].size = size;
    t5_tbl[k].validate = UT_Hook_GetArgValueByName(c, "TblValidationFuncPtr", CFE_TBL_CallbackFuncPtr_t);
    *h = (CFE_TBL_Handle_t)(k + 1);
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_TBL_Load(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k = t5_handle_index(UT_Hook_GetArgValueByName(c, "TblHandle", CFE_TBL_Handle_t));
    CFE_TBL_SrcEnum_t src = UT_Hook_GetArgValueByName(c, "SrcType", CFE_TBL_SrcEnum_t);
    const void *data = UT_Hook_GetArgValueByName(c, "SrcDataPtr", const void *);
    uint8 *img;
    int f;
    int32 rc;
    (void)ud;
    if (t5_tbl_load_rc != CFE_SUCCESS) { t5_ret(key, t5_tbl_load_rc); return; }
    if (k < 0) { t5_ret(key, CFE_TBL_ERR_INVALID_HANDLE); return; }
    img = malloc(T5_TBL_SIZE);
    memset(img, 0, T5_TBL_SIZE);
    if (src == CFE_TBL_SRC_ADDRESS)
    {
        memcpy(img, data, t5_tbl[k].size);
    }
    else
    {
        for (f = 0; f < T5_TBL_MAX; f++)
            if (t5_tblfile[f].set && strcmp(t5_tblfile[f].name, t5_tbl[k].name) == 0) break;
        if (f == T5_TBL_MAX) { free(img); t5_ret(key, CFE_TBL_ERR_FILE_FOR_WRONG_TABLE); return; }
        if (t5_tblfile[f].size != t5_tbl[k].size) { free(img); t5_ret(key, CFE_TBL_ERR_FILE_TOO_LARGE); return; }
        memcpy(img, t5_tblfile[f].image, t5_tblfile[f].size);
    }
    rc = t5_tbl[k].validate != NULL ? t5_tbl[k].validate(img) : CFE_SUCCESS;
    if (rc == CFE_SUCCESS)
    {
        memcpy(t5_tbl[k].active, img, t5_tbl[k].size);
        t5_tbl[k].updated = true;
        t5_tbl[k].loaded = true;
    }
    free(img);
    t5_ret(key, rc == CFE_SUCCESS ? CFE_SUCCESS : rc);
}

T5_FN bool t5_tbl_activate(int k)
{
    if (k >= 0 && t5_tbl[k].has_pending)
    {
        memcpy(t5_tbl[k].active, t5_tbl[k].pending, t5_tbl[k].size);
        t5_tbl[k].has_pending = false;
        t5_tbl[k].updated = true;
        return true;
    }
    return false;
}

T5_FN void H_TBL_Manage(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k = t5_handle_index(UT_Hook_GetArgValueByName(c, "TblHandle", CFE_TBL_Handle_t));
    (void)ud;
    if (k >= 0 && !t5_tbl[k].owned) { t5_ret(key, CFE_TBL_ERR_NO_ACCESS); return; }
    t5_ret(key, t5_tbl_activate(k) ? CFE_TBL_INFO_UPDATED : CFE_SUCCESS);
}

T5_FN void H_TBL_Update(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k = t5_handle_index(UT_Hook_GetArgValueByName(c, "TblHandle", CFE_TBL_Handle_t));
    (void)ud;
    t5_ret(key, t5_tbl_activate(k) ? CFE_SUCCESS : CFE_TBL_INFO_NO_UPDATE_PENDING);
}

T5_FN void H_TBL_GetStatus(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k = t5_handle_index(UT_Hook_GetArgValueByName(c, "TblHandle", CFE_TBL_Handle_t));
    (void)ud;
    t5_ret(key, (k >= 0 && t5_tbl[k].has_pending) ? CFE_TBL_INFO_UPDATE_PENDING : CFE_SUCCESS);
}

T5_FN void H_TBL_GetAddress(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    void **p = UT_Hook_GetArgValueByName(c, "TblPtr", void **);
    int k = t5_handle_index(UT_Hook_GetArgValueByName(c, "TblHandle", CFE_TBL_Handle_t));
    int32 rc;
    (void)ud;
    t5_n_getaddress++;
    if (k < 0) { *p = NULL; t5_ret(key, CFE_TBL_ERR_INVALID_HANDLE); return; }
    if (t5_tbl[k].unregistered) { *p = NULL; t5_ret(key, CFE_TBL_ERR_UNREGISTERED); return; }
    if (!t5_tbl[k].owned && !t5_tbl[k].loaded) { *p = NULL; t5_ret(key, CFE_TBL_ERR_NEVER_LOADED); return; }
    rc = t5_tbl[k].updated ? CFE_TBL_INFO_UPDATED : CFE_SUCCESS;
    t5_tbl[k].updated = false;
    *p = t5_tbl[k].active;
    t5_ret(key, rc);
}

T5_FN void H_TBL_ReleaseAddress(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k = t5_handle_index(UT_Hook_GetArgValueByName(c, "TblHandle", CFE_TBL_Handle_t));
    (void)ud;
    t5_ret(key, k < 0 ? CFE_TBL_ERR_INVALID_HANDLE : CFE_SUCCESS);
}

/* A table of another application that CFE_TBL_Share can reach once `available` (full name). */
T5_FN int t5_tbl_offer(const char *full_name, const void *image, size_t size, bool available)
{
    int k = t5_tbl_find(full_name);
    if (k < 0) for (k = 0; k < T5_TBL_MAX && t5_tbl[k].used; k++) {}
    if (k == T5_TBL_MAX) { UtAssert_Failed("table model full"); return -1; }
    if (!t5_tbl[k].used)
    {
        memset(&t5_tbl[k], 0, sizeof(t5_tbl[k]));
        t5_tbl[k].used = true;
        snprintf(t5_tbl[k].name, sizeof(t5_tbl[k].name), "%s", full_name);
    }
    t5_tbl[k].owned = false;
    t5_tbl[k].size = size;
    memcpy(t5_tbl[k].active, image, size);
    t5_tbl[k].loaded = true;
    t5_tbl[k].updated = true;
    t5_tbl[k].unregistered = false;
    t5_share_ok[k] = available;
    return k;
}

/* The owner of a shared table loads a new image (cFE Table Services update). */
T5_FN void t5_tbl_shared_update(const char *full_name, const void *image, size_t size)
{
    int k = t5_tbl_find(full_name);
    if (k < 0) { UtAssert_Failed("no shared table %s", full_name); return; }
    memcpy(t5_tbl[k].active, image, size);
    t5_tbl[k].updated = true;
}

/* The owner of a shared table stops (its registration goes away until it is offered again). */
T5_FN void t5_tbl_shared_unregister(const char *full_name)
{
    int k = t5_tbl_find(full_name);
    if (k < 0) return;
    t5_tbl[k].unregistered = true;
    t5_share_ok[k] = false;
}

T5_FN void H_TBL_Share(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TBL_Handle_t *h = UT_Hook_GetArgValueByName(c, "TblHandlePtr", CFE_TBL_Handle_t *);
    const char *name = UT_Hook_GetArgValueByName(c, "TblName", const char *);
    int k = t5_tbl_find(name);
    (void)ud;
    t5_n_share_calls++;
    if (k < 0 || t5_tbl[k].owned || !t5_share_ok[k]) { t5_ret(key, CFE_TBL_ERR_INVALID_NAME); return; }
    t5_tbl[k].unregistered = false;
    t5_tbl[k].updated = true;
    *h = (CFE_TBL_Handle_t)(k + 1);
    t5_ret(key, CFE_SUCCESS);
}

T5_FN void H_TBL_Unregister(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k = t5_handle_index(UT_Hook_GetArgValueByName(c, "TblHandle", CFE_TBL_Handle_t));
    (void)ud;
    if (k >= 0 && t5_tbl[k].owned) t5_tbl[k].used = false;
    t5_ret(key, k < 0 ? CFE_TBL_ERR_INVALID_HANDLE : CFE_SUCCESS);
}

/* Present a new image of an owned table: it must pass the application's validation first. */
T5_FN int32 t5_tbl_submit(const char *name, const void *image)
{
    int k = t5_tbl_find(name);
    uint8 *copy;
    int32 rc;
    if (k < 0 || !t5_tbl[k].owned) { UtAssert_Failed("table %s was not registered by the application", name); return -1; }
    copy = malloc(T5_TBL_SIZE);
    memcpy(copy, image, t5_tbl[k].size);
    rc = t5_tbl[k].validate != NULL ? t5_tbl[k].validate(copy) : CFE_SUCCESS;
    if (rc == CFE_SUCCESS)
    {
        memcpy(t5_tbl[k].pending, copy, t5_tbl[k].size);
        t5_tbl[k].has_pending = true;
    }
    free(copy);
    return rc;
}

/* Run only the application's validation function on an image (no state change). */
T5_FN int32 t5_tbl_validate(const char *name, const void *image)
{
    int k = t5_tbl_find(name);
    uint8 *copy;
    int32 rc;
    if (k < 0 || t5_tbl[k].validate == NULL) { UtAssert_Failed("no validation function registered for %s", name); return 0; }
    copy = malloc(T5_TBL_SIZE);
    memcpy(copy, image, t5_tbl[k].size);
    rc = t5_tbl[k].validate(copy);
    free(copy);
    return rc;
}

/* ------------------------------------------------------------------------- files, maps, devices */

#define MF_MAX        1024
#define MFD_MAX       64
#define MDIR_MAX      16
#define NAME_MAX_TEST 96
#define T5_DEV_BUF    65536
#define T5_FD_BASE    1
#define T5_SOCK_BASE  1001
#define T5_DIR_BASE   2001

typedef struct
{
    bool    used;
    bool    dir;
    bool    dev;                 /* a PTY slave: reads come from rx, writes go to tx */
    char    path[NAME_MAX_TEST];
    uint8  *data;
    size_t  size;
    uint32  order;
    uint8  *rx; size_t rx_len; /* bytes waiting for the application */
    uint8  *txb; size_t tx_len; /* bytes the application wrote */
} mfile_t;

typedef struct { bool used; int file; size_t pos; } mfd_t;
typedef struct { bool used; char path[NAME_MAX_TEST]; uint32 next_order; } mdir_t;
typedef struct { bool used; char phys[NAME_MAX_TEST]; char virt[NAME_MAX_TEST]; } t5_map_t;

T5_VAR mfile_t mf[MF_MAX];
T5_VAR mfd_t   mfd[MFD_MAX];
T5_VAR mdir_t  mdir[MDIR_MAX];
T5_VAR t5_map_t t5_maps[8];
T5_VAR uint32  mf_order;
T5_VAR uint32  n_remove, n_close;
T5_VAR int     fi_create_fail;    /* fail the next N creations of packet files (/tlm_ in the path) */
T5_VAR bool    fi_write_short;    /* OS_write writes nothing (regular files) */
T5_VAR bool    fi_mkdir_fail;     /* OS_mkdir fails */
T5_VAR int     fi_read_fail;      /* fail the next N reads of regular files */
T5_VAR int     fi_rename_fail;    /* fail the next N renames/moves */
T5_VAR int     t5_dev_blocking_reads;   /* reads that would block on a device with nothing pending */
T5_VAR int     t5_dev_waiting_selects;  /* selects with a nonzero timeout on a device with nothing pending */
T5_VAR int     t5_n_fixed_map;

T5_FN int mf_find(const char *path)
{
    int i;
    for (i = 0; i < MF_MAX; i++)
        if (mf[i].used && strcmp(mf[i].path, path) == 0) return i;
    return -1;
}

T5_FN const char *base_of(const char *path)
{
    const char *s = strrchr(path, '/');
    return s ? s + 1 : path;
}

T5_FN void parent_of(const char *path, char *out, size_t n)
{
    const char *s = strrchr(path, '/');
    size_t len = s ? (size_t)(s - path) : 0;
    if (len >= n) len = n - 1;
    memcpy(out, path, len);
    out[len] = 0;
}

T5_FN int mf_new(const char *path, bool dir)
{
    int i;
    for (i = 0; i < MF_MAX; i++)
    {
        if (!mf[i].used)
        {
            memset(&mf[i], 0, sizeof(mf[i]));
            mf[i].used = true;
            mf[i].dir = dir;
            snprintf(mf[i].path, sizeof(mf[i].path), "%s", path);
            mf[i].order = ++mf_order;
            return i;
        }
    }
    UtAssert_Failed("model file table full");
    return 0;
}

T5_FN void mf_grow(mfile_t *f, size_t n)
{
    if (n > f->size)
    {
        f->data = realloc(f->data, n ? n : 1);
        memset(f->data + f->size, 0, n - f->size);
        f->size = n;
    }
}

T5_FN void mf_free(int i)
{
    free(mf[i].data);
    free(mf[i].rx);
    free(mf[i].txb);
    memset(&mf[i], 0, sizeof(mf[i]));
}

T5_FN void mf_reset(void)
{
    int i;
    for (i = 0; i < MF_MAX; i++) if (mf[i].used) mf_free(i);
    memset(mf, 0, sizeof(mf));
    memset(mfd, 0, sizeof(mfd));
    memset(mdir, 0, sizeof(mdir));
    memset(t5_maps, 0, sizeof(t5_maps));
    mf_order = 0;
    n_remove = n_close = 0;
    fi_create_fail = 0;
    fi_write_short = false;
    fi_mkdir_fail = false;
    fi_read_fail = 0;
    fi_rename_fail = 0;
    t5_dev_blocking_reads = 0;
    t5_dev_waiting_selects = 0;
    t5_n_fixed_map = 0;
    mf_new("/cf", true);
}

/* Create or replace a regular file of `size` pattern bytes; the parent must exist. */
T5_FN void mf_put(const char *path, size_t size, uint8 seed)
{
    int i = mf_find(path);
    size_t k;
    if (i < 0) i = mf_new(path, false);
    mf[i].dir = false;
    free(mf[i].data);
    mf[i].data = NULL;
    mf[i].size = 0;
    mf_grow(&mf[i], size);
    for (k = 0; k < size; k++) mf[i].data[k] = (uint8)(seed + k * 7);
}

T5_FN void mf_put_bytes(const char *path, const void *data, size_t size)
{
    int i = mf_find(path);
    if (i < 0) i = mf_new(path, false);
    free(mf[i].data);
    mf[i].data = NULL;
    mf[i].size = 0;
    mf_grow(&mf[i], size);
    if (size) memcpy(mf[i].data, data, size);
}

T5_FN void mf_mkdir_p(const char *path)
{
    char tmp[NAME_MAX_TEST];
    size_t i;
    snprintf(tmp, sizeof(tmp), "%s", path);
    for (i = 1; tmp[i]; i++)
    {
        if (tmp[i] == '/')
        {
            tmp[i] = 0;
            if (mf_find(tmp) < 0) mf_new(tmp, true);
            tmp[i] = '/';
        }
    }
    if (mf_find(tmp) < 0) mf_new(tmp, true);
}

/* A PTY slave device at host path `path` (the parent directory is created). */
T5_FN int t5_dev_create(const char *path)
{
    char par[NAME_MAX_TEST];
    int i;
    parent_of(path, par, sizeof(par));
    mf_mkdir_p(par);
    i = mf_find(path);
    if (i < 0) i = mf_new(path, false);
    mf[i].dev = true;
    if (mf[i].rx == NULL) mf[i].rx = calloc(1, T5_DEV_BUF);
    if (mf[i].txb == NULL) mf[i].txb = calloc(1, T5_DEV_BUF);
    mf[i].rx_len = mf[i].tx_len = 0;
    return i;
}

/* Bytes written by the test into the master side: they become readable on the slave. */
T5_FN void t5_dev_write(int dev, const void *data, size_t n)
{
    if (dev < 0 || !mf[dev].dev || mf[dev].rx_len + n > T5_DEV_BUF) { UtAssert_Failed("device model overflow"); return; }
    memcpy(mf[dev].rx + mf[dev].rx_len, data, n);
    mf[dev].rx_len += n;
}

T5_FN int32 name_check(const char *path)
{
    if (strlen(base_of(path)) >= OS_MAX_FILE_NAME) return OS_FS_ERR_NAME_TOO_LONG;
    if (strlen(path) >= OS_MAX_PATH_LEN) return OS_FS_ERR_PATH_TOO_LONG;
    return OS_SUCCESS;
}

/* Host path of an OSAL path: through a fixed map when one matches, else unchanged. */
T5_FN void t5_translate(const char *path, char *out, size_t n)
{
    int k;
    for (k = 0; k < 8; k++)
    {
        size_t vl;
        if (!t5_maps[k].used) continue;
        vl = strlen(t5_maps[k].virt);
        if (strncmp(path, t5_maps[k].virt, vl) == 0 && (path[vl] == '/' || path[vl] == 0))
        {
            snprintf(out, n, "%s%s", t5_maps[k].phys, path + vl);
            return;
        }
    }
    snprintf(out, n, "%s", path);
}

T5_FN void H_OS_FileSysAddFixedMap(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t *id = UT_Hook_GetArgValueByName(c, "filesys_id", osal_id_t *);
    const char *phys = UT_Hook_GetArgValueByName(c, "phys_path", const char *);
    const char *virt = UT_Hook_GetArgValueByName(c, "virt_path", const char *);
    int k;
    (void)ud;
    t5_n_fixed_map++;
    for (k = 0; k < 8; k++)
        if (t5_maps[k].used && strcmp(t5_maps[k].virt, virt) == 0) { t5_ret(key, OS_ERR_NAME_TAKEN); return; }
    for (k = 0; k < 8 && t5_maps[k].used; k++) {}
    if (k == 8) { t5_ret(key, OS_ERR_NO_FREE_IDS); return; }
    t5_maps[k].used = true;
    snprintf(t5_maps[k].phys, sizeof(t5_maps[k].phys), "%s", phys);
    snprintf(t5_maps[k].virt, sizeof(t5_maps[k].virt), "%s", virt);
    *id = OS_ObjectIdFromInteger(0x50000ul + (unsigned long)k);
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_TranslatePath(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *v = UT_Hook_GetArgValueByName(c, "VirtualPath", const char *);
    char *l = UT_Hook_GetArgValueByName(c, "LocalPath", char *);
    (void)ud;
    t5_translate(v, l, OS_MAX_LOCAL_PATH_LEN);
    t5_ret(key, name_check(v));
}

T5_FN mfd_t *fd_of(osal_id_t id)
{
    unsigned long v = OS_ObjectIdToInteger(id);
    if (v < T5_FD_BASE || v >= T5_FD_BASE + MFD_MAX || !mfd[v - T5_FD_BASE].used) return NULL;
    return &mfd[v - T5_FD_BASE];
}

T5_FN void H_OS_OpenCreate(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t *fd = UT_Hook_GetArgValueByName(c, "filedes", osal_id_t *);
    const char *vpath = UT_Hook_GetArgValueByName(c, "path", const char *);
    int32 flags = UT_Hook_GetArgValueByName(c, "flags", int32);
    char path[NAME_MAX_TEST];
    int32 rc;
    int i = -1, k;
    char par[NAME_MAX_TEST];
    (void)ud;
    t5_translate(vpath, path, sizeof(path));
    rc = mf_find(path) >= 0 && mf[mf_find(path)].dev ? OS_SUCCESS : name_check(vpath);
    if (rc == OS_SUCCESS && fi_create_fail > 0 && (flags & OS_FILE_FLAG_CREATE) && strstr(path, "/tlm_") != NULL)
    {
        fi_create_fail--;
        rc = OS_ERROR;
    }
    if (rc == OS_SUCCESS)
    {
        i = mf_find(path);
        if (i >= 0 && mf[i].dir) rc = OS_ERROR;
        else if (i < 0 && !(flags & OS_FILE_FLAG_CREATE)) rc = OS_FS_ERR_PATH_INVALID;
        else if (i < 0 && strncmp(path, "/dev/", 5) == 0) rc = OS_FS_ERR_PATH_INVALID; /* devices are never created */
        else if (i < 0)
        {
            parent_of(path, par, sizeof(par));
            k = mf_find(par);
            if (k < 0 || !mf[k].dir) rc = OS_FS_ERR_PATH_INVALID;
            else i = mf_new(path, false);
        }
        else if ((flags & OS_FILE_FLAG_TRUNCATE) && !mf[i].dev)
        {
            mf[i].size = 0;
        }
    }
    if (rc == OS_SUCCESS)
    {
        for (k = 0; k < MFD_MAX && mfd[k].used; k++) {}
        if (k == MFD_MAX) rc = OS_ERR_NO_FREE_IDS;
        else
        {
            mfd[k].used = true;
            mfd[k].file = i;
            mfd[k].pos = 0;
            *fd = OS_ObjectIdFromInteger((unsigned long)k + T5_FD_BASE);
        }
    }
    t5_ret(key, rc);
}

T5_FN uint32 n_open_total(void)
{
    uint32 k, n = 0;
    for (k = 0; k < MFD_MAX; k++) if (mfd[k].used) n++;
    return n;
}

T5_FN void H_OS_FileOpenCheck(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *path = UT_Hook_GetArgValueByName(c, "Filename", const char *);
    int i = mf_find(path), k;
    (void)ud;
    for (k = 0; k < MFD_MAX; k++)
        if (i >= 0 && mfd[k].used && mfd[k].file == i) { t5_ret(key, OS_SUCCESS); return; }
    t5_ret(key, OS_ERROR);
}

T5_FN void H_OS_FDGetInfo(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mfd_t *d = fd_of(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t));
    OS_file_prop_t *p = UT_Hook_GetArgValueByName(c, "fd_prop", OS_file_prop_t *);
    (void)ud;
    if (d == NULL) { t5_ret(key, OS_ERR_INVALID_ID); return; }
    memset(p, 0, sizeof(*p));
    snprintf(p->Path, sizeof(p->Path), "%s", mf[d->file].path);
    p->IsValid = 1;
    t5_ret(key, OS_SUCCESS);
}

/* ------------------------------------------------------------------------- sockets */

#define T5_SOCK_MAX 16
#define T5_DGRAM_Q  64
#define T5_SENT_MAX 1024

typedef struct
{
    bool   used;
    bool   stream;
    uint16 bound_port;          /* 0 = not bound */
    bool   connected;
    uint16 peer_port;
    bool   peer_closed;
    uint8 *dq[T5_DGRAM_Q]; size_t dq_len[T5_DGRAM_Q]; int dq_head, dq_count;
    uint8 *rx; size_t rx_len;   /* stream bytes waiting for the application */
} t5_sock_t;

typedef struct { int sock; uint16 port; char ip[16]; size_t size; uint8 *bytes; bool stream; } t5_sent_t;

T5_VAR t5_sock_t t5_sock[T5_SOCK_MAX];
T5_VAR t5_sent_t t5_sent[T5_SENT_MAX];
T5_VAR int       t5_nsent;
T5_VAR uint16    t5_listen[8];        /* TCP ports the test listens on */
T5_VAR int32     t5_sock_open_rc, t5_bind_rc, t5_sendto_rc;
T5_VAR uint16    t5_bind_fail_port;   /* binding this port fails */
T5_VAR int       t5_blocking_recvs;   /* receives that would wait with nothing pending */
T5_VAR int       t5_n_connect;

T5_FN int t5_sock_index(osal_id_t id)
{
    unsigned long v = OS_ObjectIdToInteger(id);
    if (v < T5_SOCK_BASE || v >= T5_SOCK_BASE + T5_SOCK_MAX || !t5_sock[v - T5_SOCK_BASE].used) return -1;
    return (int)(v - T5_SOCK_BASE);
}

/* Socket address model: port in bytes 0-1, IPv4 in 2-5, a valid flag in 6. */
T5_FN void H_OS_SocketAddrInit(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    OS_SockAddr_t *a = UT_Hook_GetArgValueByName(c, "Addr", OS_SockAddr_t *);
    (void)ud;
    memset(a, 0, sizeof(*a));
    a->ActualLength = 8;
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_SocketAddrSetPort(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    OS_SockAddr_t *a = UT_Hook_GetArgValueByName(c, "Addr", OS_SockAddr_t *);
    uint16 port = UT_Hook_GetArgValueByName(c, "PortNum", uint16);
    (void)ud;
    memcpy(a->AddrData.Buffer, &port, 2);
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_SocketAddrGetPort(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint16 *port = UT_Hook_GetArgValueByName(c, "PortNum", uint16 *);
    const OS_SockAddr_t *a = UT_Hook_GetArgValueByName(c, "Addr", const OS_SockAddr_t *);
    (void)ud;
    memcpy(port, a->AddrData.Buffer, 2);
    t5_ret(key, OS_SUCCESS);
}

T5_FN bool t5_parse_ipv4(const char *s, uint8 out[4])
{
    int part = 0, n = 0, digits = 0;
    const char *p;
    for (p = s; ; p++)
    {
        if (*p >= '0' && *p <= '9')
        {
            n = n * 10 + (*p - '0');
            if (++digits > 3 || n > 255) return false;
        }
        else if (*p == '.' || *p == 0)
        {
            if (digits == 0 || part > 3) return false;
            out[part++] = (uint8)n;
            n = 0;
            digits = 0;
            if (*p == 0) break;
        }
        else return false;
    }
    return part == 4;
}

T5_FN void H_OS_SocketAddrFromString(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    OS_SockAddr_t *a = UT_Hook_GetArgValueByName(c, "Addr", OS_SockAddr_t *);
    const char *s = UT_Hook_GetArgValueByName(c, "string", const char *);
    uint8 ip[4];
    (void)ud;
    if (s == NULL || !t5_parse_ipv4(s, ip)) { t5_ret(key, OS_ERROR); return; }
    memcpy(a->AddrData.Buffer + 2, ip, 4);
    a->AddrData.Buffer[6] = 1;
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_SocketAddrToString(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    char *buf = UT_Hook_GetArgValueByName(c, "buffer", char *);
    size_t n = UT_Hook_GetArgValueByName(c, "buflen", size_t);
    const OS_SockAddr_t *a = UT_Hook_GetArgValueByName(c, "Addr", const OS_SockAddr_t *);
    const uint8 *ip = a->AddrData.Buffer + 2;
    (void)ud;
    snprintf(buf, n, "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
    t5_ret(key, OS_SUCCESS);
}

T5_FN uint16 t5_addr_port(const OS_SockAddr_t *a) { uint16 p; memcpy(&p, a->AddrData.Buffer, 2); return p; }

T5_FN void H_OS_SocketOpen(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t *id = UT_Hook_GetArgValueByName(c, "sock_id", osal_id_t *);
    OS_SocketType_t type = UT_Hook_GetArgValueByName(c, "Type", OS_SocketType_t);
    int k;
    (void)ud;
    if (t5_sock_open_rc != OS_SUCCESS) { t5_ret(key, t5_sock_open_rc); return; }
    for (k = 0; k < T5_SOCK_MAX && t5_sock[k].used; k++) {}
    if (k == T5_SOCK_MAX) { t5_ret(key, OS_ERR_NO_FREE_IDS); return; }
    memset(&t5_sock[k], 0, sizeof(t5_sock[k]));
    t5_sock[k].used = true;
    t5_sock[k].stream = (type == OS_SocketType_STREAM);
    t5_sock[k].rx = calloc(1, T5_DEV_BUF);
    *id = OS_ObjectIdFromInteger((unsigned long)k + T5_SOCK_BASE);
    t5_ret(key, OS_SUCCESS);
}

T5_FN int t5_sock_by_port(uint16 port)
{
    int k;
    for (k = 0; k < T5_SOCK_MAX; k++) if (t5_sock[k].used && !t5_sock[k].stream && t5_sock[k].bound_port == port) return k;
    return -1;
}

T5_FN void H_OS_SocketBind(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k = t5_sock_index(UT_Hook_GetArgValueByName(c, "sock_id", osal_id_t));
    const OS_SockAddr_t *a = UT_Hook_GetArgValueByName(c, "Addr", const OS_SockAddr_t *);
    uint16 port = t5_addr_port(a);
    (void)ud;
    if (k < 0) { t5_ret(key, OS_ERR_INVALID_ID); return; }
    if (t5_bind_rc != OS_SUCCESS || port == t5_bind_fail_port || (port != 0 && t5_sock_by_port(port) >= 0))
    {
        t5_ret(key, t5_bind_rc != OS_SUCCESS ? t5_bind_rc : OS_ERROR);
        return;
    }
    t5_sock[k].bound_port = port;
    t5_ret(key, OS_SUCCESS);
}

T5_FN bool t5_listening(uint16 port)
{
    int k;
    for (k = 0; k < 8; k++) if (t5_listen[k] == port && port != 0) return true;
    return false;
}

T5_FN void t5_connect(UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k = t5_sock_index(UT_Hook_GetArgValueByName(c, "sock_id", osal_id_t));
    const OS_SockAddr_t *a = UT_Hook_GetArgValueByName(c, "Addr", const OS_SockAddr_t *);
    uint16 port = t5_addr_port(a);
    t5_n_connect++;
    if (k < 0) { t5_ret(key, OS_ERR_INVALID_ID); return; }
    if (!t5_sock[k].stream || !t5_listening(port)) { t5_ret(key, OS_ERROR); return; }
    t5_sock[k].connected = true;
    t5_sock[k].peer_port = port;
    t5_sock[k].peer_closed = false;
    t5_sock[k].rx_len = 0;
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_SocketConnect(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c) { (void)ud; t5_connect(key, c); }
T5_FN void H_OS_SocketConnectAbs(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c) { (void)ud; t5_connect(key, c); }

/* The test (as a UDP peer) sends one datagram to the application's port. */
T5_FN bool t5_udp_inject(uint16 port, const void *data, size_t n)
{
    int k = t5_sock_by_port(port), slot;
    if (k < 0) return false;
    if (t5_sock[k].dq_count >= T5_DGRAM_Q) { UtAssert_Failed("datagram queue full"); return false; }
    slot = (t5_sock[k].dq_head + t5_sock[k].dq_count) % T5_DGRAM_Q;
    t5_sock[k].dq[slot] = malloc(n ? n : 1);
    if (n) memcpy(t5_sock[k].dq[slot], data, n);
    t5_sock[k].dq_len[slot] = n;
    t5_sock[k].dq_count++;
    return true;
}

T5_FN int32 t5_recvfrom(int k, void *buf, size_t n, int32 timeout)
{
    int slot;
    size_t len;
    if (k < 0) return OS_ERR_INVALID_ID;
    if (t5_sock[k].stream)
    {
        /* a connected stream socket read with RecvFrom yields its pending stream bytes */
        if (!t5_sock[k].connected) return OS_ERROR;
        if (t5_sock[k].rx_len == 0)
        {
            if (t5_sock[k].peer_closed) return 0;
            if (timeout != 0) t5_blocking_recvs++;
            return OS_ERROR_TIMEOUT;
        }
        len = t5_sock[k].rx_len < n ? t5_sock[k].rx_len : n;
        memcpy(buf, t5_sock[k].rx, len);
        memmove(t5_sock[k].rx, t5_sock[k].rx + len, t5_sock[k].rx_len - len);
        t5_sock[k].rx_len -= len;
        return (int32)len;
    }
    if (t5_sock[k].dq_count == 0)
    {
        if (timeout != 0) t5_blocking_recvs++;
        return OS_ERROR_TIMEOUT;
    }
    slot = t5_sock[k].dq_head;
    len = t5_sock[k].dq_len[slot] < n ? t5_sock[k].dq_len[slot] : n;
    memcpy(buf, t5_sock[k].dq[slot], len);
    free(t5_sock[k].dq[slot]);
    t5_sock[k].dq[slot] = NULL;
    t5_sock[k].dq_head = (slot + 1) % T5_DGRAM_Q;
    t5_sock[k].dq_count--;
    return (int32)len;
}

T5_FN void H_OS_SocketRecvFrom(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k = t5_sock_index(UT_Hook_GetArgValueByName(c, "sock_id", osal_id_t));
    OS_SockAddr_t *ra = UT_Hook_GetArgValueByName(c, "RemoteAddr", OS_SockAddr_t *);
    int32 rc;
    (void)ud;
    rc = t5_recvfrom(k, UT_Hook_GetArgValueByName(c, "buffer", void *), UT_Hook_GetArgValueByName(c, "buflen", size_t),
                     UT_Hook_GetArgValueByName(c, "timeout", int32));
    if (rc >= 0 && ra != NULL) { memset(ra, 0, sizeof(*ra)); ra->ActualLength = 8; }
    t5_ret(key, rc);
}

T5_FN void H_OS_SocketRecvFromAbs(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k = t5_sock_index(UT_Hook_GetArgValueByName(c, "sock_id", osal_id_t));
    OS_SockAddr_t *ra = UT_Hook_GetArgValueByName(c, "RemoteAddr", OS_SockAddr_t *);
    OS_time_t abs = UT_Hook_GetArgValueByName(c, "abs_timeout", OS_time_t);
    int32 rc;
    (void)ud;
    rc = t5_recvfrom(k, UT_Hook_GetArgValueByName(c, "buffer", void *), UT_Hook_GetArgValueByName(c, "buflen", size_t),
                     OS_TimeGetTotalMilliseconds(abs) > (int64)t5_now_sec * 1000 ? 1 : 0);
    if (rc >= 0 && ra != NULL) { memset(ra, 0, sizeof(*ra)); ra->ActualLength = 8; }
    t5_ret(key, rc);
}

T5_FN void H_OS_SocketSendTo(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k = t5_sock_index(UT_Hook_GetArgValueByName(c, "sock_id", osal_id_t));
    const void *buf = UT_Hook_GetArgValueByName(c, "buffer", const void *);
    size_t n = UT_Hook_GetArgValueByName(c, "buflen", size_t);
    const OS_SockAddr_t *a = UT_Hook_GetArgValueByName(c, "RemoteAddr", const OS_SockAddr_t *);
    const uint8 *ip = a->AddrData.Buffer + 2;
    (void)ud;
    if (k < 0) { t5_ret(key, OS_ERR_INVALID_ID); return; }
    if (t5_sendto_rc != OS_SUCCESS) { t5_ret(key, t5_sendto_rc); return; }
    if (t5_nsent < T5_SENT_MAX)
    {
        t5_sent[t5_nsent].sock = k;
        t5_sent[t5_nsent].port = t5_addr_port(a);
        snprintf(t5_sent[t5_nsent].ip, 16, "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
        t5_sent[t5_nsent].size = n;
        t5_sent[t5_nsent].bytes = malloc(n ? n : 1);
        memcpy(t5_sent[t5_nsent].bytes, buf, n);
        t5_sent[t5_nsent].stream = false;
        t5_nsent++;
    }
    t5_ret(key, (int32)n);
}

/* The test (as the TCP server) writes stream bytes to the connection to `port`. */
T5_FN bool t5_tcp_write(uint16 port, const void *data, size_t n)
{
    int k;
    for (k = 0; k < T5_SOCK_MAX; k++)
    {
        if (t5_sock[k].used && t5_sock[k].stream && t5_sock[k].connected && t5_sock[k].peer_port == port)
        {
            if (t5_sock[k].rx_len + n > T5_DEV_BUF) { UtAssert_Failed("stream model overflow"); return false; }
            memcpy(t5_sock[k].rx + t5_sock[k].rx_len, data, n);
            t5_sock[k].rx_len += n;
            return true;
        }
    }
    return false;
}

T5_FN void t5_tcp_close_peer(uint16 port)
{
    int k;
    for (k = 0; k < T5_SOCK_MAX; k++)
        if (t5_sock[k].used && t5_sock[k].stream && t5_sock[k].connected && t5_sock[k].peer_port == port)
            t5_sock[k].peer_closed = true;
}

/* Collected stream bytes the application wrote to TCP port `port` (or to device `dev` when port is 0). */
T5_FN size_t t5_stream_out(uint16 port, int dev, uint8 *out, size_t max)
{
    size_t n = 0;
    int i;
    if (port == 0)
    {
        if (dev < 0 || !mf[dev].dev) return 0;
        n = mf[dev].tx_len < max ? mf[dev].tx_len : max;
        memcpy(out, mf[dev].txb, n);
        return n;
    }
    for (i = 0; i < t5_nsent; i++)
    {
        if (!t5_sent[i].stream || t5_sent[i].port != port) continue;
        if (n + t5_sent[i].size > max) break;
        memcpy(out + n, t5_sent[i].bytes, t5_sent[i].size);
        n += t5_sent[i].size;
    }
    return n;
}

T5_FN void t5_dev_clear_out(int dev) { if (dev >= 0 && mf[dev].dev) mf[dev].tx_len = 0; }

/* Reads and writes on files, devices and stream sockets. */
T5_FN int32 do_write(osal_id_t id, const void *buf, size_t n)
{
    mfd_t *d = fd_of(id);
    mfile_t *f;
    int s = t5_sock_index(id);
    if (s >= 0)
    {
        if (!t5_sock[s].stream || !t5_sock[s].connected) return OS_ERROR;
        if (t5_sock[s].peer_closed) return OS_ERROR;
        if (t5_nsent < T5_SENT_MAX)
        {
            t5_sent[t5_nsent].sock = s;
            t5_sent[t5_nsent].port = t5_sock[s].peer_port;
            snprintf(t5_sent[t5_nsent].ip, 16, "127.0.0.1");
            t5_sent[t5_nsent].size = n;
            t5_sent[t5_nsent].bytes = malloc(n ? n : 1);
            memcpy(t5_sent[t5_nsent].bytes, buf, n);
            t5_sent[t5_nsent].stream = true;
            t5_nsent++;
        }
        return (int32)n;
    }
    if (d == NULL) return OS_ERR_INVALID_ID;
    f = &mf[d->file];
    if (f->dev)
    {
        if (f->tx_len + n > T5_DEV_BUF) return OS_ERROR;
        memcpy(f->txb + f->tx_len, buf, n);
        f->tx_len += n;
        return (int32)n;
    }
    if (fi_write_short) return 0;
    mf_grow(f, d->pos + n);
    memcpy(f->data + d->pos, buf, n);
    d->pos += n;
    return (int32)n;
}

T5_FN int32 do_read(osal_id_t id, void *buf, size_t n, int32 timeout)
{
    mfd_t *d = fd_of(id);
    mfile_t *f;
    size_t avail;
    int s = t5_sock_index(id);
    if (s >= 0)
    {
        if (!t5_sock[s].stream) return t5_recvfrom(s, buf, n, timeout);
        if (!t5_sock[s].connected) return OS_ERROR;
        if (t5_sock[s].rx_len == 0)
        {
            if (t5_sock[s].peer_closed) return 0;
            if (timeout != 0) t5_blocking_recvs++;
            return OS_ERROR_TIMEOUT;
        }
        avail = t5_sock[s].rx_len < n ? t5_sock[s].rx_len : n;
        memcpy(buf, t5_sock[s].rx, avail);
        memmove(t5_sock[s].rx, t5_sock[s].rx + avail, t5_sock[s].rx_len - avail);
        t5_sock[s].rx_len -= avail;
        return (int32)avail;
    }
    if (d == NULL) return OS_ERR_INVALID_ID;
    f = &mf[d->file];
    if (f->dev)
    {
        if (f->rx_len == 0)
        {
            if (timeout != 0) t5_dev_blocking_reads++;
            return OS_ERROR_TIMEOUT;
        }
        avail = f->rx_len < n ? f->rx_len : n;
        memcpy(buf, f->rx, avail);
        memmove(f->rx, f->rx + avail, f->rx_len - avail);
        f->rx_len -= avail;
        return (int32)avail;
    }
    if (fi_read_fail > 0) { fi_read_fail--; return OS_ERROR; }
    avail = d->pos < f->size ? f->size - d->pos : 0;
    if (n > avail) n = avail;
    if (n) memcpy(buf, f->data + d->pos, n);
    d->pos += n;
    return (int32)n;
}

T5_FN void H_OS_write(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    t5_ret(key, do_write(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t),
                         UT_Hook_GetArgValueByName(c, "buffer", const void *),
                         UT_Hook_GetArgValueByName(c, "nbytes", size_t)));
}

T5_FN void H_OS_read(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    /* OS_read waits until data arrives: on an idle device or stream that would block the caller. */
    t5_ret(key, do_read(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t),
                        UT_Hook_GetArgValueByName(c, "buffer", void *),
                        UT_Hook_GetArgValueByName(c, "nbytes", size_t), OS_PEND));
}

T5_FN void H_OS_TimedRead(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    t5_ret(key, do_read(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t),
                        UT_Hook_GetArgValueByName(c, "buffer", void *),
                        UT_Hook_GetArgValueByName(c, "nbytes", size_t),
                        UT_Hook_GetArgValueByName(c, "timeout", int32)));
}

T5_FN void H_OS_TimedWrite(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    t5_ret(key, do_write(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t),
                         UT_Hook_GetArgValueByName(c, "buffer", const void *),
                         UT_Hook_GetArgValueByName(c, "nbytes", size_t)));
}

T5_FN bool t5_readable(osal_id_t id)
{
    mfd_t *d = fd_of(id);
    int s = t5_sock_index(id);
    if (s >= 0)
        return t5_sock[s].stream ? (t5_sock[s].rx_len > 0 || t5_sock[s].peer_closed) : t5_sock[s].dq_count > 0;
    if (d == NULL) return false;
    return mf[d->file].dev ? mf[d->file].rx_len > 0 : true;
}

T5_FN void H_OS_SelectSingle(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t id = UT_Hook_GetArgValueByName(c, "objid", osal_id_t);
    uint32 *flags = UT_Hook_GetArgValueByName(c, "StateFlags", uint32 *);
    int32 msecs = UT_Hook_GetArgValueByName(c, "msecs", int32);
    uint32 want = *flags, got = 0;
    (void)ud;
    if (fd_of(id) == NULL && t5_sock_index(id) < 0) { t5_ret(key, OS_ERR_INVALID_ID); return; }
    if ((want & OS_STREAM_STATE_READABLE) && t5_readable(id)) got |= OS_STREAM_STATE_READABLE;
    if (want & OS_STREAM_STATE_WRITABLE) got |= OS_STREAM_STATE_WRITABLE;
    *flags = got;
    if (got == 0)
    {
        if (msecs != 0) t5_dev_waiting_selects++;
        t5_ret(key, OS_ERROR_TIMEOUT);
        return;
    }
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_close(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t id = UT_Hook_GetArgValueByName(c, "filedes", osal_id_t);
    mfd_t *d = fd_of(id);
    int s = t5_sock_index(id), q;
    (void)ud;
    if (s >= 0)
    {
        for (q = 0; q < T5_DGRAM_Q; q++) free(t5_sock[s].dq[q]);
        free(t5_sock[s].rx);
        memset(&t5_sock[s], 0, sizeof(t5_sock[s]));
        t5_ret(key, OS_SUCCESS);
        return;
    }
    if (d == NULL) { t5_ret(key, OS_ERR_INVALID_ID); return; }
    d->used = false;
    n_close++;
    t5_ret(key, OS_SUCCESS);
}

/* ES cleans up the OSAL streams (files, sockets) of a deleted application; fixed maps stay. */
T5_FN void t5_close_app_resources(void)
{
    int k, q;
    for (k = 0; k < MFD_MAX; k++) mfd[k].used = false;
    for (k = 0; k < T5_SOCK_MAX; k++)
    {
        if (!t5_sock[k].used) continue;
        for (q = 0; q < T5_DGRAM_Q; q++) free(t5_sock[k].dq[q]);
        free(t5_sock[k].rx);
        memset(&t5_sock[k], 0, sizeof(t5_sock[k]));
    }
}

T5_FN void H_OS_remove(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *path = UT_Hook_GetArgValueByName(c, "path", const char *);
    int i = mf_find(path);
    (void)ud;
    if (i < 0 || mf[i].dir) { t5_ret(key, OS_ERROR); return; }
    mf_free(i);
    n_remove++;
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_mkdir(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *path = UT_Hook_GetArgValueByName(c, "path", const char *);
    char par[NAME_MAX_TEST];
    int32 rc = name_check(path);
    int k;
    (void)ud;
    if (rc == OS_SUCCESS && fi_mkdir_fail) rc = OS_ERROR;
    if (rc == OS_SUCCESS && mf_find(path) >= 0) rc = OS_ERROR;
    if (rc == OS_SUCCESS)
    {
        parent_of(path, par, sizeof(par));
        k = mf_find(par);
        if (k < 0 || !mf[k].dir) rc = OS_ERROR;
        else mf_new(path, true);
    }
    t5_ret(key, rc);
}

T5_FN void H_OS_rmdir(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *path = UT_Hook_GetArgValueByName(c, "path", const char *);
    int i = mf_find(path), k;
    size_t n = strlen(path);
    (void)ud;
    if (i < 0 || !mf[i].dir) { t5_ret(key, OS_ERROR); return; }
    for (k = 0; k < MF_MAX; k++)
        if (mf[k].used && k != i && strncmp(mf[k].path, path, n) == 0 && mf[k].path[n] == '/') { t5_ret(key, OS_ERROR); return; }
    mf_free(i);
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_stat(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *path = UT_Hook_GetArgValueByName(c, "path", const char *);
    os_fstat_t *st = UT_Hook_GetArgValueByName(c, "filestats", os_fstat_t *);
    int i = mf_find(path);
    (void)ud;
    if (i < 0) { t5_ret(key, OS_FS_ERR_PATH_INVALID); return; }
    memset(st, 0, sizeof(*st));
    st->FileModeBits = OS_FILESTAT_MODE_READ | OS_FILESTAT_MODE_WRITE | (mf[i].dir ? OS_FILESTAT_MODE_DIR : 0);
    st->FileSize = mf[i].dir ? 0 : mf[i].size;
    t5_ret(key, OS_SUCCESS);
}

T5_FN mdir_t *dir_of(osal_id_t id)
{
    unsigned long v = OS_ObjectIdToInteger(id);
    if (v < T5_DIR_BASE || v >= T5_DIR_BASE + MDIR_MAX || !mdir[v - T5_DIR_BASE].used) return NULL;
    return &mdir[v - T5_DIR_BASE];
}

T5_FN void H_OS_DirectoryOpen(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t *id = UT_Hook_GetArgValueByName(c, "dir_id", osal_id_t *);
    const char *path = UT_Hook_GetArgValueByName(c, "path", const char *);
    int i = mf_find(path), k;
    (void)ud;
    if (i < 0 || !mf[i].dir) { t5_ret(key, OS_FS_ERR_PATH_INVALID); return; }
    for (k = 0; k < MDIR_MAX && mdir[k].used; k++) {}
    if (k == MDIR_MAX) { t5_ret(key, OS_ERR_NO_FREE_IDS); return; }
    mdir[k].used = true;
    snprintf(mdir[k].path, sizeof(mdir[k].path), "%s", path);
    mdir[k].next_order = 0;
    *id = OS_ObjectIdFromInteger((unsigned long)k + T5_DIR_BASE);
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_DirectoryRead(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mdir_t *d = dir_of(UT_Hook_GetArgValueByName(c, "dir_id", osal_id_t));
    os_dirent_t *ent = UT_Hook_GetArgValueByName(c, "dirent", os_dirent_t *);
    int k, best = -1;
    char par[NAME_MAX_TEST];
    (void)ud;
    if (d == NULL) { t5_ret(key, OS_ERR_INVALID_ID); return; }
    for (k = 0; k < MF_MAX; k++)
    {
        if (!mf[k].used || mf[k].order <= d->next_order) continue;
        parent_of(mf[k].path, par, sizeof(par));
        if (strcmp(par, d->path) != 0) continue;
        if (best < 0 || mf[k].order < mf[best].order) best = k;
    }
    if (best < 0) { t5_ret(key, OS_ERROR); return; }
    d->next_order = mf[best].order;
    memset(ent, 0, sizeof(*ent));
    snprintf(OS_DIRENTRY_NAME(*ent), sizeof(OS_DIRENTRY_NAME(*ent)), "%s", base_of(mf[best].path));
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_DirectoryClose(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mdir_t *d = dir_of(UT_Hook_GetArgValueByName(c, "dir_id", osal_id_t));
    (void)ud;
    if (d == NULL) { t5_ret(key, OS_ERR_INVALID_ID); return; }
    d->used = false;
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_DirectoryRewind(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mdir_t *d = dir_of(UT_Hook_GetArgValueByName(c, "dir_id", osal_id_t));
    (void)ud;
    if (d == NULL) { t5_ret(key, OS_ERR_INVALID_ID); return; }
    d->next_order = 0;
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_lseek(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mfd_t *d = fd_of(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t));
    long off = (long)UT_Hook_GetArgValueByName(c, "offset", osal_offset_t);
    uint32 wh = UT_Hook_GetArgValueByName(c, "whence", uint32);
    (void)ud;
    if (d == NULL) { t5_ret(key, OS_ERR_INVALID_ID); return; }
    if (wh == OS_SEEK_SET) d->pos = (size_t)off;
    else if (wh == OS_SEEK_CUR) d->pos = (size_t)((long)d->pos + off);
    else d->pos = (size_t)((long)mf[d->file].size + off);
    t5_ret(key, (int32)d->pos);
}

T5_FN int32 do_cp(const char *src, const char *dst, bool move)
{
    int i = mf_find(src), j, k;
    char par[NAME_MAX_TEST];
    int32 rc = name_check(dst);
    if (rc != OS_SUCCESS) return rc;
    if (move && fi_rename_fail > 0) { fi_rename_fail--; return OS_ERROR; }
    if (i < 0 || mf[i].dir || mf_find(dst) >= 0) return OS_ERROR;
    parent_of(dst, par, sizeof(par));
    k = mf_find(par);
    if (k < 0 || !mf[k].dir) return OS_ERROR;
    j = mf_new(dst, false);
    mf_grow(&mf[j], mf[i].size);
    if (mf[i].size) memcpy(mf[j].data, mf[i].data, mf[i].size);
    if (move) mf_free(i);
    return OS_SUCCESS;
}

T5_FN void H_OS_cp(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    t5_ret(key, do_cp(UT_Hook_GetArgValueByName(c, "src", const char *), UT_Hook_GetArgValueByName(c, "dest", const char *), false));
}

T5_FN void H_OS_mv(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    t5_ret(key, do_cp(UT_Hook_GetArgValueByName(c, "src", const char *), UT_Hook_GetArgValueByName(c, "dest", const char *), true));
}

T5_FN void H_OS_rename(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    t5_ret(key, do_cp(UT_Hook_GetArgValueByName(c, "old_filename", const char *),
                      UT_Hook_GetArgValueByName(c, "new_filename", const char *), true));
}

T5_FN void H_OS_FileSysStatVolume(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    OS_statvfs_t *st = UT_Hook_GetArgValueByName(c, "statbuf", OS_statvfs_t *);
    (void)ud;
    memset(st, 0, sizeof(*st));
    st->block_size = 512;
    st->total_blocks = 1u << 20;
    st->blocks_free = 1u << 20;
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_CloseFileByName(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *path = UT_Hook_GetArgValueByName(c, "Filename", const char *);
    int i = mf_find(path), k;
    (void)ud;
    if (i < 0) { t5_ret(key, OS_FS_ERR_PATH_INVALID); return; }
    for (k = 0; k < MFD_MAX; k++) if (mfd[k].used && mfd[k].file == i) { mfd[k].used = false; n_close++; }
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_CloseAllFiles(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k;
    (void)ud; (void)c;
    for (k = 0; k < MFD_MAX; k++) if (mfd[k].used) { mfd[k].used = false; n_close++; }
    t5_ret(key, OS_SUCCESS);
}

T5_FN void H_OS_chmod(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    t5_ret(key, mf_find(UT_Hook_GetArgValueByName(c, "path", const char *)) >= 0 ? OS_SUCCESS : OS_ERROR);
}

/* ------------------------------------------------------------------------- file headers */

T5_FN void H_FS_InitHeader(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_FS_Header_t *h = UT_Hook_GetArgValueByName(c, "Hdr", CFE_FS_Header_t *);
    const char *d = UT_Hook_GetArgValueByName(c, "Description", const char *);
    (void)ud; (void)key;
    memset(h, 0, sizeof(*h));
    h->ContentType = 0x63464531;
    h->SubType = UT_Hook_GetArgValueByName(c, "SubType", uint32);
    h->Length = sizeof(*h);
    if (d != NULL) strncpy(h->Description, d, sizeof(h->Description) - 1);
}

T5_FN void H_FS_WriteHeader(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t fd = UT_Hook_GetArgValueByName(c, "FileDes", osal_id_t);
    CFE_FS_Header_t *h = UT_Hook_GetArgValueByName(c, "Hdr", CFE_FS_Header_t *);
    CFE_FS_Header_t out = *h;
    (void)ud;
    out.Length = sizeof(out);
    t5_ret(key, do_write(fd, &out, sizeof(out)) == (int32)sizeof(out) ? (int32)sizeof(out) : CFE_FS_BAD_ARGUMENT);
}

T5_FN void H_FS_ReadHeader(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_FS_Header_t *h = UT_Hook_GetArgValueByName(c, "Hdr", CFE_FS_Header_t *);
    int32 n = do_read(UT_Hook_GetArgValueByName(c, "FileDes", osal_id_t), h, sizeof(*h), 0);
    (void)ud;
    t5_ret(key, n == (int32)sizeof(*h) ? n : CFE_FS_BAD_ARGUMENT);
}

/* ------------------------------------------------------------------------- ES, PSP */

T5_VAR uint32 t5_node_id = 1;

T5_FN void H_PSP_GetProcessorId(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    UT_Stub_SetReturnValue(key, t5_node_id);
}

T5_FN void H_ES_Ok(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    t5_ret(key, CFE_SUCCESS);
}

/* ------------------------------------------------------------------------- cmdval_lib reference model */

#ifdef T5_MODEL_CMDVAL

T5_VAR int  cmdval_n_validate, cmdval_n_hmac, cmdval_n_crc, cmdval_n_init;
T5_VAR int  cmdval_force_status = -1;  /* >= 0: CMDVAL_ValidateFrame returns this status */
T5_VAR uint32 cmdval_last_now;         /* NowSeconds of the last validation */
T5_VAR uint8  cmdval_last_key[32];     /* key of the last validation's configuration */

static const uint32 t5_k256[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};

#define T5_ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

T5_FN void t5_sha256_block(uint32 h[8], const uint8 *p)
{
    uint32 w[64], a, b, c, d, e, f, g, hh, t1, t2;
    int i;
    for (i = 0; i < 16; i++) w[i] = t5_be32(p + 4 * i);
    for (i = 16; i < 64; i++)
    {
        uint32 s0 = T5_ROR(w[i - 15], 7) ^ T5_ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32 s1 = T5_ROR(w[i - 2], 17) ^ T5_ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = h[0]; b = h[1]; c = h[2]; d = h[3]; e = h[4]; f = h[5]; g = h[6]; hh = h[7];
    for (i = 0; i < 64; i++)
    {
        t1 = hh + (T5_ROR(e, 6) ^ T5_ROR(e, 11) ^ T5_ROR(e, 25)) + ((e & f) ^ (~e & g)) + t5_k256[i] + w[i];
        t2 = (T5_ROR(a, 2) ^ T5_ROR(a, 13) ^ T5_ROR(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
}

/* SHA-256 of the concatenation of two buffers (the second may be empty). */
T5_FN void t5_sha256_2(const uint8 *d1, size_t n1, const uint8 *d2, size_t n2, uint8 out[32])
{
    uint32 h[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    uint8 blk[64];
    size_t total = n1 + n2, i, fill = 0;
    uint64 bits = (uint64)total * 8;
    for (i = 0; i < total; i++)
    {
        blk[fill++] = i < n1 ? d1[i] : d2[i - n1];
        if (fill == 64) { t5_sha256_block(h, blk); fill = 0; }
    }
    blk[fill++] = 0x80;
    if (fill > 56) { while (fill < 64) blk[fill++] = 0; t5_sha256_block(h, blk); fill = 0; }
    while (fill < 56) blk[fill++] = 0;
    for (i = 0; i < 8; i++) blk[56 + i] = (uint8)(bits >> (56 - 8 * i));
    t5_sha256_block(h, blk);
    for (i = 0; i < 8; i++) t5_wbe32(out + 4 * i, h[i]);
}

T5_FN void t5_hmac(const uint8 *key, size_t klen, const uint8 *data, size_t n, uint8 mac[32])
{
    uint8 k[64], ipad[64], opad[64], inner[32];
    size_t i;
    memset(k, 0, sizeof(k));
    if (klen > 64) t5_sha256_2(key, klen, NULL, 0, k);
    else memcpy(k, key, klen);
    for (i = 0; i < 64; i++) { ipad[i] = k[i] ^ 0x36; opad[i] = k[i] ^ 0x5C; }
    t5_sha256_2(ipad, 64, data, n, inner);
    t5_sha256_2(opad, 64, inner, 32, mac);
}

T5_FN uint16 t5_crc16(const uint8 *d, size_t n)
{
    uint16 crc = 0xFFFF;
    size_t i;
    int b;
    for (i = 0; i < n; i++)
    {
        crc ^= (uint16)(d[i] << 8);
        for (b = 0; b < 8; b++) crc = (crc & 0x8000) ? (uint16)((crc << 1) ^ 0x1021) : (uint16)(crc << 1);
    }
    return crc;
}

/* The pinned API (spec S3.2), with the layout of CMDVAL_Config_t and CMDVAL_FrameInfo_t. */
typedef struct
{
    uint8  HmacKey[32];
    uint16 AllowedSources[8];
    uint32 MaxAgeSeconds;
    uint8  EnableMask;
    uint8  Spare[3];
} t5_cmdval_config_t;

typedef struct
{
    uint16 SourceId;
    uint32 TimeSeconds;
    uint32 Sequence;
    const uint8 *Command;
    size_t CommandLen;
} t5_cmdval_info_t;

int32 CMDVAL_LibInit(void);
uint16 CMDVAL_Crc16(const uint8 *Data, size_t Len);
void CMDVAL_Sha256(const uint8 *Data, size_t Len, uint8 Digest[32]);
void CMDVAL_HmacSha256(const uint8 *Key, size_t KeyLen, const uint8 *Data, size_t Len, uint8 Mac[32]);
int CMDVAL_CheckSource(uint16 SourceId, const t5_cmdval_config_t *Config);
int CMDVAL_CheckAge(uint32 FrameSeconds, uint32 NowSeconds, uint32 MaxAgeSeconds);
int CMDVAL_ValidateFrame(const uint8 *Frame, size_t FrameLen, const t5_cmdval_config_t *Config, uint32 NowSeconds,
                         t5_cmdval_info_t *Info);

int32 CMDVAL_LibInit(void) { cmdval_n_init++; return CFE_SUCCESS; }
uint16 CMDVAL_Crc16(const uint8 *Data, size_t Len) { cmdval_n_crc++; return t5_crc16(Data, Len); }
void CMDVAL_Sha256(const uint8 *Data, size_t Len, uint8 Digest[32]) { t5_sha256_2(Data, Len, NULL, 0, Digest); }
void CMDVAL_HmacSha256(const uint8 *Key, size_t KeyLen, const uint8 *Data, size_t Len, uint8 Mac[32])
{
    cmdval_n_hmac++;
    t5_hmac(Key, KeyLen, Data, Len, Mac);
}

int CMDVAL_CheckSource(uint16 SourceId, const t5_cmdval_config_t *Config)
{
    int i;
    if (SourceId == 0) return 4;
    for (i = 0; i < 8; i++) if (Config->AllowedSources[i] != 0 && Config->AllowedSources[i] == SourceId) return 0;
    return 4;
}

int CMDVAL_CheckAge(uint32 FrameSeconds, uint32 NowSeconds, uint32 MaxAgeSeconds)
{
    int64 d = (int64)NowSeconds - (int64)FrameSeconds;
    if (d < 0) d = -d;
    return d <= (int64)MaxAgeSeconds ? 0 : 5;
}

int CMDVAL_ValidateFrame(const uint8 *Frame, size_t FrameLen, const t5_cmdval_config_t *Config, uint32 NowSeconds,
                         t5_cmdval_info_t *Info)
{
    uint16 len;
    uint8 mac[32];
    uint8 diff = 0;
    size_t i;
    cmdval_n_validate++;
    cmdval_last_now = NowSeconds;
    memcpy(cmdval_last_key, Config->HmacKey, 32);
    if (cmdval_force_status >= 0) return cmdval_force_status;
    if (FrameLen < 58) return 1;
    len = t5_be16(Frame + 6);
    if (t5_be16(Frame) != 0x4356 || Frame[2] != 1 || Frame[3] != 0) return 1;
    if (len < 8 || len > 1024 || FrameLen != 50u + len) return 1;
    if ((size_t)t5_be16(Frame + 16 + 4) + 7 != len || (Frame[16] & 0x10) == 0) return 1;
    if ((Config->EnableMask & 0x01) && t5_crc16(Frame, 48u + len) != t5_be16(Frame + 48 + len)) return 2;
    if (Config->EnableMask & 0x02)
    {
        t5_hmac(Config->HmacKey, 32, Frame, 16u + len, mac);
        for (i = 0; i < 32; i++) diff |= (uint8)(mac[i] ^ Frame[16 + len + i]);
        if (diff != 0) return 3;
    }
    if ((Config->EnableMask & 0x04) && CMDVAL_CheckSource(t5_be16(Frame + 4), Config) != 0) return 4;
    if ((Config->EnableMask & 0x08) && CMDVAL_CheckAge(t5_be32(Frame + 8), NowSeconds, Config->MaxAgeSeconds) != 0) return 5;
    if (Info != NULL)
    {
        Info->SourceId = t5_be16(Frame + 4);
        Info->TimeSeconds = t5_be32(Frame + 8);
        Info->Sequence = t5_be32(Frame + 12);
        Info->Command = Frame + 16;
        Info->CommandLen = len;
    }
    return 0;
}

/* Build a frame around `cmd` (spec S3.1) into `out`; returns its length. */
T5_FN size_t t5_frame(uint8 *out, const uint8 *cmd, size_t clen, const uint8 *key, uint16 source, uint32 secs, uint32 seq)
{
    t5_wbe16(out, 0x4356);
    out[2] = 1;
    out[3] = 0;
    t5_wbe16(out + 4, source);
    t5_wbe16(out + 6, (uint16)clen);
    t5_wbe32(out + 8, secs);
    t5_wbe32(out + 12, seq);
    memcpy(out + 16, cmd, clen);
    t5_hmac(key, 32, out, 16 + clen, out + 16 + clen);
    t5_wbe16(out + 48 + clen, t5_crc16(out, 48 + clen));
    return 50 + clen;
}

/* Verify a frame the application produced; returns 0 or the CMDVAL status of the first problem. */
T5_FN int t5_check_frame(const uint8 *f, size_t n, const uint8 *key)
{
    t5_cmdval_config_t cfg;
    int save = cmdval_force_status, rc, saved_n = cmdval_n_validate;
    memset(&cfg, 0, sizeof(cfg));
    memcpy(cfg.HmacKey, key, 32);
    cfg.EnableMask = 0x03;
    cmdval_force_status = -1;
    rc = CMDVAL_ValidateFrame(f, n, &cfg, 0, NULL);
    cmdval_force_status = save;
    cmdval_n_validate = saved_n;
    return rc;
}

#endif /* T5_MODEL_CMDVAL */

/* ------------------------------------------------------------------------- set-up */

T5_FN void t5_register_hooks(void)
{
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetTime), H_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetMET), H_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetTAI), H_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetUTC), H_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_Add), H_TIME_Add, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_Subtract), H_TIME_Subtract, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_Compare), H_TIME_Compare, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_Sub2MicroSecs), H_TIME_Sub2MicroSecs, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_Micro2SubSecs), H_TIME_Micro2SubSecs, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_GetLocalTime), H_OS_GetLocalTime, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_PSP_GetTime), H_PSP_GetTime, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_PSP_GetProcessorId), H_PSP_GetProcessorId, NULL);

    UT_SetHandlerFunction(UT_KEY(CFE_MSG_Init), H_MSG_Init, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetMsgId), H_MSG_GetMsgId, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetMsgId), H_MSG_SetMsgId, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetSize), H_MSG_GetSize, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetSize), H_MSG_SetSize, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetType), H_MSG_GetType, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetType), H_MSG_SetType, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetFcnCode), H_MSG_GetFcnCode, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetFcnCode), H_MSG_SetFcnCode, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GenerateChecksum), H_MSG_GenerateChecksum, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_ValidateChecksum), H_MSG_ValidateChecksum, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetMsgTime), H_MSG_SetMsgTime, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetMsgTime), H_MSG_GetMsgTime, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_TimeStampMsg), H_SB_TimeStampMsg, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_GetUserData), H_SB_GetUserData, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_GetUserDataLength), H_SB_GetUserDataLength, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_IsValidMsgId), H_SB_IsValidMsgId, NULL);

    UT_SetHandlerFunction(UT_KEY(CFE_SB_CreatePipe), H_SB_CreatePipe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_DeletePipe), H_SB_DeletePipe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_Subscribe), H_SB_Subscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_SubscribeEx), H_SB_SubscribeEx, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_SubscribeLocal), H_SB_SubscribeEx, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_Unsubscribe), H_SB_Unsubscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_UnsubscribeLocal), H_SB_Unsubscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_TransmitMsg), H_SB_TransmitMsg, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_AllocateMessageBuffer), H_SB_AllocateMessageBuffer, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_ReleaseMessageBuffer), H_SB_Ok, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_TransmitBuffer), H_SB_TransmitBuffer, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_ReceiveBuffer), H_SB_ReceiveBuffer, NULL);

    UT_SetHandlerFunction(UT_KEY(CFE_ES_RunLoop), H_ES_RunLoop, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_ES_ExitApp), H_ES_ExitApp, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_ES_WaitForStartupSync), H_ES_Ok, NULL);
    UT_SetVaHookFunction(UT_KEY(CFE_EVS_SendEvent), t5_ev_hook, NULL);
    UT_SetVaHookFunction(UT_KEY(CFE_EVS_SendEventWithAppID), t5_ev_hook, NULL);
    UT_SetVaHookFunction(UT_KEY(CFE_EVS_SendTimedEvent), t5_ev_hook, NULL);

    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Register), H_TBL_Register, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Load), H_TBL_Load, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Manage), H_TBL_Manage, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Update), H_TBL_Update, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_GetStatus), H_TBL_GetStatus, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_GetAddress), H_TBL_GetAddress, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_ReleaseAddress), H_TBL_ReleaseAddress, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Share), H_TBL_Share, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Unregister), H_TBL_Unregister, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Validate), H_SB_Ok, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Modified), H_SB_Ok, NULL);

    UT_SetHandlerFunction(UT_KEY(OS_OpenCreate), H_OS_OpenCreate, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_write), H_OS_write, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_TimedWrite), H_OS_TimedWrite, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_read), H_OS_read, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_TimedRead), H_OS_TimedRead, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_close), H_OS_close, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_remove), H_OS_remove, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_mkdir), H_OS_mkdir, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_rmdir), H_OS_rmdir, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_stat), H_OS_stat, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_DirectoryOpen), H_OS_DirectoryOpen, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_DirectoryRead), H_OS_DirectoryRead, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_DirectoryClose), H_OS_DirectoryClose, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_DirectoryRewind), H_OS_DirectoryRewind, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_lseek), H_OS_lseek, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_cp), H_OS_cp, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_mv), H_OS_mv, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_rename), H_OS_rename, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_FileOpenCheck), H_OS_FileOpenCheck, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_FDGetInfo), H_OS_FDGetInfo, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_FileSysStatVolume), H_OS_FileSysStatVolume, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_CloseFileByName), H_OS_CloseFileByName, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_CloseAllFiles), H_OS_CloseAllFiles, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_TranslatePath), H_OS_TranslatePath, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_FileSysAddFixedMap), H_OS_FileSysAddFixedMap, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_chmod), H_OS_chmod, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SelectSingle), H_OS_SelectSingle, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_FS_InitHeader), H_FS_InitHeader, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_FS_WriteHeader), H_FS_WriteHeader, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_FS_ReadHeader), H_FS_ReadHeader, NULL);

    UT_SetHandlerFunction(UT_KEY(OS_SocketOpen), H_OS_SocketOpen, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketBind), H_OS_SocketBind, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketBindAddress), H_OS_SocketBind, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketConnect), H_OS_SocketConnect, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketConnectAbs), H_OS_SocketConnectAbs, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketRecvFrom), H_OS_SocketRecvFrom, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketRecvFromAbs), H_OS_SocketRecvFromAbs, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketSendTo), H_OS_SocketSendTo, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketAddrInit), H_OS_SocketAddrInit, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketAddrSetPort), H_OS_SocketAddrSetPort, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketAddrGetPort), H_OS_SocketAddrGetPort, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketAddrFromString), H_OS_SocketAddrFromString, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_SocketAddrToString), H_OS_SocketAddrToString, NULL);

    T5_UNMODELLED(OS_ShellOutputToFile);
    T5_UNMODELLED(OS_FS_GetPhysDriveName);
    T5_UNMODELLED(OS_TaskCreate);
    T5_UNMODELLED(OS_SelectMultiple);
    T5_UNMODELLED(OS_SelectMultipleAbs);
    T5_UNMODELLED(OS_SelectSingleAbs);
    T5_UNMODELLED(OS_TimedReadAbs);
    T5_UNMODELLED(OS_TimedWriteAbs);
    T5_UNMODELLED(OS_SocketListen);
    T5_UNMODELLED(OS_SocketAccept);
    T5_UNMODELLED(OS_SocketAcceptAbs);
    T5_UNMODELLED(CFE_FS_ParseInputFileName);
    T5_UNMODELLED(CFE_FS_ParseInputFileNameEx);
    T5_UNMODELLED(CFE_FS_BackgroundFileDumpRequest);
    T5_UNMODELLED(CFE_ES_CreateChildTask);
    T5_UNMODELLED(CFE_ES_GetPoolBuf);
    T5_UNMODELLED(CFE_ES_PoolCreate);
    T5_UNMODELLED(CFE_SB_ReceiveBufferWithRoute);
    T5_UNMODELLED(CFE_TBL_GetAddresses);
}

/* Reset every model; call at the start of each test case (UtTest_Add setup function). */
T5_FN void t5_setup(void)
{
    int k;
    UT_ResetState(0);
    mf_reset();
    if (t5_tx == NULL) t5_tx = calloc(T5_TX_MAX, sizeof(*t5_tx));
    if (t5_ev == NULL) t5_ev = calloc(T5_EV_MAX, sizeof(*t5_ev));
    t5_ntx = t5_nev = 0;
    memset(t5_pipes, 0, sizeof(t5_pipes));
    memset(t5_subs, 0, sizeof(t5_subs));
    t5_pool_next = t5_alloc_next = 0;
    t5_unrouted = t5_overflow = 0;
    t5_self_delivery = true;
    t5_sub_rc = t5_unsub_rc = t5_pipe_rc = t5_recv_rc = t5_transmit_rc = CFE_SUCCESS;
    t5_n_subscribe = t5_n_unsubscribe = 0;
    memset(t5_tbl, 0, sizeof(t5_tbl));
    memset(t5_tblfile, 0, sizeof(t5_tblfile));
    memset(t5_share_ok, 0, sizeof(t5_share_ok));
    t5_tbl_reg_rc = t5_tbl_load_rc = CFE_SUCCESS;
    t5_n_tbl_register = t5_n_share_calls = t5_n_getaddress = 0;
    for (k = 0; k < T5_SOCK_MAX; k++)
    {
        int q;
        if (!t5_sock[k].used) continue;
        for (q = 0; q < T5_DGRAM_Q; q++) free(t5_sock[k].dq[q]);
        free(t5_sock[k].rx);
    }
    memset(t5_sock, 0, sizeof(t5_sock));
    for (k = 0; k < t5_nsent; k++) free(t5_sent[k].bytes);
    memset(t5_sent, 0, sizeof(t5_sent));
    t5_nsent = 0;
    memset(t5_listen, 0, sizeof(t5_listen));
    t5_sock_open_rc = t5_bind_rc = t5_sendto_rc = OS_SUCCESS;
    t5_bind_fail_port = 0;
    t5_blocking_recvs = 0;
    t5_n_connect = 0;
    t5_node_id = 1;
    t5_now_sec = 1000000;
    t5_now_sub = 0;
    t5_exit_called = false;
    t5_exit_status = 0;
    t5_timeouts = 0;
    t5_n_unmodelled = 0;
#ifdef T5_MODEL_CMDVAL
    cmdval_n_validate = cmdval_n_hmac = cmdval_n_crc = cmdval_n_init = 0;
    cmdval_force_status = -1;
#endif
    t5_register_hooks();
}

T5_FN void t5_teardown(void)
{
    t5_app_end();
}

#endif /* T5_MODEL_H */
