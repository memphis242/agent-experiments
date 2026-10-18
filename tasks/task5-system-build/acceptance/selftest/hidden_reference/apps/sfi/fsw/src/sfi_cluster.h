#ifndef SFI_CLUSTER_H
#define SFI_CLUSTER_H
#include "cfe.h"
typedef struct { uint16 ClusterCount; uint16 LargestClusterSize; uint8 Correlation; uint8 Spare[3]; } SFI_ClusterResult_t;
void SFI_ComputeClusters(const CFE_TIME_SysTime_t *Onsets, uint16 Count, uint16 WindowSec, SFI_ClusterResult_t *Result);
#endif
