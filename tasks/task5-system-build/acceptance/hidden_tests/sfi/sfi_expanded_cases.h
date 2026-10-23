/* Expanded S6 checks drive production SFI through its SB subscriptions. */
#define M_FAULT 0x18F3u
#define M_FAULT_TLM 0x08DEu
#define M_HEALTH 0x187Au
#define M_LIMIT 0x18EFu
#define EXT_STATUS_FAULTS 1152

static const uint8 *expanded_status(void)
{
    const int before = t5_ntx;
    const t5_tx_t *tx;
    cmd(4, NULL, 0);
    tx = t5_last_tx(M_STATUS, before);
    UtAssert_True(tx != NULL && tx->size == 3904, "expanded SFI status is one full 3904-byte packet");
    return tx != NULL && tx->size == 3904 ? tx->bytes + 16 : NULL;
}

static void control(int cc, uint8 index, uint8 value)
{
    const uint8 payload[4] = {index, value, 0, 0};
    cmd(cc, payload, sizeof(payload));
}

static const uint8 *report(int from, int type, int source, int active)
{
    const t5_tx_t *command = NULL, *mirror = NULL;
    int i;
    for (i = from; i < t5_ntx; ++i)
    {
        const t5_tx_t *tx = &t5_tx[i];
        const size_t offset = tx->mid == M_FAULT ? 8 : 16;
        if (tx->mid != M_FAULT && tx->mid != M_FAULT_TLM) continue;
        if (tx->size != offset + 48) { UtAssert_Failed("fault packet length %u", (unsigned)tx->size); continue; }
        if (tx->bytes[offset+4] != type || tx->bytes[offset+5] != source || tx->bytes[offset+7] != active) continue;
        if (tx->mid == M_FAULT) command = tx; else mirror = tx;
    }
    UtAssert_True(command != NULL && mirror != NULL, "fault type %d source %d active %d has command and telemetry",type,source,active);
    if (command == NULL || mirror == NULL) return NULL;
    UtAssert_True(command < mirror && command->bytes[6] == 0 && memcmp(command->bytes+8, mirror->bytes+16,48)==0,
                  "FC0 local report precedes byte-identical mirrored payload");
    return command->bytes+8;
}

static void event_input(const char *name, uint16 type)
{
    uint8 bytes[170];
    t5_build_tlm(bytes,0x0808,sizeof(bytes),0);
    memset(bytes+16,0,154);
    snprintf((char *)bytes+16,20,"%s",name);
    t5_put16(bytes+36,99); t5_put16(bytes+38,type);
    t5_deliver(bytes,sizeof(bytes));
}

static void Test_ExpandedValidation(void)
{
    stbl_t good,t;
    int i,field;
    if (!boot_default()) return;
    stbl_default(&good);
    UtAssert_True(vtbl(&good),"complete pinned SFI defaults validate");
    for (field=20;field<=23;field++)
    {
        t=good;t.b[8+field]=(uint8)(field<22?2:field==22?0:61);
        UtAssert_True(!vtbl(&t),"app enabled/event-enabled/limit/window bound at field %d",field);
    }
    t=good;t.b[30]=65;UtAssert_True(!vtbl(&t),"event limit65 rejected");
    t=good;t.b[31]=0;UtAssert_True(!vtbl(&t),"event window0 rejected");
    for(i=0;i<16;i++)
    {
        const int w=392+i*24;
        t=good;stbl_watch(&t,i,M_LIMIT,16,4,3,0);
        t5_put32(t.b+w+12,10);t5_put32(t.b+w+16,20);t.b[w+20]=1;
        UtAssert_True(vtbl(&t),"LIMIT source slot%d valid",i);
        t.b[w+20]=0;UtAssert_True(!vtbl(&t),"LIMIT persistence0 slot%d rejected",i);
    }
    for(field=0;field<9;field++)
    {
        t=good;
        switch(field)
        {
            case 0:t.b[392+7]=4;break;
            case 1:t5_put32(t.b+392,0);break;
            case 2:t.b[392+23]=2;break;
            case 3:t.b[392+22]=0;break;
            case 4:t.b[392+21]=8;break;
            case 5:t5_put32(t.b+392+12,1);break;
            case 6:t.b[776]=2;break;
            case 7:t.b[777]=2;break;
            default:t.b[777]=61;break;
        }
        UtAssert_True(!vtbl(&t),"expanded table invalid field%d rejected",field);
    }
    t=good;t5_put32(t.b+392+6*24+12,21);UtAssert_True(!vtbl(&t),"Min above Max rejected");
    t=good;t.b[392+6*24+6]=1;t5_put32(t.b+392+6*24+16,256);UtAssert_True(!vtbl(&t),"Max must fit width1");
    t=good;t.b[392+6*24+6]=2;t5_put32(t.b+392+6*24+16,65536);UtAssert_True(!vtbl(&t),"Max must fit width2");
    t=good;t5_put32(t.b+392+6*24+8,1);UtAssert_True(!vtbl(&t),"LIMIT Limit must be0");
    t=good;t.b[28]=0;t.b[29]=0;t.b[30]=1;t.b[31]=1;t.b[777]=3;
    UtAssert_True(vtbl(&t),"disabled used app and minimum event/peer bounds accepted");
    t=good;t.b[30]=64;t.b[31]=60;t.b[777]=60;t.b[392+22]=2;
    UtAssert_True(vtbl(&t),"maximum event/peer bounds and critical severity accepted");
    t=good;t.b[392+22]=3;UtAssert_True(!vtbl(&t),"watch severity3 rejected");
}

static void Test_LimitAndLatch(void)
{
    const uint8 *r,*s;
    uint8 onset[48];
    int from,seq;
    if(!boot_default())return;
    from=t5_ntx;
    watch_pkt(M_LIMIT,24,9,4);watch_pkt(M_LIMIT,24,9,4);
    UtAssert_True(t5_tx_count(M_FAULT,from)==0,"two LIMIT violations below persistence3");
    watch_pkt(M_LIMIT,24,10,4);watch_pkt(M_LIMIT,24,20,4);
    watch_pkt(M_LIMIT,24,9,4);watch_pkt(M_LIMIT,24,9,4);watch_pkt(M_LIMIT,24,9,4);
    r=report(from,4,6,1);if(r==NULL)return;memcpy(onset,r,48);seq=t5_get32(r);
    UtAssert_True(seq==1&&r[6]==2&&r[8]==1&&r[9]==1&&r[12]==0,"first LIMIT sequence1 critical latched node1 unassociated name");
    watch_pkt(M_LIMIT,24,9,4);
    UtAssert_True(t5_tx_count(M_FAULT,from)==1,"persistent violation reports only once");
    control(6,22,0);UtAssert_True(t5_count_ev(27,0)==1,"active ACK rejected");
    t5_now_sec+=1;watch_pkt(M_LIMIT,24,10,4);
    r=report(from,4,6,0);if(r==NULL)return;
    UtAssert_True(t5_get32(r)==(uint32)seq+1&&memcmp(r+32,onset+32,8)==0&&r[8]==1&&t5_get32(r+40)==t5_now_sec&&t5_get32(r+44)==t5_now_sub,"clear retains onset and latch with next serial");
    control(6,22,0);s=expanded_status();
    if(s)UtAssert_True(s[1036+22]==0&&s[1093+22]==0&&s[EXT_STATUS_FAULTS+22*48+8]==1,"ACK updates latch array only, latest historical payload stays unchanged");
    control(6,22,0);UtAssert_True(t5_count_ev(27,0)==2,"second ACK of unlatched slot rejected");
    cmd(1,NULL,0);cmd(5,NULL,0);watch_pkt(M_LIMIT,24,9,4);watch_pkt(M_LIMIT,24,9,4);watch_pkt(M_LIMIT,24,9,4);
    r=report(from,4,6,1);if(r)UtAssert_True(t5_get32(r)==3&&r[8]==1,"RESET and CLEAR_HISTORY preserve serial; later onset relatches");
    control(8,6,0);r=report(from,4,6,0);if(r)UtAssert_True(t5_get32(r)==4,"watch disable clears active fault");
    s=expanded_status();if(s)UtAssert_True(s[840+6*12]==1&&s[840+6*12+8]==0&&s[840+6*12+9]==0,"disabled watch is baseline pending with zero persistence");
    control(8,6,1);watch_pkt(M_LIMIT,24,9,4);watch_pkt(M_LIMIT,24,9,4);
    s=expanded_status();if(s)UtAssert_True(s[1036+22]==0&&s[1093+22]==1,"re-enable resets baseline/persistence and preserves latch");
    UtAssert_True(t5_last_ev_type(23)==CFE_EVS_EventType_ERROR&&t5_last_ev_type(25)==CFE_EVS_EventType_INFORMATION&&t5_last_ev_type(26)==CFE_EVS_EventType_INFORMATION&&t5_last_ev_type(27)==CFE_EVS_EventType_ERROR,"LIMIT ACK CONTROL event type assignments");
}

static void Test_LimitWidths(void)
{
    stbl_t t;
    int width,rep,i,from;
    const uint8 *s;
    if(!boot_default())return;
    for(width=1;width<=4;width*=2)for(rep=1;rep<=255;rep+=254)
    {
        stbl_default(&t);
        t.b[392+6*24+6]=(uint8)width;t.b[392+6*24+20]=(uint8)rep;
        t5_put32(t.b+392+6*24+16,width==1?255u:width==2?65535u:0xFFFFFFFFu);
        UtAssert_True(t5_tbl_submit("SfiTable",t.b)==0,"width%d persistence%d table accepted",width,rep);
        cycle(NULL,0);from=t5_ntx;
        watch_pkt(M_LIMIT,24,width==1?255u:width==2?65535u:0xFFFFFFFFu,width);
        watch_pkt(M_LIMIT,24,10,width);
        UtAssert_True(t5_tx_count(M_FAULT,from)==0,"inclusive min and maximum representable value accepted");
        for(i=0;i<rep-1;i++)watch_pkt(M_LIMIT,24,9,width);
        s=expanded_status();if(s)UtAssert_True(s[1036+22]==0,"persistence%d does not trigger early",rep);
        watch_pkt(M_LIMIT,24,9,width);report(from,4,6,1);
        s=expanded_status();if(s)UtAssert_True(s[840+6*12+9]==rep,"persistence count saturates at%d",rep);
        watch_pkt(M_LIMIT,24,10,width);report(from,4,6,0);
    }
}

static void Test_EventWindows(void)
{
    int i,from;
    const uint8 *s;
    stbl_t table;
    stbl_default(&table);stbl_app(&table,8,"SFI");
    if(!boot(&table))return;
    UtAssert_True(t5_subscribed(0x0808)&&t5_sub_msglim(0x0808)==64,"long events subscribed limit64");
    for(i=0;i<T5_SUBS_MAX;i++)if(t5_subs[i].used&&t5_subs[i].mid==0x0808)
        UtAssert_True(t5_pipes[t5_subs[i].pipe].depth>=256,"SFI event pipe depth at least256");
    from=t5_ntx;t5_now_sec=100;t5_now_sub=0x80000000u;
    for(i=0;i<20;i++){event_input("GW",CFE_EVS_EventType_INFORMATION);event_input("GW",CFE_EVS_EventType_DEBUG);event_input("GW",CFE_EVS_EventType_CRITICAL);event_input("GW_extra",CFE_EVS_EventType_ERROR);event_input("SFI",CFE_EVS_EventType_ERROR);}
    UtAssert_True(t5_tx_count(M_FAULT,from)==0,"INFORMATION DEBUG own SFI events never trigger");
    for(i=0;i<5;i++)event_input("GW",CFE_EVS_EventType_ERROR);
    UtAssert_True(t5_tx_count(M_FAULT,from)==0,"exactly limit5 ERROR events are healthy");
    event_input("GW",CFE_EVS_EventType_ERROR);report(from,5,3,1);
    t5_now_sec=109;t5_now_sub=0x80000000u;
    for(i=0;i<200;i++)event_input("GW",CFE_EVS_EventType_ERROR);
    s=expanded_status();if(s)UtAssert_True(s[8+3*36+33]==6,"arbitrary burst retains most recent limit+1 and caps count6");
    cycle_at(NULL,0,119,0x7FFFFFFFu);
    UtAssert_True(t5_tx_count(M_FAULT,from)==1,"newest burst remains active one tick before lower boundary");
    cycle_at(NULL,0,119,0x80000000u);report(from,5,3,0);
    UtAssert_True(t5_tx_count(M_FAULT,from)==2,"half-open lower boundary expires on wakeup, exactly one clear");
    UtAssert_True(t5_last_ev_type(24)==CFE_EVS_EventType_ERROR,"EVENT_RATE_ERR type ERROR");
    control(7,3,0);for(i=0;i<7;i++)event_input("GW",CFE_EVS_EventType_ERROR);
    s=expanded_status();if(s)UtAssert_True(s[8+3*36+33]==0,"disabled app ignores event window");
    control(7,3,1);event_input("GW",CFE_EVS_EventType_ERROR);
    s=expanded_status();if(s)UtAssert_True(s[8+3*36+33]==1,"re-enabled event window starts empty");
}

static void health(uint8 src,uint8 dst,int fc,int size)
{
    uint8 p[16]={0};p[0]=src;p[1]=dst;t5_put32(p+4,91);t5_put32(p+8,1);
    t5_send_cmd(M_HEALTH,fc,p,(size_t)size);
}

static void Test_HealthAndSlots(void)
{
    const uint8 *s,*r;
    int i,from;
    if(!boot_default())return;
    from=t5_ntx;cycle_at(NULL,0,100,0);cycle_at(NULL,0,200,0);
    UtAssert_True(t5_tx_count(M_FAULT,from)==0,"standalone peer unarmed indefinitely");
    r=t5_last_tx(M_HEALTH,from)?t5_last_tx(M_HEALTH,from)->bytes+8:NULL;
    UtAssert_True(r&&r[0]==1&&r[1]==2&&t5_get32(r+4)==2&&t5_get32(r+8)==200,"every wakeup publishes global health FC0 payload with local sequence/time");
    health(1,2,0,12);health(3,1,0,12);health(2,2,0,12);health(2,1,1,12);health(2,1,0,11);
    cycle_at(NULL,0,300,0);UtAssert_True(t5_tx_count(M_FAULT,from)==0,"own malformed unknown health does not arm");
    t5_now_sec=400;health(2,1,0,12);
    cycle_at(NULL,0,409,0xFFFFFFFFu);UtAssert_True(t5_tx_count(M_FAULT,from)==0,"health deadline not early");
    cycle_at(NULL,0,410,0);r=report(from,7,0,1);
    if(r)UtAssert_True(r[9]==1&&r[12]==0&&r[6]==1,"NODE_LOSS own node warning empty name");
    health(2,1,0,12);report(from,7,0,0);
    control(10,0,0);
    for(i=0;i<12;i++){t5_now_sec=500u+(uint32)i;health(2,1,0,12);}
    control(10,1,0);cycle_at(NULL,0,600,0);
    s=expanded_status();if(s)UtAssert_True(s[1036+56]==0,"disabled valid health cannot arm and re-enable remains unarmed");
    from=t5_ntx;
    for(i=0;i<8;i++){control(12,(uint8)i,1);control(12,(uint8)i,1);}
    s=expanded_status();if(s)for(i=0;i<8;i++)UtAssert_True(s[1036+48+i]==1,"LINK slot%d exact mapping",48+i);
    for(i=0;i<8;i++)
    {
        r=report(from,6,i,1);if(r)UtAssert_True(memcmp(r+12,"GW",3)==0&&r[6]==1,"LINK name GW warning");
        control(12,(uint8)i,0);control(12,(uint8)i,0);report(from,6,i,0);
    }
    UtAssert_True(t5_tx_count(M_FAULT,from)==16,"link repeated states suppressed with exact eight onset/clear pairs");
    UtAssert_True(t5_last_ev_type(30)==CFE_EVS_EventType_ERROR&&t5_last_ev_type(31)==CFE_EVS_EventType_INFORMATION&&t5_last_ev_type(32)==CFE_EVS_EventType_ERROR&&t5_last_ev_type(33)==CFE_EVS_EventType_INFORMATION,"NODE/LINK onset and clear event type assignments");
}

static void Test_MixedHistory(void)
{
    const uint8 *s;
    stbl_t t;
    int from;
    if(!boot_default())return;
    stbl_default(&t);t.b[0]=1;t.b[392+6*24+20]=1;t.b[392+6*24+21]=3;
    t5_tbl_submit("SfiTable",t.b);cycle_at(NULL,0,90,0);cmd(5,NULL,0);
    hb("SFI_T1",1);cycle_at(NULL,0,100,0);cycle_at(NULL,0,101,0);
    t5_now_sec=106;watch_pkt(M_LIMIT,24,9,4);cycle_at(NULL,0,106,0);
    s=expanded_status();if(s)UtAssert_True(s[1]==2&&t5_get16(s+6)==2&&t5_get16(s+1032)==((1u<<4)|(1u<<3))&&t5_get16(s+1034)==(1u<<6),"mixed heartbeat/watch gap exactly W clustered with associated masks");
    watch_pkt(M_LIMIT,24,10,4);t5_now_sec=120;watch_pkt(M_LIMIT,24,9,4);
    watch_pkt(M_LIMIT,24,10,4);t5_now_sec=125;watch_pkt(M_LIMIT,24,9,4);cycle_at(NULL,0,125,0);
    s=expanded_status();if(s)UtAssert_True(t5_get16(s+6)==2&&t5_get16(s+1032)==((1u<<4)|(1u<<3)),"equal-size cluster tie selects earliest onset, repeat watches count but deduplicate masks");
    from=t5_ntx;cmd(5,NULL,0);s=expanded_status();if(s)UtAssert_True(t5_get16(s+2)==0&&t5_get16(s+1032)==0&&t5_get16(s+1034)==0&&s[1036+22]==1,"CLEAR_HISTORY resets attribution but preserves physical fault");
    t.b[392+6*24+21]=255;t5_tbl_submit("SfiTable",t.b);cycle_at(NULL,0,126,0);
    report(from,4,6,0);s=expanded_status();if(s)UtAssert_True(s[1093+22]==1,"table activation clears old active source before rearm and retains latch");
}

static int dump_failure;
static void dump_open_hook(void *ud,UT_EntryKey_t key,const UT_StubContext_t *c)
{
    if(dump_failure==1)t5_ret(key,OS_ERROR);else H_OS_OpenCreate(ud,key,c);
}
static void dump_write_hook(void *ud,UT_EntryKey_t key,const UT_StubContext_t *c)
{
    if(dump_failure==2)t5_ret(key,OS_ERROR);else if(dump_failure==3)t5_ret(key,0);else H_OS_write(ud,key,c);
}
static void dump_close_hook(void *ud,UT_EntryKey_t key,const UT_StubContext_t *c)
{
    H_OS_close(ud,key,c);if(dump_failure==4)t5_ret(key,OS_ERROR);
}

static void Test_FaultDump(void)
{
    int i,k,from,first;
    uint8 saved[64*48];
    if(!boot_default())return;
    if(mf_find("/cf")<0)mf_new("/cf",true);
    cmd(9,NULL,0);k=mf_find("/cf/sfi_faults.bin");
    UtAssert_True(k>=0&&mf[k].size==16,"empty dump only16-byte header");
    if(k>=0)UtAssert_True(t5_get32(mf[k].data)==0x5346494Cu&&t5_get16(mf[k].data+4)==1&&t5_get16(mf[k].data+6)==48&&t5_get32(mf[k].data+8)==0&&t5_get32(mf[k].data+12)==0,"exact little-endian empty log header");
    from=t5_ntx;
    for(i=0;i<70;i++)
    {
        t5_now_sec++;control(12,0,(uint8)(1-i%2));
        if(i==63)
        {
            cmd(9,NULL,0);k=mf_find("/cf/sfi_faults.bin");
            UtAssert_True(k>=0&&mf[k].size==16+64*48&&t5_get32(mf[k].data+8)==64&&t5_get32(mf[k].data+12)==64,
                          "exactly64 reports fill log before any overwrite");
        }
    }
    first=0;for(i=from;i<t5_ntx;i++)if(t5_tx[i].mid==M_FAULT){if(first>=6)memcpy(saved+(first-6)*48,t5_tx[i].bytes+8,48);first++;}
    UtAssert_True(first==70,"70 edges generated for ring overwrite");
    cmd(1,NULL,0);cmd(5,NULL,0);control(6,48,0);cmd(9,NULL,0);k=mf_find("/cf/sfi_faults.bin");
    UtAssert_True(k>=0&&mf[k].size==16+64*48,"wrapped dump contains exactly64 records");
    if(k>=0&&mf[k].size==16+64*48)UtAssert_True(t5_get32(mf[k].data+8)==64&&t5_get32(mf[k].data+12)==70&&memcmp(mf[k].data+16,saved,sizeof(saved))==0,"oldest-first byte-identical reports, ACK RESET CLEAR_HISTORY preserve history and serial");
    UT_SetHandlerFunction(UT_KEY(OS_OpenCreate),dump_open_hook,NULL);
    UT_SetHandlerFunction(UT_KEY(OS_write),dump_write_hook,NULL);
    UT_SetHandlerFunction(UT_KEY(OS_close),dump_close_hook,NULL);
    for(i=1;i<=4;i++)
    {
        const int ev=t5_nev;
        k=mf_find("/cf/sfi_faults.bin");
        if(k>=0){free(mf[k].data);memset(&mf[k],0,sizeof(mf[k]));}
        dump_failure=i;cmd(9,NULL,0);
        UtAssert_True(t5_count_ev(29,ev)==1&&t5_count_ev(28,ev)==0&&mf_find("/cf/sfi_faults.bin")<0,"dump open/write/short/close failure%d removes partial output and emits one error",i);
    }
    dump_failure=0;
    UtAssert_True(t5_last_ev_type(28)==CFE_EVS_EventType_INFORMATION&&t5_last_ev_type(29)==CFE_EVS_EventType_ERROR,"dump event type assignments");
}

static void Test_FixedSlotMapping(void)
{
    stbl_t t;const uint8 *s;int i,j,from;char name[20];
    stbl_default(&t);t.b[0]=1;t.b[777]=3;
    for(i=0;i<16;i++)
    {
        uint8 *watch=t.b+392+i*24;
        snprintf(name,sizeof(name),"APP_%d",i);stbl_app(&t,i,name);t.b[31+i*24]=60;
        stbl_watch(&t,i,0x18E0u+(uint32)i,16,1,3,0);
        t5_put32(watch+12,1);t5_put32(watch+16,1);watch[20]=1;watch[21]=(uint8)i;
    }
    if(!boot(&t))return;
    from=t5_ntx;
    for(i=0;i<16;i++){snprintf(name,sizeof(name),"APP_%d",i);hb(name,1);}
    cycle(NULL,0);cycle(NULL,0);
    for(i=0;i<16;i++)watch_pkt(0x18E0u+(uint32)i,24,0,1);
    for(i=0;i<16;i++)
    {
        snprintf(name,sizeof(name),"APP_%d",i);
        for(j=0;j<6;j++)event_input(name,CFE_EVS_EventType_ERROR);
    }
    for(i=0;i<8;i++)control(12,(uint8)i,1);
    health(2,1,0,12);cycle_at(NULL,0,t5_now_sec+3,0);
    s=expanded_status();
    if(s)for(i=0;i<57;i++)
    {
        const uint8 *r=s+EXT_STATUS_FAULTS+i*48;
        const int type=i<16?1:i<32?4:i<48?5:i<56?6:7;
        const int source=i<48?i%16:i<56?i-48:0;
        UtAssert_True(s[1036+i]==1&&s[1093+i]==1&&r[4]==type&&r[5]==source&&r[7]==1&&r[8]==1&&r[9]==1&&r[10]==0&&r[11]==0,
                      "fault slot%d exact source/type/state/report metadata",i);
        if(i<48){snprintf(name,sizeof(name),"APP_%d",source);UtAssert_True(strcmp((const char *)r+12,name)==0,"slot%d source name",i);}
    }
    UtAssert_True(t5_tx_count(M_FAULT,from)==57&&t5_tx_count(M_FAULT_TLM,from)==57,"one report pair for each of57 independent fault identities");
    {
        const t5_tx_t *health_packet=t5_last_tx(M_HEALTH,from);
        UtAssert_True(health_packet&&health_packet->size==20&&health_packet->bytes[6]==0&&health_packet->bytes[8]==1&&health_packet->bytes[9]==2&&health_packet->bytes[10]==57&&health_packet->bytes[11]==57,
                      "health FC0 publication counts all57 active/latched sources");
    }
}

static void Test_EnableAndActivationEdges(void)
{
    stbl_t table;const uint8 *r,*s;uint8 old_onset[48];int i,from;
    if(!boot_default())return;
    set_miss_limit(1);hb("GW",1);cycle(NULL,0);cycle(NULL,0);
    for(i=0;i<6;i++)event_input("GW",CFE_EVS_EventType_ERROR);
    s=expanded_status();if(s)UtAssert_True(s[1036+3]&&s[1036+35],"heartbeat/event-rate independent active identities");
    from=t5_ntx;control(7,3,0);
    report(from,1,3,0);report(from,5,3,0);
    s=expanded_status();if(s)UtAssert_True(!s[1036+3]&&!s[1036+35]&&s[1093+3]&&s[1093+35]&&s[8+3*36+20]==0,"app disable clears physical faults, preserves both latches and unarms");
    hb("GW",2);for(i=0;i<10;i++)event_input("GW",CFE_EVS_EventType_ERROR);
    s=expanded_status();if(s)UtAssert_True(s[8+3*36+20]==0&&s[8+3*36+33]==0,"disabled heartbeat/events ignored rather than unknown/armed");
    control(7,3,1);hb("GW",3);cycle(NULL,0);control(7,3,1);cycle(NULL,0);
    s=expanded_status();if(s)UtAssert_True(s[1036+3]==1,"no-change enabled command preserves existing arming");
    from=t5_ntx;watch_pkt(M_STALL,24,7,4);watch_pkt(M_STALL,24,7,4);watch_pkt(M_STALL,24,7,4);
    control(8,4,1);watch_pkt(M_STALL,24,7,4);
    r=report(from,2,4,1);if(r)memcpy(old_onset,r,48);
    control(12,1,1);
    stbl_default(&table);table.b[392+4*24+7]=2;table.b[392+4*24+21]=3;strcpy((char *)table.b+8+3*24,"NEW_GW");
    from=t5_ntx;t5_tbl_submit("SfiTable",table.b);cycle(NULL,0);
    r=report(from,2,4,0);if(r)UtAssert_True(r[12]==0&&memcmp(r+32,old_onset+32,8)==0,"table activation clears old STALL metadata before new RATE association");
    s=expanded_status();if(s)UtAssert_True(s[1036+49]&&s[1093+49]&&s[1093+20]&&!s[1036+20],"table activation retains independent LINK and watch latch");
    table.b[8+3*24+21]=0;t5_tbl_submit("SfiTable",table.b);cycle(NULL,0);from=t5_ntx;
    for(i=0;i<8;i++)event_input("NEW_GW",CFE_EVS_EventType_ERROR);
    UtAssert_True(t5_tx_count(M_FAULT,from)==0,"per-app EventRateEnabled0 gates ERROR windows independently of Enabled1");
}

static void register_expanded_cases(void)
{
    UtTest_Add(Test_EnableAndActivationEdges,sfi_setup,t5_teardown,"Enable baselines and table edge metadata");
    UtTest_Add(Test_FixedSlotMapping,sfi_setup,t5_teardown,"All57 fixed fault slots");
    UtTest_Add(Test_ExpandedValidation,sfi_setup,t5_teardown,"Expanded table validation");
    UtTest_Add(Test_LimitAndLatch,sfi_setup,t5_teardown,"Fault reports latches and controls");
    UtTest_Add(Test_LimitWidths,sfi_setup,t5_teardown,"LIMIT widths and persistence");
    UtTest_Add(Test_EventWindows,sfi_setup,t5_teardown,"ERROR sliding windows");
    UtTest_Add(Test_HealthAndSlots,sfi_setup,t5_teardown,"Health arming deadline and slots");
    UtTest_Add(Test_MixedHistory,sfi_setup,t5_teardown,"Mixed history attribution");
    UtTest_Add(Test_FaultDump,sfi_setup,t5_teardown,"Binary fault log wrap and errors");
}
