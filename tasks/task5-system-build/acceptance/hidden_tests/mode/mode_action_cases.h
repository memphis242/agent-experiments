/* Native action ordering, shared resource arbitration, and errors. */
static void Test_ActionsAndRouteRelease(void)
{
    mtbl_t t;const t5_tx_t *pri,*xfer,*route;const uint8 *h;int from;uint8 *r;
    if(!mode_boot())return;
    defaults(&t);r=t.b+464+5*148;r[2]=r[3]=r[4]=1;
    t5_tbl_submit("ModeTable",t.b);tick(102,0);mcmd(1,NULL,0);
    from=t5_ntx;fault(6,2,1,1,1,"GW");tick(103,0);
    pri=target(MSTOR,2,from);xfer=target(MSTOR,11,from);route=target(MGW,10,from);
    UtAssert_True(pri&&xfer&&route&&pri<xfer&&xfer<route,"SET_PRIORITY TRANSFER SET_PREFERENCE pinned order");
    if(xfer)UtAssert_True(xfer->size==140&&!xfer->bytes[8]&&!xfer->bytes[9]&&!xfer->bytes[10]&&strcmp((const char *)xfer->bytes+12,"mode/response.bin")==0&&xfer->bytes[76]==0,"native TRANSFER FILE/ground/KEEP/path/empty destination");
    if(route)UtAssert_True(route->size==12&&route->bytes[8]==0&&route->bytes[9]==1&&route->bytes[10]==0&&route->bytes[11]==0,"SET_PREFERENCE route0 backup1");
    fault(6,3,1,1,1,"GW");tick(104,0);from=t5_ntx;
    fault(6,2,1,0,2,"GW");tick(105,0);UtAssert_True(target(MGW,10,from)==NULL,"shared route no release while another requester active");
    mcontrol(2,0);fault(6,3,1,0,2,"GW");tick(106,0);route=target(MGW,10,from);
    UtAssert_True(route&&route->bytes[9]==0&&target_count(MGW,10,from)==1,"last requester sends one release even disabled");
    h=mhk();if(h)UtAssert_True(t5_get32(h+16)==10,"ActionsSent counts3 profile,6 attempt actions,1 release");
    from=t5_ntx;mcontrol(2,1);tick(107,0);UtAssert_True(target(MGW,10,from)==NULL,"re-enable never duplicates cleanup release");
}

static void Test_RestartResourceTimeouts(void)
{
    const uint8 *s;int from,ev;
    if(!mode_boot())return;
    from=t5_ntx;fault(1,0,1,1,1,"STOR");fault(1,3,1,1,1,"GW");fault(2,4,1,1,1,"");tick(102,0x80000000u);
    s=mstatus();if(s)UtAssert_True(s[6]==0&&s[24+0*12+1]==1&&s[24+3*12+1]==0&&s[24+20*12+1]==1,"ascending slots share one restart; waiting consumes no attempt while nonrestart responses run");
    UtAssert_True(target_count(MES,5,from)==1,"one STOP only while resource busy");
    ev=t5_nev;tick(107,0x7FFFFFFFu);UtAssert_True(t5_count_ev(16,ev)==0,"STOP timeout not early at fractional deadline");
    tick(107,0x80000000u);s=mstatus();if(s)UtAssert_True(s[6]==3&&s[24+3*12+1]==1,"STOP timeout frees resource before later slot attempts in same wakeup");
    UtAssert_True(t5_count_ev(16,ev)==1,"one actionable5s STOP timeout");
    present[3]=false;tick(108,0);tick(109,0x80000000u);from=t5_nev;
    tick(114,0x7FFFFFFFu);UtAssert_True(t5_count_ev(16,from)==0,"START registration timeout not early");
    tick(114,0x80000000u);s=mstatus();if(s)UtAssert_True(s[7]==0,"START registration timeout ends sequence");
    UtAssert_True(t5_count_ev(16,from)==1,"one actionable5s START timeout");
}

static int fail_mid,fail_fc;
static void selective_transmit(void *ud,UT_EntryKey_t key,const UT_StubContext_t *ctx)
{
    const uint8 *p=(const uint8 *)UT_Hook_GetArgValueByName(ctx,"MsgPtr",const CFE_MSG_Message_t *);
    (void)ud;
    if(t5_msg_mid(p)==(uint32)fail_mid&&p[6]==fail_fc){t5_ret(key,CFE_SB_INTERNAL_ERR);return;}
    t5_capture(p);t5_ret(key,CFE_SUCCESS);
}
static void Test_TransmissionFailures(void)
{
    const uint8 *s,*h;int from;
    if(!mode_boot())return;
    UT_SetHandlerFunction(UT_KEY(CFE_SB_TransmitMsg),selective_transmit,NULL);
    fail_mid=MTLMO;fail_fc=8;from=t5_ntx;mcmd(7,NULL,0);tick(102,0);
    UtAssert_True(target(MSTOR,4,from)&&target(MGW,9,from),"profile transmit failure does not skip remaining targets");
    h=mhk();if(h)UtAssert_True(t5_get32(h+20)==1,"one profile ActionErrors count");
    fail_mid=MES;fail_fc=5;from=t5_ntx;fault(1,3,1,1,1,"GW");tick(103,0);s=mstatus();
    if(s)UtAssert_True(s[7]==0&&s[24+3*12+1]==1,"STOP transmit failure consumes attempt and ends sequence immediately");
    tick(109,0);UtAssert_True(target(MES,4,from)==NULL&&target(MSTOR,2,from),"no START after failed STOP; nonrestart action still sent");
    present[3]=false;fail_fc=4;from=t5_ntx;tick(133,0);tick(134,0);s=mstatus();
    if(s)UtAssert_True(s[7]==0&&s[24+3*12+1]==2,"failed START terminates wait immediately and retains cooldown");
    h=mhk();if(h)UtAssert_True(t5_get32(h+24)==0,"failed START excluded from RestartsSent");
    fail_mid=0;
}

static void Test_MissingRestartMatch(void)
{
    mtbl_t t;const uint8 *s;int from;
    if(!mode_boot())return;
    defaults(&t);memset(t.b+16+3*112,0,112);t5_tbl_submit("ModeTable",t.b);tick(102,0);
    from=t5_ntx;fault(1,3,1,1,1,"GW");tick(103,0);s=mstatus();
    UtAssert_True(target(MES,4,from)==NULL&&target(MES,5,from)==NULL&&target(MSTOR,2,from),"missing restart match never substitutes another app and continues nonrestart actions");
    if(s)UtAssert_True(s[24+3*12+1]==1&&s[7]==0,"missing match consumes attempt without sequence");
}

static void Test_AbsenceClearDisable(void)
{
    const uint8 *s;int from;
    if(!mode_boot())return;
    present[3]=false;from=t5_ntx;fault(1,3,1,1,1,"GW");tick(102,0);
    s=mstatus();if(s)UtAssert_True(s[7]==1&&s[24+3*12+1]==1,"initially absent target enters WAIT_STOP delay");
    UtAssert_True(target(MES,5,from)==NULL&&target(MES,4,from)==NULL,"initial absence sends neither STOP nor same-wakeup START");
    fault(1,3,1,0,2,"GW");tick(103,0);
    UtAssert_True(target(MES,4,from)!=NULL,"clear cancels future retries but continues dispatched restart");
    present[3]=true;tick(108,0);s=mstatus();if(s)UtAssert_True(s[7]==0&&!s[24+3*12],"exact START registration deadline succeeds");
    fault(1,3,1,1,3,"GW");tick(132,0);mcontrol(2,0);from=t5_ntx;present[3]=false;tick(133,0);tick(150,0);
    s=mstatus();if(s)UtAssert_True(s[7]==0&&s[24+3*12+1]==2&&s[24+3*12+3]==1,"disable cancels in-progress restart, retains consumed attempt and computed Pending");
    UtAssert_True(target(MES,4,from)==NULL,"disable cancels pending START without transmission");
    mcmd(1,NULL,0);s=mstatus();if(s)UtAssert_True(s[24+3*12+1]==2&&t5_get32(s+24+3*12+4)==162&&t5_get32(s+24+3*12+8)==3,"counter reset retains attempts cooldown and report serial");
    mcontrol(2,1);tick(161,0);UtAssert_True(target(MES,4,from)==NULL,"re-enable obeys retained cooldown");
    tick(162,0);tick(163,0);UtAssert_True(target_count(MES,4,from)==1,"one new attempt after re-enable eligibility");
}

static void Test_DeadlinePresenceWins(void)
{
    const uint8 *h,*s;int from;
    if(!mode_boot())return;
    fault(1,3,1,1,1,"GW");tick(102,0x80000000u);from=t5_nev;
    present[3]=false;tick(107,0x80000000u);
    UtAssert_True(t5_count_ev(16,from)==0,"STOP absence at exact5s deadline wins over timeout");
    tick(108,0x80000000u);present[3]=true;tick(113,0x80000000u);
    s=mstatus();h=mhk();if(s&&h)UtAssert_True(s[7]==0&&t5_get32(h+20)==0,"registration at exact5s deadline wins over timeout");
}

static void Test_TableAcceptedBoundsAndPaths(void)
{
    static const char *const bad_paths[]={"","/absolute","../up","a/../up","a..b"};
    mtbl_t t;int i,j;uint8 *r;
    if(!mode_boot())return;
    for(i=0;i<2;i++)
    {
        defaults(&t);t.b[2]=i?60:3;t.b[3]=i?120:1;
        for(j=0;j<3;j++){t.b[4+j*4]=i?2:0;t.b[5+j*4]=i?100:1;}
        for(j=0;j<4;j++){t5_put32(t.b+16+j*112+104,i?262144:16384);t5_put16(t.b+16+j*112+108,i?255:1);}
        for(j=0;j<7;j++){r=t.b+464+j*148;t5_put16(r+8,i?300:10);r[10]=i?5:1;}
        UtAssert_True(t5_tbl_validate("ModeTable",t.b)==0,"all accepted minimum/maximum table bounds%d",i);
    }
    for(i=0;i<(int)(sizeof(bad_paths)/sizeof(bad_paths[0]));i++)
    {
        defaults(&t);r=t.b+464+4*148;memset(r+20,0,64);strcpy((char *)r+20,bad_paths[i]);
        UtAssert_True(t5_tbl_validate("ModeTable",t.b)!=0,"TRANSFER rejects unsafe relative path '%s'",bad_paths[i]);
    }
    defaults(&t);r=t.b+464+4*148;memset(r+20,'x',64);UtAssert_True(t5_tbl_validate("ModeTable",t.b)!=0,"unterminated TRANSFER Path rejected");
    defaults(&t);r=t.b+464+4*148;r[84]='x';UtAssert_True(t5_tbl_validate("ModeTable",t.b)!=0,"TRANSFER destination path must be empty");
    defaults(&t);t.b[5]=101;UtAssert_True(t5_tbl_validate("ModeTable",t.b)!=0,"profile purge101 rejected");
}

static bool foreign_mode(void)
{
    uint8 msg[8];int k;
    t5_build_cmd(msg,0x18F7,0,NULL,0);
    for(k=0;k<T5_SUBS_MAX;k++)if(t5_subs[k].used&&t5_subs[k].mid==MCMD)
    {
        t5_pipe_t *pipe=&t5_pipes[t5_subs[k].pipe];const int slot=t5_pool_next++%T5_POOL;
        memcpy(t5_pool[slot],msg,8);pipe->q[(pipe->head+pipe->count)%T5_PIPE_Q]=slot;pipe->count++;
        return t5_kick();
    }
    UtAssert_Failed("MODE command subscription missing");return false;
}
static void Test_ResetStateAndForeignMid(void)
{
    const uint8 *s,*h;uint8 saved[708];int from;
    if(!mode_boot())return;
    mcontrol(2,0);t5_now_sec=102;peer(2,1,2,0,12,0xFFFFFFFFu);
    fault(4,6,2,1,0xFFFFFFFFu,"");mcontrol(3,2);tick(103,0);
    s=mstatus();if(s)memcpy(saved,s,sizeof(saved));else return;
    mcmd(1,NULL,0);s=mstatus();
    if(s)UtAssert_True(memcmp(s,saved,sizeof(saved))==0,"RESET_COUNTERS preserves mode, peer diagnostic/timer, serial and all response state");
    h=mhk();if(h){int i;for(i=8;i<=28;i+=4)UtAssert_True(t5_get32(h+i)==0,"RESET_COUNTERS cumulative field%d zero",i);UtAssert_True(h[2]==2&&h[6]==2&&t5_get32(h+44)==0xFFFFFFFFu,"reset retains mode and peer sequence");}
    from=t5_nev;foreign_mode();h=mhk();if(h)UtAssert_True(h[1]==1,"unexpected MID counts exactly one command error");
    UtAssert_True(t5_count_ev(5,from)==1,"unexpected MID emits MID_ERR");
}

static void Test_SimultaneousDemands(void)
{
    const uint8 *h;uint8 commands;
    if(!mode_boot())return;
    mcontrol(3,0);mcontrol(3,1);
    fault(3,5,1,1,1,"");fault(3,6,2,1,1,"");fault(3,6,2,0,2,"");tick(102,0);
    h=mhk();if(h)UtAssert_True(h[2]==2,"transient CRITICAL RATE overrides warning and accepted commands with one-wakeup SAFE demand");
    mcontrol(3,0);h=mhk();if(h)UtAssert_True(h[2]==2,"active warning rejects NOMINAL exit from SAFE");
    mcontrol(3,1);fault(3,6,2,1,3,"");h=mhk();commands=h?h[0]:0;tick(103,0);h=mhk();
    if(h)UtAssert_True(h[2]==2&&h[0]==commands,"new critical supersedes accepted guarded exit without another command count change");
    fault(3,6,2,0,4,"");fault(3,5,1,0,2,"");mcontrol(3,0);mcontrol(3,1);mcontrol(3,0);tick(104,0);h=mhk();
    if(h)UtAssert_True(h[2]==0,"last accepted conflicting command NOMINAL ends healthy interval immediately");
    mcontrol(2,0);fault(3,6,2,1,5,"");fault(3,6,2,0,6,"");tick(105,0);mcontrol(2,1);tick(106,0);h=mhk();
    if(h)UtAssert_True(h[2]==0,"disabled transient critical monitoring never accumulates stale minimum demand");
}
