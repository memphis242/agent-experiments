/*
 * Unit test of the reference STOR implementation: the behaviour scenarios of the acceptance behaviour test, run
 * against the same cFE/OSAL models, plus reference-specific error paths at the end of this file. Every test case
 * runs in its own forked process, which writes its coverage data (__gcov_dump) before it ends.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <semaphore.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "utassert.h"
#include "uttest.h"
#include "utstubs.h"

#include "cfe.h"
#include "cfe_psp.h"

extern void STOR_AppMain(void);
extern void __gcov_dump(void);

/* ---------------------------------------------------------------- constants */

#define T_CMD_MID      0x18C4u
#define T_SEND_HK_MID  0x18C5u
#define T_WAKEUP_MID   0x18C6u
#define T_HK_MID       0x08C4u
#define T_LIST_MID     0x08C5u
#define T_STATS_MID    0x08C6u
#define T_MID_A        0x08E0u
#define T_MID_B        0x08E1u
#define T_MID_C        0x08E2u
#define T_MID_D        0x08E3u
#define T_MID_OLD      0x08F0u
#define T_MID_UNKNOWN  0x08F7u

#define CC_NOOP        0
#define CC_RESET       1
#define CC_SET_PRI     2
#define CC_SET_DEC     3
#define CC_SET_THR     4
#define CC_ADD_FILE    5
#define CC_DEL_FILE    6
#define CC_LIST_DIR    7
#define CC_LIST_ALL    8
#define CC_STATS       9
#define CC_CLOSE       10

#define PATH_LEN       64
#define MAX_ENT        32
#define TBL_SIZE       400
#define CMD_HDR        8
#define TLM_HDR        16
#define FS_HDR         64
#define HK_PAYLOAD     48
#define LIST_PKT       (TLM_HDR + 8 + 4 * 72)
#define STATS_PKT      (TLM_HDR + MAX_ENT * 24)
#define FS_CONTENT_ID  0x63464531u
#define FS_SUBTYPE     0x5354u
#define DIR_SIZE       4096u /* st_size of a directory on the mission's host file system */

#define TLM_ROOT       "/cf/stor/tlm"
#define FILE_ROOT      "/cf/stor/files"

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
#define EID_PURGE      14
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
#define EID_STATS_OK   28

#define EV_INFO  CFE_EVS_EventType_INFORMATION
#define EV_ERROR CFE_EVS_EventType_ERROR
#define EV_DEBUG CFE_EVS_EventType_DEBUG

/* ------------------------------------------------------------ assertions */

/* Assertions made in this process (a test case's process reports them to the parent). */
static uint32 n_checks, n_failed;

/* Set once STOR stopped responding in this test case: later assertions would only restate that. */
static int app_hung;

/* Startup failures stop every other check only in the startup case; elsewhere they name the scenario. */
static bool boot_is_startup_case;

static bool note(bool ok)
{
    n_checks++;
    if (!ok) n_failed++;
    return ok;
}

/* `tag` names the specified behaviour; the review turns it into one feedback sentence. */
#define EXPECT(tag, cond, ...)  ((void)(app_hung_now() || UtAssert_True(note(cond), "{" tag "} " __VA_ARGS__)))
#define FAIL(tag, ...)          ((void)(app_hung_now() || UtAssert_True(note(false), "{" tag "} " __VA_ARGS__)))
/* A precondition of a test case did not hold: the case stops, naming the behaviour that broke the setup. */
#define SETUP_FAILED(tag, ...)  ((void)(app_hung_now() || UtAssert_True(note(false), "{" tag "} setup failed: " __VA_ARGS__)))

/* ------------------------------------------------------------ byte helpers */

static void   put32(uint8 *p, uint32 v) { memcpy(p, &v, 4); }
static uint32 get32(const uint8 *p) { uint32 v; memcpy(&v, p, 4); return v; }
static uint16 get16(const uint8 *p) { uint16 v; memcpy(&v, p, 2); return v; }
static void   put_be32(uint8 *p, uint32 v) { p[0] = (uint8)(v >> 24); p[1] = (uint8)(v >> 16); p[2] = (uint8)(v >> 8); p[3] = (uint8)v; }
static uint32 get_be32(const uint8 *p) { return ((uint32)p[0] << 24) | ((uint32)p[1] << 16) | ((uint32)p[2] << 8) | p[3]; }

static bool all_zero(const uint8 *p, size_t n)
{
    size_t i;
    for (i = 0; i < n; i++) if (p[i] != 0) return false;
    return true;
}

/* Atomic flags shared between the test thread and the application thread. */
static int  flag_get(const int *f) { return __atomic_load_n(f, __ATOMIC_SEQ_CST); }
static void flag_set(int *f, int v) { __atomic_store_n(f, v, __ATOMIC_SEQ_CST); }
static bool app_hung_now(void) { return flag_get(&app_hung) != 0; }

/* ------------------------------------------------------------- table images */

typedef struct { uint8 b[TBL_SIZE]; } tbl_t;

static void tbl_default(tbl_t *t)
{
    memset(t, 0, sizeof(*t));
    strcpy((char *)t->b, TLM_ROOT);
    strcpy((char *)t->b + PATH_LEN, FILE_ROOT);
    put32(t->b + 128, 262144);
    put32(t->b + 132, 1048576);
    put32(t->b + 136, 16384);
    t->b[140] = 80;
}

static void tbl_entry(tbl_t *t, int i, uint32 mid, uint8 pri, uint8 dec)
{
    uint8 *e = t->b + 144 + i * 8;
    put32(e, mid);
    e[4] = pri;
    e[5] = dec;
    e[6] = e[7] = 0;
}

static void tbl_sizes(tbl_t *t, uint32 tq, uint32 fq, uint32 maxf, uint8 thr)
{
    put32(t->b + 128, tq);
    put32(t->b + 132, fq);
    put32(t->b + 136, maxf);
    t->b[140] = thr;
}

/* ------------------------------------------------------------------- clock */

static uint32 now_sec;

/* --------------------------------------------------------- in-memory files */

#define MF_MAX        1024
#define MFD_MAX       OS_MAX_NUM_OPEN_FILES
#define MDIR_MAX      OS_MAX_NUM_OPEN_DIRS
#define NAME_MAX_TEST 160

typedef struct
{
    bool    used;
    bool    unlinked; /* removed while still open: invisible, freed by the last close */
    bool    dir;
    char    path[NAME_MAX_TEST];
    uint8  *data;
    size_t  size;
    uint32  order;
    uint32  mtime;
} mfile_t;

typedef struct { bool used; int file; size_t pos; int32 mode; uint32 gen; } mfd_t;
typedef struct { bool used; char path[NAME_MAX_TEST]; uint32 next_order; int dots; uint32 gen; } mdir_t;

enum { FI_WRITE_OK, FI_WRITE_ERROR, FI_WRITE_SHORT };

static mfile_t  mf[MF_MAX];
static mfd_t    mfd[MFD_MAX];
static mdir_t   mdir[MDIR_MAX];
static uint32   mf_order;
static uint32   n_remove, n_close;
static int      fi_create_fail;   /* fail the next N creations of packet files */
static int      fi_write_mode;    /* make writes to packet files fail (OS_ERROR) or come up short */
static bool     fi_mkdir_fail;    /* OS_mkdir fails */

static const char *base_of(const char *path)
{
    const char *s = strrchr(path, '/');
    return s ? s + 1 : path;
}

static void parent_of(const char *path, char *out, size_t n)
{
    const char *s = strrchr(path, '/');
    size_t len = s ? (size_t)(s - path) : 0;
    if (len >= n) len = n - 1;
    memcpy(out, path, len);
    out[len] = 0;
    if (len == 0 && s == path) snprintf(out, n, "/");
}

/* Exact lookup of a canonical path (test side). */
static int mf_find(const char *path)
{
    int i;
    for (i = 0; i < MF_MAX; i++)
        if (mf[i].used && !mf[i].unlinked && strcmp(mf[i].path, path) == 0) return i;
    return -1;
}

static int mf_new(const char *path, bool dir)
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
            mf[i].mtime = now_sec;
            return i;
        }
    }
    FAIL("harness", "the model file system is full (more than %d files and directories)", MF_MAX);
    return -1;
}

static void mf_free(int i)
{
    free(mf[i].data);
    memset(&mf[i], 0, sizeof(mf[i]));
}

static void mf_grow(mfile_t *f, size_t n)
{
    if (n > f->size)
    {
        f->data = realloc(f->data, n ? n : 1);
        memset(f->data + f->size, 0, n - f->size);
        f->size = n;
    }
}

static bool mf_is_open(int i)
{
    int k;
    for (k = 0; k < MFD_MAX; k++)
        if (mfd[k].used && mfd[k].file == i) return true;
    return false;
}

static void mf_reset(void)
{
    int i;
    for (i = 0; i < MF_MAX; i++) free(mf[i].data);
    memset(mf, 0, sizeof(mf));
    memset(mfd, 0, sizeof(mfd));
    memset(mdir, 0, sizeof(mdir));
    mf_order = 0;
    n_remove = n_close = 0;
    fi_create_fail = 0;
    fi_write_mode = FI_WRITE_OK;
    fi_mkdir_fail = false;
    mf_new("/cf", true);
}

/* Create or replace a regular file of `size` pattern bytes; the parent must exist. */
static void mf_put(const char *path, size_t size, uint8 seed)
{
    int i = mf_find(path);
    size_t k;
    if (i < 0) i = mf_new(path, false);
    if (i < 0) return;
    mf[i].dir = false;
    free(mf[i].data);
    mf[i].data = NULL;
    mf[i].size = 0;
    mf_grow(&mf[i], size);
    for (k = 0; k < size; k++) mf[i].data[k] = (uint8)(seed + k * 7);
}

static void fs_header_bytes(uint8 *b, uint32 subtype, const char *desc, uint32 secs)
{
    memset(b, 0, FS_HDR);
    put_be32(b, FS_CONTENT_ID);
    put_be32(b + 4, subtype);
    put_be32(b + 8, FS_HDR);
    put_be32(b + 24, secs);
    strncpy((char *)b + 32, desc, 31);
}

/* A packet file as STOR would have written it: a valid cFE file header followed by pattern bytes. */
static void mf_put_pkt(const char *path, size_t size, uint8 seed)
{
    int i;
    mf_put(path, size, seed);
    i = mf_find(path);
    if (i >= 0 && size >= FS_HDR) fs_header_bytes(mf[i].data, FS_SUBTYPE, "STOR packet file", 900);
}

static void mf_mkdir_p(const char *path)
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

/*
 * OS_TranslatePath's checks, in its order, followed by what the host file system does with the result:
 * repeated '/' and "." components collapse, and a trailing '/' (or "/.") is accepted only for a directory.
 * On success `out` is the canonical path.
 */
static int32 vpath(const char *path, char *out, size_t n, bool *want_dir)
{
    const char *p;
    size_t len = 0;
    *want_dir = false;
    if (path == NULL) return OS_INVALID_POINTER;
    if (strnlen(path, OS_MAX_PATH_LEN) >= OS_MAX_PATH_LEN) return OS_FS_ERR_PATH_TOO_LONG;
    if (strrchr(path, '/') == NULL) return OS_FS_ERR_PATH_INVALID;
    if (memchr(base_of(path), 0, OS_MAX_FILE_NAME) == NULL) return OS_FS_ERR_NAME_TOO_LONG;
    if (path[0] != '/') return OS_FS_ERR_PATH_INVALID;
    if (strstr(path, "..") != NULL) return OS_FS_ERR_PATH_INVALID;
    if (strncmp(path, "/cf", 3) != 0 || (path[3] != '/' && path[3] != 0)) return OS_FS_ERR_PATH_INVALID;
    out[0] = 0;
    p = path;
    while (*p)
    {
        const char *s;
        size_t cl;
        while (*p == '/') p++;
        if (*p == 0) break;
        s = strchr(p, '/');
        cl = s ? (size_t)(s - p) : strlen(p);
        if (!(cl == 1 && p[0] == '.'))
        {
            if (len + 1 + cl + 1 > n) return OS_FS_ERR_PATH_TOO_LONG;
            out[len++] = '/';
            memcpy(out + len, p, cl);
            len += cl;
            out[len] = 0;
        }
        p += cl;
    }
    if (len == 0) snprintf(out, n, "/");
    {
        size_t pl = strlen(path);
        *want_dir = (pl > 1 && path[pl - 1] == '/') || (pl > 2 && strcmp(path + pl - 2, "/.") == 0);
    }
    return OS_SUCCESS;
}

/* Lookup of an application-supplied path; -1 when it does not resolve to an existing object. */
static int mf_lookup(const char *canon, bool want_dir)
{
    int i = mf_find(canon);
    if (i >= 0 && want_dir && !mf[i].dir) return -1;
    return i;
}

static bool parent_is_dir(const char *canon)
{
    char par[NAME_MAX_TEST];
    int k;
    parent_of(canon, par, sizeof(par));
    k = mf_find(par);
    return k >= 0 && mf[k].dir;
}

static bool dir_is_empty(int d)
{
    size_t n = strlen(mf[d].path);
    int k;
    for (k = 0; k < MF_MAX; k++)
        if (mf[k].used && !mf[k].unlinked && k != d && strncmp(mf[k].path, mf[d].path, n) == 0 && mf[k].path[n] == '/') return false;
    return true;
}

static void ret_i32(UT_EntryKey_t key, int32 v)
{
    UT_Stub_SetReturnValue(key, v);
}

static bool is_packet_name(const char *name)
{
    int k;
    if (strlen(name) != 21 || strncmp(name, "tlm_", 4) != 0 || name[8] != '_' || strcmp(name + 17, ".bin") != 0) return false;
    for (k = 4; k < 8; k++)
        if (!((name[k] >= '0' && name[k] <= '9') || (name[k] >= 'A' && name[k] <= 'F'))) return false;
    for (k = 9; k < 17; k++)
        if (name[k] < '0' || name[k] > '9') return false;
    return true;
}

static osal_id_t fd_id(int slot) { return OS_ObjectIdFromInteger((unsigned long)mfd[slot].gen * 256u + (unsigned long)slot + 1u); }

static mfd_t *fd_of(osal_id_t id)
{
    unsigned long v = OS_ObjectIdToInteger(id);
    unsigned long slot = v % 256u;
    if (slot < 1 || slot > MFD_MAX || !mfd[slot - 1].used || mfd[slot - 1].gen != v / 256u) return NULL;
    return &mfd[slot - 1];
}

static void H_OS_OpenCreate(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t *fd = UT_Hook_GetArgValueByName(c, "filedes", osal_id_t *);
    const char *path = UT_Hook_GetArgValueByName(c, "path", const char *);
    int32 flags = UT_Hook_GetArgValueByName(c, "flags", int32);
    int32 mode = UT_Hook_GetArgValueByName(c, "access_mode", int32);
    char canon[NAME_MAX_TEST];
    bool want_dir;
    int32 rc;
    int i = -1, k;
    (void)ud;
    if (fd == NULL) { ret_i32(key, OS_INVALID_POINTER); return; }
    *fd = OS_OBJECT_ID_UNDEFINED;
    if (mode != OS_WRITE_ONLY && mode != OS_READ_ONLY && mode != OS_READ_WRITE) { ret_i32(key, OS_ERROR); return; }
    rc = vpath(path, canon, sizeof(canon), &want_dir);
    if (rc == OS_SUCCESS && fi_create_fail > 0 && (flags & OS_FILE_FLAG_CREATE) && strncmp(base_of(canon), "tlm_", 4) == 0)
    {
        fi_create_fail--;
        rc = OS_ERROR;
    }
    for (k = 0; rc == OS_SUCCESS && k < MFD_MAX && mfd[k].used; k++) {}
    if (rc == OS_SUCCESS && k == MFD_MAX) rc = OS_ERR_NO_FREE_IDS;
    if (rc == OS_SUCCESS)
    {
        i = mf_lookup(canon, false);
        if (i >= 0 && mf[i].dir)
        {
            /* open(2) of a directory works read-only only */
            if ((flags & OS_FILE_FLAG_CREATE) || mode != OS_READ_ONLY) rc = OS_ERROR;
        }
        else if (i >= 0 && want_dir)
        {
            rc = OS_ERROR;
        }
        else if (i < 0 && (!(flags & OS_FILE_FLAG_CREATE) || want_dir || !parent_is_dir(canon)))
        {
            rc = OS_ERROR;
        }
        else if (i < 0)
        {
            i = mf_new(canon, false);
            if (i < 0) rc = OS_ERROR;
        }
        else if (flags & OS_FILE_FLAG_TRUNCATE)
        {
            mf[i].size = 0;
            mf[i].mtime = now_sec;
        }
    }
    if (rc == OS_SUCCESS)
    {
        mfd[k].used = true;
        mfd[k].file = i;
        mfd[k].pos = 0;
        mfd[k].mode = mode;
        mfd[k].gen++;
        *fd = fd_id(k);
    }
    ret_i32(key, rc);
}

static int32 do_write(osal_id_t id, const void *buf, size_t n)
{
    mfd_t *d = fd_of(id);
    mfile_t *f;
    if (d == NULL) return OS_ERR_INVALID_ID;
    f = &mf[d->file];
    if (d->mode == OS_READ_ONLY || f->dir) return OS_ERROR;
    if (fi_write_mode != FI_WRITE_OK && strncmp(base_of(f->path), "tlm_", 4) == 0)
    {
        if (fi_write_mode == FI_WRITE_ERROR || n < 2) return OS_ERROR;
        n /= 2;
    }
    mf_grow(f, d->pos + n);
    memcpy(f->data + d->pos, buf, n);
    d->pos += n;
    f->mtime = now_sec;
    return (int32)n;
}

static void H_OS_write(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    ret_i32(key, do_write(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t),
                          UT_Hook_GetArgValueByName(c, "buffer", const void *),
                          UT_Hook_GetArgValueByName(c, "nbytes", size_t)));
}

static int32 do_read(osal_id_t id, void *buf, size_t n)
{
    mfd_t *d = fd_of(id);
    mfile_t *f;
    size_t avail;
    if (d == NULL) return OS_ERR_INVALID_ID;
    f = &mf[d->file];
    if (d->mode == OS_WRITE_ONLY || f->dir) return OS_ERROR;
    avail = d->pos < f->size ? f->size - d->pos : 0;
    if (n > avail) n = avail;
    if (n) memcpy(buf, f->data + d->pos, n);
    d->pos += n;
    return (int32)n;
}

static void H_OS_read(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    ret_i32(key, do_read(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t),
                         UT_Hook_GetArgValueByName(c, "buffer", void *),
                         UT_Hook_GetArgValueByName(c, "nbytes", size_t)));
}

static void fd_release(mfd_t *d)
{
    int f = d->file;
    d->used = false;
    n_close++;
    if (mf[f].unlinked && !mf_is_open(f)) mf_free(f);
}

static void H_OS_close(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mfd_t *d = fd_of(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t));
    (void)ud;
    if (d == NULL) { ret_i32(key, OS_ERR_INVALID_ID); return; }
    fd_release(d);
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_remove(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    char canon[NAME_MAX_TEST];
    bool want_dir;
    int32 rc = vpath(UT_Hook_GetArgValueByName(c, "path", const char *), canon, sizeof(canon), &want_dir);
    int i;
    (void)ud;
    if (rc != OS_SUCCESS) { ret_i32(key, rc); return; }
    i = mf_lookup(canon, want_dir);
    /* remove(3): an empty directory is removed like a file */
    if (i < 0 || (mf[i].dir && !dir_is_empty(i)) || i == 0) { ret_i32(key, OS_ERROR); return; }
    if (!mf[i].dir) n_remove++;
    if (mf_is_open(i)) mf[i].unlinked = true;
    else mf_free(i);
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_mkdir(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    char canon[NAME_MAX_TEST];
    bool want_dir;
    int32 rc = vpath(UT_Hook_GetArgValueByName(c, "path", const char *), canon, sizeof(canon), &want_dir);
    int i;
    (void)ud;
    if (rc == OS_SUCCESS && fi_mkdir_fail) rc = OS_ERROR;
    if (rc == OS_SUCCESS)
    {
        i = mf_find(canon);
        if (i >= 0) rc = mf[i].dir ? OS_SUCCESS : OS_ERROR; /* an existing directory is success */
        else if (!parent_is_dir(canon) || mf_new(canon, true) < 0) rc = OS_ERROR;
    }
    ret_i32(key, rc);
}

static void H_OS_rmdir(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    char canon[NAME_MAX_TEST];
    bool want_dir;
    int32 rc = vpath(UT_Hook_GetArgValueByName(c, "path", const char *), canon, sizeof(canon), &want_dir);
    int i;
    (void)ud;
    if (rc != OS_SUCCESS) { ret_i32(key, rc); return; }
    i = mf_find(canon);
    if (i <= 0 || !mf[i].dir || !dir_is_empty(i)) { ret_i32(key, OS_ERROR); return; }
    mf_free(i);
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_stat(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    os_fstat_t *st = UT_Hook_GetArgValueByName(c, "filestats", os_fstat_t *);
    char canon[NAME_MAX_TEST];
    bool want_dir;
    int32 rc = vpath(UT_Hook_GetArgValueByName(c, "path", const char *), canon, sizeof(canon), &want_dir);
    int i;
    (void)ud;
    if (st == NULL) { ret_i32(key, OS_INVALID_POINTER); return; }
    memset(st, 0, sizeof(*st));
    if (rc != OS_SUCCESS) { ret_i32(key, rc); return; }
    i = mf_lookup(canon, want_dir);
    if (i < 0) { ret_i32(key, OS_ERROR); return; }
    st->FileModeBits = OS_FILESTAT_MODE_READ | OS_FILESTAT_MODE_WRITE | (mf[i].dir ? OS_FILESTAT_MODE_DIR | OS_FILESTAT_MODE_EXEC : 0);
    st->FileSize = mf[i].dir ? DIR_SIZE : mf[i].size;
    st->FileTime = OS_TimeAssembleFromNanoseconds((int64)mf[i].mtime, 0);
    ret_i32(key, OS_SUCCESS);
}

static osal_id_t dir_id(int slot) { return OS_ObjectIdFromInteger((unsigned long)mdir[slot].gen * 256u + (unsigned long)slot + 1u); }

static mdir_t *dir_of(osal_id_t id)
{
    unsigned long v = OS_ObjectIdToInteger(id);
    unsigned long slot = v % 256u;
    if (slot < 1 || slot > MDIR_MAX || !mdir[slot - 1].used || mdir[slot - 1].gen != v / 256u) return NULL;
    return &mdir[slot - 1];
}

static void H_OS_DirectoryOpen(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t *id = UT_Hook_GetArgValueByName(c, "dir_id", osal_id_t *);
    char canon[NAME_MAX_TEST];
    bool want_dir;
    int32 rc = vpath(UT_Hook_GetArgValueByName(c, "path", const char *), canon, sizeof(canon), &want_dir);
    int i, k;
    (void)ud;
    if (id == NULL) { ret_i32(key, OS_INVALID_POINTER); return; }
    *id = OS_OBJECT_ID_UNDEFINED;
    if (rc != OS_SUCCESS) { ret_i32(key, rc); return; }
    i = mf_lookup(canon, true);
    if (i < 0 || !mf[i].dir) { ret_i32(key, OS_ERROR); return; }
    for (k = 0; k < MDIR_MAX && mdir[k].used; k++) {}
    if (k == MDIR_MAX) { ret_i32(key, OS_ERR_NO_FREE_IDS); return; }
    mdir[k].used = true;
    snprintf(mdir[k].path, sizeof(mdir[k].path), "%s", mf[i].path);
    mdir[k].next_order = 0;
    mdir[k].dots = 0;
    mdir[k].gen++;
    *id = dir_id(k);
    ret_i32(key, OS_SUCCESS);
}

/* readdir(3) order: "." and "..", then the entries in creation order. */
static void H_OS_DirectoryRead(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mdir_t *d = dir_of(UT_Hook_GetArgValueByName(c, "dir_id", osal_id_t));
    os_dirent_t *ent = UT_Hook_GetArgValueByName(c, "dirent", os_dirent_t *);
    int k, best = -1;
    char par[NAME_MAX_TEST];
    (void)ud;
    if (d == NULL) { ret_i32(key, OS_ERR_INVALID_ID); return; }
    if (ent == NULL) { ret_i32(key, OS_INVALID_POINTER); return; }
    memset(ent, 0, sizeof(*ent));
    if (d->dots < 2)
    {
        snprintf(OS_DIRENTRY_NAME(*ent), sizeof(OS_DIRENTRY_NAME(*ent)), "%s", d->dots == 0 ? "." : "..");
        d->dots++;
        ret_i32(key, OS_SUCCESS);
        return;
    }
    for (k = 0; k < MF_MAX; k++)
    {
        if (!mf[k].used || mf[k].unlinked || mf[k].order <= d->next_order) continue;
        parent_of(mf[k].path, par, sizeof(par));
        if (strcmp(par, d->path) != 0) continue;
        if (best < 0 || mf[k].order < mf[best].order) best = k;
    }
    if (best < 0) { ret_i32(key, OS_ERROR); return; }
    d->next_order = mf[best].order;
    snprintf(OS_DIRENTRY_NAME(*ent), sizeof(OS_DIRENTRY_NAME(*ent)), "%s", base_of(mf[best].path));
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_DirectoryClose(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mdir_t *d = dir_of(UT_Hook_GetArgValueByName(c, "dir_id", osal_id_t));
    (void)ud;
    if (d == NULL) { ret_i32(key, OS_ERR_INVALID_ID); return; }
    d->used = false;
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_DirectoryRewind(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mdir_t *d = dir_of(UT_Hook_GetArgValueByName(c, "dir_id", osal_id_t));
    (void)ud;
    if (d == NULL) { ret_i32(key, OS_ERR_INVALID_ID); return; }
    d->next_order = 0;
    d->dots = 0;
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_lseek(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mfd_t *d = fd_of(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t));
    long off = (long)UT_Hook_GetArgValueByName(c, "offset", osal_offset_t);
    uint32 wh = UT_Hook_GetArgValueByName(c, "whence", uint32);
    long np;
    (void)ud;
    if (d == NULL) { ret_i32(key, OS_ERR_INVALID_ID); return; }
    if (wh == OS_SEEK_SET) np = off;
    else if (wh == OS_SEEK_CUR) np = (long)d->pos + off;
    else if (wh == OS_SEEK_END) np = (long)mf[d->file].size + off;
    else { ret_i32(key, OS_ERROR); return; }
    if (np < 0) { ret_i32(key, OS_ERROR); return; }
    d->pos = (size_t)np;
    ret_i32(key, (int32)d->pos);
}

/* OS_cp: open the source read-only, create or truncate the destination, copy. */
static int32 do_cp(const char *src, const char *dst)
{
    char cs[NAME_MAX_TEST], cd[NAME_MAX_TEST];
    bool ws, wd;
    int32 rc = vpath(src, cs, sizeof(cs), &ws);
    int i, j;
    if (rc == OS_SUCCESS) rc = vpath(dst, cd, sizeof(cd), &wd);
    if (rc != OS_SUCCESS) return rc;
    i = mf_lookup(cs, ws);
    if (i < 0 || mf[i].dir) return OS_ERROR;
    j = mf_lookup(cd, false);
    if (wd || (j >= 0 && mf[j].dir) || (j < 0 && !parent_is_dir(cd))) return OS_ERROR;
    if (j < 0) j = mf_new(cd, false);
    if (j < 0) return OS_ERROR;
    mf[j].size = 0;
    mf_grow(&mf[j], mf[i].size);
    if (mf[i].size) memcpy(mf[j].data, mf[i].data, mf[i].size);
    mf[j].mtime = now_sec;
    return OS_SUCCESS;
}

/* OS_rename: rename(2) within the one file system; an existing destination file is replaced. */
static int32 do_rename(const char *src, const char *dst)
{
    char cs[NAME_MAX_TEST], cd[NAME_MAX_TEST];
    bool ws, wd;
    int32 rc = vpath(src, cs, sizeof(cs), &ws);
    int i, j, k;
    size_t n;
    if (rc == OS_SUCCESS) rc = vpath(dst, cd, sizeof(cd), &wd);
    if (rc != OS_SUCCESS) return rc;
    i = mf_lookup(cs, ws);
    if (i <= 0 || !parent_is_dir(cd)) return OS_ERROR;
    j = mf_lookup(cd, false);
    if (j == i) return OS_SUCCESS;
    if (j >= 0)
    {
        if (mf[j].dir != mf[i].dir || (mf[j].dir && !dir_is_empty(j))) return OS_ERROR;
        if (mf_is_open(j)) mf[j].unlinked = true;
        else mf_free(j);
    }
    n = strlen(cs);
    if (mf[i].dir)
    {
        for (k = 0; k < MF_MAX; k++)
        {
            if (mf[k].used && k != i && strncmp(mf[k].path, cs, n) == 0 && mf[k].path[n] == '/')
            {
                char tail[NAME_MAX_TEST];
                snprintf(tail, sizeof(tail), "%s", mf[k].path + n);
                snprintf(mf[k].path, sizeof(mf[k].path), "%s%s", cd, tail);
            }
        }
    }
    snprintf(mf[i].path, sizeof(mf[i].path), "%s", cd);
    return OS_SUCCESS;
}

static void H_OS_cp(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    ret_i32(key, do_cp(UT_Hook_GetArgValueByName(c, "src", const char *), UT_Hook_GetArgValueByName(c, "dest", const char *)));
}

static void H_OS_mv(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    ret_i32(key, do_rename(UT_Hook_GetArgValueByName(c, "src", const char *), UT_Hook_GetArgValueByName(c, "dest", const char *)));
}

static void H_OS_rename(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    ret_i32(key, do_rename(UT_Hook_GetArgValueByName(c, "old_filename", const char *),
                           UT_Hook_GetArgValueByName(c, "new_filename", const char *)));
}

static int open_index_of(const char *path)
{
    char canon[NAME_MAX_TEST];
    bool want_dir;
    if (vpath(path, canon, sizeof(canon), &want_dir) != OS_SUCCESS) return -1;
    return mf_lookup(canon, want_dir);
}

static void H_OS_FileOpenCheck(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int i = open_index_of(UT_Hook_GetArgValueByName(c, "Filename", const char *));
    (void)ud;
    ret_i32(key, (i >= 0 && mf_is_open(i)) ? OS_SUCCESS : OS_ERROR);
}

static void H_OS_FDGetInfo(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mfd_t *d = fd_of(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t));
    OS_file_prop_t *p = UT_Hook_GetArgValueByName(c, "fd_prop", OS_file_prop_t *);
    (void)ud;
    if (d == NULL) { ret_i32(key, OS_ERR_INVALID_ID); return; }
    memset(p, 0, sizeof(*p));
    snprintf(p->Path, sizeof(p->Path), "%s", mf[d->file].path);
    p->IsValid = 1;
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_CloseFileByName(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int i = open_index_of(UT_Hook_GetArgValueByName(c, "Filename", const char *)), k;
    (void)ud;
    if (i < 0) { ret_i32(key, OS_FS_ERR_PATH_INVALID); return; }
    for (k = 0; k < MFD_MAX; k++)
        if (mfd[k].used && mfd[k].file == i) fd_release(&mfd[k]);
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_CloseAllFiles(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int k;
    (void)ud; (void)c;
    for (k = 0; k < MFD_MAX; k++)
        if (mfd[k].used) fd_release(&mfd[k]);
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_TranslatePath(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *v = UT_Hook_GetArgValueByName(c, "VirtualPath", const char *);
    char *l = UT_Hook_GetArgValueByName(c, "LocalPath", char *);
    char canon[NAME_MAX_TEST];
    bool want_dir;
    int32 rc = vpath(v, canon, sizeof(canon), &want_dir);
    (void)ud;
    if (rc == OS_SUCCESS && l != NULL) snprintf(l, OS_MAX_LOCAL_PATH_LEN, ".%s", v);
    ret_i32(key, rc);
}

static void H_OS_chmod(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    ret_i32(key, open_index_of(UT_Hook_GetArgValueByName(c, "path", const char *)) >= 0 ? OS_SUCCESS : OS_ERROR);
}

static void H_OS_FileSysStatVolume(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    OS_statvfs_t *st = UT_Hook_GetArgValueByName(c, "statbuf", OS_statvfs_t *);
    (void)ud;
    memset(st, 0, sizeof(*st));
    st->block_size = 512;
    st->total_blocks = 1u << 20;
    st->blocks_free = 1u << 20;
    ret_i32(key, OS_SUCCESS);
}

static uint32 n_open_total(void)
{
    uint32 k, n = 0;
    for (k = 0; k < MFD_MAX; k++) if (mfd[k].used) n++;
    return n;
}

/* ------------------------------------------------------------------- clock hooks */

static void H_CFE_TIME_Now(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TIME_SysTime_t t;
    (void)ud; (void)c;
    memset(&t, 0, sizeof(t));
    t.Seconds = now_sec;
    UT_Stub_SetReturnValue(key, t);
}

static void H_TIME_METsec(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 s = now_sec;
    (void)ud; (void)c;
    UT_Stub_SetReturnValue(key, s);
}

static void H_TIME_METsubsec(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 s = 0;
    (void)ud; (void)c;
    UT_Stub_SetReturnValue(key, s);
}

static void H_OS_ClockNow(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    OS_time_t *t = UT_Hook_GetArgValueByName(c, "time_struct", OS_time_t *);
    (void)ud;
    if (t == NULL) { ret_i32(key, OS_INVALID_POINTER); return; }
    *t = OS_TimeAssembleFromNanoseconds((int64)now_sec, 0);
    ret_i32(key, OS_SUCCESS);
}

static void H_PSP_GetTime(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    OS_time_t *t = UT_Hook_GetArgValueByName(c, "LocalTime", OS_time_t *);
    (void)ud; (void)key;
    *t = OS_TimeAssembleFromNanoseconds((int64)now_sec, 0);
}

static void H_TIME_Add(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TIME_SysTime_t a = UT_Hook_GetArgValueByName(c, "Time1", CFE_TIME_SysTime_t);
    CFE_TIME_SysTime_t b = UT_Hook_GetArgValueByName(c, "Time2", CFE_TIME_SysTime_t);
    (void)ud;
    a.Seconds += b.Seconds;
    a.Subseconds += b.Subseconds;
    UT_Stub_SetReturnValue(key, a);
}

static void H_TIME_Subtract(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TIME_SysTime_t a = UT_Hook_GetArgValueByName(c, "Time1", CFE_TIME_SysTime_t);
    CFE_TIME_SysTime_t b = UT_Hook_GetArgValueByName(c, "Time2", CFE_TIME_SysTime_t);
    (void)ud;
    a.Seconds -= b.Seconds;
    a.Subseconds -= b.Subseconds;
    UT_Stub_SetReturnValue(key, a);
}

static void H_TIME_Compare(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TIME_SysTime_t a = UT_Hook_GetArgValueByName(c, "TimeA", CFE_TIME_SysTime_t);
    CFE_TIME_SysTime_t b = UT_Hook_GetArgValueByName(c, "TimeB", CFE_TIME_SysTime_t);
    CFE_TIME_Compare_t r = CFE_TIME_EQUAL;
    (void)ud;
    if (a.Seconds < b.Seconds || (a.Seconds == b.Seconds && a.Subseconds < b.Subseconds)) r = CFE_TIME_A_LT_B;
    else if (a.Seconds > b.Seconds || a.Subseconds > b.Subseconds) r = CFE_TIME_A_GT_B;
    UT_Stub_SetReturnValue(key, r);
}

/* ---------------------------------------------------------- messages (CCSDS header bytes) */

#define MSG_MAX_BYTES 2048
#define RING          16
#define TX_MAX        1024
#define EV_MAX        4096
#define SUB_MAX       256

typedef struct { uint32 mid; size_t size; uint8 bytes[MSG_MAX_BYTES]; } txmsg_t;

static txmsg_t *tx;
static int      ntx;
static uint16   ev_id[EV_MAX];
static uint16   ev_type[EV_MAX];
static int      nev;

static uint32 hdr_mid(const uint8 *b) { return ((uint32)b[0] << 8) | b[1]; }
static size_t hdr_size(const uint8 *b) { return (((size_t)b[4] << 8) | b[5]) + 7; }
static bool   hdr_is_cmd(const uint8 *b) { return (b[0] & 0x10) != 0; }
static bool   hdr_has_sec(const uint8 *b) { return (b[0] & 0x08) != 0; }
static bool   mid_valid(uint32 v) { return v != 0 && v <= 0x1FFF; }

static void hdr_set_size(uint8 *b, size_t size)
{
    b[4] = (uint8)((size - 7) >> 8);
    b[5] = (uint8)(size - 7);
}

static void H_MSG_Init(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    uint32 mid = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    size_t sz = UT_Hook_GetArgValueByName(c, "Size", CFE_MSG_Size_t);
    (void)ud;
    if (m == NULL || sz < 7 || sz > 0xFFFF + 7) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    memset(m, 0, sz);
    m[2] = 0xC0;
    if (mid != 0)
    {
        if (!mid_valid(mid)) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
        m[0] = (uint8)(mid >> 8);
        m[1] = (uint8)mid;
    }
    hdr_set_size(m, sz);
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_GetMsgId(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_SB_MsgId_t *out = UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t *);
    (void)ud;
    if (m == NULL || out == NULL) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    *out = CFE_SB_ValueToMsgId(hdr_mid(m));
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_SetMsgId(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    uint32 mid = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    (void)ud;
    if (m == NULL || !mid_valid(mid)) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    m[0] = (uint8)(mid >> 8);
    m[1] = (uint8)mid;
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_GetSize(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_Size_t *out = UT_Hook_GetArgValueByName(c, "Size", CFE_MSG_Size_t *);
    (void)ud;
    if (m == NULL || out == NULL) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    *out = hdr_size(m);
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_SetSize(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    size_t sz = UT_Hook_GetArgValueByName(c, "Size", CFE_MSG_Size_t);
    (void)ud;
    if (m == NULL || sz < 7 || sz > 0xFFFF + 7) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    hdr_set_size(m, sz);
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_GetFcnCode(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_FcnCode_t *out = UT_Hook_GetArgValueByName(c, "FcnCode", CFE_MSG_FcnCode_t *);
    (void)ud;
    if (m == NULL || out == NULL) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    if (!hdr_is_cmd(m) || !hdr_has_sec(m))
    {
        *out = 0;
        ret_i32(key, CFE_MSG_WRONG_MSG_TYPE);
        return;
    }
    *out = (CFE_MSG_FcnCode_t)(m[6] & 0x7F);
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_SetFcnCode(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    CFE_MSG_FcnCode_t fc = UT_Hook_GetArgValueByName(c, "FcnCode", CFE_MSG_FcnCode_t);
    (void)ud;
    if (m == NULL || fc > 0x7F) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    if (!hdr_is_cmd(m) || !hdr_has_sec(m)) { ret_i32(key, CFE_MSG_WRONG_MSG_TYPE); return; }
    m[6] = (uint8)((m[6] & 0x80) | fc);
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_GetType(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_Type_t *out = UT_Hook_GetArgValueByName(c, "Type", CFE_MSG_Type_t *);
    (void)ud;
    if (m == NULL || out == NULL) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    *out = hdr_is_cmd(m) ? CFE_MSG_Type_Cmd : CFE_MSG_Type_Tlm;
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_SetType(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint8 *m = (uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    CFE_MSG_Type_t t = UT_Hook_GetArgValueByName(c, "Type", CFE_MSG_Type_t);
    (void)ud;
    if (m == NULL) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    if (t == CFE_MSG_Type_Cmd) m[0] |= 0x10;
    else if (t == CFE_MSG_Type_Tlm) m[0] &= (uint8)~0x10u;
    else { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_GetHasSecondaryHeader(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    bool *out = UT_Hook_GetArgValueByName(c, "HasSecondary", bool *);
    (void)ud;
    if (m == NULL || out == NULL) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    *out = hdr_has_sec(m);
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_GetTypeFromMsgId(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 mid = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    CFE_MSG_Type_t *out = UT_Hook_GetArgValueByName(c, "Type", CFE_MSG_Type_t *);
    (void)ud;
    if (out == NULL) { ret_i32(key, CFE_MSG_BAD_ARGUMENT); return; }
    *out = (mid & 0x1000) ? CFE_MSG_Type_Cmd : CFE_MSG_Type_Tlm;
    ret_i32(key, CFE_SUCCESS);
}

static void H_SB_GetUserDataLength(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const uint8 *m = (const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    size_t n = 0, h;
    (void)ud;
    if (m != NULL)
    {
        h = hdr_is_cmd(m) ? CMD_HDR : TLM_HDR;
        n = hdr_size(m) > h ? hdr_size(m) - h : 0;
    }
    UT_Stub_SetReturnValue(key, n);
}

static void H_SB_IsValidMsgId(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    bool ok = mid_valid(CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t)));
    (void)ud;
    UT_Stub_SetReturnValue(key, ok);
}

/* The set of message IDs currently subscribed on STOR's pipe (a repeated subscription is harmless, as in SB). */
static int32  sub_rc, unsub_rc;
static uint32 subs[SUB_MAX];
static int    nsubs;

static bool subscribed(uint32 mid)
{
    int i;
    for (i = 0; i < nsubs; i++) if (subs[i] == mid) return true;
    return false;
}

static void H_SB_Subscribe(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 v = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    (void)ud;
    if (sub_rc == CFE_SUCCESS && !mid_valid(v)) { ret_i32(key, CFE_SB_BAD_ARGUMENT); return; }
    if (sub_rc == CFE_SUCCESS && !subscribed(v) && nsubs < SUB_MAX) subs[nsubs++] = v;
    ret_i32(key, sub_rc);
}

static void H_SB_Unsubscribe(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 v = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    int i;
    (void)ud;
    if (unsub_rc == CFE_SUCCESS)
    {
        for (i = 0; i < nsubs; i++)
        {
            if (subs[i] == v)
            {
                subs[i] = subs[--nsubs];
                break;
            }
        }
    }
    ret_i32(key, unsub_rc);
}

static void H_SB_CreatePipe(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_SB_PipeId_t *p = UT_Hook_GetArgValueByName(c, "PipeIdPtr", CFE_SB_PipeId_t *);
    uint16 depth = UT_Hook_GetArgValueByName(c, "Depth", uint16);
    int32 forced;
    (void)ud;
    if (UT_Stub_GetInt32StatusCode(c, &forced) && forced != CFE_SUCCESS) { ret_i32(key, forced); return; }
    if (p == NULL || depth == 0 || depth > OS_QUEUE_MAX_DEPTH) { ret_i32(key, CFE_SB_BAD_ARGUMENT); return; }
    *p = CFE_SB_PIPEID_C(CFE_ResourceId_FromInteger(1));
    ret_i32(key, CFE_SUCCESS);
}

static void capture_tx(const uint8 *m)
{
    size_t sz;
    if (m == NULL) return;
    sz = hdr_size(m);
    if (ntx == TX_MAX)
    {
        FAIL("harness", "more than %d telemetry packets were sent in one test case", TX_MAX);
        ntx++;
        return;
    }
    if (ntx > TX_MAX) return;
    tx[ntx].mid = hdr_mid(m);
    tx[ntx].size = sz;
    memcpy(tx[ntx].bytes, m, sz <= MSG_MAX_BYTES ? sz : MSG_MAX_BYTES);
    ntx++;
}

static void H_SB_Transmit(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    capture_tx((const uint8 *)UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *));
    ret_i32(key, CFE_SUCCESS);
}

static void H_SB_TransmitBuffer(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    capture_tx((const uint8 *)UT_Hook_GetArgValueByName(c, "BufPtr", CFE_SB_Buffer_t *));
    ret_i32(key, CFE_SUCCESS);
}

static uint8 sb_pool[8][MSG_MAX_BYTES] __attribute__((aligned(16)));
static int   sb_pool_next;

static void H_SB_Alloc(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    size_t n = UT_Hook_GetArgValueByName(c, "MsgSize", size_t);
    CFE_SB_Buffer_t *b = NULL;
    (void)ud;
    if (n <= MSG_MAX_BYTES)
    {
        b = (CFE_SB_Buffer_t *)sb_pool[sb_pool_next++ % 8];
        memset(b, 0, n);
    }
    UT_Stub_SetReturnValue(key, b);
}

/* Whether STOR registered with EVS, and whether it sent an event before doing so. */
static bool evs_registered, ev_before_register;

static int32 H_EVS_Register(void *ud, int32 rc, uint32 cnt, const UT_StubContext_t *c)
{
    (void)ud; (void)cnt; (void)c;
    if (rc == CFE_SUCCESS) evs_registered = true;
    return rc;
}

static int32 ev_hook(void *ud, int32 rc, uint32 cnt, const UT_StubContext_t *c, va_list va)
{
    (void)ud; (void)cnt; (void)va;
    if (!evs_registered) ev_before_register = true;
    if (nev < EV_MAX)
    {
        ev_id[nev] = UT_Hook_GetArgValueByName(c, "EventID", uint16);
        ev_type[nev] = (uint16)UT_Hook_GetArgValueByName(c, "EventType", CFE_EVS_EventType_Enum_t);
        nev++;
    }
    return rc;
}

static int count_ev(uint16 id, int from)
{
    int i, n = 0;
    for (i = from; i < nev; i++) if (ev_id[i] == id) n++;
    return n;
}

/* True when every event `id` sent since `from` has type `type` (and there was at least one). */
static bool ev_typed(uint16 id, int type, int from)
{
    int i, n = 0;
    for (i = from; i < nev; i++)
    {
        if (ev_id[i] != id) continue;
        if (ev_type[i] != type) return false;
        n++;
    }
    return n > 0;
}

/* -------------------------------------------------------------------- table */

static tbl_t tbl_active, tbl_pending;
static bool tbl_updated, tbl_has_pending, tbl_locked;
static CFE_TBL_CallbackFuncPtr_t tbl_validate;
static char tbl_name[64];
static size_t tbl_size;
static int32 tbl_reg_rc, tbl_load_rc, recv_rc;
static int n_tbl_register;
static uint32 tbl_update_count, tbl_update_time;

static void H_TBL_Register(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TBL_Handle_t *h = UT_Hook_GetArgValueByName(c, "TblHandlePtr", CFE_TBL_Handle_t *);
    const char *name = UT_Hook_GetArgValueByName(c, "Name", const char *);
    (void)ud;
    n_tbl_register++;
    snprintf(tbl_name, sizeof(tbl_name), "%s", name ? name : "");
    tbl_size = UT_Hook_GetArgValueByName(c, "Size", size_t);
    tbl_validate = UT_Hook_GetArgValueByName(c, "TblValidationFuncPtr", CFE_TBL_CallbackFuncPtr_t);
    if (h != NULL) *h = 1;
    ret_i32(key, tbl_reg_rc);
}

/* The image loaded at startup goes through the application's validation function, as CFE_TBL_Load does. */
static void H_TBL_Load(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int32 rc = tbl_load_rc;
    (void)ud; (void)c;
    if (rc == CFE_SUCCESS && tbl_validate != NULL)
    {
        tbl_t copy = tbl_active;
        rc = tbl_validate(&copy);
        if (rc > CFE_SUCCESS) rc = CFE_STATUS_VALIDATION_FAILURE;
    }
    if (rc == CFE_SUCCESS)
    {
        tbl_updated = true;
        tbl_update_time = now_sec;
    }
    ret_i32(key, rc);
}

/* Activate the pending image unless the application holds the table address (CFE_TBL_INFO_TABLE_LOCKED). */
static int32 tbl_try_update(void)
{
    if (!tbl_has_pending) return CFE_TBL_INFO_NO_UPDATE_PENDING;
    if (tbl_locked) return CFE_TBL_INFO_TABLE_LOCKED;
    tbl_active = tbl_pending;
    tbl_has_pending = false;
    tbl_updated = true;
    tbl_update_count++;
    tbl_update_time = now_sec;
    return CFE_SUCCESS;
}

static void H_TBL_Manage(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    int32 rc = CFE_SUCCESS;
    (void)ud; (void)c;
    if (tbl_has_pending)
    {
        rc = tbl_try_update();
        if (rc == CFE_SUCCESS) rc = CFE_TBL_INFO_UPDATED;
    }
    ret_i32(key, rc);
}

static void H_TBL_Update(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    ret_i32(key, tbl_try_update());
}

static void H_TBL_GetStatus(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    ret_i32(key, tbl_has_pending ? CFE_TBL_INFO_UPDATE_PENDING : CFE_SUCCESS);
}

static void H_TBL_GetAddress(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    void **p = UT_Hook_GetArgValueByName(c, "TblPtr", void **);
    int32 rc = tbl_updated ? CFE_TBL_INFO_UPDATED : CFE_SUCCESS;
    (void)ud;
    if (p == NULL) { ret_i32(key, CFE_TBL_BAD_ARGUMENT); return; }
    *p = &tbl_active;
    tbl_updated = false;
    tbl_locked = true;
    ret_i32(key, rc);
}

static void H_TBL_ReleaseAddress(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    tbl_locked = false;
    ret_i32(key, CFE_SUCCESS);
}

static void H_TBL_GetInfo(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TBL_Info_t *p = UT_Hook_GetArgValueByName(c, "TblInfoPtr", CFE_TBL_Info_t *);
    (void)ud;
    if (p == NULL) { ret_i32(key, CFE_TBL_BAD_ARGUMENT); return; }
    memset(p, 0, sizeof(*p));
    p->Size = tbl_size;
    p->NumUsers = 1;
    p->TableLoadedOnce = true;
    p->TimeOfLastUpdate.Seconds = tbl_update_time;
    p->TimeOfLastUpdate.Subseconds = tbl_update_count;
    snprintf(p->LastFileLoaded, sizeof(p->LastFileLoaded), "/cf/stor_tbl.tbl");
    ret_i32(key, CFE_SUCCESS);
}

static void H_TBL_Ok(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    ret_i32(key, CFE_SUCCESS);
}

/* Present a new table image as table services would after a load and activate: it must pass validation first. */
static int32 tbl_submit(const tbl_t *t)
{
    int32 rc = CFE_SUCCESS;
    tbl_t copy = *t;
    if (tbl_validate != NULL) rc = tbl_validate(&copy);
    if (rc == CFE_SUCCESS)
    {
        tbl_pending = copy;
        tbl_has_pending = true;
    }
    return rc;
}

/* ---------------------------------------------------------- file header model */

static void H_FS_InitHeader(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_FS_Header_t *h = UT_Hook_GetArgValueByName(c, "Hdr", CFE_FS_Header_t *);
    const char *d = UT_Hook_GetArgValueByName(c, "Description", const char *);
    (void)ud; (void)key;
    if (h == NULL || d == NULL) return;
    memset(h, 0, sizeof(*h));
    strncpy((char *)h->Description, d, sizeof(h->Description) - 1);
    h->SubType = UT_Hook_GetArgValueByName(c, "SubType", uint32);
}

/* CFE_FS_WriteHeader: seek to 0, fill content type, length, IDs and time, write big-endian. */
static void H_FS_WriteHeader(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t fd = UT_Hook_GetArgValueByName(c, "FileDes", osal_id_t);
    CFE_FS_Header_t *h = UT_Hook_GetArgValueByName(c, "Hdr", CFE_FS_Header_t *);
    mfd_t *d = fd_of(fd);
    uint8 b[FS_HDR];
    int32 n;
    (void)ud;
    if (h == NULL) { ret_i32(key, CFE_FS_BAD_ARGUMENT); return; }
    if (d == NULL) { ret_i32(key, CFE_STATUS_EXTERNAL_RESOURCE_FAIL); return; }
    d->pos = 0;
    h->ContentType = FS_CONTENT_ID;
    h->Length = sizeof(CFE_FS_Header_t);
    h->SpacecraftID = 0x42;
    h->ProcessorID = 1;
    h->ApplicationID = 0x10007;
    h->TimeSeconds = now_sec;
    h->TimeSubSeconds = 0;
    memset(b, 0, sizeof(b));
    put_be32(b, h->ContentType);
    put_be32(b + 4, h->SubType);
    put_be32(b + 8, h->Length);
    put_be32(b + 12, h->SpacecraftID);
    put_be32(b + 16, h->ProcessorID);
    put_be32(b + 20, h->ApplicationID);
    put_be32(b + 24, h->TimeSeconds);
    put_be32(b + 28, h->TimeSubSeconds);
    memcpy(b + 32, h->Description, 32);
    n = do_write(fd, b, sizeof(b));
    ret_i32(key, n >= 0 ? n : CFE_STATUS_EXTERNAL_RESOURCE_FAIL);
}

static void H_FS_ReadHeader(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_FS_Header_t *h = UT_Hook_GetArgValueByName(c, "Hdr", CFE_FS_Header_t *);
    osal_id_t fd = UT_Hook_GetArgValueByName(c, "FileDes", osal_id_t);
    mfd_t *d = fd_of(fd);
    uint8 b[FS_HDR];
    int32 n;
    (void)ud;
    if (h == NULL) { ret_i32(key, CFE_FS_BAD_ARGUMENT); return; }
    if (d == NULL) { ret_i32(key, CFE_STATUS_EXTERNAL_RESOURCE_FAIL); return; }
    d->pos = 0;
    n = do_read(fd, b, sizeof(b));
    if (n != (int32)sizeof(b)) { ret_i32(key, CFE_STATUS_EXTERNAL_RESOURCE_FAIL); return; }
    h->ContentType = get_be32(b);
    h->SubType = get_be32(b + 4);
    h->Length = get_be32(b + 8);
    h->SpacecraftID = get_be32(b + 12);
    h->ProcessorID = get_be32(b + 16);
    h->ApplicationID = get_be32(b + 20);
    h->TimeSeconds = get_be32(b + 24);
    h->TimeSubSeconds = get_be32(b + 28);
    memcpy(h->Description, b + 32, 32);
    ret_i32(key, n);
}

/* -------------------------------------------------- application thread / bus */

#define WAIT_APP_S   10 /* longest time STOR may take to handle one message */
#define WAIT_EXIT_S  5  /* longest time STOR may take to leave its main loop once CFE_ES_RunLoop returns false */

static pthread_t app_thread;
static bool      app_running;
static int       app_exited, app_stop, have_pending;
static sem_t     sem_app, sem_test;
static bool      sems_ready;
static uint8    *pending_buf;

static void *app_main(void *arg)
{
    (void)arg;
    STOR_AppMain();
    flag_set(&app_exited, 1);
    sem_post(&sem_test);
    return NULL;
}

/* CFE_ES_RunLoop: false once RunStatus is not APP_RUN, or when the test stops the application. */
static void H_ES_RunLoop(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 *rs = UT_Hook_GetArgValueByName(c, "RunStatus", uint32 *);
    bool run = !flag_get(&app_stop) && (rs == NULL || *rs == CFE_ES_RunStatus_APP_RUN);
    (void)ud;
    UT_Stub_SetReturnValue(key, run);
}

/*
 * CFE_SB_ReceiveBuffer: tell the test that STOR waits for a message, then hand over the message it queues.
 * Once the test stops the application, the call returns CFE_SB_TIME_OUT without waiting, so the main loop
 * reaches CFE_ES_RunLoop, which then returns false.
 */
static void H_SB_Receive(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_SB_Buffer_t **bp = UT_Hook_GetArgValueByName(c, "BufPtr", CFE_SB_Buffer_t **);
    (void)ud;
    if (flag_get(&app_stop)) { ret_i32(key, CFE_SB_TIME_OUT); return; }
    if (recv_rc != CFE_SUCCESS)
    {
        flag_set(&app_stop, 1);
        ret_i32(key, recv_rc);
        return;
    }
    sem_post(&sem_test);
    while (sem_wait(&sem_app) != 0 && errno == EINTR) {}
    if (flag_get(&app_stop) || !flag_get(&have_pending))
    {
        ret_i32(key, CFE_SB_TIME_OUT);
        return;
    }
    flag_set(&have_pending, 0);
    *bp = (CFE_SB_Buffer_t *)pending_buf;
    ret_i32(key, CFE_SUCCESS);
}

static void sems_reset(void)
{
    if (sems_ready)
    {
        sem_destroy(&sem_app);
        sem_destroy(&sem_test);
    }
    sem_init(&sem_app, 0, 0);
    sem_init(&sem_test, 0, 0);
    sems_ready = true;
}

/* Wait until STOR is back on its pipe (true) or has ended its main loop (false). */
static bool wait_test(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += WAIT_APP_S;
    while (sem_timedwait(&sem_test, &ts) != 0)
    {
        if (errno == EINTR) continue;
        FAIL("responsive", "STOR did not return to CFE_SB_ReceiveBuffer within %d s", WAIT_APP_S);
        flag_set(&app_hung, 1);
        return false;
    }
    return !flag_get(&app_exited);
}

static bool app_start(void)
{
    flag_set(&app_exited, 0);
    flag_set(&app_stop, 0);
    flag_set(&app_hung, 0);
    flag_set(&have_pending, 0);
    if (pthread_create(&app_thread, NULL, app_main, NULL) != 0)
    {
        FAIL("infra", "cannot create the application thread");
        return false;
    }
    app_running = true;
    return wait_test();
}

/* Stop the application and join its thread; a thread that does not end leaves this process unusable. */
static void app_end(void)
{
    struct timespec ts;
    int rc;
    if (!app_running) return;
    flag_set(&app_stop, 1);
    sem_post(&sem_app);
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += flag_get(&app_hung) ? 1 : WAIT_EXIT_S;
    rc = pthread_timedjoin_np(app_thread, NULL, &ts);
    if (rc != 0)
    {
        if (!flag_get(&app_hung))
            FAIL("exit-on-stop", "STOR did not leave its main loop within %d s after CFE_ES_RunLoop returned false", WAIT_EXIT_S);
        __gcov_dump();
        _exit(3);
    }
    app_running = false;
}

static uint8 msg_pool[RING][MSG_MAX_BYTES] __attribute__((aligned(16)));
static int msg_pool_next;

static bool app_deliver(uint8 *buf)
{
    if (flag_get(&app_hung)) return false; /* already reported */
    if (!app_running || flag_get(&app_exited))
    {
        FAIL("exit-unexpected", "STOR is no longer running (its main loop ended) when a message was due");
        return false;
    }
    pending_buf = buf;
    flag_set(&have_pending, 1);
    sem_post(&sem_app);
    return wait_test();
}

static uint8 *new_msg(uint32 mid, size_t size, int fcn)
{
    uint8 *b = msg_pool[msg_pool_next++ % RING];
    memset(b, 0, size);
    b[0] = (uint8)(mid >> 8);
    b[1] = (uint8)mid;
    b[2] = 0xC0;
    hdr_set_size(b, size);
    if (hdr_is_cmd(b)) b[6] = (uint8)(fcn & 0x7F);
    return b;
}

static bool send_cmd(uint32 mid, int cc, const void *payload, size_t plen)
{
    uint8 *b = new_msg(mid, CMD_HDR + plen, cc);
    if (plen) memcpy(b + CMD_HDR, payload, plen);
    return app_deliver(b);
}

static bool send_simple(int cc) { return send_cmd(T_CMD_MID, cc, NULL, 0); }

/* Telemetry packet of `size` bytes carrying `index` as its first payload word. */
static void fill_tlm(uint8 *b, uint32 mid, size_t size, uint32 index)
{
    size_t k;
    memset(b, 0, size);
    b[0] = (uint8)(mid >> 8);
    b[1] = (uint8)mid;
    b[2] = 0xC0;
    hdr_set_size(b, size);
    put32(b + TLM_HDR, index);
    for (k = TLM_HDR + 4; k < size; k++) b[k] = (uint8)(index * 3 + k);
}

static bool send_tlm(uint32 mid, size_t size, uint32 index)
{
    uint8 *b = new_msg(mid, size, 0);
    fill_tlm(b, mid, size, index);
    return app_deliver(b);
}

static bool send_wakeup(void)
{
    now_sec += 1;
    return send_cmd(T_WAKEUP_MID, 0, NULL, 0);
}

static void pstr(uint8 *dst, const char *s)
{
    memset(dst, 0, PATH_LEN);
    snprintf((char *)dst, PATH_LEN, "%s", s);
}

static bool cmd_param(int cc, uint32 mid, uint8 value)
{
    uint8 p[8] = {0};
    put32(p, mid);
    p[4] = value;
    return send_cmd(T_CMD_MID, cc, p, sizeof(p));
}

static bool cmd_thr(uint8 pct)
{
    uint8 p[4] = {0, 0, 0, 0};
    p[0] = pct;
    return send_cmd(T_CMD_MID, CC_SET_THR, p, sizeof(p));
}

static bool cmd_add(const char *src, const char *dst)
{
    uint8 p[2 * PATH_LEN];
    pstr(p, src);
    pstr(p + PATH_LEN, dst);
    return send_cmd(T_CMD_MID, CC_ADD_FILE, p, sizeof(p));
}

static bool cmd_path(int cc, const char *path)
{
    uint8 p[PATH_LEN];
    pstr(p, path);
    return send_cmd(T_CMD_MID, cc, p, sizeof(p));
}

/* ----------------------------------------------------------- telemetry readers */

typedef struct
{
    uint8  cc, cec, thr;
    uint32 used, quota, fused, fquota, stored, decimated, dropped, purged, cycles;
    uint16 fadds, fcount, active, open;
} hk_t;

static const txmsg_t *last_tx(uint32 mid)
{
    int i;
    for (i = (ntx < TX_MAX ? ntx : TX_MAX) - 1; i >= 0; i--) if (tx[i].mid == mid) return &tx[i];
    return NULL;
}

static int count_tx(uint32 mid, int from)
{
    int i, n = 0;
    for (i = from; i < ntx && i < TX_MAX; i++) n += tx[i].mid == mid;
    return n;
}

static bool get_hk(hk_t *h)
{
    const txmsg_t *t;
    int before = ntx;
    const uint8 *p;
    memset(h, 0, sizeof(*h));
    if (!send_cmd(T_SEND_HK_MID, 0, NULL, 0)) return false;
    t = last_tx(T_HK_MID);
    if (t == NULL || count_tx(T_HK_MID, before) != 1)
    {
        FAIL("hk-packet", "SEND_HK transmitted %d housekeeping packets on STOR_HK_TLM_MID, expected 1", count_tx(T_HK_MID, before));
        if (t == NULL || count_tx(T_HK_MID, before) == 0) return false;
    }
    if (t->size != TLM_HDR + HK_PAYLOAD)
    {
        FAIL("hk-packet", "housekeeping packet is %u bytes, expected %u", (unsigned)t->size, (unsigned)(TLM_HDR + HK_PAYLOAD));
        return false;
    }
    p = t->bytes + TLM_HDR;
    h->cc = p[0]; h->cec = p[1]; h->thr = p[2];
    h->used = get32(p + 4); h->quota = get32(p + 8); h->fused = get32(p + 12); h->fquota = get32(p + 16);
    h->stored = get32(p + 20); h->decimated = get32(p + 24); h->dropped = get32(p + 28);
    h->purged = get32(p + 32); h->cycles = get32(p + 36);
    h->fadds = get16(p + 40); h->fcount = get16(p + 42); h->active = get16(p + 44); h->open = get16(p + 46);
    return true;
}

typedef struct { uint32 mid; uint8 pri, dec; uint32 rx, st, de, dr; } est_t;

static bool get_stats(est_t out[MAX_ENT], int *n)
{
    const txmsg_t *t;
    int i, m = 0, ev0 = nev;
    int before = ntx;
    bool unused_zero = true;
    *n = 0;
    if (!send_simple(CC_STATS)) return false;
    t = last_tx(T_STATS_MID);
    if (t == NULL || count_tx(T_STATS_MID, before) == 0)
    {
        FAIL("stats-packet", "SEND_STATS did not transmit a packet on STOR_STATS_TLM_MID");
        return false;
    }
    if (t->size != STATS_PKT)
    {
        FAIL("stats-packet", "statistics packet is %u bytes, expected %u", (unsigned)t->size, (unsigned)STATS_PKT);
        return false;
    }
    EXPECT("stats-event", count_ev(EID_STATS_OK, ev0) == 1 && ev_typed(EID_STATS_OK, EV_INFO, ev0),
           "SEND_STATS sent STOR_STATS_INF_EID (28) %d times, expected once as INFORMATION", count_ev(EID_STATS_OK, ev0));
    for (i = 0; i < MAX_ENT; i++)
    {
        const uint8 *e = t->bytes + TLM_HDR + i * 24;
        if (get32(e) == 0)
        {
            unused_zero = unused_zero && all_zero(e, 24);
            continue;
        }
        out[m].mid = get32(e); out[m].pri = e[4]; out[m].dec = e[5];
        out[m].rx = get32(e + 8); out[m].st = get32(e + 12); out[m].de = get32(e + 16); out[m].dr = get32(e + 20);
        m++;
    }
    EXPECT("stats-packet", unused_zero, "statistics elements without a MsgId are not all zero");
    *n = m;
    return true;
}

static const est_t *est_find(const est_t *e, int n, uint32 mid)
{
    int i;
    for (i = 0; i < n; i++) if (e[i].mid == mid) return &e[i];
    return NULL;
}

typedef struct { uint16 total, index, count; char name[4][PATH_LEN + 1]; uint32 size[4]; } lpkt_t;

static int get_list(int first_tx, lpkt_t *out, int max)
{
    int i, n = 0, k;
    bool pad_zero = true;
    for (i = first_tx; i < ntx && i < TX_MAX && n < max; i++)
    {
        const uint8 *p;
        if (tx[i].mid != T_LIST_MID) continue;
        if (tx[i].size != LIST_PKT)
        {
            FAIL("list-packet", "listing packet is %u bytes, expected the full %u", (unsigned)tx[i].size, (unsigned)LIST_PKT);
            continue;
        }
        p = tx[i].bytes + TLM_HDR;
        out[n].total = get16(p); out[n].index = get16(p + 2); out[n].count = get16(p + 4);
        for (k = 0; k < 4; k++)
        {
            memcpy(out[n].name[k], p + 8 + k * 72, PATH_LEN);
            out[n].name[k][PATH_LEN] = 0;
            out[n].size[k] = get32(p + 8 + k * 72 + PATH_LEN);
            if (k >= out[n].count && !all_zero(p + 8 + k * 72, 72)) pad_zero = false;
        }
        n++;
    }
    EXPECT("list-packet", pad_zero, "a listing packet has data in the entries beyond EntriesInPacket (they must be zero-filled)");
    return n;
}

/* ---------------------------------------------------------- wider API coverage */

/* Calls the specification rules out (or this review cannot answer): reported by name instead of a silent default. */
static void H_Unmodelled(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *name = ud;
    (void)c;
    FAIL("unmodelled", "unmodelled call: %s", name);
    ret_i32(key, -1);
}

#define UNMODELLED(fn) UT_SetHandlerFunction(UT_KEY(fn), H_Unmodelled, (void *)#fn)

static void register_hooks(void)
{
    UT_SetHandlerFunction(UT_KEY(OS_OpenCreate), H_OS_OpenCreate, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_write), H_OS_write, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_TimedWrite), H_OS_write, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_read), H_OS_read, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_TimedRead), H_OS_read, NULL);
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
    UT_SetHandlerFunction(UT_KEY(OS_CloseFileByName), H_OS_CloseFileByName, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_CloseAllFiles), H_OS_CloseAllFiles, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_TranslatePath), H_OS_TranslatePath, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_chmod), H_OS_chmod, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_FileSysStatVolume), H_OS_FileSysStatVolume, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_GetLocalTime), H_OS_ClockNow, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_GetMonotonicTime), H_OS_ClockNow, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_PSP_GetTime), H_PSP_GetTime, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetTime), H_CFE_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetMET), H_CFE_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetTAI), H_CFE_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetUTC), H_CFE_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetMETseconds), H_TIME_METsec, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetMETsubsecs), H_TIME_METsubsec, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_Add), H_TIME_Add, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_Subtract), H_TIME_Subtract, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_Compare), H_TIME_Compare, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_Init), H_MSG_Init, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetMsgId), H_MSG_GetMsgId, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetMsgId), H_MSG_SetMsgId, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetSize), H_MSG_GetSize, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetSize), H_MSG_SetSize, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetFcnCode), H_MSG_GetFcnCode, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetFcnCode), H_MSG_SetFcnCode, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetType), H_MSG_GetType, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetType), H_MSG_SetType, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetHasSecondaryHeader), H_MSG_GetHasSecondaryHeader, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetTypeFromMsgId), H_MSG_GetTypeFromMsgId, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_GetUserDataLength), H_SB_GetUserDataLength, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_IsValidMsgId), H_SB_IsValidMsgId, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_CreatePipe), H_SB_CreatePipe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_Subscribe), H_SB_Subscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_SubscribeEx), H_SB_Subscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_SubscribeLocal), H_SB_Subscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_Unsubscribe), H_SB_Unsubscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_UnsubscribeLocal), H_SB_Unsubscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_TransmitMsg), H_SB_Transmit, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_TransmitBuffer), H_SB_TransmitBuffer, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_AllocateMessageBuffer), H_SB_Alloc, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_ReleaseMessageBuffer), H_TBL_Ok, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_ReceiveBuffer), H_SB_Receive, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_ES_RunLoop), H_ES_RunLoop, NULL);
    UT_SetHookFunction(UT_KEY(CFE_EVS_Register), H_EVS_Register, NULL);
    UT_SetVaHookFunction(UT_KEY(CFE_EVS_SendEvent), ev_hook, NULL);
    UT_SetVaHookFunction(UT_KEY(CFE_EVS_SendEventWithAppID), ev_hook, NULL);
    UT_SetVaHookFunction(UT_KEY(CFE_EVS_SendTimedEvent), ev_hook, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Register), H_TBL_Register, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Load), H_TBL_Load, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Manage), H_TBL_Manage, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Update), H_TBL_Update, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_GetStatus), H_TBL_GetStatus, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_GetAddress), H_TBL_GetAddress, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_ReleaseAddress), H_TBL_ReleaseAddress, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_GetInfo), H_TBL_GetInfo, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Validate), H_TBL_Ok, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Modified), H_TBL_Ok, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_FS_InitHeader), H_FS_InitHeader, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_FS_WriteHeader), H_FS_WriteHeader, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_FS_ReadHeader), H_FS_ReadHeader, NULL);
    UNMODELLED(OS_ShellOutputToFile);
    UNMODELLED(OS_GetFsInfo);
    UNMODELLED(OS_FS_GetPhysDriveName);
    UNMODELLED(OS_FileSysAddFixedMap);
    UNMODELLED(OS_TaskCreate);
    UNMODELLED(CFE_FS_ParseInputFileName);
    UNMODELLED(CFE_FS_ParseInputFileNameEx);
    UNMODELLED(CFE_FS_BackgroundFileDumpRequest);
    UNMODELLED(CFE_ES_CreateChildTask);
    UNMODELLED(CFE_ES_GetPoolBuf);
    UNMODELLED(CFE_ES_PoolCreate);
    UNMODELLED(CFE_ES_PoolCreateNoSem);
    UNMODELLED(CFE_ES_PoolCreateEx);
}

/* --------------------------------------------------------------- test fixture */

static void Setup(void)
{
    UT_ResetState(0);
    mf_reset();
    ntx = nev = nsubs = 0;
    evs_registered = ev_before_register = false;
    sb_pool_next = 0;
    msg_pool_next = 0;
    sub_rc = unsub_rc = CFE_SUCCESS;
    tbl_reg_rc = tbl_load_rc = CFE_SUCCESS;
    recv_rc = CFE_SUCCESS;
    n_tbl_register = 0;
    tbl_validate = NULL;
    tbl_name[0] = 0;
    tbl_size = 0;
    tbl_has_pending = false;
    tbl_updated = false;
    tbl_locked = false;
    tbl_update_count = 0;
    tbl_update_time = 0;
    now_sec = 1000;
    app_running = false;
    boot_is_startup_case = false;
    sems_reset();
    if (tx == NULL) tx = calloc(TX_MAX, sizeof(*tx));
    tbl_default(&tbl_active);
    tbl_entry(&tbl_active, 0, 0x0801, 10, 1);
    tbl_entry(&tbl_active, 1, 0x0800, 20, 1);
    tbl_entry(&tbl_active, 2, 0x0883, 30, 2);
    tbl_entry(&tbl_active, 3, 0x0805, 40, 1);
    register_hooks();
}

static bool is_dir(const char *path)
{
    int i = mf_find(path);
    return i >= 0 && mf[i].dir;
}

/* Boot the application with `t` as the loaded table; false (with a reported failure) when it does not start. */
static bool boot(const tbl_t *t)
{
    tbl_active = *t;
    if (!app_start())
    {
        if (flag_get(&app_hung)) return false;
        if (!boot_is_startup_case)
            FAIL("startup-scenario", "STOR_AppMain ended during initialization (events 7/8/9/10/11/12 sent %d/%d/%d/%d/%d/%d times)",
                 count_ev(EID_PIPE_ERR, 0), count_ev(EID_TBLREG, 0), count_ev(EID_TBLLOAD, 0), count_ev(EID_TBLVAL, 0),
                 count_ev(EID_SUB_ERR, 0), count_ev(EID_DIR_ERR, 0));
        else if (count_ev(EID_DIR_ERR, 0) > 0 || !is_dir(TLM_ROOT) || !is_dir(FILE_ROOT))
            FAIL("startup-roots", "STOR_AppMain ended during initialization (STOR_DIR_CREATE_ERR_EID sent %d times; roots present %d/%d)",
                 count_ev(EID_DIR_ERR, 0), is_dir(TLM_ROOT), is_dir(FILE_ROOT));
        else
            FAIL("startup-run", "STOR_AppMain ended during initialization with a valid table (events 7/8/9/10/11 sent %d/%d/%d/%d/%d times)",
                 count_ev(EID_PIPE_ERR, 0), count_ev(EID_TBLREG, 0), count_ev(EID_TBLLOAD, 0), count_ev(EID_TBLVAL, 0),
                 count_ev(EID_SUB_ERR, 0));
        return false;
    }
    if (!is_dir(TLM_ROOT) || !is_dir(FILE_ROOT))
    {
        if (!boot_is_startup_case)
        {
            FAIL("startup-scenario", "TelemetryRoot " TLM_ROOT " and FileRoot " FILE_ROOT " were not both present after startup");
            return false;
        }
        FAIL("startup-roots", "TelemetryRoot " TLM_ROOT " and FileRoot " FILE_ROOT " were not both created at startup");
        return false;
    }
    return true;
}

static bool exists_file(const char *path)
{
    int i = mf_find(path);
    return i >= 0 && !mf[i].dir;
}

static size_t size_of_file(const char *path)
{
    int i = mf_find(path);
    return i >= 0 ? mf[i].size : (size_t)-1;
}

/* Packet files (by name) directly in TelemetryRoot. */
static bool is_root_packet_file(int i)
{
    char par[NAME_MAX_TEST];
    if (!mf[i].used || mf[i].unlinked || mf[i].dir) return false;
    parent_of(mf[i].path, par, sizeof(par));
    return strcmp(par, TLM_ROOT) == 0 && is_packet_name(base_of(mf[i].path));
}

static uint32 tlm_bytes_on_disk(void)
{
    int i;
    uint32 s = 0;
    for (i = 0; i < MF_MAX; i++)
        if (is_root_packet_file(i)) s += (uint32)mf[i].size;
    return s;
}

static int tlm_file_count(void)
{
    int i, n = 0;
    for (i = 0; i < MF_MAX; i++)
        if (is_root_packet_file(i)) n++;
    return n;
}

/* Number of entries in TelemetryRoot that are not packet files (STOR must create none). */
static int tlm_foreign_entries(void)
{
    int i, n = 0;
    char par[NAME_MAX_TEST];
    size_t rl = strlen(TLM_ROOT);
    for (i = 0; i < MF_MAX; i++)
    {
        if (!mf[i].used || mf[i].unlinked || strncmp(mf[i].path, TLM_ROOT "/", rl + 1) != 0) continue;
        parent_of(mf[i].path, par, sizeof(par));
        if (strcmp(par, TLM_ROOT) != 0 || !is_root_packet_file(i)) n++;
    }
    return n;
}

/* Packet files of one MID, ordered by sequence number; returns how many. */
static int mid_files(uint32 mid, int *idx, int max)
{
    char prefix[48];
    int i, k, n = 0;
    snprintf(prefix, sizeof(prefix), TLM_ROOT "/tlm_%04X_", (unsigned)mid);
    for (i = 0; i < MF_MAX && n < max; i++)
        if (is_root_packet_file(i) && strncmp(mf[i].path, prefix, strlen(prefix)) == 0) idx[n++] = i;
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

/* Remember every packet file name ever seen, to tell which were purged. */
#define SEEN_MAX 128
static char seen[SEEN_MAX][NAME_MAX_TEST];
static int  nseen;

static void snap(void)
{
    int i, k;
    for (i = 0; i < MF_MAX; i++)
    {
        if (!is_root_packet_file(i)) continue;
        for (k = 0; k < nseen; k++) if (strcmp(seen[k], mf[i].path) == 0) break;
        if (k == nseen && nseen < SEEN_MAX) snprintf(seen[nseen++], NAME_MAX_TEST, "%s", mf[i].path);
    }
}

static bool send_tlm_snap(uint32 mid, size_t size, uint32 index)
{
    bool ok = send_tlm(mid, size, index);
    snap();
    return ok;
}

/* ===================================================================== tests */

static void Test_StartupAndHousekeeping(void)
{
    hk_t h;
    int n_ent;
    boot_is_startup_case = true;
    if (!boot(&tbl_active)) return;
    EXPECT("startup-init-event", count_ev(EID_INIT, 0) == 1 && ev_typed(EID_INIT, EV_INFO, 0),
           "STOR_INIT_INF_EID (1) sent %d times, expected once as INFORMATION", count_ev(EID_INIT, 0));
    EXPECT("startup-evs-register", evs_registered && !ev_before_register, "CFE_EVS_Register succeeded %d, an event was sent before it %d",
           evs_registered, ev_before_register);
    EXPECT("startup-table-register", n_tbl_register == 1 && strcmp(tbl_name, "StorageTable") == 0 && tbl_size == TBL_SIZE && tbl_validate != NULL,
           "table registered %d times as '%s' with %u bytes, validation function %s", n_tbl_register, tbl_name, (unsigned)tbl_size,
           tbl_validate ? "present" : "missing");
    EXPECT("startup-subscribe-cmds", subscribed(T_CMD_MID) && subscribed(T_SEND_HK_MID) && subscribed(T_WAKEUP_MID),
           "subscribed to STOR_CMD_MID %d, STOR_SEND_HK_MID %d, STOR_WAKEUP_MID %d", subscribed(T_CMD_MID), subscribed(T_SEND_HK_MID),
           subscribed(T_WAKEUP_MID));
    n_ent = subscribed(0x0801) + subscribed(0x0800) + subscribed(0x0883) + subscribed(0x0805);
    EXPECT("table-subscribe-entries", n_ent == 4, "subscribed to %d of the four used table entries", n_ent);
    EXPECT("tlm-root-clean", tlm_foreign_entries() == 0, "%d entries other than packet files in TelemetryRoot after startup",
           tlm_foreign_entries());
    if (!get_hk(&h)) return;
    EXPECT("hk-table-values", h.quota == 262144 && h.fquota == 1048576 && h.thr == 80,
           "TelemetryQuotaBytes %u FileQuotaBytes %u threshold %u, expected 262144/1048576/80", h.quota, h.fquota, h.thr);
    EXPECT("hk-active-entries", h.active == 4, "ActiveEntries %u with four used table entries", h.active);
    EXPECT("hk-initial", h.cc == 0 && h.cec == 0 && h.used == 0 && h.fused == 0 && h.fcount == 0 && h.open == 0 && h.stored == 0 &&
                             h.decimated == 0 && h.dropped == 0 && h.purged == 0 && h.cycles == 0 && h.fadds == 0,
           "housekeeping after startup on empty roots is not all zero (cc %u cec %u used %u fused %u files %u open %u)", h.cc, h.cec, h.used,
           h.fused, h.fcount, h.open);
    send_wakeup();
    if (!get_hk(&h)) return;
    EXPECT("cmd-hk-wakeup-not-counted", h.cc == 0 && h.cec == 0, "after SEND_HK and WAKEUP: CommandCounter %u CommandErrorCounter %u", h.cc,
           h.cec);
    EXPECT("cmd-noop", send_simple(CC_NOOP) && count_ev(EID_NOOP, 0) == 1 && ev_typed(EID_NOOP, EV_INFO, 0),
           "NOOP sent STOR_NOOP_INF_EID (3) %d times, expected once as INFORMATION", count_ev(EID_NOOP, 0));
    if (get_hk(&h)) EXPECT("cmd-noop", h.cc == 1 && h.cec == 0, "after NOOP: CommandCounter %u, CommandErrorCounter %u", h.cc, h.cec);
}

static void Test_StartupErrTblReg(void)
{
    tbl_reg_rc = CFE_TBL_ERR_INVALID_NAME;
    app_start();
    EXPECT("startup-err-tblreg", count_ev(EID_TBLREG, 0) >= 1 && ev_typed(EID_TBLREG, EV_ERROR, 0),
           "a table registration failure sent STOR_TBL_REG_ERR_EID (8) %d times", count_ev(EID_TBLREG, 0));
}

static void Test_StartupErrTblLoad(void)
{
    tbl_load_rc = CFE_TBL_ERR_NO_ACCESS;
    app_start();
    EXPECT("startup-err-tblload", count_ev(EID_TBLLOAD, 0) >= 1 && ev_typed(EID_TBLLOAD, EV_ERROR, 0),
           "a table load failure sent STOR_TBL_LOAD_ERR_EID (9) %d times", count_ev(EID_TBLLOAD, 0));
}

static void Test_StartupErrSubscribe(void)
{
    sub_rc = CFE_SB_BAD_ARGUMENT;
    app_start();
    EXPECT("startup-err-subscribe", count_ev(EID_SUB_ERR, 0) >= 1 && ev_typed(EID_SUB_ERR, EV_ERROR, 0),
           "a subscription failure sent STOR_SUB_ERR_EID (11) %d times", count_ev(EID_SUB_ERR, 0));
}

static void Test_StartupErrMkdir(void)
{
    fi_mkdir_fail = true;
    app_start();
    EXPECT("startup-err-mkdir", count_ev(EID_DIR_ERR, 0) >= 1 && ev_typed(EID_DIR_ERR, EV_ERROR, 0),
           "a root directory creation failure sent STOR_DIR_CREATE_ERR_EID (12) %d times", count_ev(EID_DIR_ERR, 0));
}

static void Test_StartupErrPipe(void)
{
    recv_rc = CFE_SB_PIPE_RD_ERR;
    app_start();
    EXPECT("startup-err-pipe", count_ev(EID_PIPE_ERR, 0) >= 1 && ev_typed(EID_PIPE_ERR, EV_ERROR, 0),
           "a pipe read failure sent STOR_PIPE_ERR_EID (7) %d times", count_ev(EID_PIPE_ERR, 0));
}

static bool vtbl(const tbl_t *t)
{
    tbl_t copy = *t;
    return tbl_validate != NULL && tbl_validate(&copy) == CFE_SUCCESS;
}

static void Test_TableValidation(void)
{
    tbl_t t, good;
    int i, ev0;
    if (!boot(&tbl_active)) return;
    if (tbl_validate == NULL)
    {
        SETUP_FAILED("startup-table-register", "no table validation function was registered");
        return;
    }
    tbl_default(&good);
    tbl_entry(&good, 0, T_MID_A, 5, 1);
    if (!vtbl(&good))
    {
        SETUP_FAILED("tbl-accept", "a valid table (default sizes, one entry) is rejected");
        return;
    }

    ev0 = nev;
    t = good; t.b[0] = 0;
    EXPECT("tbl-reject-roots", !vtbl(&t), "an empty TelemetryRoot is accepted");
    EXPECT("tbl-reject-event", count_ev(EID_TBLVAL, ev0) >= 1 && ev_typed(EID_TBLVAL, EV_ERROR, ev0),
           "a rejected table sent STOR_TBL_VAL_ERR_EID (10) %d times", count_ev(EID_TBLVAL, ev0));
    t = good; t.b[PATH_LEN] = 0;
    EXPECT("tbl-reject-roots", !vtbl(&t), "an empty FileRoot is accepted");
    t = good; memset(t.b, 'x', PATH_LEN);
    EXPECT("tbl-reject-roots", !vtbl(&t), "an unterminated TelemetryRoot is accepted");
    t = good; memset(t.b + PATH_LEN, 'x', PATH_LEN);
    EXPECT("tbl-reject-roots", !vtbl(&t), "an unterminated FileRoot is accepted");
    t = good; tbl_sizes(&t, 0, 1048576, 16384, 80);
    EXPECT("tbl-reject-quota", !vtbl(&t), "TelemetryQuotaBytes 0 is accepted");
    t = good; tbl_sizes(&t, 262144, 0, 16384, 80);
    EXPECT("tbl-reject-quota", !vtbl(&t), "FileQuotaBytes 0 is accepted");
    t = good; tbl_sizes(&t, 262144, 1048576, 1023, 80);
    EXPECT("tbl-reject-maxfile", !vtbl(&t), "MaxPacketFileBytes 1023 is accepted");
    t = good; tbl_sizes(&t, 262144, 1048576, 262145, 80);
    EXPECT("tbl-reject-maxfile", !vtbl(&t), "MaxPacketFileBytes above TelemetryQuotaBytes is accepted");
    t = good; tbl_sizes(&t, 262144, 1048576, 16384, 0);
    EXPECT("tbl-reject-threshold", !vtbl(&t), "PurgeThresholdPercent 0 is accepted");
    t = good; tbl_sizes(&t, 262144, 1048576, 16384, 101);
    EXPECT("tbl-reject-threshold", !vtbl(&t), "PurgeThresholdPercent 101 is accepted");
    t = good; tbl_entry(&t, 1, T_MID_B, 0, 1);
    EXPECT("tbl-reject-entry", !vtbl(&t), "a used entry with Priority 0 is accepted");
    t = good; tbl_entry(&t, 1, T_MID_B, 5, 0);
    EXPECT("tbl-reject-entry", !vtbl(&t), "a used entry with Decimation 0 is accepted");
    t = good; tbl_entry(&t, 1, T_MID_A, 7, 2);
    EXPECT("tbl-reject-duplicate", !vtbl(&t), "duplicate MsgIds in adjacent entries are accepted");
    t = good; tbl_entry(&t, 31, T_MID_A, 7, 2);
    EXPECT("tbl-reject-duplicate", !vtbl(&t), "duplicate MsgIds in entries 0 and 31 are accepted");

    t = good; tbl_sizes(&t, 1024, 1, 1024, 1);
    EXPECT("tbl-accept", vtbl(&t), "MaxPacketFileBytes == TelemetryQuotaBytes == 1024, FileQuotaBytes 1 and threshold 1 are rejected");
    t = good; tbl_sizes(&t, 262144, 1048576, 262144, 100);
    EXPECT("tbl-accept", vtbl(&t), "MaxPacketFileBytes == TelemetryQuotaBytes and threshold 100 are rejected");
    t = good; tbl_entry(&t, 5, 0, 0, 0);
    EXPECT("tbl-accept", vtbl(&t), "an unused entry (MsgId CFE_SB_INVALID_MSG_ID) with Priority 0 is rejected");
    tbl_default(&t);
    for (i = 0; i < MAX_ENT; i++) tbl_entry(&t, i, 0x0900u + (uint32)i, 255, 255);
    EXPECT("tbl-accept", vtbl(&t), "32 used entries with Priority and Decimation 255 are rejected");
    t = good; memset(t.b + 144, 0, 8 * MAX_ENT);
    EXPECT("tbl-accept", vtbl(&t), "a table with no used entries is rejected");
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
    for (i = 0; i < 4; i++) send_tlm(T_MID_A, 100, (uint32)i);
    for (i = 0; i < 10; i++) send_tlm(T_MID_B, 100, (uint32)i);
    for (i = 0; i < 10; i++) send_tlm(T_MID_C, 100, (uint32)i);
    if (!get_stats(e, &n)) return;
    a = est_find(e, n, T_MID_A); b = est_find(e, n, T_MID_B); c = est_find(e, n, T_MID_C);
    EXPECT("stats-packet", a && b && c && n == 3, "statistics list %d entries, expected exactly the three used entries", n);
    if (!(a && b && c)) return;
    EXPECT("decimation", a->rx == 4 && a->st == 4 && a->de == 0 && a->dr == 0, "decimation 1: received/stored/decimated/dropped %u/%u/%u/%u, expected 4/4/0/0",
           a->rx, a->st, a->de, a->dr);
    EXPECT("decimation", b->rx == 10 && b->st == 4 && b->de == 6, "decimation 3: received %u stored %u decimated %u, expected 10/4/6", b->rx,
           b->st, b->de);
    EXPECT("decimation", c->rx == 10 && c->st == 1 && c->de == 9, "decimation 255: received %u stored %u decimated %u, expected 10/1/9", c->rx,
           c->st, c->de);
    EXPECT("stats-packet", a->pri == 10 && b->pri == 20 && c->pri == 30 && a->dec == 1 && b->dec == 3 && c->dec == 255,
           "statistics report priorities %u/%u/%u and decimations %u/%u/%u, expected 10/20/30 and 1/3/255", a->pri, b->pri, c->pri, a->dec,
           b->dec, c->dec);
    if (get_hk(&h))
        EXPECT("hk-packet-totals", h.stored == 9 && h.decimated == 15, "PacketsStored %u, PacketsDecimated %u, expected 9 and 15", h.stored,
               h.decimated);
    send_simple(CC_CLOSE);
    nf = mid_files(T_MID_B, idx, 16);
    found = 0;
    for (i = 0; i < nf; i++)
    {
        size_t pos = FS_HDR;
        while (pos + 100 <= mf[idx[i]].size)
        {
            uint32 got = get32(mf[idx[i]].data + pos + TLM_HDR);
            if (found < 4)
                EXPECT("decimation", got == expect_idx[found], "with decimation 3 the stored packet #%d is #%u, expected #%u", found, got,
                       expect_idx[found]);
            found++;
            pos += 100;
        }
    }
    EXPECT("decimation", found == 4, "%d packets of the decimation 3 entry are in its files, expected 4", found);

    EXPECT("cmd-set-param-ok", cmd_param(CC_SET_DEC, T_MID_B, 1) && count_ev(EID_PARAM_OK, 0) == 1 && ev_typed(EID_PARAM_OK, EV_INFO, 0),
           "SET_DECIMATION sent STOR_SET_PARAM_INF_EID (16) %d times, expected once as INFORMATION", count_ev(EID_PARAM_OK, 0));
    EXPECT("cmd-set-param-ok", cmd_param(CC_SET_PRI, T_MID_B, 77) && count_ev(EID_PARAM_OK, 0) == 2,
           "SET_PRIORITY sent STOR_SET_PARAM_INF_EID (16): total %d, expected 2", count_ev(EID_PARAM_OK, 0));
    send_tlm(T_MID_B, 100, 10);
    send_tlm(T_MID_B, 100, 11);
    if (get_stats(e, &n))
    {
        b = est_find(e, n, T_MID_B);
        EXPECT("cmd-set-param-ok", b && b->pri == 77 && b->dec == 1 && b->rx == 12 && b->st == 6,
               "after SET_*: priority %u decimation %u received %u stored %u, expected 77/1/12/6", b ? b->pri : 0, b ? b->dec : 0,
               b ? b->rx : 0, b ? b->st : 0);
    }
}

static void check_packet_file(const char *path, uint32 mid, size_t pkt, uint32 first_idx, uint32 count)
{
    int i = mf_find(path);
    uint32 k;
    const uint8 *h;
    uint8 want[MSG_MAX_BYTES];
    bool same = true;
    EXPECT("pkt-file-naming", i >= 0 && !mf[i].dir, "%s does not exist", path);
    if (i < 0 || mf[i].dir) return;
    EXPECT("pkt-file-content", mf[i].size == FS_HDR + (size_t)count * pkt, "%s is %u bytes, expected %u", base_of(path),
           (unsigned)mf[i].size, (unsigned)(FS_HDR + count * pkt));
    if (mf[i].size < FS_HDR) return;
    h = mf[i].data;
    EXPECT("pkt-file-header", get_be32(h) == FS_CONTENT_ID && get_be32(h + 8) == FS_HDR,
           "%s does not start with a header written by CFE_FS_WriteHeader (content type 0x%08X, length %u)", base_of(path),
           (unsigned)get_be32(h), (unsigned)get_be32(h + 8));
    EXPECT("pkt-file-header", get_be32(h + 4) == FS_SUBTYPE, "%s header sub type 0x%X, expected 0x5354", base_of(path),
           (unsigned)get_be32(h + 4));
    EXPECT("pkt-file-header", strncmp((const char *)h + 32, "STOR packet file", 32) == 0, "%s header description '%.32s'", base_of(path),
           (const char *)h + 32);
    for (k = 0; k < count && FS_HDR + (k + 1) * pkt <= mf[i].size; k++)
    {
        fill_tlm(want, mid, pkt, first_idx + k);
        if (memcmp(mf[i].data + FS_HDR + k * pkt, want, pkt) != 0) same = false;
    }
    EXPECT("pkt-file-content", same, "%s does not hold packets #%u..#%u exactly as received", base_of(path), first_idx, first_idx + count - 1);
}

static void Test_RolloverAndNaming(void)
{
    tbl_t t;
    hk_t h;
    int i, idx[8], nb, ev0;
    uint32 before;
    tbl_default(&t);
    tbl_sizes(&t, 65536, 1048576, 1024, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 20, 1);
    if (!boot(&t)) return;
    for (i = 0; i < 10; i++) send_tlm(T_MID_A, 300, (uint32)i);
    check_packet_file(TLM_ROOT "/tlm_08E0_00000000.bin", T_MID_A, 300, 0, 3);
    check_packet_file(TLM_ROOT "/tlm_08E0_00000001.bin", T_MID_A, 300, 3, 3);
    check_packet_file(TLM_ROOT "/tlm_08E0_00000002.bin", T_MID_A, 300, 6, 3);
    check_packet_file(TLM_ROOT "/tlm_08E0_00000003.bin", T_MID_A, 300, 9, 1);
    EXPECT("pkt-rollover", !exists_file(TLM_ROOT "/tlm_08E0_00000004.bin") && tlm_file_count() == 4,
           "%d packet files for ten 300-byte packets with MaxPacketFileBytes 1024, expected 4", tlm_file_count());
    if (get_hk(&h))
    {
        EXPECT("tlm-bytes-used", h.used == 3 * 964 + 364 && h.used == tlm_bytes_on_disk(), "TelemetryBytesUsed %u, expected %u (on disk %u)",
               h.used, 3 * 964 + 364, tlm_bytes_on_disk());
        EXPECT("hk-packet-totals", h.stored == 10, "PacketsStored %u, expected 10", h.stored);
        EXPECT("hk-open-files", h.open == 1 && n_open_total() == 1, "OpenPacketFiles %u with %u OSAL files open, expected 1 and 1", h.open,
               n_open_total());
    }
    /* Exactly filling MaxPacketFileBytes is allowed: 64 + 4 * 240 == 1024. */
    for (i = 0; i < 9; i++) send_tlm(T_MID_B, 240, (uint32)i);
    nb = mid_files(T_MID_B, idx, 8);
    EXPECT("pkt-file-naming", exists_file(TLM_ROOT "/tlm_08E1_00000000.bin") && exists_file(TLM_ROOT "/tlm_08E1_00000001.bin"),
           "the second MID's files do not start at sequence 0 (sequence numbers are per MID)");
    EXPECT("pkt-rollover", nb == 3 && mf[idx[0]].size == 1024 && mf[idx[1]].size == 1024 && mf[idx[2]].size == 304,
           "nine 240-byte packets gave %d files (expected 3 of 1024, 1024 and 304 bytes: exactly filling the maximum is allowed)", nb);
    before = n_close;
    ev0 = nev;
    EXPECT("cmd-close-files", send_simple(CC_CLOSE) && count_ev(EID_CLOSE_OK, ev0) == 1 && ev_typed(EID_CLOSE_OK, EV_INFO, ev0),
           "CLOSE_FILES sent STOR_CLOSE_INF_EID (27) %d times, expected once as INFORMATION", count_ev(EID_CLOSE_OK, ev0));
    EXPECT("cmd-close-files", n_close - before == 2 && n_open_total() == 0, "CLOSE_FILES closed %u files and left %u open, expected 2 and 0",
           n_close - before, n_open_total());
    if (get_hk(&h)) EXPECT("hk-open-files", h.open == 0, "OpenPacketFiles %u after CLOSE_FILES", h.open);
    send_tlm(T_MID_A, 300, 20);
    nb = mid_files(T_MID_A, idx, 8);
    EXPECT("pkt-no-reopen", nb == 5 && exists_file(TLM_ROOT "/tlm_08E0_00000004.bin") && size_of_file(TLM_ROOT "/tlm_08E0_00000003.bin") == 364,
           "a packet after CLOSE_FILES must start the next sequence (tlm_08E0_00000004.bin), %d files for the MID", nb);
    /* A packet that cannot fit even in an empty file is dropped; the open file stays open. */
    ev0 = nev;
    send_tlm(T_MID_A, 1000, 21);
    if (get_hk(&h))
    {
        EXPECT("pkt-too-large", count_ev(EID_FILE_WRITE, ev0) == 1 && ev_typed(EID_FILE_WRITE, EV_ERROR, ev0) && h.dropped == 1 &&
                                    h.stored == 20 && h.open == 1 && mid_files(T_MID_A, idx, 8) == 5,
               "a 1000-byte packet with MaxPacketFileBytes 1024: event 15 x%d, dropped %u, stored %u, open %u, %d files (expected 1/1/20/1/5)",
               count_ev(EID_FILE_WRITE, ev0), h.dropped, h.stored, h.open, mid_files(T_MID_A, idx, 8));
    }
    EXPECT("tlm-root-clean", tlm_foreign_entries() == 0, "%d entries other than packet files were created in TelemetryRoot",
           tlm_foreign_entries());
}

static void Test_SequenceAndScan(void)
{
    tbl_t t;
    hk_t h;
    tbl_default(&t);
    tbl_sizes(&t, 65536, 1048576, 1024, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 10, 1);
    mf_mkdir_p(TLM_ROOT "/old");
    mf_mkdir_p(FILE_ROOT "/x");
    mf_put_pkt(TLM_ROOT "/tlm_08E0_00000007.bin", 1000, 1);
    mf_put(TLM_ROOT "/readme.txt", 500, 7);
    mf_put_pkt(TLM_ROOT "/tlm_08E1_00000003.bin", 500, 2);
    mf_put_pkt(TLM_ROOT "/old/tlm_08E0_00000020.bin", 200, 8);
    mf_put(FILE_ROOT "/x/y.bin", 700, 3);
    mf_put(FILE_ROOT "/z.bin", 300, 4);
    if (!boot(&t)) return;
    EXPECT("startup-roots-existing", count_ev(EID_DIR_ERR, 0) == 0, "existing roots raised STOR_DIR_CREATE_ERR_EID (12) %d times",
           count_ev(EID_DIR_ERR, 0));
    if (get_hk(&h))
    {
        EXPECT("scan-tlm", h.used == 1500, "TelemetryBytesUsed %u at startup, expected 1500 (the packet files directly in TelemetryRoot only)",
               h.used);
        EXPECT("scan-files", h.fused == 1000 && h.fcount == 2, "FileBytesUsed %u and FileCount %u at startup, expected 1000 and 2 (recursive)",
               h.fused, h.fcount);
    }
    send_tlm(T_MID_A, 300, 0);
    EXPECT("scan-sequence", exists_file(TLM_ROOT "/tlm_08E0_00000008.bin"),
           "the next file of a MID is not sequence 8 after an existing sequence 7 (only files directly in TelemetryRoot count)");
    send_tlm(T_MID_B, 300, 0);
    EXPECT("scan-sequence", exists_file(TLM_ROOT "/tlm_08E1_00000004.bin"), "the next file of the second MID is not sequence 4 after 3");
    if (get_hk(&h))
        EXPECT("scan-tlm", h.used == 1500 + 2 * (FS_HDR + 300), "TelemetryBytesUsed %u after two new files, expected %u", h.used,
               1500 + 2 * (FS_HDR + 300));
    EXPECT("purge-non-packet", exists_file(TLM_ROOT "/readme.txt") && exists_file(TLM_ROOT "/old/tlm_08E0_00000020.bin"),
           "a file in TelemetryRoot that is not a packet file (or is in a subdirectory) was removed");
}

/* Purge: A (priority 5), B (200), C (100); quota 16384, threshold 50 percent (8192), files of 964 bytes. */
static void Test_PurgeOrder(void)
{
    tbl_t t;
    hk_t h;
    int i, k, nb, nc, na, idx[64], ev0;
    bool lowest_first = true;
    int present_seen = 0;
    tbl_default(&t);
    tbl_sizes(&t, 16384, 1048576, 1024, 50);
    tbl_entry(&t, 0, T_MID_A, 5, 1);
    tbl_entry(&t, 1, T_MID_B, 200, 1);
    tbl_entry(&t, 2, T_MID_C, 100, 1);
    nseen = 0;
    mf_mkdir_p(TLM_ROOT);
    mf_put(TLM_ROOT "/notes.txt", 100, 3);
    if (!boot(&t)) return;
    /* B: two files, C: one file, all closed: 2892 bytes, below the 8192 limit. */
    for (i = 0; i < 6; i++) send_tlm_snap(T_MID_B, 300, (uint32)i);
    for (i = 0; i < 3; i++) send_tlm_snap(T_MID_C, 300, (uint32)i);
    send_simple(CC_CLOSE);
    nb = mid_files(T_MID_B, idx, 8);
    nc = mid_files(T_MID_C, idx, 8);
    if (n_remove != 0 || nb != 2 || nc != 1)
    {
        SETUP_FAILED("purge-threshold", "%u files removed below the threshold, %d files for B and %d for C (expected 0, 2, 1)", n_remove, nb, nc);
        return;
    }
    /* A: eleven files of 964 bytes would need 10604 bytes: the oldest A files must be purged. */
    ev0 = nev;
    for (i = 0; i < 33; i++) send_tlm_snap(T_MID_A, 300, (uint32)i);
    if (!get_hk(&h)) return;
    EXPECT("purge-threshold", h.purged > 0 && h.used <= 8192, "after 33 more packets: FilesPurged %u, TelemetryBytesUsed %u (limit 8192)",
           h.purged, h.used);
    EXPECT("tlm-bytes-used", h.used == tlm_bytes_on_disk(), "TelemetryBytesUsed %u, packet files on disk %u", h.used, tlm_bytes_on_disk());
    EXPECT("quota-drop", h.dropped == 0, "PacketsDropped %u although the quota was never reached", h.dropped);
    EXPECT("purge-counters", h.purged == n_remove, "FilesPurged %u, packet files removed %u", h.purged, n_remove);
    EXPECT("purge-event", count_ev(EID_PURGE, ev0) == (int)h.cycles && h.cycles > 0 && ev_typed(EID_PURGE, EV_DEBUG, ev0),
           "PurgeCycles %u, STOR_PURGE_INF_EID (14, DEBUG) sent %d times (one per purge pass)", h.cycles, count_ev(EID_PURGE, ev0));
    nb = mid_files(T_MID_B, idx, 8);
    nc = mid_files(T_MID_C, idx, 8);
    EXPECT("purge-order", nb == 2 && nc == 1, "higher-priority files were purged while lower-priority ones remained (B %d of 2, C %d of 1)", nb,
           nc);
    na = mid_files(T_MID_A, idx, 64);
    EXPECT("purge-threshold", na < 11 && na >= 1, "%d of 11 A files remain", na);
    for (k = 0; k < nseen; k++)
    {
        int j;
        if (strncmp(seen[k], TLM_ROOT "/tlm_08E0_", strlen(TLM_ROOT) + 10) != 0) continue;
        if (!exists_file(seen[k]))
        {
            for (j = 0; j < nseen; j++)
                if (strncmp(seen[j], TLM_ROOT "/tlm_08E0_", strlen(TLM_ROOT) + 10) == 0 && exists_file(seen[j]) && strcmp(seen[j], seen[k]) < 0)
                    lowest_first = false;
        }
        else
        {
            present_seen++;
        }
    }
    EXPECT("purge-order", lowest_first, "a purged file had a higher sequence number than a remaining file of the same MID");
    EXPECT("pkt-file-naming", present_seen == na, "a packet file appeared that was never seen while packets were stored");
    /* The open file is never purged, even when over the threshold. */
    EXPECT("cmd-set-threshold-ok", cmd_thr(1) && count_ev(EID_PARAM_OK, 0) == 1, "SET_PURGE_THRESHOLD 1 sent STOR_SET_PARAM_INF_EID %d times",
           count_ev(EID_PARAM_OK, 0));
    if (get_hk(&h))
    {
        EXPECT("purge-open-files", h.open >= 1 && tlm_file_count() == (int)h.open,
               "with a 1 percent threshold %d packet files remain and %u are open: exactly the open files must remain", tlm_file_count(), h.open);
        EXPECT("purge-set-threshold", h.thr == 1 && h.used == tlm_bytes_on_disk(), "threshold %u, TelemetryBytesUsed %u, on disk %u", h.thr,
               h.used, tlm_bytes_on_disk());
    }
    send_simple(CC_CLOSE);
    EXPECT("purge-close", tlm_file_count() == 0, "%d packet files left after CLOSE_FILES with a 1 percent threshold (a purge pass follows the close)",
           tlm_file_count());
    EXPECT("purge-non-packet", exists_file(TLM_ROOT "/notes.txt"), "a non-packet file in TelemetryRoot was purged");
}

/*
 * Existing packet files: no purge during initialization; the first pass deletes the file whose MID is not a used
 * entry (priority 0), then orders equal priorities by sequence number and then by MID value.
 */
static void Test_PurgeExisting(void)
{
    tbl_t t;
    hk_t h;
    int ev0;
    tbl_default(&t);
    tbl_sizes(&t, 4096, 1048576, 1024, 20); /* limit 819 */
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 10, 1);
    mf_mkdir_p(TLM_ROOT "/keep");
    mf_put_pkt(TLM_ROOT "/tlm_08F0_00000002.bin", 300, 1);
    mf_put_pkt(TLM_ROOT "/tlm_08E1_00000005.bin", 300, 2);
    mf_put_pkt(TLM_ROOT "/tlm_08E1_00000004.bin", 300, 3);
    mf_put_pkt(TLM_ROOT "/tlm_08E0_00000005.bin", 300, 4);
    mf_put_pkt(TLM_ROOT "/tlm_08E0_00000006.bin", 300, 5);
    mf_put(TLM_ROOT "/readme.txt", 500, 6);
    mf_put_pkt(TLM_ROOT "/keep/tlm_08E0_00000001.bin", 300, 7);
    if (!boot(&t)) return;
    EXPECT("purge-not-at-init", tlm_file_count() == 5 && n_remove == 0, "%u packet files were removed during initialization", n_remove);
    if (!get_hk(&h)) return;
    if (h.used != 1500)
    {
        SETUP_FAILED("scan-tlm", "TelemetryBytesUsed %u at startup, expected 1500", h.used);
        return;
    }
    EXPECT("purge-not-at-init", h.purged == 0 && h.cycles == 0, "FilesPurged %u PurgeCycles %u before any WAKEUP", h.purged, h.cycles);
    ev0 = nev;
    send_wakeup();
    if (n_remove == 0)
    {
        FAIL("purge-wakeup", "a WAKEUP with 1500 bytes of closed packet files above the 819-byte limit purged nothing");
        return;
    }
    EXPECT("purge-unused-mid", !exists_file(TLM_ROOT "/tlm_08F0_00000002.bin"),
           "the packet file of a MID that is not a used entry (priority 0) was not purged");
    EXPECT("purge-order", !exists_file(TLM_ROOT "/tlm_08E1_00000004.bin") && exists_file(TLM_ROOT "/tlm_08E0_00000006.bin"),
           "with equal priorities the lowest sequence number was not purged first");
    EXPECT("purge-tie-break", !exists_file(TLM_ROOT "/tlm_08E0_00000005.bin") && exists_file(TLM_ROOT "/tlm_08E1_00000005.bin"),
           "with equal priority and sequence, the file of the lower MID value (0x08E0) was not purged first");
    EXPECT("purge-non-packet", exists_file(TLM_ROOT "/readme.txt") && exists_file(TLM_ROOT "/keep/tlm_08E0_00000001.bin"),
           "a file that is not a packet file directly in TelemetryRoot was purged");
    if (get_hk(&h))
    {
        EXPECT("purge-counters", h.purged == 3 && h.used == 600 && h.used == tlm_bytes_on_disk(),
               "FilesPurged %u TelemetryBytesUsed %u (on disk %u), expected 3 and 600", h.purged, h.used, tlm_bytes_on_disk());
        EXPECT("purge-event", h.cycles == 1 && count_ev(EID_PURGE, ev0) == 1 && ev_typed(EID_PURGE, EV_DEBUG, ev0),
               "PurgeCycles %u, STOR_PURGE_INF_EID (14) %d times, expected one DEBUG event for one pass", h.cycles, count_ev(EID_PURGE, ev0));
    }
    send_tlm(T_MID_A, 300, 0);
    EXPECT("scan-sequence", exists_file(TLM_ROOT "/tlm_08E0_00000007.bin"),
           "the next file of MID 0x08E0 is not sequence 7 (highest existing 6 directly in TelemetryRoot)");
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
    for (i = 0; i < 6; i++) send_tlm(T_MID_B, 300, (uint32)i);
    for (i = 0; i < 9; i++) send_tlm(T_MID_A, 300, (uint32)i);
    send_simple(CC_CLOSE);
    if (n_remove != 0)
    {
        SETUP_FAILED("purge-threshold", "%u files were purged below the threshold", n_remove);
        return;
    }
    EXPECT("cmd-set-param-ok", cmd_param(CC_SET_PRI, T_MID_B, 1) && cmd_param(CC_SET_PRI, T_MID_A, 250) && count_ev(EID_PARAM_OK, 0) == 2,
           "SET_PRIORITY at run time sent STOR_SET_PARAM_INF_EID %d times, expected 2", count_ev(EID_PARAM_OK, 0));
    EXPECT("cmd-set-threshold-ok", cmd_thr(20) && count_ev(EID_PARAM_OK, 0) == 3, "SET_PURGE_THRESHOLD 20 sent STOR_SET_PARAM_INF_EID");
    send_wakeup();
    if (!get_hk(&h)) return;
    nb = mid_files(T_MID_B, idx, 16);
    na = mid_files(T_MID_A, idx, 16);
    EXPECT("purge-runtime-priority", nb == 0 && na == 3, "after reversing the priorities B has %d files and A %d, expected 0 and 3", nb, na);
    EXPECT("purge-set-threshold", h.used <= 3276 && h.used == tlm_bytes_on_disk(), "TelemetryBytesUsed %u after the purge (limit 3276)",
           h.used);
}

static void Test_QuotaDrop(void)
{
    tbl_t t;
    hk_t h;
    est_t e[MAX_ENT];
    int i, n = 0, ev0;
    uint32 dropped0;
    tbl_default(&t);
    /* Four files of 964 bytes use 3856; 330 bytes remain: a 300-byte packet fits, but not with a new file's header. */
    tbl_sizes(&t, 4186, 1048576, 1024, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 20, 1);
    if (!boot(&t)) return;
    for (i = 0; i < 12; i++) send_tlm(T_MID_A, 300, (uint32)i);
    if (!get_hk(&h)) return;
    if (h.stored != 12 || h.dropped != 0 || count_ev(EID_QUOTA, 0) != 0)
    {
        SETUP_FAILED("quota-drop", "12 packets below the quota: stored %u, dropped %u, event 13 x%d (expected 12/0/0)", h.stored, h.dropped,
                     count_ev(EID_QUOTA, 0));
        return;
    }
    for (i = 12; i < 20; i++) send_tlm(T_MID_A, 300, (uint32)i);
    if (!get_hk(&h)) return;
    EXPECT("quota-header", h.stored == 12 && h.dropped == 8,
           "stored %u dropped %u, expected 12 and 8: a packet that starts a new file needs room for the 64-byte header too", h.stored,
           h.dropped);
    EXPECT("quota-drop", h.used <= 4186 && h.used == tlm_bytes_on_disk(), "TelemetryBytesUsed %u (on disk %u) with TelemetryQuotaBytes 4186",
           h.used, tlm_bytes_on_disk());
    EXPECT("quota-no-purge", h.purged == 0 && tlm_file_count() == 4,
           "FilesPurged %u, %d packet files: the quota check must not delete files (threshold 100)", h.purged, tlm_file_count());
    EXPECT("quota-event", count_ev(EID_QUOTA, 0) == 1 && ev_typed(EID_QUOTA, EV_ERROR, 0),
           "STOR_TLM_QUOTA_ERR_EID (13, ERROR) sent %d times for 8 drops of one entry, expected once", count_ev(EID_QUOTA, 0));
    if (get_stats(e, &n))
    {
        const est_t *a = est_find(e, n, T_MID_A);
        EXPECT("quota-drop", a && a->dr == 8 && a->st == 12 && a->rx == 20, "per-entry Received %u Stored %u Dropped %u, expected 20/12/8",
               a ? a->rx : 0, a ? a->st : 0, a ? a->dr : 0);
    }
    /* Another entry is reported separately: its first drop sends the event for that entry. */
    for (i = 0; i < 2; i++) send_tlm(T_MID_B, 300, (uint32)i);
    EXPECT("quota-event", count_ev(EID_QUOTA, 0) == 2, "STOR_TLM_QUOTA_ERR_EID sent %d times after a second entry's drops, expected 2",
           count_ev(EID_QUOTA, 0));
    /* A lower threshold frees space; the next packet stores again and a later drop reports again. */
    EXPECT("cmd-set-threshold-ok", cmd_thr(30), "SET_PURGE_THRESHOLD 30 not handled");
    if (!get_hk(&h)) return;
    EXPECT("purge-set-threshold", h.purged == 3 && h.used == 964, "SET_PURGE_THRESHOLD 30: FilesPurged %u TelemetryBytesUsed %u, expected 3 and 964",
           h.purged, h.used);
    dropped0 = h.dropped;
    ev0 = count_ev(EID_QUOTA, 0);
    send_tlm(T_MID_A, 300, 100);
    if (get_hk(&h))
        EXPECT("quota-drop", h.dropped == dropped0 && h.stored == 13, "after space was freed: dropped %u -> %u, stored %u (expected 13)",
               dropped0, h.dropped, h.stored);
    EXPECT("quota-event", count_ev(EID_QUOTA, 0) == ev0, "STOR_TLM_QUOTA_ERR_EID sent although the packet was stored");
    cmd_thr(100);
    for (i = 0; i < 20; i++) send_tlm(T_MID_A, 300, (uint32)(200 + i));
    EXPECT("quota-event", count_ev(EID_QUOTA, 0) == ev0 + 1, "a new drop episode after a store sent STOR_TLM_QUOTA_ERR_EID %d times, expected 1",
           count_ev(EID_QUOTA, 0) - ev0);
}

static void Test_SetParamCommands(void)
{
    hk_t a, b;
    est_t e[MAX_ENT];
    int n = 0;
    const est_t *x;
    if (!boot(&tbl_active)) return;
    if (!get_hk(&a)) return;
    cmd_param(CC_SET_PRI, 0x0FFF, 5);
    cmd_param(CC_SET_DEC, 0x0883, 0);
    cmd_param(CC_SET_PRI, 0x0883, 0);
    EXPECT("cmd-set-param-reject", count_ev(EID_PARAM_ERR, 0) == 3 && ev_typed(EID_PARAM_ERR, EV_ERROR, 0) && count_ev(EID_PARAM_OK, 0) == 0,
           "unknown MsgId or value 0 sent STOR_SET_PARAM_ERR_EID (17, ERROR) %d times, expected 3", count_ev(EID_PARAM_ERR, 0));
    if (get_hk(&b))
        EXPECT("cmd-set-param-reject", b.cec == a.cec + 3 && b.cc == a.cc, "three rejections: CommandErrorCounter %u -> %u, CommandCounter %u -> %u",
               a.cec, b.cec, a.cc, b.cc);
    cmd_thr(0);
    cmd_thr(101);
    EXPECT("cmd-set-threshold-reject", count_ev(EID_PARAM_ERR, 0) == 5, "thresholds 0 and 101: STOR_SET_PARAM_ERR_EID total %d, expected 5",
           count_ev(EID_PARAM_ERR, 0));
    if (get_hk(&b))
        EXPECT("cmd-set-threshold-reject", b.thr == 80 && b.cec == a.cec + 5, "threshold %u (expected 80), CommandErrorCounter %u -> %u", b.thr, a.cec,
               b.cec);
    cmd_thr(50);
    EXPECT("cmd-set-threshold-ok", count_ev(EID_PARAM_OK, 0) == 1 && ev_typed(EID_PARAM_OK, EV_INFO, 0),
           "SET_PURGE_THRESHOLD 50 sent STOR_SET_PARAM_INF_EID (16, INFORMATION) %d times", count_ev(EID_PARAM_OK, 0));
    if (get_hk(&b)) EXPECT("cmd-set-threshold-ok", b.thr == 50 && b.cc == a.cc + 1, "threshold %u after SET_PURGE_THRESHOLD 50", b.thr);
    cmd_thr(100);
    if (get_hk(&b)) EXPECT("cmd-set-threshold-ok", b.thr == 100, "threshold %u after SET_PURGE_THRESHOLD 100", b.thr);
    cmd_thr(1);
    if (get_hk(&b)) EXPECT("cmd-set-threshold-ok", b.thr == 1, "threshold %u after SET_PURGE_THRESHOLD 1", b.thr);
    cmd_param(CC_SET_PRI, 0x0801, 255);
    cmd_param(CC_SET_DEC, 0x0801, 255);
    if (get_stats(e, &n))
    {
        x = est_find(e, n, 0x0801);
        EXPECT("cmd-set-param-ok", x && x->pri == 255 && x->dec == 255, "priority %u and decimation %u after setting both to 255", x ? x->pri : 0,
               x ? x->dec : 0);
    }
}

static void Test_CommandErrors(void)
{
    hk_t a, b;
    uint8 extra[4] = {0};
    const int cc_with_payload[] = {CC_SET_PRI, CC_SET_DEC, CC_SET_THR, CC_ADD_FILE, CC_DEL_FILE, CC_LIST_DIR};
    int i, before;
    if (!boot(&tbl_active)) return;
    if (!get_hk(&a)) return;
    send_simple(77);
    EXPECT("cmd-bad-cc", count_ev(EID_CC_ERR, 0) == 1 && ev_typed(EID_CC_ERR, EV_ERROR, 0),
           "function code 77 sent STOR_CC_ERR_EID (2, ERROR) %d times, expected once", count_ev(EID_CC_ERR, 0));
    if (get_hk(&b))
        EXPECT("cmd-bad-cc", b.cec == a.cec + 1 && b.cc == a.cc, "function code 77: CommandErrorCounter %u -> %u, CommandCounter %u -> %u", a.cec,
               b.cec, a.cc, b.cc);
    a = b;
    send_cmd(T_CMD_MID, CC_NOOP, extra, 4);
    send_cmd(T_CMD_MID, CC_RESET, extra, 4);
    send_cmd(T_CMD_MID, CC_LIST_ALL, extra, 4);
    send_cmd(T_CMD_MID, CC_STATS, extra, 4);
    send_cmd(T_CMD_MID, CC_CLOSE, extra, 4);
    for (i = 0; i < 6; i++) send_cmd(T_CMD_MID, cc_with_payload[i], extra, 1);
    EXPECT("cmd-bad-length", count_ev(EID_LEN_ERR, 0) == 11 && ev_typed(EID_LEN_ERR, EV_ERROR, 0),
           "STOR_CMD_LEN_ERR_EID (6, ERROR) sent %d times for 11 wrongly sized commands", count_ev(EID_LEN_ERR, 0));
    EXPECT("cmd-bad-length", count_ev(EID_NOOP, 0) == 0 && count_ev(EID_RESET, 0) == 0 && count_tx(T_LIST_MID, 0) == 0 &&
                                 count_tx(T_STATS_MID, 0) == 0 && count_ev(EID_CLOSE_OK, 0) == 0,
           "a wrongly sized command was executed");
    if (get_hk(&b))
        EXPECT("cmd-bad-length", b.cec == a.cec + 11 && b.cc == a.cc, "11 wrongly sized commands: CommandErrorCounter %u -> %u, CommandCounter %u -> %u",
               a.cec, b.cec, a.cc, b.cc);
    a = b;
    before = ntx;
    send_cmd(T_SEND_HK_MID, 0, extra, 4);
    send_cmd(T_WAKEUP_MID, 0, extra, 4);
    EXPECT("cmd-hk-wakeup-length", count_ev(EID_LEN_ERR, 0) == 13 && count_tx(T_HK_MID, before) == 0,
           "a SEND_HK and a WAKEUP with 4 extra bytes: STOR_CMD_LEN_ERR_EID total %d (expected 13), %d housekeeping packets sent (expected 0)",
           count_ev(EID_LEN_ERR, 0), count_tx(T_HK_MID, before));
    if (get_hk(&b))
        EXPECT("cmd-hk-wakeup-length", b.cec == a.cec + 2 && b.cc == a.cc, "wrongly sized SEND_HK and WAKEUP: CommandErrorCounter %u -> %u", a.cec,
               b.cec);
    a = b;
    send_tlm(T_MID_UNKNOWN, 100, 0);
    EXPECT("bad-mid", count_ev(EID_MID_ERR, 0) == 1 && ev_typed(EID_MID_ERR, EV_ERROR, 0),
           "a packet with an unknown message ID sent STOR_MID_ERR_EID (5, ERROR) %d times, expected once", count_ev(EID_MID_ERR, 0));
    if (get_hk(&b))
        EXPECT("bad-mid", b.cec == a.cec && b.cc == a.cc && b.stored == a.stored, "an unknown message ID changed counters (errors %u -> %u, commands %u -> %u)",
               a.cec, b.cec, a.cc, b.cc);
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
    if (!get_hk(&a)) return;
    send_simple(CC_NOOP);
    cmd_param(CC_SET_PRI, T_MID_A, 7);
    cmd_param(CC_SET_DEC, T_MID_A, 2);
    cmd_thr(60);
    cmd_add("/cf/src.bin", "x.bin");
    if (!exists_file(FILE_ROOT "/x.bin"))
    {
        SETUP_FAILED("file-add", "ADD_FILE of /cf/src.bin as x.bin did not create " FILE_ROOT "/x.bin");
        return;
    }
    cmd_path(CC_LIST_DIR, "");
    send_simple(CC_LIST_ALL);
    get_stats(e, &n);
    send_simple(CC_CLOSE);
    cmd_path(CC_DEL_FILE, "x.bin");
    if (!get_hk(&b)) return;
    EXPECT("cmd-counter", (uint8)(b.cc - a.cc) == 10 && b.cec == a.cec, "ten accepted commands: CommandCounter %u -> %u, errors %u -> %u", a.cc,
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
    if (!get_hk(&b)) return;
    EXPECT("cmd-error-counter", b.cc == a.cc && (uint8)(b.cec - a.cec) == 9, "nine rejected commands: CommandCounter %u -> %u, errors %u -> %u",
           a.cc, b.cc, a.cec, b.cec);
}

static void Test_ResetCounters(void)
{
    tbl_t t;
    hk_t a, b;
    est_t e[MAX_ENT];
    int n = 0, i;
    tbl_default(&t);
    tbl_sizes(&t, 2048, 4096, 1024, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 20, 2);
    if (!boot(&t)) return;
    mf_put("/cf/small.bin", 1000, 9);
    mf_put("/cf/big.bin", 5000, 9);
    /* B: 4 received, 2 stored (one file of 664 bytes), 2 decimated; A: files of 964 and 364, then drops. */
    for (i = 0; i < 4; i++) send_tlm(T_MID_B, 300, (uint32)i);
    for (i = 0; i < 12; i++) send_tlm(T_MID_A, 300, (uint32)i);
    cmd_thr(50); /* limit 1024: the closed 964-byte A file is purged */
    send_simple(CC_NOOP);
    cmd_param(CC_SET_PRI, T_MID_A, 42);
    cmd_param(CC_SET_PRI, 0x0FFE, 2);
    cmd_add("/cf/small.bin", "keep.bin");
    cmd_add("/cf/big.bin", "toobig.bin");
    if (!get_hk(&a)) return;
    if (a.decimated != 2)
    {
        SETUP_FAILED("decimation", "PacketsDecimated %u after 4 packets with decimation 2, expected 2", a.decimated);
        return;
    }
    if (a.stored != 6 || a.dropped != 8)
    {
        SETUP_FAILED("quota-drop", "PacketsStored %u PacketsDropped %u with TelemetryQuotaBytes 2048, expected 6 and 8", a.stored, a.dropped);
        return;
    }
    if (a.purged != 1 || a.cycles != 1)
    {
        SETUP_FAILED("purge-set-threshold", "FilesPurged %u PurgeCycles %u after SET_PURGE_THRESHOLD 50, expected 1 and 1", a.purged, a.cycles);
        return;
    }
    if (a.fadds != 1 || a.fcount != 1)
    {
        SETUP_FAILED("file-add-quota", "FileAddsRejected %u FileCount %u after one accepted and one too large ADD_FILE, expected 1 and 1", a.fadds,
                     a.fcount);
        return;
    }
    if (a.cc != 4 || a.cec != 2)
    {
        SETUP_FAILED("cmd-counter", "CommandCounter %u CommandErrorCounter %u after 4 accepted and 2 rejected commands", a.cc, a.cec);
        return;
    }
    EXPECT("cmd-reset", send_simple(CC_RESET) && count_ev(EID_RESET, 0) == 1 && ev_typed(EID_RESET, EV_INFO, 0),
           "RESET_COUNTERS sent STOR_RESET_INF_EID (4, INFORMATION) %d times, expected once", count_ev(EID_RESET, 0));
    if (!get_hk(&b)) return;
    EXPECT("cmd-reset", b.cc == 0 && b.cec == 0, "after RESET_COUNTERS CommandCounter %u CommandErrorCounter %u, expected 0 and 0", b.cc, b.cec);
    EXPECT("cmd-reset", b.stored == 0 && b.decimated == 0 && b.dropped == 0 && b.purged == 0 && b.cycles == 0 && b.fadds == 0,
           "after RESET_COUNTERS stored %u decimated %u dropped %u purged %u cycles %u rejected adds %u", b.stored, b.decimated, b.dropped, b.purged,
           b.cycles, b.fadds);
    EXPECT("cmd-reset-keeps", b.used == a.used && b.fused == a.fused && b.fcount == a.fcount && b.open == a.open && b.thr == a.thr &&
                                  b.active == a.active && b.used == tlm_bytes_on_disk(),
           "RESET_COUNTERS changed byte accounting, FileCount, open files, ActiveEntries or the threshold");
    if (get_stats(e, &n))
    {
        const est_t *x = est_find(e, n, T_MID_A), *y = est_find(e, n, T_MID_B);
        EXPECT("cmd-reset", x && y && x->rx == 0 && x->st == 0 && x->dr == 0 && y->rx == 0 && y->st == 0 && y->de == 0 && y->dr == 0,
               "per-entry counters after RESET_COUNTERS are not all zero");
        EXPECT("cmd-reset-keeps", x && x->pri == 42 && y && y->dec == 2, "RESET_COUNTERS changed run-time priority or decimation");
    }
}

static void Test_FileAddDelete(void)
{
    hk_t h, h2;
    tbl_t t;
    int ev0;
    tbl_default(&t);
    tbl_sizes(&t, 262144, 8192, 16384, 80);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    if (!boot(&t)) return;
    mf_put("/cf/src.bin", 3000, 5);
    mf_put("/cf/big.bin", 6000, 6);

    EXPECT("file-add", cmd_add("/cf/src.bin", "a/b/copy.bin") && count_ev(EID_ADD_OK, 0) == 1 && ev_typed(EID_ADD_OK, EV_INFO, 0),
           "ADD_FILE sent STOR_FILE_ADD_INF_EID (22, INFORMATION) %d times, expected once", count_ev(EID_ADD_OK, 0));
    EXPECT("file-add", exists_file(FILE_ROOT "/a/b/copy.bin") && size_of_file(FILE_ROOT "/a/b/copy.bin") == 3000 &&
                           memcmp(mf[mf_find(FILE_ROOT "/a/b/copy.bin")].data, mf[mf_find("/cf/src.bin")].data, 3000) == 0,
           "ADD_FILE to a/b/copy.bin did not create the directories and an identical copy");
    if (!get_hk(&h)) return;
    EXPECT("file-add", h.fcount == 1 && h.fused == 3000, "after ADD_FILE FileCount %u FileBytesUsed %u, expected 1 and 3000", h.fcount, h.fused);

    ev0 = nev;
    cmd_add("/cf/src.bin", "a/b/copy.bin");
    EXPECT("file-add-exists", count_ev(EID_EXISTS, ev0) == 1 && ev_typed(EID_EXISTS, EV_ERROR, ev0),
           "an existing destination sent STOR_FILE_EXISTS_ERR_EID (20, ERROR) %d times", count_ev(EID_EXISTS, ev0));
    cmd_add("/cf/missing.bin", "m.bin");
    EXPECT("file-add-source", count_ev(EID_SRC_ERR, ev0) == 1 && ev_typed(EID_SRC_ERR, EV_ERROR, ev0) && !exists_file(FILE_ROOT "/m.bin"),
           "a missing source: STOR_FILE_SRC_ERR_EID (19, ERROR) %d times, destination created %d", count_ev(EID_SRC_ERR, ev0),
           exists_file(FILE_ROOT "/m.bin"));
    cmd_add("/cf/stor", "d/dir.bin");
    EXPECT("file-add-source", count_ev(EID_SRC_ERR, ev0) == 2 && mf_find(FILE_ROOT "/d") < 0,
           "a directory as the source: STOR_FILE_SRC_ERR_EID total %d (expected 2), directory d created %d", count_ev(EID_SRC_ERR, ev0),
           mf_find(FILE_ROOT "/d") >= 0);
    cmd_add("/cf/big.bin", "newdir/big.bin");
    EXPECT("file-add-quota", count_ev(EID_FQUOTA, ev0) == 1 && ev_typed(EID_FQUOTA, EV_ERROR, ev0),
           "a file over FileQuotaBytes sent STOR_FILE_QUOTA_ERR_EID (21, ERROR) %d times", count_ev(EID_FQUOTA, ev0));
    EXPECT("file-add-quota", !exists_file(FILE_ROOT "/newdir/big.bin") && mf_find(FILE_ROOT "/newdir") < 0,
           "a quota rejection left a file or created the directory newdir");
    if (get_hk(&h2))
        EXPECT("file-add-errors", h2.fadds == 1 && h2.fcount == 1 && h2.fused == 3000 && h2.cec == h.cec + 4 && h2.cc == h.cc,
               "after 4 rejected ADD_FILEs: FileAddsRejected %u (expected 1), FileCount %u, FileBytesUsed %u, errors %u -> %u", h2.fadds, h2.fcount,
               h2.fused, h.cec, h2.cec);
    /* exact fit: 3000 + 5192 == 8192 */
    mf_put("/cf/fit.bin", 5192, 1);
    EXPECT("file-add-quota", cmd_add("/cf/fit.bin", "fit.bin") && count_ev(EID_ADD_OK, 0) == 2 && exists_file(FILE_ROOT "/fit.bin"),
           "a file that exactly fills FileQuotaBytes was rejected");
    mf_put("/cf/one.bin", 1, 1);
    EXPECT("file-add-quota", cmd_add("/cf/one.bin", "one.bin") && count_ev(EID_FQUOTA, 0) == 2 && !exists_file(FILE_ROOT "/one.bin"),
           "one byte over FileQuotaBytes was accepted");

    ev0 = nev;
    EXPECT("file-delete", cmd_path(CC_DEL_FILE, "fit.bin") && count_ev(EID_DEL_OK, ev0) == 1 && ev_typed(EID_DEL_OK, EV_INFO, ev0) &&
                              !exists_file(FILE_ROOT "/fit.bin"),
           "DELETE_FILE of fit.bin: STOR_FILE_DEL_INF_EID (24, INFORMATION) %d times, file removed %d", count_ev(EID_DEL_OK, ev0),
           !exists_file(FILE_ROOT "/fit.bin"));
    mf_mkdir_p(FILE_ROOT "/emptydir");
    cmd_path(CC_DEL_FILE, "fit.bin");
    cmd_path(CC_DEL_FILE, "a");
    cmd_path(CC_DEL_FILE, "emptydir");
    EXPECT("file-delete-errors", count_ev(EID_DEL_ERR, ev0) == 3 && ev_typed(EID_DEL_ERR, EV_ERROR, ev0),
           "DELETE_FILE of a missing file, a directory and an empty directory sent STOR_FILE_DEL_ERR_EID (23, ERROR) %d times, expected 3",
           count_ev(EID_DEL_ERR, ev0));
    EXPECT("file-delete-errors", is_dir(FILE_ROOT "/a") && is_dir(FILE_ROOT "/emptydir"), "DELETE_FILE removed a directory");
    if (get_hk(&h))
        EXPECT("file-delete", h.fcount == 1 && h.fused == 3000, "after the delete FileCount %u FileBytesUsed %u, expected 1 and 3000", h.fcount,
               h.fused);
}

static void Test_PathRejection(void)
{
    hk_t a, b;
    int before_files = 0, now_files = 0, i;
    uint8 p[2 * PATH_LEN];
    if (!boot(&tbl_active)) return;
    mf_put("/cf/src.bin", 100, 5);
    mf_mkdir_p(FILE_ROOT "/sub");
    if (!get_hk(&a)) return;
    for (i = 0; i < MF_MAX; i++) before_files += mf[i].used;
    cmd_add("/cf/src.bin", "../x");
    cmd_add("/cf/src.bin", "/abs");
    cmd_add("/cf/src.bin", "a/../b");
    cmd_add("/cf/src.bin", "..");
    cmd_add("/cf/src.bin", "a/b/..");
    cmd_add("/cf/src.bin", "");
    EXPECT("path-reject", count_ev(EID_PATH_ERR, 0) == 6 && ev_typed(EID_PATH_ERR, EV_ERROR, 0),
           "six bad ADD_FILE destinations (.., leading /, empty) sent STOR_PATH_ERR_EID (18, ERROR) %d times", count_ev(EID_PATH_ERR, 0));
    cmd_path(CC_DEL_FILE, "../x");
    cmd_path(CC_DEL_FILE, "/cf/src.bin");
    cmd_path(CC_DEL_FILE, "");
    cmd_path(CC_LIST_DIR, "..");
    cmd_path(CC_LIST_DIR, "/cf");
    cmd_path(CC_LIST_DIR, "sub/../..");
    EXPECT("path-reject", count_ev(EID_PATH_ERR, 0) == 12, "bad DELETE_FILE and LIST_DIR paths: STOR_PATH_ERR_EID total %d, expected 12",
           count_ev(EID_PATH_ERR, 0));
    memset(p, 'a', sizeof(p));
    send_cmd(T_CMD_MID, CC_ADD_FILE, p, sizeof(p));
    memset(p, 'b', PATH_LEN);
    pstr(p + PATH_LEN, "u.bin");
    send_cmd(T_CMD_MID, CC_ADD_FILE, p, sizeof(p));
    memset(p, 'c', PATH_LEN);
    send_cmd(T_CMD_MID, CC_DEL_FILE, p, PATH_LEN);
    send_cmd(T_CMD_MID, CC_LIST_DIR, p, PATH_LEN);
    EXPECT("path-unterminated", count_ev(EID_PATH_ERR, 0) == 16,
           "path fields without a NUL (ADD_FILE source and destination, DELETE_FILE, LIST_DIR): STOR_PATH_ERR_EID total %d, expected 16",
           count_ev(EID_PATH_ERR, 0));
    EXPECT("path-reject", exists_file("/cf/src.bin"), "a path starting with / deleted the real file");
    for (i = 0; i < MF_MAX; i++) now_files += mf[i].used;
    EXPECT("path-reject", now_files == before_files, "rejected paths created or removed files or directories (%d -> %d)", before_files, now_files);
    EXPECT("path-reject", count_tx(T_LIST_MID, 0) == 0 && count_ev(EID_SRC_ERR, 0) == 0 && count_ev(EID_DEL_ERR, 0) == 0 &&
                              count_ev(EID_LIST_ERR, 0) == 0,
           "a rejected path was still processed (listing packet or another error event)");
    if (get_hk(&b))
        EXPECT("path-reject", b.cec == a.cec + 16 && b.cc == a.cc, "16 path rejections: CommandErrorCounter %u -> %u, CommandCounter %u -> %u",
               a.cec, b.cec, a.cc, b.cc);
}

static bool list_has_dots(const lpkt_t *p, int n)
{
    int i, k;
    for (i = 0; i < n; i++)
        for (k = 0; k < 4 && k < p[i].count; k++)
            if (strcmp(p[i].name[k], ".") == 0 || strcmp(p[i].name[k], "..") == 0 || strcmp(p[i].name[k], "./") == 0 ||
                strcmp(p[i].name[k], "../") == 0 || strstr(p[i].name[k], "/.") != NULL)
                return true;
    return false;
}

static void Test_Listings(void)
{
    lpkt_t p[8];
    int first, n, i, ev0;
    if (!boot(&tbl_active)) return;

    first = ntx;
    ev0 = nev;
    send_simple(CC_LIST_ALL);
    n = get_list(first, p, 8);
    EXPECT("list-dots", !list_has_dots(p, n), "a listing contains . or .. entries");
    EXPECT("list-empty", n == 1 && p[0].total == 0 && p[0].count == 0 && p[0].index == 0,
           "LIST_ALL of an empty file area gave %d packets, expected one zero-filled packet with TotalEntries 0", n);
    EXPECT("list-event", count_ev(EID_LIST_OK, ev0) == 1 && ev_typed(EID_LIST_OK, EV_INFO, ev0),
           "a successful listing sent STOR_LIST_INF_EID (26, INFORMATION) %d times, expected once", count_ev(EID_LIST_OK, ev0));

    /* root = [z.txt, d1/, y.txt, empty/], d1 = [a.bin, sub/, b.bin], d1/sub = [deep.bin] in this creation order */
    mf_put(FILE_ROOT "/z.txt", 11, 1);
    mf_mkdir_p(FILE_ROOT "/d1");
    mf_put(FILE_ROOT "/d1/a.bin", 33, 3);
    mf_mkdir_p(FILE_ROOT "/d1/sub");
    mf_put(FILE_ROOT "/d1/sub/deep.bin", 44, 4);
    mf_put(FILE_ROOT "/d1/b.bin", 55, 5);
    mf_put(FILE_ROOT "/y.txt", 22, 2);
    mf_mkdir_p(FILE_ROOT "/empty");

    first = ntx;
    ev0 = nev;
    send_simple(CC_LIST_ALL);
    n = get_list(first, p, 8);
    EXPECT("list-dots", !list_has_dots(p, n), "LIST_ALL lists . or .. entries");
    EXPECT("list-split", n == 2 && p[0].total == 5 && p[1].total == 5 && p[0].index == 0 && p[1].index == 1 && p[0].count == 4 &&
                             p[1].count == 1,
           "LIST_ALL of 5 files gave %d packets (expected 2: TotalEntries 5, PacketIndex 0 and 1, EntriesInPacket 4 and 1)", n);
    if (n == 2)
    {
        EXPECT("list-all", strcmp(p[0].name[0], "z.txt") == 0 && p[0].size[0] == 11 && strcmp(p[0].name[1], "d1/a.bin") == 0 &&
                               p[0].size[1] == 33 && strcmp(p[0].name[2], "d1/sub/deep.bin") == 0 && p[0].size[2] == 44 &&
                               strcmp(p[0].name[3], "d1/b.bin") == 0 && p[0].size[3] == 55 && strcmp(p[1].name[0], "y.txt") == 0 &&
                               p[1].size[0] == 22,
               "LIST_ALL order/names: %s %s %s %s %s (expected z.txt d1/a.bin d1/sub/deep.bin d1/b.bin y.txt)", p[0].name[0], p[0].name[1],
               p[0].name[2], p[0].name[3], p[1].name[0]);
    }
    EXPECT("list-event", count_ev(EID_LIST_OK, ev0) == 1, "LIST_ALL with two packets sent STOR_LIST_INF_EID %d times, expected once",
           count_ev(EID_LIST_OK, ev0));

    first = ntx;
    cmd_path(CC_LIST_DIR, "");
    n = get_list(first, p, 8);
    EXPECT("list-dots", !list_has_dots(p, n), "LIST_DIR of the root lists . or .. entries");
    EXPECT("list-dir", n == 1 && p[0].total == 4 && p[0].count == 4 && strcmp(p[0].name[0], "z.txt") == 0 && strcmp(p[0].name[1], "d1/") == 0 &&
                           p[0].size[1] == 0 && strcmp(p[0].name[2], "y.txt") == 0 && strcmp(p[0].name[3], "empty/") == 0 && p[0].size[0] == 11,
           "LIST_DIR of the root: %d packets, entries %s %s %s %s (expected z.txt d1/ y.txt empty/, directories with size 0)", n,
           n ? p[0].name[0] : "", n ? p[0].name[1] : "", n ? p[0].name[2] : "", n ? p[0].name[3] : "");
    first = ntx;
    cmd_path(CC_LIST_DIR, "d1");
    n = get_list(first, p, 8);
    EXPECT("list-dir", n == 1 && p[0].total == 3 && p[0].count == 3 && strcmp(p[0].name[0], "a.bin") == 0 && strcmp(p[0].name[1], "sub/") == 0 &&
                           strcmp(p[0].name[2], "b.bin") == 0 && p[0].size[0] == 33,
           "LIST_DIR d1: %d packets, entries %s %s %s (expected the bare names a.bin sub/ b.bin)", n, n ? p[0].name[0] : "",
           n ? p[0].name[1] : "", n ? p[0].name[2] : "");
    first = ntx;
    cmd_path(CC_LIST_DIR, "empty");
    n = get_list(first, p, 8);
    EXPECT("list-empty", n == 1 && p[0].total == 0 && p[0].count == 0, "LIST_DIR of an empty directory gave %d packets", n);

    ev0 = nev;
    first = ntx;
    cmd_path(CC_LIST_DIR, "nosuchdir");
    cmd_path(CC_LIST_DIR, "z.txt");
    n = get_list(first, p, 8);
    EXPECT("list-error", n == 0 && count_ev(EID_LIST_ERR, ev0) == 2 && ev_typed(EID_LIST_ERR, EV_ERROR, ev0) && count_ev(EID_LIST_OK, ev0) == 0,
           "LIST_DIR of a missing path and of a regular file: STOR_LIST_ERR_EID (25, ERROR) %d times (expected 2), %d packets (expected 0)",
           count_ev(EID_LIST_ERR, ev0), n);

    for (i = 0; i < 9; i++)
    {
        char path[96];
        snprintf(path, sizeof(path), FILE_ROOT "/empty/e%d.bin", i);
        mf_put(path, 100 + (size_t)i, 1);
    }
    first = ntx;
    cmd_path(CC_LIST_DIR, "empty");
    n = get_list(first, p, 8);
    EXPECT("list-split", n == 3 && p[0].total == 9 && p[2].total == 9 && p[0].count == 4 && p[1].count == 4 && p[2].count == 1 && p[2].index == 2,
           "nine entries gave %d packets, expected three of 4, 4 and 1 entries", n);
}

static void Test_TableUpdate(void)
{
    tbl_t t;
    hk_t h;
    est_t e[MAX_ENT];
    int n = 0, i, idx[8];
    tbl_default(&t);
    tbl_sizes(&t, 262144, 1048576, 16384, 80);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 20, 1);
    if (!boot(&t)) return;
    for (i = 0; i < 2; i++) send_tlm(T_MID_A, 100, (uint32)i);
    for (i = 0; i < 3; i++) send_tlm(T_MID_B, 100, (uint32)i);
    cmd_param(CC_SET_PRI, T_MID_A, 99);
    cmd_param(CC_SET_DEC, T_MID_A, 9);
    cmd_thr(33);
    if (n_open_total() != 2)
    {
        SETUP_FAILED("hk-open-files", "%u OSAL files open after packets of two entries, expected 2", n_open_total());
        return;
    }
    tbl_default(&t);
    tbl_sizes(&t, 262144, 1048576, 16384, 70);
    tbl_entry(&t, 0, T_MID_B, 20, 1);
    tbl_entry(&t, 1, T_MID_D, 15, 4);
    if (tbl_submit(&t) != CFE_SUCCESS)
    {
        SETUP_FAILED("tbl-accept", "the application's validation function rejected a valid table update");
        return;
    }
    send_wakeup();
    send_cmd(T_SEND_HK_MID, 0, NULL, 0);
    send_wakeup();
    if (!get_hk(&h)) return;
    if (tbl_has_pending)
    {
        FAIL("table-update-check", "a table update was not applied within a WAKEUP, SEND_HK, WAKEUP, SEND_HK sequence");
        return;
    }
    EXPECT("table-update-subscriptions", !subscribed(T_MID_A) && subscribed(T_MID_D) && subscribed(T_MID_B),
           "after the update: subscribed to the removed MID %d, the new MID %d, the kept MID %d", subscribed(T_MID_A), subscribed(T_MID_D),
           subscribed(T_MID_B));
    EXPECT("table-update-runtime", h.thr == 70, "threshold %u after a table update with threshold 70 (run-time value was 33)", h.thr);
    EXPECT("table-update-entries", h.active == 2, "ActiveEntries %u after an update to two used entries", h.active);
    EXPECT("table-update-entries", h.open == 1 && n_open_total() == 1,
           "after the update OpenPacketFiles %u, OSAL files open %u, expected 1: the removed entry's file is closed, the kept entry's stays open",
           h.open, n_open_total());
    if (get_stats(e, &n))
    {
        const est_t *d = est_find(e, n, T_MID_D), *b = est_find(e, n, T_MID_B);
        EXPECT("table-update-runtime", d && d->pri == 15 && d->dec == 4 && d->rx == 0, "the new entry reports priority %u decimation %u received %u",
               d ? d->pri : 0, d ? d->dec : 0, d ? d->rx : 0);
        EXPECT("table-update-entries", est_find(e, n, T_MID_A) == NULL && n == 2, "statistics after the update list %d entries including the removed one",
               n);
        EXPECT("table-update-entries", b && b->rx == 3 && b->st == 3, "the kept entry's counters were not kept (received %u stored %u, expected 3 and 3)",
               b ? b->rx : 0, b ? b->st : 0);
    }
    send_tlm(T_MID_B, 100, 3);
    EXPECT("table-update-entries", mid_files(T_MID_B, idx, 8) == 1 && size_of_file(TLM_ROOT "/tlm_08E1_00000000.bin") == FS_HDR + 400,
           "the kept entry's open file was not kept (%d files)", mid_files(T_MID_B, idx, 8));
    cmd_param(CC_SET_PRI, T_MID_B, 123);
    tbl_entry(&t, 0, T_MID_B, 21, 2);
    if (tbl_submit(&t) != CFE_SUCCESS)
    {
        SETUP_FAILED("tbl-accept", "the application's validation function rejected a valid second table update");
        return;
    }
    send_wakeup();
    send_cmd(T_SEND_HK_MID, 0, NULL, 0);
    send_wakeup();
    if (get_stats(e, &n))
    {
        const est_t *b = est_find(e, n, T_MID_B);
        EXPECT("table-update-runtime", b && b->pri == 21 && b->dec == 2, "a table update did not override run-time priority (%u) and decimation (%u)",
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
    send_tlm(T_MID_A, 300, 0);
    for (i = 0; i < 4; i++) send_wakeup();
    send_tlm(T_MID_B, 300, 0); /* four seconds after A */
    for (i = 0; i < 5; i++) send_wakeup();
    if (get_hk(&h)) EXPECT("idle-close", h.open == 2, "%u files open when they were idle for 9 and 5 seconds, expected 2", h.open);
    send_wakeup(); /* A has been idle for 10 s */
    send_wakeup();
    if (get_hk(&h)) EXPECT("idle-close", h.open == 1, "%u files open when idle for 11 and 7 seconds, expected 1", h.open);
    for (i = 0; i < 4; i++) send_wakeup();
    if (get_hk(&h)) EXPECT("idle-close", h.open == 0 && n_open_total() == 0, "%u files open (%u OSAL) when both were idle for over 10 s",
                           h.open, n_open_total());
    send_tlm(T_MID_A, 300, 1);
    EXPECT("pkt-no-reopen", mid_files(T_MID_A, idx, 8) == 2, "a packet after the idle close did not start a new file");
    /* Idle time is cFE time: one WAKEUP after the clock moved on by 10 s closes the file. */
    now_sec += 10;
    send_wakeup();
    if (get_hk(&h)) EXPECT("idle-close", h.open == 0, "%u files open at the first WAKEUP 11 s (by cFE time) after the last write", h.open);
}

static void Test_WriteFailures(void)
{
    tbl_t t;
    hk_t h;
    int ev0;
    uint32 used;
    tbl_default(&t);
    tbl_sizes(&t, 65536, 1048576, 1024, 100);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    if (!boot(&t)) return;

    fi_create_fail = 1;
    send_tlm(T_MID_A, 300, 0);
    EXPECT("write-fail-create", count_ev(EID_FILE_WRITE, 0) == 1 && ev_typed(EID_FILE_WRITE, EV_ERROR, 0),
           "a packet file that cannot be created sent STOR_FILE_WRITE_ERR_EID (15, ERROR) %d times", count_ev(EID_FILE_WRITE, 0));
    send_tlm(T_MID_A, 300, 1);
    EXPECT("write-fail-create", exists_file(TLM_ROOT "/tlm_08E0_00000000.bin") && tlm_file_count() == 1,
           "after a failed creation the next file is not sequence 0 (the sequence number must not be consumed)");
    if (get_hk(&h))
        EXPECT("write-fail-create", h.stored == 1 && h.dropped == 0 && h.used == tlm_bytes_on_disk(),
               "after a failed creation: stored %u dropped %u (expected 1 and 0), TelemetryBytesUsed %u, on disk %u", h.stored, h.dropped, h.used,
               tlm_bytes_on_disk());

    ev0 = nev;
    fi_write_mode = FI_WRITE_ERROR;
    send_tlm(T_MID_A, 300, 2);
    fi_write_mode = FI_WRITE_OK;
    EXPECT("write-fail-packet", count_ev(EID_FILE_WRITE, ev0) == 1, "an OS_write error sent STOR_FILE_WRITE_ERR_EID %d times",
           count_ev(EID_FILE_WRITE, ev0));
    send_tlm(T_MID_A, 300, 3);
    if (get_hk(&h))
        EXPECT("write-fail-packet", h.stored == 2 && h.dropped == 0 && h.used == tlm_bytes_on_disk() &&
                                        exists_file(TLM_ROOT "/tlm_08E0_00000001.bin") && size_of_file(TLM_ROOT "/tlm_08E0_00000000.bin") == 364,
               "after an OS_write error: stored %u dropped %u (expected 2 and 0), used %u on disk %u; the file must be closed",
               h.stored, h.dropped, h.used, tlm_bytes_on_disk());

    ev0 = nev;
    fi_write_mode = FI_WRITE_SHORT;
    send_tlm(T_MID_A, 300, 4);
    fi_write_mode = FI_WRITE_OK;
    EXPECT("write-fail-packet", count_ev(EID_FILE_WRITE, ev0) == 1, "a short OS_write (150 of 300 bytes) sent STOR_FILE_WRITE_ERR_EID %d times",
           count_ev(EID_FILE_WRITE, ev0));
    send_tlm(T_MID_A, 300, 5);
    if (get_hk(&h))
        EXPECT("write-fail-packet", h.stored == 3 && h.used == tlm_bytes_on_disk() && size_of_file(TLM_ROOT "/tlm_08E0_00000001.bin") == 514 &&
                                        exists_file(TLM_ROOT "/tlm_08E0_00000002.bin"),
               "after a short write: stored %u (expected 3), used %u on disk %u; the bytes written count and the file must be closed", h.stored,
               h.used, tlm_bytes_on_disk());

    send_simple(CC_CLOSE);
    ev0 = nev;
    fi_write_mode = FI_WRITE_ERROR;
    send_tlm(T_MID_A, 300, 6);
    fi_write_mode = FI_WRITE_OK;
    EXPECT("write-fail-create", count_ev(EID_FILE_WRITE, ev0) == 1 && !exists_file(TLM_ROOT "/tlm_08E0_00000003.bin"),
           "a header write failure: STOR_FILE_WRITE_ERR_EID %d times, partial file left %d", count_ev(EID_FILE_WRITE, ev0),
           exists_file(TLM_ROOT "/tlm_08E0_00000003.bin"));
    send_tlm(T_MID_A, 300, 7);
    used = tlm_bytes_on_disk();
    if (get_hk(&h))
        EXPECT("write-fail-create", exists_file(TLM_ROOT "/tlm_08E0_00000003.bin") && h.stored == 4 && h.used == used,
               "after a header write failure: next file sequence 3 present %d, stored %u (expected 4), used %u on disk %u",
               exists_file(TLM_ROOT "/tlm_08E0_00000003.bin"), h.stored, h.used, used);
    EXPECT("responsive", get_hk(&h), "STOR does not answer SEND_HK after write failures");
}

/* ------------------------------------------------------- per-test-case process isolation */

#define CHILD_LIMIT_S 60
#define HANG_LIMIT    2

typedef struct { uint32 total; uint32 failed; } child_report_t;

static int n_hung_cases;

static void run_isolated(void (*body)(void))
{
    int fds[2];
    pid_t pid;
    int status = 0;
    child_report_t rep = {0, 0};
    struct timespec start, now, nap = {0, 5000000};
    bool timed_out = false;
    if (n_hung_cases >= HANG_LIMIT)
    {
        FAIL("skipped", "not run: STOR stopped responding in %d earlier test cases", n_hung_cases);
        return;
    }
    if (pipe(fds) != 0)
    {
        FAIL("infra", "cannot create a pipe (errno %d)", errno);
        return;
    }
    pid = fork();
    if (pid < 0)
    {
        FAIL("infra", "cannot fork (errno %d)", errno);
        close(fds[0]);
        close(fds[1]);
        return;
    }
    if (pid == 0)
    {
        ssize_t wr;
        close(fds[0]);
        n_checks = n_failed = 0;
        body();
        app_end();
        rep.failed = n_failed;
        rep.total = n_checks;
        wr = write(fds[1], &rep, sizeof(rep));
        (void)wr;
        __gcov_dump();
        _exit(rep.failed ? 1 : 0);
    }
    close(fds[1]);
    clock_gettime(CLOCK_MONOTONIC, &start);
    while (waitpid(pid, &status, WNOHANG) == 0)
    {
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (now.tv_sec - start.tv_sec >= CHILD_LIMIT_S)
        {
            kill(pid, SIGKILL);
            waitpid(pid, &status, 0);
            timed_out = true;
            break;
        }
        nanosleep(&nap, NULL);
    }
    if (read(fds[0], &rep, sizeof(rep)) != (ssize_t)sizeof(rep)) memset(&rep, 0, sizeof(rep));
    close(fds[0]);
    if (timed_out)
    {
        n_hung_cases++;
        FAIL("hang", "STOR did not finish this test case within %d s and was stopped", CHILD_LIMIT_S);
    }
    else if (WIFSIGNALED(status))
    {
        FAIL("crash", "STOR crashed with signal %d (%s)", WTERMSIG(status), strsignal(WTERMSIG(status)));
    }
    else if (WIFEXITED(status) && WEXITSTATUS(status) == 0)
    {
        UtAssert_True(true, "{meta} %u assertions passed", (unsigned)rep.total);
    }
    else if (WIFEXITED(status) && WEXITSTATUS(status) == 1)
    {
        FAIL("meta", "%u of %u assertions failed", (unsigned)rep.failed, (unsigned)rep.total);
    }
    else if (WIFEXITED(status) && WEXITSTATUS(status) == 3)
    {
        n_hung_cases++;
        FAIL("meta", "the test case ended because STOR did not stop");
    }
    else
    {
        FAIL("crash", "the process ended with exit status %d while STOR was running (STOR must not call exit)", WEXITSTATUS(status));
    }
}

/* ------------------------------------------------------- reference-specific error paths */

static void H_RefWriteFails(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    ret_i32(key, OS_ERROR);
}

static void H_RefReadFails(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    ret_i32(key, OS_ERROR);
}

static void H_RefDirOpenFails(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    ret_i32(key, OS_ERROR);
}

static void Test_RefInitFailures(void)
{
    tbl_t t;
    UT_SetDefaultReturnValue(UT_KEY(CFE_EVS_Register), -1);
    EXPECT("startup-run", !app_start(), "STOR kept running after CFE_EVS_Register failed");
    app_end();
    Setup();
    UT_SetDefaultReturnValue(UT_KEY(CFE_SB_CreatePipe), -1);
    EXPECT("startup-run", !app_start(), "STOR kept running after CFE_SB_CreatePipe failed");
    app_end();
    Setup();
    tbl_default(&t);
    tbl_sizes(&t, 0, 1, 1, 1);
    tbl_active = t;
    EXPECT("startup-run", !app_start(), "STOR kept running with a table that fails validation");
}

static void Test_RefFileErrors(void)
{
    tbl_t t;
    uint8 raw[2 * PATH_LEN];
    hk_t h;
    tbl_default(&t);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    mf_mkdir_p(TLM_ROOT);
    mf_mkdir_p(TLM_ROOT "/tlm_0001_00000001.bin");
    mf_put(TLM_ROOT "/tlm_08e0_00000001.bin", 10, 1);
    mf_put(TLM_ROOT "/tlm_08E0_0000000x.bin", 10, 1);
    if (!boot(&t)) return;
    mf_put("/cf/src.bin", 100, 5);
    memset(raw, 'x', sizeof(raw));
    send_cmd(T_CMD_MID, CC_LIST_DIR, raw, PATH_LEN);
    send_cmd(T_CMD_MID, CC_ADD_FILE, raw, sizeof(raw));
    fi_mkdir_fail = true;
    cmd_add("/cf/src.bin", "d1/d2/f.bin");
    fi_mkdir_fail = false;
    EXPECT("file-add", count_ev(EID_FILE_WRITE, 0) == 1 && mf_find(FILE_ROOT "/d1") < 0, "a directory creation failure in ADD_FILE");
    cmd_add("/cf/src.bin", "a_destination_name_longer_than_osal_allows.bin");
    UT_SetHandlerFunction(UT_KEY(OS_write), H_RefWriteFails, NULL);
    cmd_add("/cf/src.bin", "c.bin");
    UT_SetHandlerFunction(UT_KEY(OS_write), H_OS_write, NULL);
    UT_SetHandlerFunction(UT_KEY(OS_read), H_RefReadFails, NULL);
    cmd_add("/cf/src.bin", "d.bin");
    UT_SetHandlerFunction(UT_KEY(OS_read), H_OS_read, NULL);
    EXPECT("file-add", count_ev(EID_FILE_WRITE, 0) >= 2, "failed copies are reported with event 15");
    mf_put(FILE_ROOT "/an_entry_name_longer_than_osal_allows.bin", 5, 1);
    send_simple(CC_LIST_ALL);
    EXPECT("responsive", get_hk(&h), "STOR answers SEND_HK");
}

static void Test_RefTableEdges(void)
{
    tbl_t t;
    hk_t h;
    tbl_default(&t);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    tbl_entry(&t, 1, T_MID_B, 10, 1);
    if (!boot(&t)) return;
    send_tlm(T_MID_A, 300, 0);
    tbl_default(&t);
    tbl_entry(&t, 0, T_MID_B, 10, 1);
    tbl_entry(&t, 1, T_MID_D, 10, 1);
    sub_rc = CFE_SB_BAD_ARGUMENT;
    tbl_submit(&t);
    send_wakeup();
    send_cmd(T_SEND_HK_MID, 0, NULL, 0);
    send_wakeup();
    EXPECT("table-update-subscriptions", count_ev(EID_SUB_ERR, 0) == 1, "a failed subscription on a table update sends event 11");
    sub_rc = CFE_SUCCESS;
    EXPECT("responsive", get_hk(&h), "STOR answers SEND_HK");
}

static void Test_RefTimeStepBack(void)
{
    tbl_t t;
    hk_t h;
    tbl_default(&t);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    if (!boot(&t)) return;
    send_tlm(T_MID_A, 300, 0);
    now_sec -= 5; /* cFE time steps back (for example when the flywheel stops) */
    send_cmd(T_WAKEUP_MID, 0, NULL, 0);
    if (get_hk(&h)) EXPECT("idle-close", h.open == 1, "a time step back closed an idle file early");
    now_sec += 15;
    send_cmd(T_WAKEUP_MID, 0, NULL, 0);
    if (get_hk(&h)) EXPECT("idle-close", h.open == 0, "the file was not closed after 10 s");
}

static void Test_RefScanMany(void)
{
    tbl_t t;
    hk_t h;
    char path[96];
    int i;
    tbl_default(&t);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    mf_mkdir_p(TLM_ROOT);
    for (i = 0; i < 70; i++)
    {
        snprintf(path, sizeof(path), TLM_ROOT "/tlm_%04X_%08d.bin", 0x0900 + i, i % 3);
        mf_put_pkt(path, FS_HDR, 1);
    }
    mf_put_pkt(TLM_ROOT "/tlm_0900_00000005.bin", FS_HDR, 1);
    mf_put_pkt(TLM_ROOT "/tlm_0900_00000002.bin", FS_HDR, 1);
    if (!boot(&t)) return;
    if (get_hk(&h)) EXPECT("scan-tlm", h.used == 72 * FS_HDR, "TelemetryBytesUsed %u for 72 header-only files", h.used);
}

static void Test_RefNoTelemetryRoot(void)
{
    tbl_t t;
    hk_t h;
    tbl_default(&t);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    if (!boot(&t)) return;
    {
        int i = mf_find(TLM_ROOT);
        snprintf(mf[i].path, sizeof(mf[i].path), "/cf/stor/gone");
    }
    send_tlm(T_MID_A, 300, 0);
    EXPECT("write-fail-create", count_ev(EID_FILE_WRITE, 0) == 1, "a missing TelemetryRoot gives event 15");
    EXPECT("responsive", get_hk(&h), "STOR answers SEND_HK");
}

static void Test_RefScanFails(void)
{
    tbl_t t;
    hk_t h;
    tbl_default(&t);
    tbl_entry(&t, 0, T_MID_A, 10, 1);
    mf_mkdir_p(TLM_ROOT);
    mf_mkdir_p(FILE_ROOT);
    UT_SetHandlerFunction(UT_KEY(OS_DirectoryOpen), H_RefDirOpenFails, NULL);
    if (!boot(&t)) return;
    if (get_hk(&h)) EXPECT("scan-tlm", h.used == 0 && h.fused == 0, "nothing is counted when the roots cannot be read");
}

#define ISOLATED(fn) static void fn##_Isolated(void) { run_isolated(fn); }
ISOLATED(Test_StartupAndHousekeeping)
ISOLATED(Test_StartupErrTblReg)
ISOLATED(Test_StartupErrTblLoad)
ISOLATED(Test_StartupErrSubscribe)
ISOLATED(Test_StartupErrMkdir)
ISOLATED(Test_StartupErrPipe)
ISOLATED(Test_TableValidation)
ISOLATED(Test_DecimationAndStats)
ISOLATED(Test_RolloverAndNaming)
ISOLATED(Test_SequenceAndScan)
ISOLATED(Test_PurgeOrder)
ISOLATED(Test_PurgeExisting)
ISOLATED(Test_PurgePriorityChange)
ISOLATED(Test_QuotaDrop)
ISOLATED(Test_SetParamCommands)
ISOLATED(Test_CommandErrors)
ISOLATED(Test_CommandCounters)
ISOLATED(Test_ResetCounters)
ISOLATED(Test_FileAddDelete)
ISOLATED(Test_PathRejection)
ISOLATED(Test_Listings)
ISOLATED(Test_TableUpdate)
ISOLATED(Test_IdleClose)
ISOLATED(Test_WriteFailures)
ISOLATED(Test_RefInitFailures)
ISOLATED(Test_RefFileErrors)
ISOLATED(Test_RefTableEdges)
ISOLATED(Test_RefTimeStepBack)
ISOLATED(Test_RefScanMany)
ISOLATED(Test_RefNoTelemetryRoot)
ISOLATED(Test_RefScanFails)

void UtTest_Setup(void)
{
    EXPECT("infra", sizeof(CFE_MSG_TelemetryHeader_t) == TLM_HDR, "telemetry header is %u bytes", (unsigned)sizeof(CFE_MSG_TelemetryHeader_t));
    EXPECT("infra", sizeof(CFE_MSG_CommandHeader_t) == CMD_HDR, "command header is %u bytes", (unsigned)sizeof(CFE_MSG_CommandHeader_t));
    EXPECT("infra", sizeof(CFE_FS_Header_t) == FS_HDR, "file header is %u bytes", (unsigned)sizeof(CFE_FS_Header_t));
    UtTest_Add(Test_StartupAndHousekeeping_Isolated, Setup, NULL, "Startup and housekeeping");
    UtTest_Add(Test_StartupErrTblReg_Isolated, Setup, NULL, "Startup error: table registration");
    UtTest_Add(Test_StartupErrTblLoad_Isolated, Setup, NULL, "Startup error: table load");
    UtTest_Add(Test_StartupErrSubscribe_Isolated, Setup, NULL, "Startup error: subscription");
    UtTest_Add(Test_StartupErrMkdir_Isolated, Setup, NULL, "Startup error: directory creation");
    UtTest_Add(Test_StartupErrPipe_Isolated, Setup, NULL, "Startup error: pipe read");
    UtTest_Add(Test_TableValidation_Isolated, Setup, NULL, "Table validation");
    UtTest_Add(Test_DecimationAndStats_Isolated, Setup, NULL, "Decimation and statistics");
    UtTest_Add(Test_RolloverAndNaming_Isolated, Setup, NULL, "Packet files and roll-over");
    UtTest_Add(Test_SequenceAndScan_Isolated, Setup, NULL, "Startup scan and sequence");
    UtTest_Add(Test_PurgeOrder_Isolated, Setup, NULL, "Purge order");
    UtTest_Add(Test_PurgeExisting_Isolated, Setup, NULL, "Purge of existing files");
    UtTest_Add(Test_PurgePriorityChange_Isolated, Setup, NULL, "Purge after priority change");
    UtTest_Add(Test_QuotaDrop_Isolated, Setup, NULL, "Telemetry quota drops");
    UtTest_Add(Test_SetParamCommands_Isolated, Setup, NULL, "Parameter commands");
    UtTest_Add(Test_CommandErrors_Isolated, Setup, NULL, "Command errors");
    UtTest_Add(Test_CommandCounters_Isolated, Setup, NULL, "Command counters");
    UtTest_Add(Test_ResetCounters_Isolated, Setup, NULL, "Reset counters");
    UtTest_Add(Test_FileAddDelete_Isolated, Setup, NULL, "File add and delete");
    UtTest_Add(Test_PathRejection_Isolated, Setup, NULL, "Path rejection");
    UtTest_Add(Test_Listings_Isolated, Setup, NULL, "Listing packets");
    UtTest_Add(Test_TableUpdate_Isolated, Setup, NULL, "Table update");
    UtTest_Add(Test_IdleClose_Isolated, Setup, NULL, "Idle close");
    UtTest_Add(Test_WriteFailures_Isolated, Setup, NULL, "File write failures");
    UtTest_Add(Test_RefInitFailures_Isolated, Setup, NULL, "Reference: InitFailures");
    UtTest_Add(Test_RefFileErrors_Isolated, Setup, NULL, "Reference: FileErrors");
    UtTest_Add(Test_RefTableEdges_Isolated, Setup, NULL, "Reference: TableEdges");
    UtTest_Add(Test_RefTimeStepBack_Isolated, Setup, NULL, "Reference: TimeStepBack");
    UtTest_Add(Test_RefScanMany_Isolated, Setup, NULL, "Reference: ScanMany");
    UtTest_Add(Test_RefNoTelemetryRoot_Isolated, Setup, NULL, "Reference: NoTelemetryRoot");
    UtTest_Add(Test_RefScanFails_Isolated, Setup, NULL, "Reference: ScanFails");
}
