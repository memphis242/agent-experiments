/* Deterministic S3.6 vectors drive the production entry point and socket queue. */
#define SOURCE_MID 0x08D1u

static void serial_frame(frame_t *f, uint16 source, uint32 serial, const uint8 *key)
{
    f->clen = t5_build_cmd(f->cmd, SAMPLE_CMD_MID, 0, NULL, 0);
    f->n = t5_frame(f->f, f->cmd, f->clen, key, source, t5_now_sec, serial);
}

static void expect_serial(uint16 source, uint32 serial, bool accepted)
{
    frame_t f;
    serial_frame(&f, source, serial, KEY);
    UtAssert_True(frame_executes(&f) == accepted, "source %u serial %u expected %s", source,
                  (unsigned)serial, accepted ? "accept" : "reject");
}

static const t5_tx_t *source_stats(void)
{
    const int before = t5_ntx;
    const t5_tx_t *t;
    cmd(5, NULL, 0);
    t = t5_last_tx(SOURCE_MID, before);
    UtAssert_True(t != NULL, "SEND_SOURCE_STATS emits SOURCE_TLM");
    if (t == NULL) return NULL;
    UtAssert_True(t->size == 304, "SOURCE_TLM contains all nine 32-byte rows (304 bytes with header)");
    return t->size == 304 ? t : NULL;
}

static void Test_ReplayBoundaries(void)
{
    hk_t h;
    if (!boot_default()) return;
    expect_serial(1, 0, true);
    expect_serial(1, 0, false);
    expect_serial(1, 100, true);
    expect_serial(1, 37, true);
    expect_serial(1, 36, false);
    expect_serial(1, 100, false);
    expect_serial(1, 100u + 0x80000000u, false);
    expect_serial(1, 1000, true);
    expect_serial(1, 100, false);
    expect_serial(2, 0xFFFFFFFFu, true);
    expect_serial(2, 0, true);
    expect_serial(2, 0xFFFFFFFFu, false);
    if (get_hk(&h)) UtAssert_True(h.rx == 12 && h.acc == 6 && h.replay == 6 && h.slots == 2,
        "replay/window/wrap totals received12 accepted6 replay6 slots2 (%u/%u/%u/%u)",
        (unsigned)h.rx, (unsigned)h.acc, (unsigned)h.replay, h.slots);
    UtAssert_True(t5_count_ev(EID_REJECT, 0) == 6 && t5_ev_has(EID_REJECT, 0, "REPLAY", NULL, NULL),
                  "each replay emits exactly one REPLAY error");
}

static void Test_ReplayLifetimeAndFailures(void)
{
    frame_t f;
    vt_t table;
    hk_t h;
    int status;
    if (!boot_default()) return;
    for (status = 1; status <= 5; status++)
    {
        serial_frame(&f, 1, 7, KEY);
        cmdval_force_status = status;
        frame_executes(&f);
        cmdval_force_status = -1;
        if (get_hk(&h)) UtAssert_True(h.slots == 0, "validation failure %d allocates no replay slot", status);
    }
    expect_serial(1, 7, true);
    cmd(CC_RESET, NULL, 0);
    expect_serial(1, 7, false);
    if (get_hk(&h)) UtAssert_True(h.replay == 1 && h.slots == 1, "RESET retains slot identity and seen bitmap");
    /* CMDI's invalid function code is an app rejection after successful ingress. */
    mkframe(&f, CMDI_CMD_MID, 99, NULL, 0, 1, 0, KEY);
    f.n = t5_frame(f.f, f.cmd, f.clen, KEY, 1, t5_now_sec, 8);
    frame_executes(&f);
    expect_serial(1, 8, false);
    vt_default(&table);
    UtAssert_True(t5_tbl_submit("ValTable", table.b) == CFE_SUCCESS, "valid table activates");
    wakeup();
    expect_serial(1, 7, true);
    {
        const t5_tx_t *stats = source_stats();
        int col;
        if (stats != NULL) for (col = 0; col < 7; col++)
            UtAssert_True(t5_get32(stats->bytes + 20 + col * 4) == (col == 0 ? 1u : 0u), "table activation resets source counter%d", col);
    }
    if (get_hk(&h)) UtAssert_True(h.slots == 1, "table activation clears replay state before first new source");
}

static void Test_RuntimeSlots(void)
{
    vt_t table;
    frame_t f;
    hk_t h;
    uint16 source;
    vt_default(&table); table.b[52] = 0x0B;
    if (!boot(&table)) return;
    expect_serial(0, 0, false);
    serial_frame(&f, 9, 5, KEY); bad_mac(&f); frame_executes(&f);
    for (source = 1; source <= 8; source++) expect_serial(source, 0, true);
    expect_serial(9, 5, false);
    expect_serial(8, 1, true);
    if (get_hk(&h)) UtAssert_True(h.slots == 8 && h.src == 2 && h.hmac == 1 && h.acc == 9,
        "SOURCE disabled still forbids zero and ninth source; failed validation uses no slot");
}

static void Test_NextKeyValidation(void)
{
    vt_t table;
    if (!boot_default()) return;
    vt_default(&table);
    table.b[88] = 2;
    UtAssert_True(t5_tbl_validate("ValTable", table.b) != CFE_SUCCESS, "NextValid2 rejected");
    table.b[88] = 1;
    UtAssert_True(t5_tbl_validate("ValTable", table.b) != CFE_SUCCESS, "NEXT valid with zero key rejected");
    memcpy(table.b + 56, KEY, 32);
    UtAssert_True(t5_tbl_validate("ValTable", table.b) != CFE_SUCCESS, "NEXT equal ACTIVE rejected");
    memcpy(table.b + 56, KEY2, 32);
    UtAssert_True(t5_tbl_validate("ValTable", table.b) == CFE_SUCCESS, "different nonzero NEXT accepted");
    table.b[88] = 0;
    UtAssert_True(t5_tbl_validate("ValTable", table.b) != CFE_SUCCESS, "NextValid0 requires zero NEXT");
}

static void Test_KeySwitchAndMandatoryAuthentication(void)
{
    vt_t table;
    frame_t f;
    hk_t h;
    const t5_tx_t *stats;
    vt_default(&table); memcpy(table.b + 56, KEY2, 32); table.b[88] = 1;
    if (!boot(&table)) return;
    serial_frame(&f, 1, 10, KEY2);
    UtAssert_True(!frame_executes(&f), "NEXT never validates an ordinary frame");
    expect_serial(1, 10, true);
    cmd_mask(0x0D);
    mkframe(&f, CMDI_CMD_MID, 4, NULL, 0, 1, 0, KEY2);
    f.n = t5_frame(f.f, f.cmd, f.clen, KEY2, 1, t5_now_sec, 10);
    UtAssert_True(!frame_executes(&f), "SWITCH_KEY with HMAC disabled still requires ACTIVE HMAC");
    if (get_hk(&h)) UtAssert_True(h.next_valid == 1 && h.switches == 0 && h.hmac == 2,
        "failed mandatory HMAC changes no keys and counts HMAC before replay");
    f.n = t5_frame(f.f, f.cmd, f.clen, KEY, 1, t5_now_sec, 11);
    frame_executes(&f);
    if (get_hk(&h)) UtAssert_True(h.next_valid == 0 && h.switches == 1, "valid ACTIVE switch consumes NEXT");
    UtAssert_True(modified_calls >= 1, "key switch notifies CFE_TBL_Modified");
    cmd_mask(0x0F);
    serial_frame(&f, 1, 12, KEY); UtAssert_True(!frame_executes(&f), "retired key rejected");
    serial_frame(&f, 1, 10, KEY2); UtAssert_True(!frame_executes(&f), "new key cannot replay consumed serial10");
    serial_frame(&f, 1, 11, KEY2); UtAssert_True(!frame_executes(&f), "switch serial itself remains consumed");
    serial_frame(&f, 1, 12, KEY2); UtAssert_True(frame_executes(&f), "fresh serial under ACTIVE accepted");
    cmd(4, NULL, 0);
    UtAssert_True(t5_count_ev(17, 0) == 1, "SWITCH_KEY without NEXT emits KEY_ERR");
    if (get_hk(&h)) UtAssert_True(h.replay == 2 && h.slots == 1 && h.switches == 1, "switch retains replay lifetime");
    stats = source_stats();
    {
        int i;
        for (i = 0; i < t5_nev; i++) UtAssert_True(strstr(t5_ev[i].text, "another-32-byte-key-for-cmdi-ut!") == NULL, "events contain no NEXT material");
    }
    if (stats != NULL) UtAssert_True(t5_get32(stats->bytes + 20) == 3, "source1 Accepted counts ordinary/switch/fresh exactly");
    {
        const int k = t5_tbl_find("ValTable");
        UtAssert_True(k >= 0, "shared working table exists");
        /* Validate a copy read through public Table Services, never private app state. */
        if (k >= 0)
        {
            void *address = NULL;
            const int32 rc = CFE_TBL_GetAddress(&address, (CFE_TBL_Handle_t)(k + 1));
            if (rc == CFE_SUCCESS || rc == CFE_TBL_INFO_UPDATED)
            {
                const uint8 *body = address;
                UtAssert_True(memcmp(body, KEY2, 32) == 0 && body[88] == 0,
                              "SWITCH_KEY updates ACTIVE and NextValid in shared working copy");
                UtAssert_True(memcmp(body + 56, (uint8[32]){0}, 32) == 0, "shared NEXT bytes zero after switch");
                UtAssert_True(body[52] == 0x0F && t5_get32(body + 48) == 30, "SWITCH_KEY preserves shared mask/age despite runtime mask override");
                CFE_TBL_ReleaseAddress((CFE_TBL_Handle_t)(k + 1));
            }
            else UtAssert_Failed("working-copy address unavailable");
        }
    }
}

static void Test_SourceStatistics(void)
{
    frame_t f;
    const t5_tx_t *stats;
    int status, row, col;
    if (!boot_default()) return;
    expect_serial(1, 1, true);
    for (status = 1; status <= 5; status++)
    {
        serial_frame(&f, 1, 2, KEY); cmdval_force_status = status; frame_executes(&f);
    }
    cmdval_force_status = -1;
    expect_serial(1, 1, false);
    serial_frame(&f, 7, 0, KEY); frame_executes(&f); frame_executes(&f);
    inject("abcde", 5); wakeup();
    /* A readable listed source on a FORMAT error belongs to its listed row. */
    serial_frame(&f, 2, 0, KEY); f.f[0] = 0; inject_frame(&f); wakeup();
    stats = source_stats();
    if (stats == NULL) return;
    for (row = 0; row < 9; row++)
    {
        const uint8 *p = stats->bytes + 16 + row * 32;
        UtAssert_True(t5_get16(p) == (row < 2 ? row + 1 : 0) && t5_get16(p + 2) == 0,
                      "source row%d identity and zero spare", row);
        for (col = 0; col < 7; col++)
        {
            const uint32 expected = row == 0 ? 1 : row == 1 ? (col == 1) : row == 8 ? (col == 1 ? 1 : col == 4 ? 2 : 0) : 0;
            UtAssert_True(t5_get32(p + 4 + col * 4) == expected, "source row%d counter%d expected%u", row, col, (unsigned)expected);
        }
    }
    cmd(CC_RESET, NULL, 0);
    stats = source_stats();
    if (stats != NULL)
        for (row = 0; row < 9; row++) for (col = 0; col < 7; col++)
            UtAssert_True(t5_get32(stats->bytes + 20 + row * 32 + col * 4) == 0, "RESET clears row%d counter%d", row, col);
}

static void Test_SameWakeupSelfCommands(void)
{
    vt_t table;
    frame_t a, b, c;
    uint8 payload[4] = {0};
    hk_t h;
    int before;
    vt_default(&table); memcpy(table.b + 56, KEY2, 32); table.b[88] = 1;
    if (!boot(&table)) return;
    mkframe(&a, CMDI_CMD_MID, 4, NULL, 0, 1, 0, KEY);
    mkframe(&b, SAMPLE_CMD_MID, 0, NULL, 0, 1, 0, KEY);
    mkframe(&c, SAMPLE_CMD_MID, 0, NULL, 0, 1, 0, KEY2);
    inject_frame(&a); inject_frame(&b); inject_frame(&c);
    before = t5_ntx; wakeup();
    UtAssert_True(t5_tx_count(SAMPLE_CMD_MID, before) == 1, "same wakeup rejects retired and executes fresh key");
    if (get_hk(&h)) UtAssert_True(h.hmac == 1 && h.switches == 1 && h.acc == 2, "key self-command dispatch precedes next queued datagram");
    payload[0] = 0x0D;
    mkframe(&a, CMDI_CMD_MID, CC_SET_MASK, payload, 4, 1, 0, KEY2);
    mkframe(&b, SAMPLE_CMD_MID, 0, NULL, 0, 1, 0, KEY2); bad_mac(&b);
    inject_frame(&a); inject_frame(&b); before = t5_ntx; wakeup();
    UtAssert_True(t5_tx_count(SAMPLE_CMD_MID, before) == 1, "same wakeup mask update applies immediately");
    t5_put32(payload, 120);
    mkframe(&a, CMDI_CMD_MID, CC_SET_MAX_AGE, payload, 4, 1, 0, KEY2);
    mkframe(&b, SAMPLE_CMD_MID, 0, NULL, 0, 1, -90, KEY2);
    inject_frame(&a); inject_frame(&b); before = t5_ntx; wakeup();
    UtAssert_True(t5_tx_count(SAMPLE_CMD_MID, before) == 1, "same wakeup age update accepts newly valid queued frame");
}

static void Register_Expanded(void)
{
    UtTest_Add(Test_ReplayBoundaries, Setup, Teardown, "Replay boundaries and wrap");
    UtTest_Add(Test_ReplayLifetimeAndFailures, Setup, Teardown, "Replay lifetime and early failures");
    UtTest_Add(Test_RuntimeSlots, Setup, Teardown, "Runtime source slots");
    UtTest_Add(Test_NextKeyValidation, Setup, Teardown, "NEXT key validation");
    UtTest_Add(Test_KeySwitchAndMandatoryAuthentication, Setup, Teardown, "Key switch authentication and shared state");
    UtTest_Add(Test_SourceStatistics, Setup, Teardown, "Nine-row source statistics");
    UtTest_Add(Test_SameWakeupSelfCommands, Setup, Teardown, "Same-wakeup self-command ordering");
}
