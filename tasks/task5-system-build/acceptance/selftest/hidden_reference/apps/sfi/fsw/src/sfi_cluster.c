#include "sfi_cluster.h"
static uint64 sfi_full(const CFE_TIME_SysTime_t *t) { return ((uint64)t->Seconds << 32) | t->Subseconds; }
void SFI_ComputeClusters(const CFE_TIME_SysTime_t *Onsets, uint16 Count, uint16 WindowSec, SFI_ClusterResult_t *Result)
{
    uint64 v[16], x;
    uint16 i, j, run;
    memset(Result, 0, sizeof(*Result));
    if (Count == 0) return;
    for (i = 0; i < Count && i < 16; i++) v[i] = sfi_full(&Onsets[i]);
    for (i = 1; i < Count; i++)
        for (j = i; j > 0 && v[j - 1] > v[j]; j--) { x = v[j]; v[j] = v[j - 1]; v[j - 1] = x; }
    Result->ClusterCount = 1;
    run = 1;
    Result->LargestClusterSize = 1;
    for (i = 1; i < Count; i++)
    {
        if (v[i] - v[i - 1] <= ((uint64)WindowSec << 32)) run++;
        else { Result->ClusterCount++; run = 1; }
        if (run > Result->LargestClusterSize) Result->LargestClusterSize = run;
    }
    Result->Correlation = Result->LargestClusterSize >= 2 ? 2 : 1;
}
