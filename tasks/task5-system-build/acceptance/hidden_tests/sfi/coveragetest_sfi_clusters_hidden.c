/*
 * Black-box test of SFI_ComputeClusters (spec S6.3) on chosen onset sets.
 *
 * The result type and the prototype are declared here from the specification; the function is
 * reached through its pinned name in fsw/src/sfi_cluster.c. Onsets are full CFE_TIME_SysTime_t
 * values (subseconds are 1/2^32 s), clusters are maximal runs of sorted onsets whose consecutive
 * gaps are at most W seconds.
 */
#include <stdio.h>
#include <string.h>

#include "utassert.h"
#include "uttest.h"
#include "utstubs.h"
#include "cfe.h"

typedef struct
{
    uint16 ClusterCount;
    uint16 LargestClusterSize;
    uint8  Correlation; /* 0 NONE, 1 UNCORRELATED, 2 CLUSTERED */
    uint8  Spare[3];
} cl_result_t;

void SFI_ComputeClusters(const CFE_TIME_SysTime_t *Onsets, uint16 Count, uint16 WindowSec, cl_result_t *Result);

#define NONE         0
#define UNCORRELATED 1
#define CLUSTERED    2
#define HALF         0x80000000u

static const char *corr_name(int c)
{
    return c == NONE ? "NONE" : c == UNCORRELATED ? "UNCORRELATED" : c == CLUSTERED ? "CLUSTERED" : "an undefined value";
}

static CFE_TIME_SysTime_t T(uint32 s, uint32 sub)
{
    CFE_TIME_SysTime_t t;
    t.Seconds = s;
    t.Subseconds = sub;
    return t;
}

/* Run the function on `n` onsets with window `w` and compare all three results. */
static void expect(const char *what, const CFE_TIME_SysTime_t *on, uint16 n, uint16 w, uint16 count, uint16 largest, uint8 corr)
{
    cl_result_t r;
    CFE_TIME_SysTime_t copy[16];
    memset(&r, 0xA5, sizeof(r));
    if (n > 0) memcpy(copy, on, n * sizeof(on[0]));
    SFI_ComputeClusters(on, n, w, &r);
    UtAssert_True(r.ClusterCount == count && r.LargestClusterSize == largest && r.Correlation == corr,
                  "%s (W %u): ClusterCount %u, LargestClusterSize %u, Correlation %s; expected %u, %u, %s", what, w,
                  r.ClusterCount, r.LargestClusterSize, corr_name(r.Correlation), count, largest, corr_name(corr));
    if (n > 0)
        UtAssert_True(memcmp(copy, on, n * sizeof(on[0])) == 0, "%s: the caller's onset array is left unchanged", what);
}

static void Setup(void) { UT_ResetState(0); }
static void Teardown(void) {}

static void Test_Empty(void)
{
    CFE_TIME_SysTime_t on[1];
    on[0] = T(100, 0);
    expect("no onsets", on, 0, 5, 0, 0, NONE);
}

static void Test_Single(void)
{
    CFE_TIME_SysTime_t on[1];
    on[0] = T(100, 0);
    expect("one onset", on, 1, 5, 1, 1, UNCORRELATED);
    on[0] = T(0, 0);
    expect("one onset at time 0", on, 1, 5, 1, 1, UNCORRELATED);
}

static void Test_EqualTimes(void)
{
    CFE_TIME_SysTime_t on[3];
    on[0] = T(100, HALF);
    on[1] = T(100, HALF);
    expect("two equal onsets", on, 2, 5, 1, 2, CLUSTERED);
    on[2] = T(100, HALF);
    expect("three equal onsets", on, 3, 1, 1, 3, CLUSTERED);
}

static void Test_GapExactlyWindow(void)
{
    CFE_TIME_SysTime_t on[3];
    on[0] = T(100, 0);
    on[1] = T(105, 0);
    expect("onsets exactly W seconds apart", on, 2, 5, 1, 2, CLUSTERED);
    on[0] = T(100, HALF);
    on[1] = T(105, HALF);
    expect("onsets exactly W seconds apart with equal subseconds", on, 2, 5, 1, 2, CLUSTERED);
    on[0] = T(100, 0xFFFFFFFFu);
    on[1] = T(105, 0xFFFFFFFFu);
    expect("onsets exactly W seconds apart at the end of a second", on, 2, 5, 1, 2, CLUSTERED);
    on[0] = T(100, HALF);
    on[1] = T(105, HALF - 1);
    expect("onsets just below W seconds apart", on, 2, 5, 1, 2, CLUSTERED);
}

static void Test_GapAboveWindow(void)
{
    CFE_TIME_SysTime_t on[3];
    on[0] = T(100, 0);
    on[1] = T(105, 1);
    expect("onsets W seconds and one subsecond tick apart", on, 2, 5, 2, 1, UNCORRELATED);
    on[0] = T(100, HALF);
    on[1] = T(105, HALF + 1);
    expect("onsets W seconds and one tick apart within a second", on, 2, 5, 2, 1, UNCORRELATED);
    on[0] = T(100, 0xFFFFFFFFu);
    on[1] = T(106, 0);
    expect("onsets W seconds and one tick apart across a second boundary", on, 2, 5, 2, 1, UNCORRELATED);
    on[0] = T(100, 0);
    on[1] = T(106, 0);
    on[2] = T(112, 0);
    expect("three onsets W + 1 seconds apart", on, 3, 5, 3, 1, UNCORRELATED);
}

static void Test_WindowOne(void)
{
    CFE_TIME_SysTime_t on[3];
    on[0] = T(200, 0);
    on[1] = T(201, 0);
    expect("onsets one second apart", on, 2, 1, 1, 2, CLUSTERED);
    on[1] = T(201, 1);
    expect("onsets one second and one tick apart", on, 2, 1, 2, 1, UNCORRELATED);
    on[0] = T(200, HALF);
    on[1] = T(201, 0);
    on[2] = T(201, HALF + 1);
    expect("half a second, then a second and a tick", on, 3, 1, 1, 3, CLUSTERED);
}

static void Test_Unsorted(void)
{
    CFE_TIME_SysTime_t on[5];
    on[0] = T(130, 0);
    on[1] = T(100, 0);
    on[2] = T(200, 0);
    on[3] = T(128, 0);
    on[4] = T(104, 0);
    expect("five unsorted onsets forming clusters {100, 104}, {128, 130}, {200}", on, 5, 5, 3, 2, CLUSTERED);
    on[0] = T(110, 0);
    on[1] = T(100, 0);
    on[2] = T(120, 0);
    expect("three unsorted onsets 10 s apart", on, 3, 5, 3, 1, UNCORRELATED);
    on[0] = T(105, 0);
    on[1] = T(100, 0);
    on[2] = T(110, 0);
    expect("three unsorted onsets that chain", on, 3, 5, 1, 3, CLUSTERED);
    on[0] = T(100, HALF);
    on[1] = T(100, 0);
    expect("two onsets in the same second, later one first", on, 2, 5, 1, 2, CLUSTERED);
}

static void Test_Chains(void)
{
    CFE_TIME_SysTime_t on[5];
    on[0] = T(100, 0);
    on[1] = T(104, 0);
    on[2] = T(108, 0);
    expect("a chain whose first and last onsets are more than W apart", on, 3, 5, 1, 3, CLUSTERED);
    on[3] = T(113, 0);
    on[4] = T(118, 0);
    expect("a chain of five, each gap at most W", on, 5, 5, 1, 5, CLUSTERED);
    on[4] = T(119, 0);
    expect("a chain of four, then a gap above W", on, 5, 5, 2, 4, CLUSTERED);
}

static void Test_Largest(void)
{
    CFE_TIME_SysTime_t on[6];
    on[0] = T(100, 0);
    on[1] = T(200, 0);
    on[2] = T(201, 0);
    on[3] = T(202, 0);
    on[4] = T(300, 0);
    on[5] = T(301, 0);
    expect("clusters of sizes 1, 3 and 2", on, 6, 5, 3, 3, CLUSTERED);
    on[0] = T(400, 0);
    on[1] = T(401, 0);
    on[2] = T(500, 0);
    on[3] = T(600, 0);
    on[4] = T(601, 0);
    on[5] = T(602, 0);
    expect("clusters of sizes 2, 1 and 3", on, 6, 5, 3, 3, CLUSTERED);
}

static void Test_Sixteen(void)
{
    CFE_TIME_SysTime_t on[16];
    int i;
    for (i = 0; i < 16; i++) on[i] = T(1000u + 10u * (uint32)i, 0);
    expect("sixteen onsets 10 s apart", on, 16, 5, 16, 1, UNCORRELATED);
    for (i = 0; i < 16; i++) on[i] = T(1000u + (uint32)(15 - i), 0);
    expect("sixteen onsets 1 s apart in reverse order", on, 16, 5, 1, 16, CLUSTERED);
    for (i = 0; i < 16; i++) on[i] = T(1000u + 6u * (uint32)(i / 2), 0);
    expect("eight pairs of equal onsets 6 s apart", on, 16, 5, 8, 2, CLUSTERED);
}

static void Test_LargeTimes(void)
{
    CFE_TIME_SysTime_t on[3];
    on[0] = T(4000000000u, 0);
    on[1] = T(4000000005u, 0);
    on[2] = T(3999999990u, 0);
    expect("onsets above 2^31 seconds", on, 3, 5, 2, 2, CLUSTERED);
    on[0] = T(0xFFFFFFFFu, 0);
    on[1] = T(0, 0);
    expect("onsets at the two ends of the seconds range", on, 2, 600, 2, 1, UNCORRELATED);
    on[0] = T(0xFFFFFFFAu, 0);
    on[1] = T(0xFFFFFFFFu, 1);
    expect("onsets near the end of the seconds range, W seconds and one tick apart", on, 2, 5, 2, 1, UNCORRELATED);
    on[1] = T(0xFFFFFFFFu, 0);
    expect("onsets near the end of the seconds range, exactly W seconds apart", on, 2, 5, 1, 2, CLUSTERED);
}

/* Onsets in read-only storage: an implementation that casts the qualifier away and sorts the caller's array
 * in place faults here instead of returning. */
static const CFE_TIME_SysTime_t ReadOnlyOnsets[5] = {{40, 0}, {10, 0}, {30, 0}, {12, HALF}, {20, 0}};

static void Test_ReadOnly(void)
{
    expect("unsorted onsets in read-only storage", ReadOnlyOnsets, 5, 3, 4, 2, CLUSTERED);
    expect("the same onsets with a window of 10 s", ReadOnlyOnsets, 5, 10, 1, 5, CLUSTERED);
}

void UtTest_Setup(void)
{
    UtTest_Add(Test_Empty, Setup, Teardown, "No recent misses");
    UtTest_Add(Test_Single, Setup, Teardown, "A single miss");
    UtTest_Add(Test_EqualTimes, Setup, Teardown, "Equal onset times");
    UtTest_Add(Test_GapExactlyWindow, Setup, Teardown, "Gap of exactly W");
    UtTest_Add(Test_GapAboveWindow, Setup, Teardown, "Gap just above W");
    UtTest_Add(Test_WindowOne, Setup, Teardown, "Window of one second");
    UtTest_Add(Test_Unsorted, Setup, Teardown, "Unsorted onsets");
    UtTest_Add(Test_Chains, Setup, Teardown, "Chained onsets");
    UtTest_Add(Test_Largest, Setup, Teardown, "Largest cluster");
    UtTest_Add(Test_Sixteen, Setup, Teardown, "Sixteen onsets");
    UtTest_Add(Test_LargeTimes, Setup, Teardown, "Large onset times");
    UtTest_Add(Test_ReadOnly, Setup, Teardown, "Onsets in read-only storage");
}
