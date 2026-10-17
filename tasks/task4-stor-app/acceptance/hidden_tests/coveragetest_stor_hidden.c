/*
 * Black-box behaviour test of the STOR application.
 *
 * The application's entry point STOR_AppMain runs on its own thread against the
 * UT-assert cFE/OSAL stubs. The stubs behind it are small models:
 *   - the software bus delivers the messages this test queues, one at a time,
 *     and the test waits until the application asks for the next one;
 *   - CFE_MSG, CFE_TBL, CFE_FS, CFE_EVS and CFE_TIME keep the state the application
 *     needs (message headers, one table, file headers, event history, a clock);
 *   - OSAL files and directories live in an in-memory tree with the real
 *     name-length limits.
 * Only names, structure layouts, message IDs, function codes and event IDs from the
 * STOR specification are used; structures are decoded by offset.
 */
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

extern void STOR_AppMain(void);

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
#define TLM_HDR        16
#define FS_HDR         64
#define HK_PAYLOAD     48

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

/* ------------------------------------------------------------ byte helpers */

static void put32(uint8 *p, uint32 v) { memcpy(p, &v, 4); }
static uint32 get32(const uint8 *p) { uint32 v; memcpy(&v, p, 4); return v; }
static uint16 get16(const uint8 *p) { uint16 v; memcpy(&v, p, 2); return v; }

/* ------------------------------------------------------------- table images */

typedef struct { uint8 b[TBL_SIZE]; } tbl_t;

static void tbl_default(tbl_t *t)
{
    memset(t, 0, sizeof(*t));
    strcpy((char *)t->b, "/cf/stor/tlm");
    strcpy((char *)t->b + PATH_LEN, "/cf/stor/files");
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

/* --------------------------------------------------------- in-memory files */

#define MF_MAX        1024
#define MFD_MAX       64
#define MDIR_MAX      16
#define NAME_MAX_TEST 96

typedef struct
{
    bool    used;
    bool    dir;
    char    path[NAME_MAX_TEST];
    uint8  *data;
    size_t  size;
    uint32  order;
} mfile_t;

typedef struct { bool used; int file; size_t pos; } mfd_t;
typedef struct { bool used; char path[NAME_MAX_TEST]; uint32 next_order; } mdir_t;

static mfile_t  mf[MF_MAX];
static mfd_t    mfd[MFD_MAX];
static mdir_t   mdir[MDIR_MAX];
static uint32   mf_order;
static uint32   n_remove, n_close;
static int      fi_create_fail;   /* fail the next N creations of packet files */
static bool     fi_write_short;   /* OS_write writes nothing */
static bool     fi_mkdir_fail;    /* OS_mkdir fails */

static int mf_find(const char *path)
{
    int i;
    for (i = 0; i < MF_MAX; i++)
        if (mf[i].used && strcmp(mf[i].path, path) == 0) return i;
    return -1;
}

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
            return i;
        }
    }
    UtAssert_Failed("model file table full");
    return 0;
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
    fi_write_short = false;
    fi_mkdir_fail = false;
    mf_new("/cf", true);
}

/* Create or replace a regular file of `size` pattern bytes; the parent must exist. */
static void mf_put(const char *path, size_t size, uint8 seed)
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

static int32 name_check(const char *path)
{
    if (strlen(base_of(path)) >= OS_MAX_FILE_NAME) return OS_FS_ERR_NAME_TOO_LONG;
    if (strlen(path) >= OS_MAX_PATH_LEN) return OS_FS_ERR_PATH_TOO_LONG;
    return OS_SUCCESS;
}

static void ret_i32(UT_EntryKey_t key, int32 v)
{
    UT_Stub_SetReturnValue(key, v);
}

static void H_OS_OpenCreate(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t *fd = UT_Hook_GetArgValueByName(c, "filedes", osal_id_t *);
    const char *path = UT_Hook_GetArgValueByName(c, "path", const char *);
    int32 flags = UT_Hook_GetArgValueByName(c, "flags", int32);
    int32 rc = name_check(path);
    int i = -1, k;
    char par[NAME_MAX_TEST];
    (void)ud;
    if (rc == OS_SUCCESS && fi_create_fail > 0 && (flags & OS_FILE_FLAG_CREATE) && strstr(path, "/tlm_") != NULL)
    {
        fi_create_fail--;
        rc = OS_ERROR;
    }
    if (rc == OS_SUCCESS)
    {
        i = mf_find(path);
        if (i >= 0 && mf[i].dir)
        {
            rc = OS_ERROR;
        }
        else if (i < 0 && !(flags & OS_FILE_FLAG_CREATE))
        {
            rc = OS_ERROR;
        }
        else if (i < 0)
        {
            parent_of(path, par, sizeof(par));
            k = mf_find(par);
            if (k < 0 || !mf[k].dir) rc = OS_ERROR;
            else i = mf_new(path, false);
        }
        else if (flags & OS_FILE_FLAG_TRUNCATE)
        {
            mf[i].size = 0;
        }
    }
    if (rc == OS_SUCCESS)
    {
        for (k = 0; k < MFD_MAX && mfd[k].used; k++) {}
        if (k == MFD_MAX)
        {
            rc = OS_ERROR;
        }
        else
        {
            mfd[k].used = true;
            mfd[k].file = i;
            mfd[k].pos = 0;
            *fd = OS_ObjectIdFromInteger((unsigned long)k + 1);
        }
    }
    ret_i32(key, rc);
}

static mfd_t *fd_of(osal_id_t id)
{
    unsigned long v = OS_ObjectIdToInteger(id);
    if (v < 1 || v > MFD_MAX || !mfd[v - 1].used) return NULL;
    return &mfd[v - 1];
}

static int32 do_write(osal_id_t id, const void *buf, size_t n)
{
    mfd_t *d = fd_of(id);
    mfile_t *f;
    if (d == NULL) return OS_ERR_INVALID_ID;
    if (fi_write_short) return 0;
    f = &mf[d->file];
    mf_grow(f, d->pos + n);
    memcpy(f->data + d->pos, buf, n);
    d->pos += n;
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

static void H_OS_close(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mfd_t *d = fd_of(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t));
    (void)ud;
    if (d == NULL) { ret_i32(key, OS_ERR_INVALID_ID); return; }
    d->used = false;
    n_close++;
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_remove(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *path = UT_Hook_GetArgValueByName(c, "path", const char *);
    int i = mf_find(path);
    (void)ud;
    if (i < 0 || mf[i].dir) { ret_i32(key, OS_ERROR); return; }
    free(mf[i].data);
    memset(&mf[i], 0, sizeof(mf[i]));
    n_remove++;
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_mkdir(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
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
    ret_i32(key, rc);
}

static void H_OS_rmdir(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *path = UT_Hook_GetArgValueByName(c, "path", const char *);
    int i = mf_find(path), k;
    size_t n = strlen(path);
    (void)ud;
    if (i < 0 || !mf[i].dir) { ret_i32(key, OS_ERROR); return; }
    for (k = 0; k < MF_MAX; k++)
    {
        if (mf[k].used && k != i && strncmp(mf[k].path, path, n) == 0 && mf[k].path[n] == '/')
        {
            ret_i32(key, OS_ERROR);
            return;
        }
    }
    memset(&mf[i], 0, sizeof(mf[i]));
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_stat(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const char *path = UT_Hook_GetArgValueByName(c, "path", const char *);
    os_fstat_t *st = UT_Hook_GetArgValueByName(c, "filestats", os_fstat_t *);
    int i = mf_find(path);
    (void)ud;
    if (i < 0) { ret_i32(key, OS_ERROR); return; }
    memset(st, 0, sizeof(*st));
    st->FileModeBits = OS_FILESTAT_MODE_READ | OS_FILESTAT_MODE_WRITE | (mf[i].dir ? OS_FILESTAT_MODE_DIR : 0);
    st->FileSize = mf[i].dir ? 0 : mf[i].size;
    ret_i32(key, OS_SUCCESS);
}

static mdir_t *dir_of(osal_id_t id)
{
    unsigned long v = OS_ObjectIdToInteger(id);
    if (v < 1 || v > MDIR_MAX || !mdir[v - 1].used) return NULL;
    return &mdir[v - 1];
}

static void H_OS_DirectoryOpen(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t *id = UT_Hook_GetArgValueByName(c, "dir_id", osal_id_t *);
    const char *path = UT_Hook_GetArgValueByName(c, "path", const char *);
    int i = mf_find(path), k;
    (void)ud;
    if (i < 0 || !mf[i].dir) { ret_i32(key, OS_ERROR); return; }
    for (k = 0; k < MDIR_MAX && mdir[k].used; k++) {}
    if (k == MDIR_MAX) { ret_i32(key, OS_ERROR); return; }
    mdir[k].used = true;
    snprintf(mdir[k].path, sizeof(mdir[k].path), "%s", path);
    mdir[k].next_order = 0;
    *id = OS_ObjectIdFromInteger((unsigned long)k + 1);
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_DirectoryRead(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mdir_t *d = dir_of(UT_Hook_GetArgValueByName(c, "dir_id", osal_id_t));
    os_dirent_t *ent = UT_Hook_GetArgValueByName(c, "dirent", os_dirent_t *);
    int k, best = -1;
    char par[NAME_MAX_TEST];
    (void)ud;
    if (d == NULL) { ret_i32(key, OS_ERR_INVALID_ID); return; }
    for (k = 0; k < MF_MAX; k++)
    {
        if (!mf[k].used || mf[k].order <= d->next_order) continue;
        parent_of(mf[k].path, par, sizeof(par));
        if (strcmp(par, d->path) != 0) continue;
        if (best < 0 || mf[k].order < mf[best].order) best = k;
    }
    if (best < 0) { ret_i32(key, OS_ERROR); return; }
    d->next_order = mf[best].order;
    memset(ent, 0, sizeof(*ent));
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
    ret_i32(key, OS_SUCCESS);
}

static void H_OS_lseek(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    mfd_t *d = fd_of(UT_Hook_GetArgValueByName(c, "filedes", osal_id_t));
    long off = (long)UT_Hook_GetArgValueByName(c, "offset", osal_offset_t);
    uint32 wh = UT_Hook_GetArgValueByName(c, "whence", uint32);
    (void)ud;
    if (d == NULL) { ret_i32(key, OS_ERR_INVALID_ID); return; }
    if (wh == OS_SEEK_SET) d->pos = (size_t)off;
    else if (wh == OS_SEEK_CUR) d->pos = (size_t)((long)d->pos + off);
    else d->pos = (size_t)((long)mf[d->file].size + off);
    ret_i32(key, (int32)d->pos);
}

static int32 do_cp(const char *src, const char *dst, bool move)
{
    int i = mf_find(src), j, k;
    char par[NAME_MAX_TEST];
    int32 rc = name_check(dst);
    if (rc != OS_SUCCESS) return rc;
    if (i < 0 || mf[i].dir || mf_find(dst) >= 0) return OS_ERROR;
    parent_of(dst, par, sizeof(par));
    k = mf_find(par);
    if (k < 0 || !mf[k].dir) return OS_ERROR;
    j = mf_new(dst, false);
    mf_grow(&mf[j], mf[i].size);
    memcpy(mf[j].data, mf[i].data, mf[i].size);
    if (move)
    {
        free(mf[i].data);
        memset(&mf[i], 0, sizeof(mf[i]));
    }
    return OS_SUCCESS;
}

static void H_OS_cp(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    ret_i32(key, do_cp(UT_Hook_GetArgValueByName(c, "src", const char *),
                       UT_Hook_GetArgValueByName(c, "dest", const char *), false));
}

static void H_OS_mv(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    ret_i32(key, do_cp(UT_Hook_GetArgValueByName(c, "src", const char *),
                       UT_Hook_GetArgValueByName(c, "dest", const char *), true));
}

static void H_OS_rename(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    ret_i32(key, do_cp(UT_Hook_GetArgValueByName(c, "old_filename", const char *),
                       UT_Hook_GetArgValueByName(c, "new_filename", const char *), true));
}

static bool path_is_open(const char *path)
{
    int i = mf_find(path), k;
    if (i < 0) return false;
    for (k = 0; k < MFD_MAX; k++)
        if (mfd[k].used && mfd[k].file == i) return true;
    return false;
}

static void H_OS_FileOpenCheck(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud;
    ret_i32(key, path_is_open(UT_Hook_GetArgValueByName(c, "Filename", const char *)) ? OS_SUCCESS : OS_ERROR);
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

static uint32 n_open_total(void)
{
    uint32 k, n = 0;
    for (k = 0; k < MFD_MAX; k++) if (mfd[k].used) n++;
    return n;
}

/* ------------------------------------------------------------------- clock */

static uint32 now_sec;

static void H_CFE_TIME_Now(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TIME_SysTime_t t;
    (void)ud; (void)c;
    memset(&t, 0, sizeof(t));
    t.Seconds = now_sec;
    UT_Stub_SetReturnValue(key, t);
}

static void H_OS_GetLocalTime(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    OS_time_t *t = UT_Hook_GetArgValueByName(c, "time_struct", OS_time_t *);
    (void)ud;
    *t = OS_TimeAssembleFromNanoseconds((int64)now_sec, 0);
    ret_i32(key, OS_SUCCESS);
}

static void H_PSP_GetTime(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    OS_time_t *t = UT_Hook_GetArgValueByName(c, "LocalTime", OS_time_t *);
    (void)ud; (void)key;
    *t = OS_TimeAssembleFromNanoseconds((int64)now_sec, 0);
}

/* ---------------------------------------------------------- messages, events */

#define MSG_MAX_BYTES 2048
#define REG_MAX       64
#define RING          16
#define TX_MAX        256
#define EV_MAX        4096

typedef struct
{
    const void *ptr;
    uint32      mid;
    size_t      size;
    uint16      fcn;
    int         type;
} msgreg_t;

typedef struct { uint32 mid; size_t size; uint8 bytes[MSG_MAX_BYTES]; } txmsg_t;

static msgreg_t  reg[REG_MAX];
static int       nreg;
static txmsg_t  *tx;
static int       ntx;
static uint16    ev_id[EV_MAX];
static uint16    ev_type[EV_MAX];
static int       nev;

static msgreg_t *reg_find(const void *p)
{
    int i;
    for (i = 0; i < nreg; i++)
        if (reg[i].ptr == p) return &reg[i];
    return NULL;
}

static msgreg_t *reg_set(const void *p)
{
    msgreg_t *r = reg_find(p);
    if (r == NULL)
    {
        if (nreg == REG_MAX)
        {
            UtAssert_Failed("message registry full");
            return &reg[REG_MAX - 1];
        }
        r = &reg[nreg++];
        memset(r, 0, sizeof(*r));
        r->ptr = p;
    }
    return r;
}

static void H_MSG_Init(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_MSG_Message_t *m = UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    CFE_SB_MsgId_t id = UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t);
    size_t sz = UT_Hook_GetArgValueByName(c, "Size", CFE_MSG_Size_t);
    msgreg_t *r = reg_set(m);
    (void)ud;
    memset(m, 0, sz);
    r->mid = CFE_SB_MsgIdToValue(id);
    r->size = sz;
    r->type = (r->mid & 0x1000) ? CFE_MSG_Type_Cmd : CFE_MSG_Type_Tlm;
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_GetMsgId(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const CFE_MSG_Message_t *m = UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_SB_MsgId_t *out = UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t *);
    msgreg_t *r = reg_find(m);
    (void)ud;
    *out = CFE_SB_ValueToMsgId(r ? r->mid : 0);
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_SetMsgId(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_MSG_Message_t *m = UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    (void)ud;
    reg_set(m)->mid = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_GetSize(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const CFE_MSG_Message_t *m = UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_Size_t *out = UT_Hook_GetArgValueByName(c, "Size", CFE_MSG_Size_t *);
    msgreg_t *r = reg_find(m);
    (void)ud;
    *out = r ? r->size : 0;
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_SetSize(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_MSG_Message_t *m = UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    (void)ud;
    reg_set(m)->size = UT_Hook_GetArgValueByName(c, "Size", CFE_MSG_Size_t);
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_GetFcnCode(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const CFE_MSG_Message_t *m = UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_FcnCode_t *out = UT_Hook_GetArgValueByName(c, "FcnCode", CFE_MSG_FcnCode_t *);
    msgreg_t *r = reg_find(m);
    (void)ud;
    *out = r ? r->fcn : 0;
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_SetFcnCode(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_MSG_Message_t *m = UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    (void)ud;
    reg_set(m)->fcn = (uint16)UT_Hook_GetArgValueByName(c, "FcnCode", CFE_MSG_FcnCode_t);
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_GetType(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const CFE_MSG_Message_t *m = UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    CFE_MSG_Type_t *out = UT_Hook_GetArgValueByName(c, "Type", CFE_MSG_Type_t *);
    msgreg_t *r = reg_find(m);
    (void)ud;
    *out = (CFE_MSG_Type_t)(r ? r->type : CFE_MSG_Type_Invalid);
    ret_i32(key, CFE_SUCCESS);
}

static void H_MSG_SetType(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_MSG_Message_t *m = UT_Hook_GetArgValueByName(c, "MsgPtr", CFE_MSG_Message_t *);
    (void)ud;
    reg_set(m)->type = (int)UT_Hook_GetArgValueByName(c, "Type", CFE_MSG_Type_t);
    ret_i32(key, CFE_SUCCESS);
}

static void H_SB_IsValidMsgId(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    bool ok = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t)) != 0;
    (void)ud;
    UT_Stub_SetReturnValue(key, ok);
}

static int32  sub_rc, unsub_rc;
static uint32 subs[256];
static int    nsubs;
static uint32 unsubs[256];
static int    nunsubs;

static void H_SB_Subscribe(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 v = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    (void)ud;
    if (sub_rc == CFE_SUCCESS && nsubs < 256) subs[nsubs++] = v;
    ret_i32(key, sub_rc);
}

static void H_SB_Unsubscribe(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    uint32 v = CFE_SB_MsgIdToValue(UT_Hook_GetArgValueByName(c, "MsgId", CFE_SB_MsgId_t));
    (void)ud;
    if (nunsubs < 256) unsubs[nunsubs++] = v;
    ret_i32(key, unsub_rc);
}

static void H_SB_Transmit(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    const CFE_MSG_Message_t *m = UT_Hook_GetArgValueByName(c, "MsgPtr", const CFE_MSG_Message_t *);
    msgreg_t *r = reg_find(m);
    (void)ud;
    if (r == NULL)
    {
        UtAssert_Failed("a message was transmitted that was never initialized with CFE_MSG_Init");
    }
    else if (ntx < TX_MAX && r->size <= MSG_MAX_BYTES)
    {
        tx[ntx].mid = r->mid;
        tx[ntx].size = r->size;
        memcpy(tx[ntx].bytes, m, r->size);
        ntx++;
    }
    ret_i32(key, CFE_SUCCESS);
}

static int32 ev_hook(void *ud, int32 rc, uint32 cnt, const UT_StubContext_t *c, va_list va)
{
    (void)ud; (void)cnt; (void)va;
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

static int last_ev_type(uint16 id)
{
    int i;
    for (i = nev - 1; i >= 0; i--) if (ev_id[i] == id) return ev_type[i];
    return -1;
}

/* -------------------------------------------------------------------- table */

static tbl_t tbl_active, tbl_pending;
static bool tbl_updated, tbl_has_pending;
static CFE_TBL_CallbackFuncPtr_t tbl_validate;
static char tbl_name[64];
static size_t tbl_size;
static int32 tbl_reg_rc, tbl_load_rc, recv_rc;
static int n_tbl_register;

static void H_TBL_Register(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_TBL_Handle_t *h = UT_Hook_GetArgValueByName(c, "TblHandlePtr", CFE_TBL_Handle_t *);
    const char *name = UT_Hook_GetArgValueByName(c, "Name", const char *);
    (void)ud;
    n_tbl_register++;
    snprintf(tbl_name, sizeof(tbl_name), "%s", name);
    tbl_size = UT_Hook_GetArgValueByName(c, "Size", size_t);
    tbl_validate = UT_Hook_GetArgValueByName(c, "TblValidationFuncPtr", CFE_TBL_CallbackFuncPtr_t);
    *h = 1;
    ret_i32(key, tbl_reg_rc);
}

static void H_TBL_Load(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    ret_i32(key, tbl_load_rc);
}

static void tbl_activate(void)
{
    if (tbl_has_pending)
    {
        tbl_active = tbl_pending;
        tbl_has_pending = false;
        tbl_updated = true;
    }
}

static void H_TBL_Activate(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    tbl_activate();
    ret_i32(key, CFE_SUCCESS);
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
    *p = &tbl_active;
    tbl_updated = false;
    ret_i32(key, rc);
}

static void H_TBL_Ok(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    (void)ud; (void)c;
    ret_i32(key, CFE_SUCCESS);
}

/* Present a new table image: it must pass the application's own validation first. */
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
    memset(h, 0, sizeof(*h));
    h->ContentType = 0x63464531;
    h->SubType = UT_Hook_GetArgValueByName(c, "SubType", uint32);
    h->Length = sizeof(*h);
    if (d != NULL) strncpy(h->Description, d, sizeof(h->Description) - 1);
}

static void H_FS_WriteHeader(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    osal_id_t fd = UT_Hook_GetArgValueByName(c, "FileDes", osal_id_t);
    CFE_FS_Header_t *h = UT_Hook_GetArgValueByName(c, "Hdr", CFE_FS_Header_t *);
    CFE_FS_Header_t out = *h;
    (void)ud;
    out.Length = sizeof(out);
    ret_i32(key, do_write(fd, &out, sizeof(out)) == (int32)sizeof(out) ? (int32)sizeof(out) : CFE_FS_BAD_ARGUMENT);
}

/* -------------------------------------------------- application thread / bus */

static pthread_t app_thread;
static bool app_running, app_exited, app_stop;
static sem_t sem_app, sem_test;
static uint8 *pending_buf;
static bool have_pending;

static void *app_main(void *arg)
{
    (void)arg;
    STOR_AppMain();
    app_exited = true;
    sem_post(&sem_test);
    return NULL;
}

static void H_ES_RunLoop(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    bool run = !app_stop;
    (void)ud; (void)c;
    UT_Stub_SetReturnValue(key, run);
}

static void H_SB_Receive(void *ud, UT_EntryKey_t key, const UT_StubContext_t *c)
{
    CFE_SB_Buffer_t **bp = UT_Hook_GetArgValueByName(c, "BufPtr", CFE_SB_Buffer_t **);
    (void)ud;
    if (recv_rc != CFE_SUCCESS)
    {
        app_stop = true;
        ret_i32(key, recv_rc);
        return;
    }
    sem_post(&sem_test);
    sem_wait(&sem_app);
    if (app_stop || !have_pending)
    {
        ret_i32(key, CFE_SB_NO_MESSAGE);
        return;
    }
    have_pending = false;
    *bp = (CFE_SB_Buffer_t *)pending_buf;
    ret_i32(key, CFE_SUCCESS);
}

static bool wait_test(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += 10;
    while (sem_timedwait(&sem_test, &ts) != 0)
    {
        if (errno == EINTR) continue;
        UtAssert_Failed("STOR did not return to its software bus pipe within 10 s");
        return false;
    }
    return !app_exited;
}

static bool app_start(void)
{
    app_running = true;
    app_exited = false;
    app_stop = false;
    have_pending = false;
    if (pthread_create(&app_thread, NULL, app_main, NULL) != 0)
    {
        UtAssert_Failed("cannot create the application thread");
        app_running = false;
        return false;
    }
    return wait_test();
}

static void app_end(void)
{
    struct timespec ts;
    if (!app_running) return;
    app_stop = true;
    if (!app_exited)
    {
        sem_post(&sem_app);
        clock_gettime(CLOCK_REALTIME, &ts);
        ts.tv_sec += 5;
        while (!app_exited && sem_timedwait(&sem_test, &ts) != 0 && errno == EINTR) {}
    }
    if (app_exited) pthread_join(app_thread, NULL);
    app_running = false;
}

static uint8 msg_pool[RING][MSG_MAX_BYTES] __attribute__((aligned(16)));
static int msg_pool_next;

static bool app_deliver(uint8 *buf)
{
    if (!app_running || app_exited)
    {
        UtAssert_Failed("STOR is not running");
        return false;
    }
    pending_buf = buf;
    have_pending = true;
    sem_post(&sem_app);
    return wait_test();
}

static uint8 *new_msg(uint32 mid, size_t size, int type, int fcn)
{
    uint8 *b = msg_pool[msg_pool_next++ % RING];
    msgreg_t *r;
    memset(b, 0, size);
    b[0] = (uint8)(mid >> 8);
    b[1] = (uint8)mid;
    b[2] = 0xC0;
    b[4] = (uint8)((size - 7) >> 8);
    b[5] = (uint8)(size - 7);
    r = reg_set(b);
    r->mid = mid;
    r->size = size;
    r->type = type;
    r->fcn = (uint16)fcn;
    return b;
}

static bool send_cmd(uint32 mid, int cc, const void *payload, size_t plen)
{
    size_t hdr = sizeof(CFE_MSG_CommandHeader_t);
    uint8 *b = new_msg(mid, hdr + plen, CFE_MSG_Type_Cmd, cc);
    if (plen) memcpy(b + hdr, payload, plen);
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
    b[4] = (uint8)((size - 7) >> 8);
    b[5] = (uint8)(size - 7);
    put32(b + TLM_HDR, index);
    for (k = TLM_HDR + 4; k < size; k++) b[k] = (uint8)(index * 3 + k);
}

static bool send_tlm(uint32 mid, size_t size, uint32 index)
{
    uint8 *b = new_msg(mid, size, CFE_MSG_Type_Tlm, 0);
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
    for (i = ntx - 1; i >= 0; i--) if (tx[i].mid == mid) return &tx[i];
    return NULL;
}

static bool get_hk(hk_t *h)
{
    const txmsg_t *t;
    int before = ntx;
    const uint8 *p;
    memset(h, 0, sizeof(*h));
    if (!send_cmd(T_SEND_HK_MID, 0, NULL, 0)) return false;
    t = last_tx(T_HK_MID);
    if (t == NULL || ntx == before)
    {
        UtAssert_Failed("SEND_HK did not transmit a housekeeping packet on STOR_HK_TLM_MID");
        return false;
    }
    UtAssert_True(t->size == TLM_HDR + HK_PAYLOAD, "housekeeping packet is %u bytes, expected %u", (unsigned)t->size,
                  (unsigned)(TLM_HDR + HK_PAYLOAD));
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
    int i, m = 0;
    int before = ntx;
    if (!send_simple(CC_STATS)) return false;
    t = last_tx(T_STATS_MID);
    if (t == NULL || ntx == before)
    {
        UtAssert_Failed("SEND_STATS did not transmit a packet on STOR_STATS_TLM_MID");
        return false;
    }
    UtAssert_True(t->size == TLM_HDR + MAX_ENT * 24, "statistics packet is %u bytes, expected %u", (unsigned)t->size,
                  (unsigned)(TLM_HDR + MAX_ENT * 24));
    for (i = 0; i < MAX_ENT; i++)
    {
        const uint8 *e = t->bytes + TLM_HDR + i * 24;
        if (get32(e) == 0) continue;
        out[m].mid = get32(e); out[m].pri = e[4]; out[m].dec = e[5];
        out[m].rx = get32(e + 8); out[m].st = get32(e + 12); out[m].de = get32(e + 16); out[m].dr = get32(e + 20);
        m++;
    }
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
    for (i = first_tx; i < ntx && n < max; i++)
    {
        const uint8 *p;
        if (tx[i].mid != T_LIST_MID) continue;
        UtAssert_True(tx[i].size == TLM_HDR + 8 + 4 * 72, "listing packet is %u bytes, expected %u", (unsigned)tx[i].size,
                      (unsigned)(TLM_HDR + 8 + 4 * 72));
        p = tx[i].bytes + TLM_HDR;
        out[n].total = get16(p); out[n].index = get16(p + 2); out[n].count = get16(p + 4);
        for (k = 0; k < 4; k++)
        {
            memcpy(out[n].name[k], p + 8 + k * 72, PATH_LEN);
            out[n].name[k][PATH_LEN] = 0;
            out[n].size[k] = get32(p + 8 + k * 72 + PATH_LEN);
        }
        n++;
    }
    return n;
}

/* --------------------------------------------------------------- test fixture */

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
    UT_SetHandlerFunction(UT_KEY(OS_GetLocalTime), H_OS_GetLocalTime, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_PSP_GetTime), H_PSP_GetTime, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetTime), H_CFE_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetMET), H_CFE_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetTAI), H_CFE_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TIME_GetUTC), H_CFE_TIME_Now, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_Init), H_MSG_Init, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetMsgId), H_MSG_GetMsgId, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetMsgId), H_MSG_SetMsgId, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetSize), H_MSG_GetSize, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetSize), H_MSG_SetSize, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetFcnCode), H_MSG_GetFcnCode, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetFcnCode), H_MSG_SetFcnCode, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_GetType), H_MSG_GetType, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_MSG_SetType), H_MSG_SetType, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_IsValidMsgId), H_SB_IsValidMsgId, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_Subscribe), H_SB_Subscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_SubscribeEx), H_SB_Subscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_SubscribeLocal), H_SB_Subscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_Unsubscribe), H_SB_Unsubscribe, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_TransmitMsg), H_SB_Transmit, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_SB_ReceiveBuffer), H_SB_Receive, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_ES_RunLoop), H_ES_RunLoop, NULL);
    UT_SetVaHookFunction(UT_KEY(CFE_EVS_SendEvent), ev_hook, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Register), H_TBL_Register, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Load), H_TBL_Load, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Manage), H_TBL_Activate, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_Update), H_TBL_Activate, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_GetStatus), H_TBL_GetStatus, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_GetAddress), H_TBL_GetAddress, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_TBL_ReleaseAddress), H_TBL_Ok, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_FS_InitHeader), H_FS_InitHeader, NULL);
    UT_SetHandlerFunction(UT_KEY(CFE_FS_WriteHeader), H_FS_WriteHeader, NULL);
}

static void Setup(void)
{
    UT_ResetState(0);
    mf_reset();
    nreg = ntx = nev = nsubs = nunsubs = 0;
    msg_pool_next = 0;
    sub_rc = unsub_rc = CFE_SUCCESS;
    tbl_reg_rc = tbl_load_rc = CFE_SUCCESS;
    recv_rc = CFE_SUCCESS;
    n_tbl_register = 0;
    tbl_validate = NULL;
    tbl_name[0] = 0;
    tbl_size = 0;
    tbl_has_pending = false;
    tbl_updated = true;
    now_sec = 1000;
    if (tx == NULL) tx = calloc(TX_MAX, sizeof(*tx));
    tbl_default(&tbl_active);
    tbl_entry(&tbl_active, 0, 0x0801, 10, 1);
    tbl_entry(&tbl_active, 1, 0x0800, 20, 1);
    tbl_entry(&tbl_active, 2, 0x0883, 30, 2);
    tbl_entry(&tbl_active, 3, 0x0805, 40, 1);
    register_hooks();
}

static void Teardown(void)
{
    app_end();
}

/* Boot the application with `t` as the loaded table. */
static bool boot(const tbl_t *t)
{
    tbl_t copy = *t;
    tbl_active = copy;
    tbl_updated = true;
    return app_start();
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

static uint32 tlm_bytes_on_disk(void)
{
    int i;
    uint32 s = 0;
    for (i = 0; i < MF_MAX; i++)
        if (mf[i].used && !mf[i].dir && strncmp(mf[i].path, "/cf/stor/tlm/", 13) == 0) s += (uint32)mf[i].size;
    return s;
}

static int tlm_file_count(void)
{
    int i, n = 0;
    for (i = 0; i < MF_MAX; i++)
        if (mf[i].used && !mf[i].dir && strncmp(mf[i].path, "/cf/stor/tlm/", 13) == 0) n++;
    return n;
}

/* Packet files of one MID, ordered by sequence number; returns how many. */
static int mid_files(uint32 mid, int *idx, int max)
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

/* Remember every packet file name ever seen, to tell which were purged. */
#define SEEN_MAX 128
static char seen[SEEN_MAX][NAME_MAX_TEST];
static int  nseen;

static void snap(void)
{
    int i, k;
    for (i = 0; i < MF_MAX; i++)
    {
        if (!mf[i].used || mf[i].dir || strncmp(mf[i].path, "/cf/stor/tlm/", 13) != 0) continue;
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
    int i, found_cmd = 0, found_hk = 0, found_wake = 0, found_ent = 0;
    UtAssert_True(boot(&tbl_active), "STOR_AppMain starts and waits on its pipe");
    UtAssert_True(count_ev(EID_INIT, 0) == 1, "STOR_INIT_INF_EID (1) emitted once, saw %d", count_ev(EID_INIT, 0));
    UtAssert_True(last_ev_type(EID_INIT) == CFE_EVS_EventType_INFORMATION, "event 1 is INFORMATION");
    UtAssert_UINT32_EQ(UT_GetStubCount(UT_KEY(CFE_EVS_Register)), 1);
    UtAssert_True(n_tbl_register == 1 && strcmp(tbl_name, "StorageTable") == 0, "table registered as StorageTable (%s)", tbl_name);
    UtAssert_True(tbl_size == TBL_SIZE, "table size %u, expected %u", (unsigned)tbl_size, TBL_SIZE);
    UtAssert_True(tbl_validate != NULL, "a table validation function is registered");
    for (i = 0; i < nsubs; i++)
    {
        found_cmd += subs[i] == T_CMD_MID;
        found_hk += subs[i] == T_SEND_HK_MID;
        found_wake += subs[i] == T_WAKEUP_MID;
        found_ent += subs[i] == 0x0801 || subs[i] == 0x0800 || subs[i] == 0x0883 || subs[i] == 0x0805;
    }
    UtAssert_True(found_cmd && found_hk && found_wake, "subscribed to the command, SEND_HK and WAKEUP message IDs");
    UtAssert_True(found_ent == 4, "subscribed to each of the four table entries (%d)", found_ent);
    UtAssert_True(mf_find("/cf/stor/tlm") >= 0 && mf_find("/cf/stor/files") >= 0, "both roots created, one level at a time");
    if (!get_hk(&h)) return;
    UtAssert_True(h.quota == 262144 && h.fquota == 1048576 && h.thr == 80 && h.active == 4, "quotas %u/%u threshold %u active %u",
                  h.quota, h.fquota, h.thr, h.active);
    UtAssert_True(h.cc == 0 && h.cec == 0 && h.used == 0 && h.fused == 0 && h.open == 0, "initial housekeeping is zero");
    UtAssert_True(send_simple(CC_NOOP) && count_ev(EID_NOOP, 0) == 1, "NOOP emits event 3");
    UtAssert_True(last_ev_type(EID_NOOP) == CFE_EVS_EventType_INFORMATION, "event 3 is INFORMATION");
    if (get_hk(&h)) UtAssert_UINT32_EQ(h.cc, 1);
}

static void Test_StartupErrors(void)
{
    tbl_reg_rc = CFE_TBL_ERR_INVALID_NAME;
    app_start();
    UtAssert_True(count_ev(EID_TBLREG, 0) >= 1, "table registration failure emits event 8");
    app_end();

    Setup();
    tbl_load_rc = CFE_TBL_ERR_NO_ACCESS;
    app_start();
    UtAssert_True(count_ev(EID_TBLLOAD, 0) >= 1, "table load failure emits event 9");
    app_end();

    Setup();
    sub_rc = CFE_SB_BAD_ARGUMENT;
    app_start();
    UtAssert_True(count_ev(EID_SUB_ERR, 0) >= 1, "subscription failure emits event 11");
    app_end();

    Setup();
    fi_mkdir_fail = true;
    app_start();
    UtAssert_True(count_ev(EID_DIR_ERR, 0) >= 1, "root directory creation failure emits event 12");
    app_end();

    Setup();
    recv_rc = CFE_SB_PIPE_RD_ERR;
    app_start();
    UtAssert_True(count_ev(EID_PIPE_ERR, 0) >= 1, "pipe read failure emits event 7");
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
    UtAssert_True(tbl_validate != NULL, "validation function registered");
    if (tbl_validate == NULL) return;
    tbl_default(&good);
    tbl_entry(&good, 0, T_MID_A, 5, 1);
    UtAssert_True(vtbl(&good), "a valid table is accepted");

    ev0 = nev;
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

    /* Boundaries that must be accepted. */
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
    for (i = 0; i < 4; i++) send_tlm(T_MID_A, 100, (uint32)i);
    for (i = 0; i < 10; i++) send_tlm(T_MID_B, 100, (uint32)i);
    for (i = 0; i < 10; i++) send_tlm(T_MID_C, 100, (uint32)i);
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
            uint32 got = get32(mf[idx[i]].data + pos + TLM_HDR);
            if (found < 4)
                UtAssert_True(got == expect_idx[found], "decimation 3 stored packet #%u, expected #%u", got, expect_idx[found]);
            found++;
            pos += 100;
        }
    }
    UtAssert_UINT32_EQ((uint32)found, 4);

    /* Run-time changes. */
    UtAssert_True(cmd_param(CC_SET_DEC, T_MID_B, 1) && count_ev(EID_PARAM_OK, 0) == 1, "SET_DECIMATION emits event 16");
    UtAssert_True(last_ev_type(EID_PARAM_OK) == CFE_EVS_EventType_INFORMATION, "event 16 is INFORMATION");
    UtAssert_True(cmd_param(CC_SET_PRI, T_MID_B, 77) && count_ev(EID_PARAM_OK, 0) == 2, "SET_PRIORITY emits event 16");
    send_tlm(T_MID_B, 100, 10);
    send_tlm(T_MID_B, 100, 11);
    if (get_stats(e, &n))
    {
        b = est_find(e, n, T_MID_B);
        UtAssert_True(b && b->pri == 77 && b->dec == 1 && b->rx == 12 && b->st == 6,
                      "after SET_*: priority %u decimation %u received %u stored %u", b ? b->pri : 0, b ? b->dec : 0,
                      b ? b->rx : 0, b ? b->st : 0);
    }
}

static void check_packet_file(const char *path, uint32 mid, size_t pkt, uint32 first_idx, uint32 count)
{
    int i = mf_find(path);
    uint32 k;
    CFE_FS_Header_t *h;
    uint8 want[MSG_MAX_BYTES];
    UtAssert_True(i >= 0, "file %s exists", path);
    if (i < 0) return;
    UtAssert_True(mf[i].size == FS_HDR + (size_t)count * pkt, "%s is %u bytes, expected %u", path, (unsigned)mf[i].size,
                  (unsigned)(FS_HDR + count * pkt));
    if (mf[i].size < FS_HDR) return;
    h = (CFE_FS_Header_t *)mf[i].data;
    UtAssert_True(h->ContentType == 0x63464531, "%s starts with a cFE file header", path);
    UtAssert_True(h->SubType == 0x5354, "%s sub type 0x%X is STOR_FILE_SUBTYPE 0x5354", path, (unsigned)h->SubType);
    UtAssert_True(strncmp(h->Description, "STOR packet file", sizeof(h->Description)) == 0, "%s description '%.32s'", path,
                  h->Description);
    for (k = 0; k < count && FS_HDR + (k + 1) * pkt <= mf[i].size; k++)
    {
        fill_tlm(want, mid, pkt, first_idx + k);
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
    for (i = 0; i < 10; i++) send_tlm(T_MID_A, 300, (uint32)i);
    check_packet_file("/cf/stor/tlm/tlm_08E0_00000000.bin", T_MID_A, 300, 0, 3);
    check_packet_file("/cf/stor/tlm/tlm_08E0_00000001.bin", T_MID_A, 300, 3, 3);
    check_packet_file("/cf/stor/tlm/tlm_08E0_00000002.bin", T_MID_A, 300, 6, 3);
    check_packet_file("/cf/stor/tlm/tlm_08E0_00000003.bin", T_MID_A, 300, 9, 1);
    UtAssert_True(!exists_file("/cf/stor/tlm/tlm_08E0_00000004.bin"), "no fifth file");
    if (get_hk(&h))
    {
        UtAssert_True(h.used == 3 * 964 + 364 && h.used == tlm_bytes_on_disk(), "TelemetryBytesUsed %u, expected %u (disk %u)", h.used,
                      3 * 964 + 364, tlm_bytes_on_disk());
        UtAssert_True(h.open == 1 && h.stored == 10, "OpenPacketFiles %u, PacketsStored %u", h.open, h.stored);
        UtAssert_True(n_open_total() == 1, "exactly one OSAL file is still open (%u)", n_open_total());
    }
    /* Exactly filling MaxPacketFileBytes is allowed: 64 + 4 * 240 == 1024. */
    for (i = 0; i < 9; i++) send_tlm(T_MID_B, 240, (uint32)i);
    nb = mid_files(T_MID_B, idx, 8);
    UtAssert_True(nb == 3, "%d files for the second MID, expected 3", nb);
    UtAssert_True(exists_file("/cf/stor/tlm/tlm_08E1_00000000.bin") && exists_file("/cf/stor/tlm/tlm_08E1_00000001.bin") &&
                      exists_file("/cf/stor/tlm/tlm_08E1_00000002.bin"),
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
    send_tlm(T_MID_A, 300, 20);
    nb = mid_files(T_MID_A, idx, 8);
    UtAssert_True(nb == 5 && exists_file("/cf/stor/tlm/tlm_08E0_00000004.bin"),
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
    mf_put("/cf/stor/tlm/tlm_08E0_00000007.bin", 1000, 1);
    mf_put("/cf/stor/tlm/tlm_08E1_00000003.bin", 500, 2);
    mf_put("/cf/stor/files/x/y.bin", 700, 3);
    mf_put("/cf/stor/files/z.bin", 300, 4);
    if (!boot(&t)) return;
    UtAssert_True(count_ev(EID_DIR_ERR, 0) == 0, "existing roots are not an error");
    if (get_hk(&h))
    {
        UtAssert_True(h.used == 1500, "TelemetryBytesUsed %u initialized from the existing packet files (1500)", h.used);
        UtAssert_True(h.fused == 1000, "FileBytesUsed %u initialized from the existing files (1000)", h.fused);
    }
    send_tlm(T_MID_A, 300, 0);
    UtAssert_True(exists_file("/cf/stor/tlm/tlm_08E0_00000008.bin"), "sequence continues from the highest existing sequence of the MID plus one");
    send_tlm(T_MID_B, 300, 0);
    UtAssert_True(exists_file("/cf/stor/tlm/tlm_08E1_00000004.bin"), "sequence numbers are per MID");
    if (get_hk(&h)) UtAssert_True(h.used == 1500 + 2 * (64 + 300), "TelemetryBytesUsed %u after two packets", h.used);
}

/* Purge: A (priority 5), B (200), C (100); quota 16384, threshold 50 percent (8192), files of 964 bytes. */
static void Test_PurgeOrder(void)
{
    tbl_t t;
    hk_t h;
    int i, k, nb, nc, na, idx[64];
    char first_missing_seen[NAME_MAX_TEST] = "";
    bool lowest_first = true, none_lost_high = true;
    int open_after;
    tbl_default(&t);
    tbl_sizes(&t, 16384, 1048576, 1024, 50);
    tbl_entry(&t, 0, T_MID_A, 5, 1);
    tbl_entry(&t, 1, T_MID_B, 200, 1);
    tbl_entry(&t, 2, T_MID_C, 100, 1);
    nseen = 0;
    if (!boot(&t)) return;
    /* B: two files, C: one file, all closed: 2892 bytes, below the 8192 limit. */
    for (i = 0; i < 6; i++) send_tlm_snap(T_MID_B, 300, (uint32)i);
    for (i = 0; i < 3; i++) send_tlm_snap(T_MID_C, 300, (uint32)i);
    send_simple(CC_CLOSE);
    UtAssert_UINT32_EQ(n_remove, 0);
    nb = mid_files(T_MID_B, idx, 8);
    nc = mid_files(T_MID_C, idx, 8);
    UtAssert_True(nb == 2 && nc == 1, "setup: %d files for B, %d for C", nb, nc);
    /* A: eleven files of 964 bytes would need 10604 bytes: the oldest A files must be purged. */
    for (i = 0; i < 33; i++) send_tlm_snap(T_MID_A, 300, (uint32)i);
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
    /* the A files that are gone must be the ones with the lowest sequence numbers */
    {
        int present_seen = 0;
        for (k = 0; k < nseen; k++)
        {
            if (strncmp(seen[k], "/cf/stor/tlm/tlm_08E0_", 22) != 0) continue;
        }
        (void)first_missing_seen;
        for (k = 0; k < nseen; k++)
        {
            int j;
            if (strncmp(seen[k], "/cf/stor/tlm/tlm_08E0_", 22) != 0) continue;
            if (!exists_file(seen[k]))
            {
                for (j = 0; j < nseen; j++)
                    if (strncmp(seen[j], "/cf/stor/tlm/tlm_08E0_", 22) == 0 && exists_file(seen[j]) && strcmp(seen[j], seen[k]) < 0)
                        lowest_first = false;
            }
            else
            {
                present_seen++;
            }
        }
        UtAssert_True(lowest_first, "purged files are the lowest sequence numbers of the lowest priority");
        UtAssert_True(present_seen == na, "every remaining A file was seen");
    }
    /* The open file is never purged, even when over the threshold. */
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
    /* Four files of 964 bytes fit (3856); the next file needs 64 + 300 bytes and would exceed 4096. */
    for (i = 0; i < 12; i++) send_tlm(T_MID_A, 300, (uint32)i);
    UtAssert_UINT32_EQ((uint32)count_ev(EID_QUOTA, 0), 0);
    for (i = 12; i < 20; i++) send_tlm(T_MID_A, 300, (uint32)i);
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
    /* Another entry is reported separately: its first drop emits the event for that entry. */
    for (i = 0; i < 2; i++) send_tlm(T_MID_B, 300, (uint32)i);
    UtAssert_True(count_ev(EID_QUOTA, 0) == 2, "event 13 is reported once per entry (%d)", count_ev(EID_QUOTA, 0));
    /* A lower threshold frees space; the next packet stores again and a later drop reports again. */
    UtAssert_True(cmd_thr(30), "SET_PURGE_THRESHOLD 30");
    if (!get_hk(&h)) return;
    UtAssert_True(h.purged > 0 && h.used <= 4096 * 30 / 100, "purge after SET_PURGE_THRESHOLD: FilesPurged %u used %u", h.purged, h.used);
    dropped0 = h.dropped;
    ev0 = count_ev(EID_QUOTA, 0);
    send_tlm(T_MID_A, 300, 100);
    if (get_hk(&h)) UtAssert_True(h.dropped == dropped0, "stores again after space was freed (dropped %u -> %u)", dropped0, h.dropped);
    UtAssert_UINT32_EQ((uint32)count_ev(EID_QUOTA, 0), (uint32)ev0);
    cmd_thr(100);
    for (i = 0; i < 20; i++) send_tlm(T_MID_A, 300, (uint32)(200 + i));
    UtAssert_True(count_ev(EID_QUOTA, 0) == ev0 + 1, "a new drop episode reports event 13 again (%d -> %d)", ev0, count_ev(EID_QUOTA, 0));
}

static void Test_SetParamCommands(void)
{
    hk_t a, b;
    est_t e[MAX_ENT];
    int n = 0;
    const est_t *x;
    if (!boot(&tbl_active)) return;
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
    const int cc_with_payload[] = {CC_SET_PRI, CC_SET_DEC, CC_SET_THR, CC_ADD_FILE, CC_DEL_FILE, CC_LIST_DIR};
    int i;
    if (!boot(&tbl_active)) return;
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
    if (get_hk(&b)) UtAssert_True(b.cec >= a.cec + 12, "error counter %u -> %u (at least +12)", a.cec, b.cec);
    send_tlm(T_MID_UNKNOWN, 100, 0);
    UtAssert_True(count_ev(EID_MID_ERR, 0) == 1, "a packet with an unknown message ID emits event 5 (%d)", count_ev(EID_MID_ERR, 0));
    UtAssert_True(last_ev_type(EID_MID_ERR) == CFE_EVS_EventType_ERROR, "event 5 is ERROR");
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
    for (i = 0; i < 3; i++) send_tlm(T_MID_A, 300, (uint32)i);
    for (i = 0; i < 4; i++) send_tlm(T_MID_B, 300, (uint32)i);
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
                      memcmp(mf[mf_find("/cf/stor/files/a/b/copy.bin")].data, mf[mf_find("/cf/src.bin")].data, 3000) == 0,
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
    /* exact fit: 3000 + 5192 == 8192 */
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
    if (!boot(&tbl_active)) return;
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
    for (i = 0; i < ntx; i++) pk += tx[i].mid == T_LIST_MID;
    UtAssert_UINT32_EQ((uint32)pk, 0);
}

static void Test_Listings(void)
{
    lpkt_t p[8];
    int first, n, i, ev0;
    if (!boot(&tbl_active)) return;

    first = ntx;
    UtAssert_True(send_simple(CC_LIST_ALL), "LIST_ALL of an empty file area");
    n = get_list(first, p, 8);
    UtAssert_True(n == 1 && p[0].total == 0 && p[0].count == 0 && p[0].index == 0, "an empty listing is one packet with TotalEntries 0 (%d packets)", n);
    UtAssert_True(count_ev(EID_LIST_OK, 0) == 1 && last_ev_type(EID_LIST_OK) == CFE_EVS_EventType_INFORMATION, "event 26 is INFORMATION");

    /* root = [z.txt, d1/, y.txt, empty/], d1 = [a.bin, sub/, b.bin], d1/sub = [deep.bin] in this creation order */
    mf_put("/cf/stor/files/z.txt", 11, 1);
    mf_mkdir_p("/cf/stor/files/d1");
    mf_put("/cf/stor/files/d1/a.bin", 33, 3);
    mf_mkdir_p("/cf/stor/files/d1/sub");
    mf_put("/cf/stor/files/d1/sub/deep.bin", 44, 4);
    mf_put("/cf/stor/files/d1/b.bin", 55, 5);
    mf_put("/cf/stor/files/y.txt", 22, 2);
    mf_mkdir_p("/cf/stor/files/empty");

    first = ntx;
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

    first = ntx;
    cmd_path(CC_LIST_DIR, "");
    n = get_list(first, p, 8);
    UtAssert_True(n == 1 && p[0].total == 4 && p[0].count == 4, "LIST_DIR of the root: %d packets, total %u", n, n ? p[0].total : 0);
    if (n == 1)
        UtAssert_True(strcmp(p[0].name[0], "z.txt") == 0 && strcmp(p[0].name[1], "d1/") == 0 && p[0].size[1] == 0 &&
                          strcmp(p[0].name[2], "y.txt") == 0 && strcmp(p[0].name[3], "empty/") == 0,
                      "root entries in directory order, directories with / and size 0: %s %s %s %s", p[0].name[0], p[0].name[1],
                      p[0].name[2], p[0].name[3]);
    first = ntx;
    cmd_path(CC_LIST_DIR, "d1");
    n = get_list(first, p, 8);
    UtAssert_True(n == 1 && p[0].total == 3 && p[0].count == 3 && strcmp(p[0].name[0], "a.bin") == 0 && strcmp(p[0].name[1], "sub/") == 0 &&
                      strcmp(p[0].name[2], "b.bin") == 0,
                  "LIST_DIR d1 has a.bin, sub/, b.bin");
    first = ntx;
    cmd_path(CC_LIST_DIR, "empty");
    n = get_list(first, p, 8);
    UtAssert_True(n == 1 && p[0].total == 0 && p[0].count == 0, "an empty directory is one packet with TotalEntries 0");

    ev0 = nev;
    first = ntx;
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
    first = ntx;
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
    int n = 0, i, unsub_a = 0, sub_d = 0;
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
    UtAssert_True(tbl_submit(&t) == CFE_SUCCESS, "the new table passes the application's validation");
    send_wakeup();
    send_wakeup();
    if (!get_hk(&h)) return;
    for (i = 0; i < nunsubs; i++) unsub_a += unsubs[i] == T_MID_A;
    for (i = 0; i < nsubs; i++) sub_d += subs[i] == T_MID_D;
    UtAssert_True(unsub_a >= 1, "an entry no longer in the table is unsubscribed");
    UtAssert_True(sub_d >= 1, "a new table entry is subscribed");
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
    UtAssert_True(tbl_submit(&t) == CFE_SUCCESS, "second table accepted");
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
    send_tlm(T_MID_A, 300, 0);
    for (i = 0; i < 4; i++) send_wakeup();
    send_tlm(T_MID_B, 300, 0);     /* four seconds after A */
    for (i = 0; i < 5; i++) send_wakeup();
    if (get_hk(&h)) UtAssert_True(h.open == 2, "both files stay open for less than 10 idle seconds (%u open at 9 s)", h.open);
    send_wakeup();                 /* A has been idle for 10 s */
    send_wakeup();
    if (get_hk(&h)) UtAssert_True(h.open == 1, "the file idle for 10 s is closed on WAKEUP, the other stays open (%u open)", h.open);
    for (i = 0; i < 4; i++) send_wakeup();
    if (get_hk(&h)) UtAssert_True(h.open == 0, "all idle files are closed (%u open)", h.open);
    UtAssert_True(n_open_total() == 0, "OSAL files are closed");
    send_tlm(T_MID_A, 300, 1);
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
    send_tlm(T_MID_A, 300, 0);
    UtAssert_True(count_ev(EID_FILE_WRITE, 0) >= 1, "a packet file that cannot be created emits event 15");
    UtAssert_True(last_ev_type(EID_FILE_WRITE) == CFE_EVS_EventType_ERROR, "event 15 is ERROR");
    send_tlm(T_MID_A, 300, 1);
    if (get_hk(&h))
        UtAssert_True(h.used == tlm_bytes_on_disk(), "TelemetryBytesUsed %u matches the files on disk %u after the failure", h.used,
                      tlm_bytes_on_disk());
    fi_write_short = true;
    ev0 = count_ev(EID_FILE_WRITE, 0);
    send_tlm(T_MID_A, 300, 2);
    UtAssert_True(count_ev(EID_FILE_WRITE, 0) > ev0, "a failed packet write emits event 15");
    fi_write_short = false;
    UtAssert_True(get_hk(&h), "STOR still answers SEND_HK after write failures");
}

void UtTest_Setup(void)
{
    sem_init(&sem_app, 0, 0);
    sem_init(&sem_test, 0, 0);
    UtAssert_True(sizeof(CFE_MSG_TelemetryHeader_t) == TLM_HDR, "telemetry header is %u bytes", (unsigned)sizeof(CFE_MSG_TelemetryHeader_t));
    UtAssert_True(sizeof(CFE_FS_Header_t) == FS_HDR, "file header is %u bytes", (unsigned)sizeof(CFE_FS_Header_t));
    UtTest_Add(Test_StartupAndHousekeeping, Setup, Teardown, "Startup and housekeeping");
    UtTest_Add(Test_StartupErrors, Setup, Teardown, "Startup error events");
    UtTest_Add(Test_TableValidation, Setup, Teardown, "Table validation");
    UtTest_Add(Test_DecimationAndStats, Setup, Teardown, "Decimation and statistics");
    UtTest_Add(Test_RolloverAndNaming, Setup, Teardown, "Packet files and roll-over");
    UtTest_Add(Test_SequenceAndScan, Setup, Teardown, "Startup scan and sequence");
    UtTest_Add(Test_PurgeOrder, Setup, Teardown, "Purge order");
    UtTest_Add(Test_PurgePriorityChange, Setup, Teardown, "Purge after priority change");
    UtTest_Add(Test_QuotaDrop, Setup, Teardown, "Telemetry quota drops");
    UtTest_Add(Test_SetParamCommands, Setup, Teardown, "Parameter commands");
    UtTest_Add(Test_CommandErrors, Setup, Teardown, "Command errors");
    UtTest_Add(Test_CommandCounters, Setup, Teardown, "Command counters");
    UtTest_Add(Test_ResetCounters, Setup, Teardown, "Reset counters");
    UtTest_Add(Test_FileAddDelete, Setup, Teardown, "File add and delete");
    UtTest_Add(Test_PathRejection, Setup, Teardown, "Path rejection");
    UtTest_Add(Test_Listings, Setup, Teardown, "Listing packets");
    UtTest_Add(Test_TableUpdate, Setup, Teardown, "Table update");
    UtTest_Add(Test_IdleClose, Setup, Teardown, "Idle close");
    UtTest_Add(Test_WriteFailures, Setup, Teardown, "File write failures");
}
