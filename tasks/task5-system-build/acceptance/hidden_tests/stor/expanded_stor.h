/* S1.6 quota and file-information checks use the production SB interface. */
static bool quota(uint32 telemetry, uint32 files)
{
    uint8 p[8]; t5_put32(p, telemetry); t5_put32(p + 4, files);
    return send_cmd(T_CMD_MID, 13, p, sizeof(p));
}

static const t5_tx_t *file_info(uint8 area, const char *path)
{
    uint8 p[68] = {0};
    const int before = t5_ntx;
    p[0] = area; pstr(p + 4, path);
    send_cmd(T_CMD_MID, 14, p, sizeof(p));
    return t5_last_tx(0x08C8, before);
}

static void Test_QuotaAtomicityAndActivation(void)
{
    tbl_t t;
    hk_t a, b;
    tbl_quiet(&t, 8192);
    if (!boot(&t)) return;
    mf_put("/cf/source.bin", 3000, 5); cmd_add("/cf/source.bin", "keep.bin");
    get_hk(&a);
    quota(2048, 0); quota(1023, 4096);
    if (get_hk(&b)) UtAssert_True(b.quota == a.quota && b.fquota == a.fquota && b.cec == a.cec + 2 && b.cc == a.cc,
        "invalid file quota0 or telemetry<MaxPacketFileBytes rejects both values atomically");
    quota(1024, 1);
    if (get_hk(&b)) UtAssert_True(b.quota == 1024 && b.fquota == 1 && b.fused == 3000 && b.fcount == 1,
        "lower-than-used file quota is accepted and existing files remain");
    UtAssert_True(exists_file("/cf/stor/files/keep.bin"), "file-area quota reduction preserves existing content");
    cmd_add("/cf/source.bin", "rejected.bin");
    UtAssert_True(!exists_file("/cf/stor/files/rejected.bin"), "new addition immediately follows reduced quota");
    send_pkt(T_MID_A, 700, 1); send_pkt(T_MID_A, 700, 2);
    if (get_hk(&b)) UtAssert_True(b.dropped == 1 && b.stored == 1, "reduced telemetry quota drops while its only file is open");
    UtAssert_True(t5_tbl_submit("StorageTable", t.b) == CFE_SUCCESS, "valid table activates"); send_wakeup();
    send_pkt(T_MID_A, 700, 3);
    if (get_hk(&b)) UtAssert_True(b.quota == 262144 && b.fquota == 8192 && b.stored == 2,
        "table activation restores both quotas and permits storage again");
    send_simple(CC_CLOSE); quota(1024, 8192);
    if (get_hk(&b)) UtAssert_True(b.used <= 1024 && b.purged > 0, "valid reduction immediately purges closed telemetry files");
}

static void Test_FileInfoCrcAndNoMutation(void)
{
    tbl_t t;
    hk_t a, b;
    uint8 binary[257];
    const t5_tx_t *reply;
    const char *paths[] = {"empty.bin", "digits.bin", "binary.bin"};
    const uint8 *contents[3];
    const size_t lengths[] = {0, 9, sizeof(binary)};
    int i, j;
    tbl_quiet(&t, 8192);
    if (!boot(&t)) return;
    for (i = 0; i < (int)sizeof(binary); i++) binary[i] = (uint8)i;
    contents[0] = (const uint8 *)""; contents[1] = (const uint8 *)"123456789"; contents[2] = binary;
    mf_put_bytes("/cf/stor/files/empty.bin", "", 0);
    mf_put_bytes("/cf/stor/files/digits.bin", "123456789", 9);
    mf_put_bytes("/cf/stor/files/binary.bin", binary, sizeof(binary));
    get_hk(&a);
    for (i = 0; i < 3; i++)
    {
        reply = file_info(0, paths[i]);
        UtAssert_True(reply != NULL && reply->size == 92, "FILE_INFO %s returns full92-byte packet", paths[i]);
        if (reply != NULL && reply->size == 92)
        {
            const uint8 *p = reply->bytes + 16;
            const uint32 expected = i == 1 ? 0xCBF43926u : crc32_iso_hdlc(contents[i], lengths[i]);
            UtAssert_True(p[0] == 0 && strcmp((const char *)p + 4, paths[i]) == 0 && t5_get32(p + 68) == lengths[i] &&
                t5_get32(p + 72) == expected, "FILE_INFO identity/size/ISO-HDLC CRC for %s", paths[i]);
            for (j = 1; j < 4; j++) UtAssert_True(p[j] == 0, "FILE_INFO spare byte%d zero", j);
        }
    }
    if (get_hk(&b)) UtAssert_True(b.xstate == a.xstate && b.rxstate == a.rxstate && b.xid == a.xid && b.fused == a.fused && b.fcount == a.fcount,
        "FILE_INFO changes no file or transfer state");
    UtAssert_True(memcmp(data_of("/cf/stor/files/binary.bin"), binary, sizeof(binary)) == 0, "FILE_INFO never mutates bytes");
    UtAssert_True(count_ev(37, 0) == 3 && last_ev_type(37) == CFE_EVS_EventType_INFORMATION, "FILE_INFO success emits event37 INFORMATION");
    send_pkt(T_MID_A, 300, 1); send_simple(CC_CLOSE);
    reply = file_info(1, "tlm_18E0_00000000.bin");
    if (reply != NULL && reply->size == 92) UtAssert_True(t5_get32(reply->bytes + 88) ==
        crc32_iso_hdlc(data_of("/cf/stor/tlm/tlm_18E0_00000000.bin"), 364), "packet-file CRC includes cFE file header");
}

static void H_FileInfoOsalError(void *ud, UT_EntryKey_t key, const UT_StubContext_t *context)
{
    (void)ud; (void)context;
    t5_ret(key, OS_ERROR);
}

static void Test_FileInfoErrors(void)
{
    tbl_t t;
    uint8 p[68];
    int before, ev;
    tbl_quiet(&t, 8192);
    if (!boot(&t)) return;
    send_pkt(T_MID_A, 300, 1);
    mf_mkdir_p("/cf/stor/files/directory");
    {
        const char *invalid[] = {"../bad", "/absolute"};
        int i;
        for (i = 0; i < 2; i++)
        {
            ev = t5_nev; before = t5_ntx; file_info(0, invalid[i]);
            UtAssert_True(t5_last_tx(0x08C8, before) == NULL && count_ev(EID_PATH_ERR, ev) == 1,
                          "invalid FILE_INFO path rejects with PATH_ERR and no reply");
        }
    }
    memset(p, 'x', sizeof(p)); p[0] = 0; before = t5_ntx; ev = t5_nev;
    send_cmd(T_CMD_MID, 14, p, sizeof(p));
    UtAssert_True(t5_last_tx(0x08C8, before) == NULL && count_ev(EID_PATH_ERR, ev) == 1, "unterminated FILE_INFO path rejects PATH_ERR");
    ev = t5_nev;
    UtAssert_True(file_info(1, "not-a-packet.bin") == NULL && count_ev(EID_PATH_ERR, ev) == 1, "TLM FILE_INFO requires exactly a packet-file name");
    before = t5_ntx;
    file_info(2, "x.bin"); file_info(0, "missing.bin"); file_info(0, "directory"); file_info(1, "tlm_18E0_00000000.bin");
    UtAssert_True(t5_last_tx(0x08C8, before) == NULL, "invalid area, missing, directory and open packet file produce no reply");
    /* Invalid Area uses the same S2.2 path/area validation as TRANSFER. */
    UtAssert_True(count_ev(38, 0) == 4 && last_ev_type(38) == CFE_EVS_EventType_ERROR, "non-path FILE_INFO failures emit FILE_INFO_ERR38");
    mf_put("/cf/stor/files/read.bin", 10, 1); fi_read_fail = 1; ev = t5_nev;
    UtAssert_True(file_info(0, "read.bin") == NULL && count_ev(38, ev) == 1, "OSAL read failure produces FILE_INFO_ERR and no reply");
    fi_read_fail = 0;
    ev = t5_nev;
    UT_SetHandlerFunction(UT_KEY(OS_OpenCreate), H_FileInfoOsalError, NULL);
    UtAssert_True(file_info(0, "read.bin") == NULL && count_ev(38, ev) == 1, "OSAL open failure produces FILE_INFO_ERR and no reply");
    UT_SetHandlerFunction(UT_KEY(OS_OpenCreate), H_OS_OpenCreate, NULL);
    ev = t5_nev;
    UT_SetHandlerFunction(UT_KEY(OS_stat), H_FileInfoOsalError, NULL);
    UtAssert_True(file_info(0, "read.bin") == NULL && count_ev(38, ev) == 1, "OSAL stat failure produces FILE_INFO_ERR and no reply");
    UT_SetHandlerFunction(UT_KEY(OS_stat), H_OS_stat, NULL);
}

static void Test_PriorityByteReclassification(void)
{
    tbl_t t;
    hk_t h;
    tbl_quiet(&t, 8192);
    if (!boot(&t)) return;
    send_pkt(T_MID_A, 300, 1); send_pkt(T_MID_B, 300, 2);
    if (get_hk(&h)) UtAssert_True(h.priority[10] == 364 && h.priority[20] == 364, "open packet files belong to current priority");
    cmd_param(CC_SET_PRI, T_MID_A, 200);
    if (get_hk(&h)) UtAssert_True(h.priority[10] == 0 && h.priority[200] == 364, "SET_PRIORITY reclassifies existing files");
    send_simple(CC_CLOSE);
    cmd_transfer(AREA_TLM, 0, ACT_ARCHIVE, "tlm_18E0_00000000.bin", ""); wakeups(2);
    if (get_hk(&h)) UtAssert_True(h.priority[200] == 364, "archived files remain in current priority accounting");
    t5_put32(t.b + 144, 0); tbl_entry(&t, 1, T_MID_B, 30, 1);
    UtAssert_True(t5_tbl_submit("StorageTable", t.b) == CFE_SUCCESS, "table with removed MID accepted"); send_wakeup();
    if (get_hk(&h)) UtAssert_True(h.priority[0] == 364 && h.priority[30] == 364 && h.priority[200] == 0,
        "table activation assigns unknown MID priority0 and reclassifies retained MID");
    cmd_transfer(AREA_TLM, 0, ACT_DELETE, "tlm_18E0_00000000.arc", ""); wakeups(2);
    if (get_hk(&h)) UtAssert_True(h.priority[0] == 0 && h.used == 364, "transfer DELETE debits priority accounting");
    cmd_thr(1); quota(1024, 8192);
    if (get_hk(&h)) UtAssert_True(h.priority[30] == 0 && h.used == 0, "purge debits priority accounting");
}

static int file_info_close_errors;

static void H_FileInfoCloseError(void *ud, UT_EntryKey_t key, const UT_StubContext_t *context)
{
    const osal_id_t id = UT_Hook_GetArgValueByName(context, "filedes", osal_id_t);
    const mfd_t *descriptor = fd_of(id);
    const bool fail = file_info_close_errors == 0 && descriptor != NULL &&
        strcmp(mf[descriptor->file].path, "/cf/stor/files/close.bin") == 0;
    H_OS_close(ud, key, context);
    if (fail)
    {
        file_info_close_errors++;
        t5_ret(key, OS_ERROR);
    }
}

static void Test_FileInfoCloseFailure(void)
{
    tbl_t table;
    hk_t before, after;
    uint8 content[17] = {0};
    const t5_tx_t *reply;
    int i, event_start, open_before;
    tbl_quiet(&table, 8192);
    if (!boot(&table)) return;
    for (i = 0; i < (int)sizeof(content); i++) content[i] = (uint8)(i * 13);
    mf_put_bytes("/cf/stor/files/close.bin", content, sizeof(content));
    if (!get_hk(&before)) return;
    open_before = (int)n_open_total();
    event_start = t5_nev;
    file_info_close_errors = 0;
    UT_SetHandlerFunction(UT_KEY(OS_close), H_FileInfoCloseError, NULL);
    reply = file_info(AREA_FILE, "close.bin");
    UT_SetHandlerFunction(UT_KEY(OS_close), H_OS_close, NULL);
    UtAssert_True(file_info_close_errors == 1, "FILE_INFO close-error stimulus reaches its source-file descriptor exactly once");
    UtAssert_True(reply == NULL && count_ev(38, event_start) == 1 && count_ev(37, event_start) == 0 &&
        last_ev_type(38) == CFE_EVS_EventType_ERROR, "OSAL close failure emits one FILE_INFO_ERR and no success event or reply");
    if (get_hk(&after)) UtAssert_True(after.cec == (uint8)(before.cec + 1) && after.cc == before.cc &&
        after.xstate == before.xstate && after.rxstate == before.rxstate && after.xid == before.xid &&
        after.fused == before.fused && after.fcount == before.fcount && after.used == before.used,
        "FILE_INFO close failure counts one rejected command without changing transfer or byte accounting");
    UtAssert_True(size_of_file("/cf/stor/files/close.bin") == sizeof(content) &&
        memcmp(data_of("/cf/stor/files/close.bin"), content, sizeof(content)) == 0,
        "FILE_INFO close failure leaves source size and contents unchanged");
    UtAssert_True((int)n_open_total() == open_before, "close-error fixture leaves no retained source descriptor");
    reply = file_info(AREA_FILE, "close.bin");
    UtAssert_True(reply != NULL && reply->size == 92 && t5_get32(reply->bytes + 84) == sizeof(content) &&
        t5_get32(reply->bytes + 88) == crc32_iso_hdlc(content, sizeof(content)), "FILE_INFO remains usable after the reported close failure");
}

static void Register_Expanded(void)
{
    UtTest_Add(Test_FileInfoCloseFailure, Setup, Teardown, "S1 FILE_INFO close failure");
    UtTest_Add(Test_QuotaAtomicityAndActivation, Setup, Teardown, "S1 Quota atomicity and activation");
    UtTest_Add(Test_FileInfoCrcAndNoMutation, Setup, Teardown, "S1 FILE_INFO CRC and no mutation");
    UtTest_Add(Test_FileInfoErrors, Setup, Teardown, "S1 FILE_INFO errors");
    UtTest_Add(Test_PriorityByteReclassification, Setup, Teardown, "S1 Priority byte reclassification");
}
