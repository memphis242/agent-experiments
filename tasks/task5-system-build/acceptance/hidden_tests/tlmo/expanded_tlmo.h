/* S4.4 packet statistics are observed at the production telemetry boundary. */
#define MID_STREAM_STATS 0x08D6u
#define STREAM_ROW_SIZE 32
#define STREAM_PAYLOAD (4 + MAX_STREAMS * STREAM_ROW_SIZE)
typedef struct { uint16 slot, queued; uint32 mid, received, sent, dropped, decimated, errors; uint8 cls, factor; } stream_stat_t;

static bool decimation(uint32 mid, uint8 factor)
{
    uint8 p[8] = {0};
    t5_put32(p, mid); p[4] = factor;
    return t5_send_cmd(MID_CMD, 7, p, sizeof(p));
}

static bool profile(uint8 mode)
{
    uint8 p[4] = {mode, 0, 0, 0};
    return t5_send_cmd(MID_CMD, 8, p, sizeof(p));
}

static const t5_tx_t *stream_snapshot(void)
{
    const int before = t5_ntx;
    const t5_tx_t *t;
    cmd0(9);
    t = t5_last_tx(MID_STREAM_STATS, before);
    UtAssert_True(t != NULL, "SEND_STREAM_STATS emits one full-sized STREAM_TLM snapshot");
    if (t == NULL) return NULL;
    UtAssert_True(t->size == 16 + STREAM_PAYLOAD, "STREAM_TLM has 64 rows, size2068");
    return t->size == 16 + STREAM_PAYLOAD ? t : NULL;
}

static bool stream_row(uint32 mid, stream_stat_t *out)
{
    const t5_tx_t *t = stream_snapshot();
    uint16 count;
    int i;
    memset(out, 0, sizeof(*out));
    if (t == NULL) return false;
    count = t5_get16(t->bytes + 16);
    UtAssert_True(count <= MAX_STREAMS && t5_get16(t->bytes + 18) == 0, "stream count bounded and spare zero");
    if (count > MAX_STREAMS) return false;
    for (i = 0; i < count; i++)
    {
        const uint8 *p = t->bytes + 20 + i * STREAM_ROW_SIZE;
        if (t5_get32(p + 4) != mid) continue;
        out->slot = t5_get16(p); out->mid = mid; out->cls = p[8]; out->factor = p[9]; out->queued = t5_get16(p + 10);
        out->received = t5_get32(p + 12); out->sent = t5_get32(p + 16); out->dropped = t5_get32(p + 20);
        out->decimated = t5_get32(p + 24); out->errors = t5_get32(p + 28);
        UtAssert_True(t5_get16(p + 2) == 0, "stream spare zero");
        return true;
    }
    UtAssert_Failed("stream 0x%04X absent in snapshot", (unsigned)mid);
    return false;
}

static void Test_DecimationAndPhase(void)
{
    ptbl_t t;
    stream_stat_t a;
    hk_t h;
    int i, from;
    ptbl_quad(&t, 0, 64);
    t.b[4 + 2 * 8 + 5] = 3;
    if (!boot(&t)) return;
    for (i = 0; i < 7; i++) inj(V2, (uint32)i);
    if (stream_row(V2, &a)) UtAssert_True(a.received == 7 && a.queued == 3 && a.decimated == 4 && a.dropped == 0,
        "factor3 keeps phase0 packets0,3,6 without counting decimation as drops");
    profile(1); cmd_budget(0);
    cmd0(CC_RESET); inj(V2, 7); inj(V2, 8); inj(V2, 9);
    if (stream_row(V2, &a)) UtAssert_True(a.received == 3 && a.queued == 4 && a.decimated == 2,
        "RESET preserves phase and queued count, clears cumulative counters");
    decimation(V2, 255); inj(V2, 10);
    for (i = 0; i < 254; i++) inj(V2, (uint32)(11 + i));
    inj(V2, 265);
    if (stream_row(V2, &a)) UtAssert_True(a.factor == 255 && a.queued == 6 && a.decimated == 256,
        "SET_DECIMATION resets phase, factor255 keeps first and256th");
    decimation(V2, 0); decimation(VF, 2);
    UtAssert_True(t5_count_ev(25, 0) == 2, "zero factor and absent stream reject with SET_DECIMATION_ERR");
    decimation(V2, 1); inj(V2, 266); inj(V2, 267);
    from = t5_nsent; cmd_budget(256); wakeups(2);
    {
        static const sd_t expected[] = {{V2,0}, {V2,3}, {V2,6}, {V2,9}, {V2,10}, {V2,265}, {V2,266}, {V2,267}};
        expect_sent(from, expected, 8, "phase survives profile selection and counter reset; SET_DECIMATION resets it");
    }
    if (get_hk(&h)) UtAssert_True(h.drop[2] == 0, "decimation never increments class drops");
}

static void Test_ProfilesAndQueuedClasses(void)
{
    ptbl_t t;
    stream_stat_t a;
    cmap_t map;
    int from;
    ptbl_quad(&t, 0, 8);
    if (!boot(&t)) return;
    inj(V3, 1); inj(V2, 2);
    profile(1); cmd_budget(0); inj(V3, 3);
    if (get_map(&map)) UtAssert_True(map.budget == 0 && map.cls[3] == 2, "DEGRADED map applies before override budget");
    profile(2);
    if (get_map(&map)) UtAssert_True(map.budget == 4 && map.cls[2] == 1 && map.cls[3] == 1, "SAFE profile budget4/class map");
    cmd_budget(0); inj(V3, 4);
    if (stream_row(V3, &a)) UtAssert_True(a.cls == 1 && a.queued == 3 && a.received == 3,
        "profile updates effective class without resetting stream counters or queue");
    from = t5_nsent; cmd_budget(256); wakeup();
    {
        static const sd_t expected[] = {{V3,4}, {V2,2}, {V3,3}, {V3,1}};
        expect_sent(from, expected, 4, "queued packets retain enqueue class through profile changes");
    }
    profile(0);
    if (get_map(&map)) UtAssert_True(map.budget == 16 && map.cls[3] == 3, "NOMINAL restores profile budget16");
    profile(3); UtAssert_True(t5_count_ev(27, 0) == 1, "profile3 rejected");
}

static void Test_StreamOwnershipReorderAndDiscard(void)
{
    ptbl_t t, replacement;
    stream_stat_t a, b;
    int from;
    ptbl_init(&t, 0, 2); ptbl_set(&t, 0, V3, 3); ptbl_set(&t, 1, V0, 0);
    if (!boot(&t)) return;
    inj(V3, 1); inj(V3, 2); inj(V0, 3);
    if (stream_row(V3, &a)) UtAssert_True(a.received == 2 && a.dropped == 1 && a.queued == 1,
        "eviction by another stream debits victim ownership");
    cmd_class(V3, 1); inj_size(V3, 4, 8193);
    if (stream_row(V3, &a)) UtAssert_True(a.dropped == 2 && a.queued == 1, "oversize drop attributed to stream");
    cmd_class(V3, 3); decimation(V3, 3); inj(V3, 5); inj(V3, 6);
    ptbl_init(&replacement, 0, 8); ptbl_set(&replacement, 0, V0, 0); ptbl_set(&replacement, 1, V3, 2);
    replacement.b[4 + 8 + 5] = 3;
    UtAssert_True(t5_tbl_submit(TBL_NAME, replacement.b) == CFE_SUCCESS, "reordered table accepted"); wakeup();
    if (stream_row(V3, &a)) UtAssert_True(a.slot == 1 && a.queued == 1 && a.received == 0 && a.sent == 0 &&
        a.decimated == 0 && a.dropped == 0, "retained queued MID follows new slot; cumulative stats reset");
    if (stream_row(V0, &b)) UtAssert_True(b.slot == 0 && b.queued == 1, "other retained MID follows new slot");
    inj(V3, 7);
    if (stream_row(V3, &a)) UtAssert_True(a.queued == 2 && a.received == 1 && a.decimated == 0,
        "table reset phase keeps first packet");
    from = t5_nsent; cmd_budget(256); wakeup();
    {
        static const sd_t expected[] = {{V0,3}, {V3,7}, {V3,2}};
        expect_sent(from, expected, 3, "reordered retained packet keeps its original class3");
    }
    cmd_budget(0); inj(V3, 8); inj(V3, 9); inj(V3, 10);
    cmd_remove(V3);
    if (stream_row(V0, &b)) UtAssert_True(b.queued == 0 && b.dropped == 0, "remove discards owner packets without charging other stream");
}

static void Test_StreamSendErrorsAndFullSnapshot(void)
{
    ptbl_t t;
    stream_stat_t a;
    const t5_tx_t *snapshot;
    int i, j;
    ptbl_init(&t, 0, 64);
    if (!boot(&t)) return;
    snapshot = stream_snapshot();
    if (snapshot != NULL)
    {
        UtAssert_True(t5_get16(snapshot->bytes + 16) == 0, "empty snapshot count0");
        for (i = 20; i < (int)snapshot->size; i++) UtAssert_True(snapshot->bytes[i] == 0, "empty snapshot tail byte%d zero", i);
    }
    for (i = 0; i < 64; i++) cmd_add(0x1800u + (uint32)i, (uint8)(i % 4));
    snapshot = stream_snapshot();
    if (snapshot != NULL)
    {
        UtAssert_True(t5_get16(snapshot->bytes + 16) == 64, "full snapshot count64");
        for (i = 0; i < 64; i++)
        {
            const uint8 *p = snapshot->bytes + 20 + i * 32;
            UtAssert_True(t5_get16(p) == i && t5_get32(p + 4) == 0x1800u + (uint32)i && p[8] == i % 4 && p[9] == 1,
                          "full snapshot row%d ordered, ADD_STREAM default factor1", i);
            for (j = 10; j < 32; j++) UtAssert_True(p[j] == 0, "full snapshot row%d zero counter/spare byte%d", i, j);
        }
    }
    cmd_remove(0x1800u); cmd_add(V0, 0); inj(V0, 1);
    t5_sendto_rc = OS_ERROR; cmd_budget(1); wakeup(); t5_sendto_rc = OS_SUCCESS;
    if (stream_row(V0, &a)) UtAssert_True(a.errors == 1 && a.sent == 0 && a.queued == 0, "failed UDP send attributed to stream then removed");
}

static void Test_ExpandedTableBounds(void)
{
    ptbl_t t;
    int i;
    ptbl_quad(&t, 0, 64);
    if (!boot(&t)) return;
    t.b[9] = 0;
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) != CFE_SUCCESS, "used stream decimation0 rejected");
    t.b[9] = 255;
    UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) == CFE_SUCCESS, "used stream decimation255 accepted");
    for (i = 0; i < 3; i++)
    {
        t5_put16(t.b + 516 + i * 8, 257);
        UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) != CFE_SUCCESS, "profile%d budget257 rejected", i);
        t5_put16(t.b + 516 + i * 8, 256);
        t.b[518 + i * 8 + i] = 4;
        UtAssert_True(t5_tbl_validate(TBL_NAME, t.b) != CFE_SUCCESS, "profile%d map4 rejected", i);
        t.b[518 + i * 8 + i] = 3;
    }
}

/* Eight units fit well below the subscribed pipe's pinned depth and MID limit. */
static bool maximum_packet_burst(const uint32 mids[8], const uint32 values[8])
{
    static uint8 packets[8][8192] __attribute__((aligned(16)));
    const uint8 *messages[8] = {0};
    size_t sizes[8] = {0};
    int i;
    for (i = 0; i < 8; i++)
    {
        sizes[i] = t5_build_verif(packets[i], mids[i], sizeof(packets[i]), values[i], 4);
        messages[i] = packets[i];
    }
    return t5_deliver_burst(messages, sizes, 8) && wakeup();
}

static void Test_MaximumSizePacketBursts(void)
{
    ptbl_t table;
    stream_stat_t row;
    hk_t hk;
    uint32 mids[8] = {0}, values[8] = {0};
    int i, from;
    ptbl_quad(&table, 0, 8);
    if (!boot(&table)) return;
    for (i = 0; i < 8; i++) { mids[i] = V3; values[i] = (uint32)i; }
    if (!maximum_packet_burst(mids, values)) return;
    if (stream_row(V3, &row)) UtAssert_True(row.received == 8 && row.queued == 8 && row.dropped == 0,
        "eight maximum-size packets occupy the complete copied queue without drops");
    for (i = 0; i < 8; i++)
    {
        mids[i] = i < 4 ? V2 : V0;
        values[i] = (uint32)(i < 4 ? 100 + i : 200 + i - 4);
    }
    if (!maximum_packet_burst(mids, values)) return;
    for (i = 0; i < 8; i++) { mids[i] = V3; values[i] = (uint32)(300 + i); }
    if (!maximum_packet_burst(mids, values)) return;
    if (stream_row(V3, &row)) UtAssert_True(row.received == 16 && row.queued == 0 && row.dropped == 16 && row.sent == 0,
        "maximum-size overflow attributes eight evictions and eight rejected arrivals to their owner");
    if (stream_row(V2, &row)) UtAssert_True(row.received == 4 && row.queued == 4 && row.dropped == 0,
        "class2 maximum-size arrivals retain four queue entries");
    if (stream_row(V0, &row)) UtAssert_True(row.received == 4 && row.queued == 4 && row.dropped == 0,
        "class0 maximum-size arrivals retain four queue entries");
    if (get_hk(&hk)) UtAssert_True(hk.rx == 24 && hk.queued == 8 && hk.drop[0] == 0 && hk.drop[1] == 0 &&
        hk.drop[2] == 0 && hk.drop[3] == 16, "maximum-size bounded bursts have exact aggregate receive/drop/queue totals");
    from = t5_nsent;
    cmd_budget(3);
    for (i = 0; i < 3; i++)
    {
        const int before = t5_nsent;
        wakeup();
        UtAssert_True(t5_nsent - before == (i < 2 ? 3 : 2), "maximum-size drain wakeup%d respects budget3", i);
    }
    {
        static const sd_t expected[] = {{V0,200}, {V0,201}, {V0,202}, {V0,203}, {V2,100}, {V2,101}, {V2,102}, {V2,103}};
        expect_sent(from, expected, 8, "maximum-size packets drain by class and FIFO within class");
    }
    for (i = from; i < t5_nsent; i++) UtAssert_True(t5_sent[i].size == 8192, "maximum-size queued packet%d remains exactly8192 bytes", i - from);
    expect_intact(from);
    if (stream_row(V0, &row)) UtAssert_True(row.sent == 4 && row.queued == 0 && row.errors == 0, "maximum-size class0 sends attributed to owner");
    if (stream_row(V2, &row)) UtAssert_True(row.sent == 4 && row.queued == 0 && row.errors == 0, "maximum-size class2 sends attributed to owner");
}

static void Test_DecimatedOversizeProcessingOrder(void)
{
    ptbl_t table;
    stream_stat_t row;
    hk_t hk;
    const t5_tx_t *housekeeping;
    int from;
    ptbl_quad(&table, 0, 8);
    table.b[4 + 2 * 8 + 5] = 2;
    if (!boot(&table)) return;
    inj(V2, 0);
    inj_size(V2, 1, 8193);
    wakeup();
    if (stream_row(V2, &row)) UtAssert_True(row.received == 2 && row.decimated == 1 && row.queued == 1 && row.dropped == 0,
        "phase1 oversized packet is decimated before length validation, with no drop");
    UtAssert_True(t5_count_ev(EID_DROP, 0) == 0, "decimated oversized packet emits no drop event");
    inj_size(V2, 2, 8193);
    inj(V2, 3);
    inj_size(V2, 4, 8192);
    wakeup();
    if (stream_row(V2, &row)) UtAssert_True(row.received == 5 && row.decimated == 2 && row.queued == 2 &&
        row.dropped == 1 && row.sent == 0 && row.errors == 0, "only the kept oversized packet counts as a stream drop");
    if (get_hk(&hk)) UtAssert_True(hk.rx == 5 && hk.drop[0] == 0 && hk.drop[1] == 0 && hk.drop[2] == 1 && hk.drop[3] == 0,
        "decimated oversized packet does not increment any class drop");
    housekeeping = t5_last_tx(MID_HK, 0);
    if (housekeeping != NULL && housekeeping->size == TLM_HDR + HK_PAYLOAD)
        UtAssert_True(t5_get32(housekeeping->bytes + TLM_HDR + 56) == 2, "PacketsDecimated counts oversized and ordinary decimated packets");
    UtAssert_True(t5_count_ev(EID_DROP, 0) == 1, "kept oversized packet emits exactly one class drop event");
    from = t5_nsent; cmd_budget(8); wakeup();
    {
        static const sd_t expected[] = {{V2,0}, {V2,4}};
        expect_sent(from, expected, 2, "only phase0 packets within the size limit drain");
    }
    UtAssert_True(t5_nsent - from == 2 && t5_sent[from].size == PKT && t5_sent[from + 1].size == 8192,
        "decimation/length processing preserves both kept packet lengths");
    expect_intact(from);
    if (stream_row(V2, &row)) UtAssert_True(row.sent == 2 && row.queued == 0 && row.dropped == 1 && row.decimated == 2,
        "drain updates Sent and Queued without changing Decimated or Dropped");
}

static void Test_PartialSnapshotZeroTail(void)
{
    ptbl_t table;
    const t5_tx_t *snapshot;
    int i;
    ptbl_init(&table, 0, 8);
    ptbl_set(&table, 1, V3, 3);
    table.b[4 + 1 * 8 + 5] = 3;
    ptbl_set(&table, 7, V0, 0);
    if (!boot(&table)) return;
    for (i = 0; i < 62; i++) cmd_add(0x1800u + (uint32)i, 1);
    snapshot = stream_snapshot();
    if (snapshot != NULL) UtAssert_True(t5_get16(snapshot->bytes + 16) == 64, "full snapshot populates every row before partial snapshot");
    for (i = 0; i < 62; i++) cmd_remove(0x1800u + (uint32)i);
    for (i = 0; i < 5; i++) inj(V3, (uint32)i);
    inj(V0, 9); wakeup();
    snapshot = stream_snapshot();
    if (snapshot == NULL) return;
    UtAssert_True(t5_get16(snapshot->bytes + 16) == 2 && t5_get16(snapshot->bytes + 18) == 0,
        "partial snapshot reports two used slots with zero payload spare");
    {
        const uint8 *first = snapshot->bytes + 20;
        const uint8 *second = first + STREAM_ROW_SIZE;
        UtAssert_True(t5_get16(first) == 1 && t5_get16(first + 2) == 0 && t5_get32(first + 4) == V3 &&
            first[8] == 3 && first[9] == 3 && t5_get16(first + 10) == 2 && t5_get32(first + 12) == 5 &&
            t5_get32(first + 16) == 0 && t5_get32(first + 20) == 0 && t5_get32(first + 24) == 3 && t5_get32(first + 28) == 0,
            "partial snapshot compacts sparse slot1 into first row with exact counters");
        UtAssert_True(t5_get16(second) == 7 && t5_get16(second + 2) == 0 && t5_get32(second + 4) == V0 &&
            second[8] == 0 && second[9] == 1 && t5_get16(second + 10) == 1 && t5_get32(second + 12) == 1 &&
            t5_get32(second + 16) == 0 && t5_get32(second + 20) == 0 && t5_get32(second + 24) == 0 && t5_get32(second + 28) == 0,
            "partial snapshot compacts sparse slot7 into second row in ascending slot order");
    }
    for (i = 20 + 2 * STREAM_ROW_SIZE; i < (int)snapshot->size; i++)
        UtAssert_True(snapshot->bytes[i] == 0, "partial snapshot unused byte%d is zero after a full snapshot", i - 16);
}

static void Register_Expanded(void)
{
    UtTest_Add(Test_MaximumSizePacketBursts, Setup, Teardown, "Maximum-size bounded packet bursts");
    UtTest_Add(Test_DecimatedOversizeProcessingOrder, Setup, Teardown, "Decimation precedes oversized length checks");
    UtTest_Add(Test_PartialSnapshotZeroTail, Setup, Teardown, "Partial snapshot zero tail after full snapshot");
    UtTest_Add(Test_DecimationAndPhase, Setup, Teardown, "Decimation and phase lifetime");
    UtTest_Add(Test_ProfilesAndQueuedClasses, Setup, Teardown, "Profiles and queued class retention");
    UtTest_Add(Test_StreamOwnershipReorderAndDiscard, Setup, Teardown, "Stream ownership across table reorder");
    UtTest_Add(Test_StreamSendErrorsAndFullSnapshot, Setup, Teardown, "Stream send errors and full snapshots");
    UtTest_Add(Test_ExpandedTableBounds, Setup, Teardown, "Expanded table bounds");
}
