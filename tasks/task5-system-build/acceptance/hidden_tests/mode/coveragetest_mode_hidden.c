/* Drive production MODE through its public SB entry point and native target commands. */
#include "t5_model.h"
#include "cfe_es_msg.h"
#include <stddef.h>

extern void MODE_AppMain(void);
#define MCMD 0x18F0u
#define MHKREQ 0x18F1u
#define MWAKE 0x18F2u
#define MHK 0x08F0u
#define MSTATUS 0x08F1u
#define MFAULT 0x18F3u
#define MPEER 0x187Bu
#define MES 0x1806u
#define MTLMO 0x18D4u
#define MSTOR 0x18C4u
#define MGW 0x18D8u
#define RESTART_BASE 16
#define RESPONSE_BASE 464
#define RESPONSE_SIZE 148
#define MODE_STATUS_SIZE 708

typedef struct { uint8 b[1500]; } mtbl_t;
static bool present[4];
static const char *const names[4]={"STOR","CMDI","TLMO","GW"};
static const char *const entries[4]={"STOR_AppMain","CMDI_AppMain","TLMO_AppMain","GW_AppMain"};
static const char *const paths[4]={"/cf/stor.so","/cf/cmdi.so","/cf/tlmo.so","/cf/gw.so"};

static void presence(void *ud,UT_EntryKey_t key,const UT_StubContext_t *ctx)
{
    const char *name=UT_Hook_GetArgValueByName(ctx,"AppName",const char *);
    CFE_ES_AppId_t *id=UT_Hook_GetArgValueByName(ctx,"AppIdPtr",CFE_ES_AppId_t *);
    int i;(void)ud;
    for(i=0;i<4;i++)if(strcmp(name,names[i])==0&&present[i])
    {
        *id=CFE_ES_APPID_C((unsigned long)i+1);t5_ret(key,CFE_SUCCESS);return;
    }
    t5_ret(key,CFE_ES_ERR_NAME_NOT_FOUND);
}

static void defaults(mtbl_t *table)
{
    static const uint8 profiles[12]={0,80,255,0,1,65,255,0,2,50,3,0};
    static const uint8 flags[7][5]={{1,1,0,0,1},{0,1,0,0,1},{0,1,0,0,1},{0,1,0,0,2},{0,1,1,0,1},{0,0,0,1,1},{0,0,0,1,1}};
    static const uint32 stacks[4]={131072,32768,32768,131072};
    static const uint16 priorities[4]={70,40,45,70};
    int i;
    memset(table,0,sizeof(*table));table->b[0]=1;table->b[1]=1;table->b[2]=10;table->b[3]=20;
    memcpy(table->b+4,profiles,12);
    for(i=0;i<4;i++)
    {
        uint8 *r=table->b+RESTART_BASE+i*112;
        strcpy((char *)r,names[i]);strcpy((char *)r+20,entries[i]);strcpy((char *)r+40,paths[i]);
        t5_put32(r+104,stacks[i]);t5_put16(r+108,priorities[i]);
    }
    for(i=0;i<7;i++)
    {
        uint8 *r=table->b+RESPONSE_BASE+i*RESPONSE_SIZE;
        r[0]=flags[i][4];r[1]=flags[i][0];r[2]=flags[i][1];r[3]=flags[i][2];r[4]=flags[i][3];
        r[6]=1;r[7]=200;t5_put16(r+8,30);r[10]=3;t5_put32(r+12,0x0801);
        strcpy((char *)r+20,"mode/response.bin");
    }
}

static void mode_setup(void)
{
    mtbl_t t;int i;
    t5_setup();t5_app_entry=MODE_AppMain;defaults(&t);t5_tbl_set_file("ModeTable",t.b,sizeof(t));
    for(i=0;i<4;i++)present[i]=true;
    UT_SetHandlerFunction(UT_KEY(CFE_ES_GetAppIDByName),presence,NULL);
}
static bool mcmd(int code,const void *p,size_t n){return t5_send_cmd(MCMD,code,p,n);}
static void mcontrol(int code,int value){const uint8 p[4]={(uint8)value,0,0,0};mcmd(code,p,4);}
static void tick(uint32 seconds,uint32 subseconds)
{
    t5_now_sec=seconds;t5_now_sub=subseconds;t5_send_cmd(MWAKE,0,NULL,0);
}
static bool mode_boot(void)
{
    if(!t5_app_start())return false;
    tick(100,0);tick(101,0);return true;
}
static const uint8 *mstatus(void)
{
    const int from=t5_ntx;const t5_tx_t *t;
    mcmd(4,NULL,0);t=t5_last_tx(MSTATUS,from);
    UtAssert_True(t&&t->size==16+MODE_STATUS_SIZE,"MODE status full native payload708 bytes");
    return t&&t->size==16+MODE_STATUS_SIZE?t->bytes+16:NULL;
}
static const uint8 *mhk(void)
{
    const int from=t5_ntx;const t5_tx_t *t;
    t5_send_cmd(MHKREQ,0,NULL,0);t=t5_last_tx(MHK,from);
    UtAssert_True(t&&t->size==64,"MODE housekeeping full48-byte payload");
    return t&&t->size==64?t->bytes+16:NULL;
}
static void fault(uint8 type,uint8 source,uint8 severity,uint8 active,uint32 sequence,const char *name)
{
    uint8 p[48]={0};t5_put32(p,sequence);p[4]=type;p[5]=source;p[6]=severity;p[7]=active;p[8]=1;p[9]=1;
    snprintf((char *)p+12,20,"%s",name);t5_put32(p+32,90);t5_put32(p+40,t5_now_sec);
    t5_send_cmd(MFAULT,0,p,sizeof(p));
}
static void peer(uint8 src,uint8 dst,uint8 mode,int code,int length,uint32 sequence)
{
    uint8 p[16]={0};p[0]=src;p[1]=dst;p[2]=mode;t5_put32(p+4,sequence);t5_put32(p+8,1);
    t5_send_cmd(MPEER,code,p,(size_t)length);
}
static const t5_tx_t *target(uint32 mid,int code,int from)
{
    int i;for(i=from;i<t5_ntx;i++)if(t5_tx[i].mid==mid&&t5_tx[i].size>=8&&t5_tx[i].bytes[6]==code)return &t5_tx[i];
    return NULL;
}
static int target_count(uint32 mid,int code,int from)
{
    int i,count=0;for(i=from;i<t5_ntx;i++)if(t5_tx[i].mid==mid&&t5_tx[i].size>=8&&t5_tx[i].bytes[6]==code)count++;
    return count;
}
static void profile_check(int from,int mode)
{
    static const uint8 purge[3]={80,65,50};
    const t5_tx_t *tlmo=target(MTLMO,8,from),*stor=target(MSTOR,4,from),*gw=target(MGW,9,from);
    UtAssert_True(tlmo&&stor&&gw,"profile sends SELECT_PROFILE, SET_PURGE_THRESHOLD, SET_ENABLED_MASK");
    if(!tlmo||!stor||!gw)return;
    UtAssert_True(tlmo<stor&&stor<gw&&tlmo->size==12&&stor->size==12&&gw->size==12,"profile target order and native4-byte payloads");
    UtAssert_True(tlmo->bytes[8]==mode&&stor->bytes[8]==purge[mode]&&gw->bytes[8]==(mode==2?3:255),"profile%d exact target values",mode);
    UtAssert_True(!tlmo->bytes[9]&&!tlmo->bytes[10]&&!tlmo->bytes[11]&&!stor->bytes[9]&&!gw->bytes[9],"profile spare bytes0");
}

static void Test_StartupProfiles(void)
{
    const uint8 *s,*h;int from=t5_ntx,i;
    present[3]=false;
    if(!t5_app_start())return;
    tick(100,0);tick(101,0);
    UtAssert_True(target(MTLMO,8,from)==NULL,"initial profile waits for all ES registrations");
    present[3]=true;from=t5_ntx;tick(102,0);tick(103,0);profile_check(from,0);
    UtAssert_True(target_count(MTLMO,8,from)==1,"initial profile exactly once within2 wakeups");
    s=mstatus();if(s)UtAssert_True(s[0]==0&&s[1]==1&&s[4]==255&&s[3]==0&&s[5]==0&&s[6]==255&&s[7]==0,"initial mode peer diagnostics and idle restart");
    UtAssert_True(t5_subscribed(MFAULT)&&t5_sub_msglim(MFAULT)==64&&t5_subscribed(MPEER)&&t5_sub_msglim(MPEER)==8,"fault and mode subscriptions limits64/8");
    UtAssert_True(t5_last_ev_type(1)==CFE_EVS_EventType_INFORMATION,"MODE INIT event type INFORMATION");
    for(i=0;i<T5_SUBS_MAX;i++)if(t5_subs[i].used&&t5_subs[i].mid==MFAULT)
        UtAssert_True(t5_pipes[t5_subs[i].pipe].depth>=128,"MODE main pipe depth at least128");
    from=t5_ntx;mcontrol(3,0);tick(104,0);UtAssert_True(target(MTLMO,8,from)==NULL,"same-mode command does not reapply profile");
    mcmd(7,NULL,0);tick(105,0);profile_check(from,0);
    from=t5_ntx;mcontrol(3,1);tick(106,0x80000000u);profile_check(from,1);
    s=mstatus();if(s)UtAssert_True(t5_get32(s+12)==106,"healthy DEGRADED timer starts on entry");
    mcontrol(3,1);tick(120,0);tick(126,0x7FFFFFFFu);h=mhk();if(h)UtAssert_True(h[2]==1,"same mode preserves fractional recovery deadline");
    tick(126,0x80000000u);h=mhk();if(h)UtAssert_True(h[2]==0,"recovery exactly20 full-time seconds");
}

static void Test_TableBounds(void)
{
    mtbl_t good,t;int field,i;uint8 *r;
    if(!mode_boot())return;
    defaults(&good);
    UtAssert_True(t5_tbl_validate("ModeTable",good.b)==0,"all pinned defaults validate");
    for(field=0;field<8;field++)
    {
        t=good;
        switch(field){case 0:t.b[0]=2;break;case 1:t.b[1]=2;break;case 2:t.b[2]=2;break;case 3:t.b[2]=61;break;case 4:t.b[3]=0;break;case 5:t.b[3]=121;break;case 6:t.b[4]=3;break;default:t.b[5]=0;break;}
        UtAssert_True(t5_tbl_validate("ModeTable",t.b)!=0,"table global/profile invalid field%d",field);
    }
    for(i=0;i<4;i++)for(field=0;field<10;field++)
    {
        t=good;r=t.b+16+i*112;
        switch(field)
        {
            case 0:memset(r,'x',20);break;case 1:r[20]=0;break;case 2:memset(r+20,'x',20);break;
            case 3:r[40]='x';break;case 4:memset(r+40,'x',64);break;case 5:r[20]='x';break;
            case 6:t5_put32(r+104,16383);break;case 7:t5_put32(r+104,262145);break;
            case 8:t5_put16(r+108,0);break;default:t5_put16(r+108,256);break;
        }
        UtAssert_True(t5_tbl_validate("ModeTable",t.b)!=0,"restart%d invalid pinned field%d",i,field);
    }
    t=good;memcpy(t.b+128,t.b+16,112);UtAssert_True(t5_tbl_validate("ModeTable",t.b)!=0,"duplicate restart app rejected");
    t=good;strcpy((char *)t.b+16,"MODE");UtAssert_True(t5_tbl_validate("ModeTable",t.b)!=0,"MODE itself not restart allow-list");
    for(i=0;i<7;i++)for(field=0;field<18;field++)
    {
        t=good;r=t.b+464+i*148;r[2]=r[3]=r[4]=1;
        switch(field)
        {
            case 0:r[0]=0;break;case 1:r[0]=3;break;case 2:r[1]=2;break;case 3:r[2]=2;break;
            case 4:r[3]=2;break;case 5:r[4]=2;break;case 6:r[6]=2;break;
            case 7:r[5]=16;break;case 8:r[7]=0;break;case 9:t5_put32(r+12,0);break;
            case 10:t5_put16(r+8,9);break;case 11:t5_put16(r+8,301);break;
            case 12:r[10]=0;break;case 13:r[10]=6;break;case 14:r[16]=1;break;
            case 15:r[17]=1;break;case 16:r[18]=1;break;default:strcpy((char *)r+20,"../escape");break;
        }
        UtAssert_True(t5_tbl_validate("ModeTable",t.b)!=0,"response%d invalid field%d",i,field);
    }
    t=good;r=t.b+464;r[1]=r[2]=r[3]=r[4]=0;r[5]=255;r[7]=0;t5_put32(r+12,0);memset(r+20,'x',128);
    UtAssert_True(t5_tbl_validate("ModeTable",t.b)==0,"disabled action fields need not validate");
}

static void Test_ReportValidationAndSerial(void)
{
    const uint8 *s;int i,from;uint8 p[48]={0};
    if(!mode_boot())return;
    from=t5_nev;
    for(i=0;i<10;i++)
    {
        memset(p,0,sizeof(p));t5_put32(p,1);p[4]=1;p[5]=3;p[6]=1;p[7]=1;p[8]=1;p[9]=1;strcpy((char *)p+12,"GW");
        switch(i){case 0:p[4]=0;break;case 1:p[4]=8;break;case 2:p[5]=16;break;case 3:p[6]=0;break;case 4:p[6]=3;break;case 5:p[7]=2;break;case 6:p[8]=2;break;case 7:memset(p+12,'x',20);break;default:break;}
        t5_send_cmd(MFAULT,i==8?1:0,p,i==9?47:48);
    }
    UtAssert_True(t5_count_ev(21,from)==10,"all malformed reports rejected with FAULT_ERR once each");
    UtAssert_True(t5_last_ev_type(21)==CFE_EVS_EventType_ERROR,"FAULT_ERR event type ERROR");
    p[9]=2;t5_send_cmd(MFAULT,0,p,48);s=mstatus();if(s)UtAssert_True(s[24+3*12]==0,"other-node report ignored");
    fault(1,3,1,1,0xFFFFFFFFu,"GW");fault(1,3,1,0,0,"GW");
    fault(1,3,1,1,0,"GW");fault(1,3,1,1,0xFFFFFFFFu,"GW");fault(1,3,1,1,0x80000000u,"GW");
    s=mstatus();if(s)UtAssert_True(s[24+3*12]==0&&t5_get32(s+24+3*12+8)==0,"wrap accepts FFFFFFFF->0; duplicate stale half-range ignored");
    fault(4,6,2,1,1,"");fault(6,7,1,1,1,"GW");fault(7,0,1,1,1,"");
    s=mstatus();if(s)UtAssert_True(s[24+22*12]&&s[24+55*12]&&s[24+56*12],"exact watch/link/peer slot mapping");
}

static void Test_PrecedenceRecoveryDisable(void)
{
    const uint8 *s,*h;int from;
    if(!mode_boot())return;
    mcontrol(2,0);fault(4,6,2,1,1,"");tick(102,0);h=mhk();if(h)UtAssert_True(h[2]==0&&h[4]==1,"disabled monitoring retains active fault without transition");
    mcontrol(2,1);tick(103,0);h=mhk();if(h)UtAssert_True(h[2]==2,"re-enable reevaluates active critical fault");
    from=t5_nev;mcontrol(3,0);mcontrol(3,1);UtAssert_True(t5_count_ev(14,from)==2,"critical blocks commanded SAFE exits");
    fault(4,6,2,0,2,"");tick(104,0);tick(150,0);h=mhk();if(h)UtAssert_True(h[2]==2,"SAFE never auto-recovers after clear");
    mcontrol(3,1);tick(151,0x80000000u);s=mstatus();if(s)UtAssert_True(s[0]==1&&t5_get32(s+12)==151,"guarded healthy SAFE->DEGRADED timer");
    fault(2,4,1,1,1,"");tick(160,0);s=mstatus();if(s)UtAssert_True(t5_get32(s+12)==0,"new active warning resets healthy interval");
    fault(2,4,1,0,2,"");tick(161,0x80000000u);mcmd(1,NULL,0);tick(181,0x7FFFFFFFu);
    h=mhk();if(h)UtAssert_True(h[2]==1,"RESET counters preserves fractional recovery timer");
    tick(181,0x80000000u);h=mhk();if(h)UtAssert_True(h[2]==0,"inactive latched fault permits20s recovery");
    fault(4,6,2,1,3,"");fault(4,6,2,0,4,"");tick(182,0);h=mhk();if(h)UtAssert_True(h[2]==2,"transient critical onset has minimum one-wakeup SAFE demand");
    mcontrol(3,0);mcontrol(3,1);tick(183,0);h=mhk();if(h)UtAssert_True(h[2]==1,"last accepted command wins while guards clear");
}

static void Test_RestartSnapshotAndOrder(void)
{
    mtbl_t table;const t5_tx_t *stop,*start,*priority;const uint8 *s;int from;
    CFE_ES_StartAppCmd_Payload_t native;
    if(!mode_boot())return;
    from=t5_ntx;fault(1,3,1,1,1,"GW");tick(102,0);
    stop=target(MES,5,from);priority=target(MSTOR,2,from);
    UtAssert_True(stop&&priority&&stop<priority,"restart STOP precedes SET_PRIORITY in attempt wakeup");
    if(stop)UtAssert_True(stop->size==28&&strcmp((const char *)stop->bytes+8,"GW")==0,"native STOP target GW");
    if(priority)UtAssert_True(priority->size==16&&t5_get32(priority->bytes+8)==0x0801&&priority->bytes[12]==200,"SET_PRIORITY exact native MID/priority payload");
    defaults(&table);t5_put32(table.b+16+3*112+104,65536);t5_put16(table.b+16+3*112+108,71);
    t5_tbl_submit("ModeTable",table.b);tick(103,0);present[3]=false;tick(104,0);
    UtAssert_True(target(MES,4,from)==NULL,"absence observed after STOP defers START to next wakeup");
    tick(105,0);start=target(MES,4,from);
    UtAssert_True(start&&start->size==8+sizeof(native),"START uses bundled CFE_ES_StartAppCmd_Payload_t");
    if(start&&start->size==8+sizeof(native))
    {
        memcpy(&native,start->bytes+8,sizeof(native));
        UtAssert_True(strcmp(native.Application,"GW")==0&&strcmp(native.AppEntryPoint,"GW_AppMain")==0&&strcmp(native.AppFileName,"/cf/gw.so")==0&&native.StackSize==131072&&native.Priority==70&&native.ExceptionAction==0,"START retains dispatch snapshot across table activation");
    }
    present[3]=true;tick(110,0);s=mstatus();if(s)UtAssert_True(s[7]==0&&s[24+3*12]==1&&s[24+3*12+1]==1&&t5_get32(s+24+3*12+4)==132,"registration at exact deadline completes restart but does not clear physical fault; cooldown retained");
    fault(1,3,1,0,2,"GW");fault(1,3,1,1,3,"GW");from=t5_ntx;tick(132,0);present[3]=false;tick(133,0);tick(134,0);
    start=target(MES,4,from);if(start&&start->size==8+sizeof(native)){memcpy(&native,start->bytes+8,sizeof(native));UtAssert_True(native.StackSize==65536&&native.Priority==71,"subsequent attempt uses newly activated restart parameters");}
}

static void Test_CooldownCap(void)
{
    const uint8 *s,*h;int iteration,from;
    if(!mode_boot())return;
    present[3]=false;
    from=t5_ntx;
    for(iteration=0;iteration<3;iteration++)
    {
        const uint32 base=102u+(uint32)iteration*30u;
        fault(1,3,1,1,(uint32)iteration*2u+1u,"GW");tick(base,0x80000000u);tick(base+1,0x80000000u);
        present[3]=true;tick(base+2,0x80000000u);fault(1,3,1,0,(uint32)iteration*2u+2u,"GW");
        mcmd(1,NULL,0);mcontrol(6,4); /* an unrelated inactive reset cannot release GW cap */
        present[3]=false;
    }
    UtAssert_True(target_count(MES,4,from)==3,"three successful START transmissions across recurrences");
    tick(192,0x7FFFFFFFu);s=mstatus();if(s)UtAssert_True(s[24+3*12+2]==0,"inactive third attempt does not exhaust at cap");
    fault(1,3,1,1,7,"GW");tick(192,0x7FFFFFFFu);s=mstatus();if(s)UtAssert_True(s[24+3*12+2]==0,"full-time eligibility not rounded down");
    tick(192,0x80000000u);s=mstatus();if(s)UtAssert_True(s[0]==2&&s[24+3*12+2]==1&&s[24+3*12+1]==3,"eligible fourth recurrence exhausts in SAFE with3 attempts");
    tick(193,0);UtAssert_True(target_count(MES,4,from)==3&&t5_count_ev(17,0)==1,"no fourth START and RETRY_ERR emitted once");
    UtAssert_True(t5_last_ev_type(17)==CFE_EVS_EventType_ERROR,"RETRY_ERR event type ERROR");
    mcontrol(6,3);s=mstatus();if(s)UtAssert_True(s[24+3*12+2]==1,"active RESET_RESPONSE rejected");
    fault(1,3,1,0,8,"GW");mcmd(1,NULL,0);tick(194,0);h=mhk();if(h)UtAssert_True(h[2]==2,"clear and counter reset preserve SAFE exhaustion");
    mcontrol(6,3);mcontrol(3,0);tick(195,0);s=mstatus();if(s)UtAssert_True(s[0]==0&&s[24+3*12+1]==0&&s[24+3*12+2]==0,"inactive RESET_RESPONSE then explicit exit restores NOMINAL");
}

static void Test_PeerTimers(void)
{
    const uint8 *s,*h;int from;
    if(!mode_boot())return;
    tick(200,0);h=mhk();if(h)UtAssert_True(h[7]==0,"no peer loss before first contact");
    from=t5_nev;peer(1,2,0,0,12,1);peer(3,1,0,0,12,1);peer(2,2,0,0,12,1);peer(2,1,3,0,12,1);peer(2,1,0,1,12,1);peer(2,1,0,0,11,1);
    UtAssert_True(t5_count_ev(22,from)==5,"own peer ignored; malformed peers rejected once");
    t5_now_sec=300;peer(2,1,2,0,12,77);tick(309,0xFFFFFFFFu);s=mstatus();if(s)UtAssert_True(s[0]==0&&s[4]==2&&s[5]==0,"peer SAFE diagnostic never forces local mode and no early expiry");
    tick(310,0);s=mstatus();if(s)UtAssert_True(s[0]==1&&s[5]==1,"independent peer mode expiry at10s degrades");
    peer(2,1,0,0,12,78);tick(311,0);fault(7,0,1,1,1,"");tick(340,0);h=mhk();if(h)UtAssert_True(h[2]==1,"independent SFI NODE_LOSS blocks recovery after mode restored");
    fault(7,0,1,0,2,"");mcontrol(5,0);
    t5_now_sec=400;peer(2,1,2,0,12,99);s=mstatus();if(s)UtAssert_True(s[3]==0&&s[4]==2&&s[5]==0,"disabled peer diagnostics update without arming");
    mcontrol(5,1);tick(500,0);s=mstatus();if(s)UtAssert_True(s[3]==0&&s[5]==0,"re-enable requires subsequent valid contact");
    from=t5_ntx;mcontrol(2,0);tick(501,0);UtAssert_True(t5_tx_count(MPEER,from)==1&&t5_tx_count(0x18DF,from)==1,"publication and heartbeat continue disabled");
    UtAssert_True(t5_last_ev_type(18)==CFE_EVS_EventType_ERROR&&t5_last_ev_type(19)==CFE_EVS_EventType_INFORMATION&&t5_last_ev_type(22)==CFE_EVS_EventType_ERROR,"peer loss/restored/error event types");
    {
        const t5_tx_t *publication=t5_last_tx(MPEER,from),*hb=t5_last_tx(0x18DF,from);
        UtAssert_True(publication&&publication->size==20&&publication->bytes[6]==0&&publication->bytes[8]==1&&publication->bytes[9]==2&&publication->bytes[10]<=2&&t5_get32(publication->bytes+16)==501,
                      "global peer FC0 native source destination current mode and onboard seconds");
        UtAssert_True(hb&&hb->size==32&&strcmp((const char *)hb->bytes+8,"MODE")==0,"MODE C.5 heartbeat payload names MODE");
    }
}

static void Test_CommandCounters(void)
{
    const uint8 *h;uint8 junk[4]={0};int from;
    if(!mode_boot())return;
    mcmd(1,NULL,0);from=t5_nev;
    mcmd(99,NULL,0);mcmd(0,junk,4);mcontrol(2,2);mcontrol(3,3);mcontrol(5,2);mcontrol(6,57);
    h=mhk();if(h)UtAssert_True(h[0]==0&&h[1]==6,"each rejected command increments only error counter once");
    UtAssert_True(t5_count_ev(2,from)==1&&t5_count_ev(6,from)==1&&t5_count_ev(14,from)==4,"unknown,length,control event IDs");
    mcmd(0,NULL,0);mcmd(4,NULL,0);mcmd(7,NULL,0);h=mhk();if(h)UtAssert_True(h[0]==3&&h[1]==6,"request commands counted; HK and wakeup excluded");
    mcmd(1,NULL,0);h=mhk();if(h)UtAssert_True(h[0]==0&&h[1]==0&&t5_get32(h+8)==0&&t5_get32(h+12)==0,"RESET_COUNTERS zeroes all cumulative counts");
}

#include "mode_action_cases.h"

void UtTest_Setup(void)
{
    UtTest_Add(Test_SimultaneousDemands,mode_setup,t5_teardown,"Simultaneous demands and decision guard");
    UtTest_Add(Test_AbsenceClearDisable,mode_setup,t5_teardown,"Initial absence clear continuation and disable");
    UtTest_Add(Test_DeadlinePresenceWins,mode_setup,t5_teardown,"Presence wins exact restart deadlines");
    UtTest_Add(Test_TableAcceptedBoundsAndPaths,mode_setup,t5_teardown,"Accepted bounds and unsafe transfer paths");
    UtTest_Add(Test_ResetStateAndForeignMid,mode_setup,t5_teardown,"Counter reset non-effects and foreign MID");
    UtTest_Add(Test_ActionsAndRouteRelease,mode_setup,t5_teardown,"Action payloads and shared route cleanup");
    UtTest_Add(Test_RestartResourceTimeouts,mode_setup,t5_teardown,"Restart resource and exact timeouts");
    UtTest_Add(Test_TransmissionFailures,mode_setup,t5_teardown,"Action transmission failures");
    UtTest_Add(Test_MissingRestartMatch,mode_setup,t5_teardown,"Missing restart target isolation");
    UtTest_Add(Test_StartupProfiles,mode_setup,t5_teardown,"Startup profiles and exact recovery");
    UtTest_Add(Test_TableBounds,mode_setup,t5_teardown,"Mode table bounds and defaults");
    UtTest_Add(Test_ReportValidationAndSerial,mode_setup,t5_teardown,"Fault validation serials and slots");
    UtTest_Add(Test_PrecedenceRecoveryDisable,mode_setup,t5_teardown,"Precedence SAFE recovery and disable");
    UtTest_Add(Test_RestartSnapshotAndOrder,mode_setup,t5_teardown,"Native restart snapshot and action order");
    UtTest_Add(Test_CooldownCap,mode_setup,t5_teardown,"Cooldown recurrences cap and reset");
    UtTest_Add(Test_PeerTimers,mode_setup,t5_teardown,"Independent peer timers and publication");
    UtTest_Add(Test_CommandCounters,mode_setup,t5_teardown,"Mode command counter semantics");
}
