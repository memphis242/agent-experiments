/* S5.4 boundary cases use only public messages, table activation and link traffic. */
static void Test_InterruptedPrimaryStability(void)
{
    conn_t c;
    cs_t cs[8];
    uint32 start;
    if (!boot(true)) return;
    ka_conn(&c, 6000, 7000); add_conn(0, &c);
    udp_conn(&c, 1, 6001, 7001, V_E7); add_conn(1, &c);
    route_cmd(0, V_E7, 0, 1);
    start = t5_now_sec;
    at_time(start + 6, 0);
    if (get_cs(cs)) UtAssert_True(route_selected[0] == 1, "primary loss selects backup");
    ka_in(6000, 2, 1, 1, 0); at_time(start + 7, 0);
    for (int sec = 8; sec <= 11; sec++)
    {
        ka_in(6000, 2, 1, 1, 0); at_time(start + sec, 0);
    }
    at_time(start + 16, 0xFFFFFFFFu);
    if (get_cs(cs)) UtAssert_True(route_selected[0] == 1 && cs[0].state == ST_UP,
                                  "backup remains selected just before the first stability deadline");
    /* Expiry and the first failback deadline coincide; deadlines precede routes. */
    at_time(start + 17, 0);
    if (get_cs(cs)) UtAssert_True(route_selected[0] == 1 && cs[0].state == ST_DOWN,
                                  "primary interruption at failback deadline cancels that interval");
    ka_in(6000, 2, 1, 1, 0); at_time(start + 18, 0);
    for (int sec = 19; sec <= 27; sec++)
    {
        ka_in(6000, 2, 1, 1, 0); at_time(start + sec, 0);
        if (get_cs(cs)) UtAssert_True(route_selected[0] == 1,
                                      "each wakeup before ten continuous seconds retains backup");
    }
    ka_in(6000, 2, 1, 1, 0); at_time(start + 28, 0);
    if (get_cs(cs)) UtAssert_True(route_selected[0] == 0,
                                  "failback uses a fresh ten-second interval after the interruption");
}

static void Test_RouteBothUnavailable(void)
{
    conn_t c;
    cs_t cs[8];
    hk_t before, after;
    uint8 pkt[40];
    int io, ix[8];
    uint32 start;
    size_t n;
    if (!boot(true)) return;
    ka_conn(&c, 6000, 7000); add_conn(0, &c);
    ka_conn(&c, 6001, 7001); add_conn(1, &c);
    route_cmd(0, V_E7, 0, 1);
    start = t5_now_sec;
    at_time(start + 6, 0);
    if (get_cs(cs)) UtAssert_True(cs[0].state == ST_DOWN && cs[1].state == ST_DOWN && route_selected[0] == 255,
                                  "both enabled endpoints DOWN leave no selected connection");
    if (!get_hk(&before)) return;
    n = t5_build_verif(pkt, V_E7, sizeof(pkt), 1, 4);
    t5_deliver(pkt, n); at_time(start + 6, 0);
    if (get_hk(&after)) UtAssert_True(after.egerr == before.egerr + 1 && after.egr == before.egr,
                                     "an unroutable packet counts one egress error, not one per endpoint");
    /* Probes can be sent while DOWN; data counters must remain unchanged. */
    if (get_cs(cs)) UtAssert_True(cs[0].tx == 0 && cs[1].tx == 0, "neither endpoint transmits the dropped data packet");
    ka_in(6001, 2, 1, 1, 0); at_time(start + 7, 0);
    if (get_cs(cs)) UtAssert_True(route_selected[0] == 1 && cs[1].tx == 0,
                                  "backup recovery selects it immediately without replaying dropped data");
    ka_in(6000, 2, 1, 1, 0); at_time(start + 8, 0);
    if (get_cs(cs)) UtAssert_True(route_selected[0] == 1, "recovered primary must complete stability interval");
    io = t5_nsent;
    n = t5_build_verif(pkt, V_E7, sizeof(pkt), 2, 4);
    t5_deliver(pkt, n); at_time(start + 8, 0);
    UtAssert_True(udp_out(7000, io, ix, 8) == 0 && udp_out(7001, io, ix, 8) == 1,
                  "fresh packet egresses only over recovered backup with no duplicate fanout");
    /* Also cover initial selection with neither endpoint UP. */
    idx_cmd(CC_DISABLE, 0); idx_cmd(CC_DISABLE, 1);
    route_cmd(0, V_E7, 0, 1); at_time(start + 8, 0);
    if (get_cs(cs)) UtAssert_True(route_selected[0] == 255, "a new route with both endpoints disabled selects none");
}

static void Test_TableActivationPreferenceReset(void)
{
    conn_t c;
    cs_t cs[8];
    uint8 image[TBL_SIZE] = {0}, pkt[40];
    int io, ix[8];
    size_t n;
    udp_conn(&c, 1, 6000, 7000, V_E7); tbl_put(conntbl, 0, &c);
    udp_conn(&c, 1, 6001, 7001, V_E7); tbl_put(conntbl, 1, &c);
    t5_put32(conntbl + 8 * CONN_SIZE, V_E7);
    conntbl[8 * CONN_SIZE + 4] = 0; conntbl[8 * CONN_SIZE + 5] = 1;
    if (!boot(true)) return;
    prefer_cmd(0, 1); wakeup();
    if (get_cs(cs)) UtAssert_True(route_selected[0] == 1 && route_preference[0] == 1,
                                  "active table route has a visible command-set backup preference");
    /* Activate the same endpoints and route so retained preferences cannot hide in unused slots. */
    memcpy(image, conntbl, sizeof(image));
    if (!UtAssert_True(t5_tbl_submit("ConnTable", image) == CFE_SUCCESS, "native route table activation validates")) return;
    wakeups(3);
    if (get_cs(cs))
    {
        UtAssert_True(cs[0].state == ST_UP && cs[1].state == ST_UP, "table activation opens both defined endpoints");
        for (int slot = 0; slot < 16; slot++)
            UtAssert_True(route_preference[slot] == 0, "table activation resets every preference slot");
        UtAssert_True(route_selected[0] == 0, "reapplied table initially selects its UP primary");
    }
    io = t5_nsent;
    n = t5_build_verif(pkt, V_E7, sizeof(pkt), 1, 4);
    t5_deliver(pkt, n); wakeup();
    UtAssert_True(udp_out(7000, io, ix, 8) == 1 && udp_out(7001, io, ix, 8) == 0,
                  "activated route egress confirms reset preference, without duplicate traffic");
}

static void Test_TcpTimeoutAndKeepAliveReports(void)
{
    conn_t c;
    cs_t cs[8];
    hk_t h;
    uint8 frame[96], wire[100];
    size_t n;
    int ev, tx, attempts;
    uint32 start;
    if (!boot(true)) return;
    at_time(t5_now_sec + 1, 0);
    conn_set(&c, IF_TCP, 1, 0, "127.0.0.1", 7015, NULL);
    c.b[88] = 1; c.b[89] = 2;
    start = t5_now_sec; ev = t5_nev;
    add_conn(0, &c);
    for (int sec = 1; sec < 20; sec++)
    {
        at_time(start + sec, 0);
        if (get_cs(cs)) UtAssert_True(cs[0].state == ST_CONNECTING && cs[0].timeouts == 0,
                                      "CONNECTING is not subject to the six-second UP keep-alive timer");
    }
    at_time(start + 20, 0);
    if (get_cs(cs)) UtAssert_True(cs[0].state == ST_DOWN && cs[0].en == 1 && cs[0].timeouts == 0,
                                  "connect exhaustion is enabled DOWN without a keep-alive timeout");
    UtAssert_True(t5_n_connect == 4 && t5_count_ev(28, ev) == 1 && t5_count_ev(29, ev) == 0,
                  "four refused attempts emit only CONN_TIMEOUT once");
    UtAssert_True(timeout_ev(ev, 0, "127.0.0.1", 7015), "connect timeout identifies endpoint and connection");
    attempts = t5_n_connect;
    at_time(start + 40, 0);
    UtAssert_True(t5_n_connect == attempts && t5_count_ev(28, ev) == 1 && t5_count_ev(29, ev) == 0,
                  "connect-exhausted DOWN neither retries nor expires a keep-alive timer");

    idx_cmd(CC_DISABLE, 0);
    t5_listen[0] = 7015;
    idx_cmd(CC_ENABLE, 0);
    start = t5_now_sec; ev = t5_nev; tx = t5_ntx; attempts = t5_n_connect;
    at_time(start + 5, 0xFFFFFFFFu);
    if (get_cs(cs)) UtAssert_True(cs[0].state == ST_UP, "connected TCP stays UP before keep-alive deadline");
    at_time(start + 6, 0);
    if (get_cs(cs)) UtAssert_True(cs[0].state == ST_DOWN && cs[0].en == 1 && cs[0].timeouts == 1,
                                  "connected TCP silence expires only the keep-alive timer");
    UtAssert_True(t5_n_connect == attempts && stream_socks() == 1,
                  "keep-alive loss retains TCP transport and does not start a connect cycle");
    UtAssert_True(t5_count_ev(29, ev) == 1 && t5_count_ev(28, ev) == 0, "TCP keep-alive expiry emits only KEEPALIVE_ERR");
    assert_link_report(tx, 0, 1);
    at_time(start + 12, 0);
    UtAssert_True(t5_count_ev(29, ev) == 1 && t5_tx_count(SFI_CMD_MID, tx) == 1,
                  "silence while DOWN produces no repeated timer event or report");
    n = ka_frame(frame, 2, 1, 1, 0, 88);
    n = framed(wire, NULL, 0, frame, n);
    UtAssert_True(t5_tcp_write(7015, wire, n), "retained TCP transport receives recovery probe"); tx = t5_ntx;
    at_time(start + 13, 0);
    if (get_cs(cs)) UtAssert_True(cs[0].state == ST_UP && cs[0].lastka == start + 13 && cs[0].timeouts == 1,
                                  "valid TCP keep-alive recovers retained transport");
    assert_link_report(tx, 0, 0);
    UtAssert_True(t5_count_ev(20, ev) == 1 && t5_n_connect == attempts, "probe recovery emits one UP without reconnecting");

    /* A transport failure reports immediately and its failed reconnect cycle times out separately. */
    t5_listen[0] = 0; tx = t5_ntx; ev = t5_nev;
    t5_tcp_close_peer(7015);
    at_time(start + 14, 0);
    if (get_cs(cs)) UtAssert_True(cs[0].state == ST_CONNECTING && cs[0].timeouts == 1,
                                  "TCP transport loss begins a fresh cycle without incrementing timer expiries");
    assert_link_report(tx, 0, 1);
    for (int sec = 15; sec <= 34; sec++) at_time(start + sec, 0);
    UtAssert_True(t5_count_ev(28, ev) == 1 && t5_count_ev(29, ev) == 0 && t5_tx_count(SFI_CMD_MID, tx) == 1,
                  "reconnect exhaustion emits CONN_TIMEOUT without a second link-fault edge");
    tx = t5_ntx; idx_cmd(CC_DISABLE, 0);
    assert_link_report(tx, 0, 0);
    if (get_hk(&h)) UtAssert_True(h.ka_timeout == 1, "HK counts timer expiries only, not connect exhaustion or transport loss");
}
