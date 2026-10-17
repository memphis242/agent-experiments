#include "utassert.h"
#include "uttest.h"
#include "utstubs.h"
#include "stor.h"

static void Test_Link(void)
{
    UtAssert_True(sizeof(STOR_StorageTable_t) == 400, "table size");
}

void UtTest_Setup(void)
{
    UtTest_Add(Test_Link, NULL, NULL, "Link");
}
