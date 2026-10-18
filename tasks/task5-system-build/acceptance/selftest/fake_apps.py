#!/usr/bin/env python3
"""Add do-nothing stand-ins of the six Task 5 components to a mission tree, so that the hidden tests can be
compiled, linked and run against something (every behavioural case must then fail, none may crash or hang).

Usage: fake_apps.py <tree> [--hidden]   (--hidden also wires each hidden test in, as the review does)
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parent

LOOP_APP = """#include "cfe.h"
void {prefix}_AppMain(void)
{{
    CFE_SB_PipeId_t  pipe;
    CFE_SB_Buffer_t *buf;
    uint32           run = CFE_ES_RunStatus_APP_RUN;
    CFE_SB_CreatePipe(&pipe, 16, "{prefix}_FAKE");
    while (CFE_ES_RunLoop(&run))
    {{
        if (CFE_SB_ReceiveBuffer(&buf, pipe, CFE_SB_PEND_FOREVER) != CFE_SUCCESS)
        {{
            break;
        }}
    }}
    CFE_ES_ExitApp(run);
}}
"""

CMDVAL_FAKE = """#include "cfe.h"
#include "cmdval_lib.h"
int32 CMDVAL_LibInit(void) { return CFE_SUCCESS; }
uint16 CMDVAL_Crc16(const uint8 *Data, size_t Len) { (void)Data; (void)Len; return 0; }
void CMDVAL_Sha256(const uint8 *Data, size_t Len, uint8 Digest[32]) { (void)Data; (void)Len; memset(Digest, 0, 32); }
void CMDVAL_HmacSha256(const uint8 *Key, size_t KeyLen, const uint8 *Data, size_t Len, uint8 Mac[32])
{ (void)Key; (void)KeyLen; (void)Data; (void)Len; memset(Mac, 0, 32); }
CMDVAL_Status_t CMDVAL_CheckSource(uint16 SourceId, const CMDVAL_Config_t *Config) { (void)SourceId; (void)Config; return CMDVAL_OK; }
CMDVAL_Status_t CMDVAL_CheckAge(uint32 F, uint32 N, uint32 M) { (void)F; (void)N; (void)M; return CMDVAL_OK; }
CMDVAL_Status_t CMDVAL_ValidateFrame(const uint8 *Frame, size_t FrameLen, const CMDVAL_Config_t *Config, uint32 NowSeconds,
                                     CMDVAL_FrameInfo_t *Info)
{ (void)Frame; (void)FrameLen; (void)Config; (void)NowSeconds; (void)Info; return CMDVAL_OK; }
"""

CMDVAL_HDR = """#ifndef CMDVAL_LIB_H
#define CMDVAL_LIB_H
#include "common_types.h"
#include <stddef.h>
#include <string.h>
typedef enum { CMDVAL_OK = 0, CMDVAL_ERR_FORMAT = 1, CMDVAL_ERR_CRC = 2, CMDVAL_ERR_HMAC = 3, CMDVAL_ERR_SOURCE = 4,
               CMDVAL_ERR_AGE = 5 } CMDVAL_Status_t;
typedef struct { uint8 HmacKey[32]; uint16 AllowedSources[8]; uint32 MaxAgeSeconds; uint8 EnableMask; uint8 Spare[3]; } CMDVAL_Config_t;
typedef struct { uint16 SourceId; uint32 TimeSeconds; uint32 Sequence; const uint8 *Command; size_t CommandLen; } CMDVAL_FrameInfo_t;
int32 CMDVAL_LibInit(void);
uint16 CMDVAL_Crc16(const uint8 *Data, size_t Len);
void CMDVAL_Sha256(const uint8 *Data, size_t Len, uint8 Digest[32]);
void CMDVAL_HmacSha256(const uint8 *Key, size_t KeyLen, const uint8 *Data, size_t Len, uint8 Mac[32]);
CMDVAL_Status_t CMDVAL_CheckSource(uint16 SourceId, const CMDVAL_Config_t *Config);
CMDVAL_Status_t CMDVAL_CheckAge(uint32 FrameSeconds, uint32 NowSeconds, uint32 MaxAgeSeconds);
CMDVAL_Status_t CMDVAL_ValidateFrame(const uint8 *Frame, size_t FrameLen, const CMDVAL_Config_t *Config, uint32 NowSeconds,
                                     CMDVAL_FrameInfo_t *Info);
#endif
"""

SFI_CLUSTER_H = """#ifndef SFI_CLUSTER_H
#define SFI_CLUSTER_H
#include "cfe.h"
typedef struct { uint16 ClusterCount; uint16 LargestClusterSize; uint8 Correlation; uint8 Spare[3]; } SFI_ClusterResult_t;
void SFI_ComputeClusters(const CFE_TIME_SysTime_t *Onsets, uint16 Count, uint16 WindowSec, SFI_ClusterResult_t *Result);
#endif
"""

SFI_CLUSTER_C = """#include "sfi_cluster.h"
void SFI_ComputeClusters(const CFE_TIME_SysTime_t *Onsets, uint16 Count, uint16 WindowSec, SFI_ClusterResult_t *Result)
{ (void)Onsets; (void)Count; (void)WindowSec; memset(Result, 0, sizeof(*Result)); }
"""

COMPONENTS = {
    # name: (directory, entry prefix or None for the library)
    "stor": ("apps/stor", "STOR"), "cmdi": ("apps/cmdi", "CMDI"), "tlmo": ("apps/tlmo", "TLMO"),
    "gw": ("apps/gw", "GW"), "sfi": ("apps/sfi", "SFI"), "cmdval_lib": ("libs/cmdval_lib", None),
}


def add(tree: Path, hidden: bool) -> None:
    for name, (rel, prefix) in COMPONENTS.items():
        d = tree / rel
        if d.exists():
            shutil.rmtree(d)
        (d / "fsw" / "src").mkdir(parents=True)
        (d / "fsw" / "inc").mkdir(parents=True)
        srcs = []
        if prefix is None:
            (d / "fsw" / "public_inc").mkdir(parents=True)
            (d / "fsw" / "public_inc" / "cmdval_lib.h").write_text(CMDVAL_HDR)
            (d / "fsw" / "src" / "cmdval_lib.c").write_text(CMDVAL_FAKE)
            srcs.append("fsw/src/cmdval_lib.c")
            (d / "ut-stubs").mkdir()
            (d / "ut-stubs" / "cmdval_lib_stubs.c").write_text(CMDVAL_FAKE)
            inc = ("target_include_directories(cmdval_lib PUBLIC fsw/public_inc)\n"
                   "if (ENABLE_UNIT_TESTS)\n  add_cfe_coverage_stubs(cmdval_lib ut-stubs/cmdval_lib_stubs.c)\nendif ()")
        else:
            (d / "fsw" / "src" / f"{name}_app.c").write_text(LOOP_APP.format(prefix=prefix))
            srcs.append(f"fsw/src/{name}_app.c")
            if name == "sfi":
                (d / "fsw" / "src" / "sfi_cluster.h").write_text(SFI_CLUSTER_H)
                (d / "fsw" / "src" / "sfi_cluster.c").write_text(SFI_CLUSTER_C)
                srcs.append("fsw/src/sfi_cluster.c")
            inc = f"target_include_directories({name} PUBLIC fsw/inc)"
        cm = (f"project(CFE_{name.upper()} C)\nadd_cfe_app({name} {' '.join(srcs)})\n{inc}\n")
        if name in ("cmdi", "gw"):
            cm += f"add_cfe_app_dependency({name} cmdval_lib)\n"
        (d / "CMakeLists.txt").write_text(cm)
        if hidden:
            hd = d / "hidden_acceptance"
            hd.mkdir()
            for f in (PACK / "hidden_tests" / name).iterdir():
                if f.suffix in (".c", ".txt"):
                    (hd / f.name).symlink_to(f)
            (hd / "t5_model.h").symlink_to(PACK / "hidden_tests" / "t5_model.h")
            with open(d / "CMakeLists.txt", "a") as fh:
                fh.write("\nif (ENABLE_UNIT_TESTS)\n  add_subdirectory(hidden_acceptance)\nendif (ENABLE_UNIT_TESTS)\n")
    targets = tree / "sample_defs" / "targets.cmake"
    text = targets.read_text()
    if "cmdval_lib stor cmdi tlmo gw sfi" not in text:
        text = re.sub(r"(list\(APPEND MISSION_GLOBAL_APPLIST sample_app sample_lib)\)",
                      r"\1 cmdval_lib stor cmdi tlmo gw sfi)", text, count=1)
        assert "cmdval_lib stor cmdi tlmo gw sfi" in text, "targets.cmake layout changed"
        targets.write_text(text)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    add(Path(sys.argv[1]).resolve(), "--hidden" in sys.argv[2:])
