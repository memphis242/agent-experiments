/************************************************************************
 * NASA Docket No. GSC-19,200-1, and identified as "cFS Draco"
 *
 * Copyright (c) 2023 United States Government as represented by the
 * Administrator of the National Aeronautics and Space Administration.
 * All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"); you may
 * not use this file except in compliance with the License. You may obtain
 * a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 ************************************************************************/

/**
 * @file
 *   Specification for the STOR command function codes
 *
 * @note
 *   This file should be strictly limited to the command/function code (CC)
 *   macro definitions.  Other definitions such as enums, typedefs, or other
 *   macros should be placed in the msgdefs.h or msg.h files.
 */
#ifndef STOR_FCNCODES_H
#define STOR_FCNCODES_H

#include "stor_fcncode_values.h"

/************************************************************************
 * Macro Definitions
 ************************************************************************/

/*
** STOR command codes
*/
#define STOR_NOOP_CC                STOR_CCVAL(NOOP)
#define STOR_RESET_COUNTERS_CC      STOR_CCVAL(RESET_COUNTERS)
#define STOR_SET_PRIORITY_CC        STOR_CCVAL(SET_PRIORITY)
#define STOR_SET_DECIMATION_CC      STOR_CCVAL(SET_DECIMATION)
#define STOR_SET_PURGE_THRESHOLD_CC STOR_CCVAL(SET_PURGE_THRESHOLD)
#define STOR_ADD_FILE_CC            STOR_CCVAL(ADD_FILE)
#define STOR_DELETE_FILE_CC         STOR_CCVAL(DELETE_FILE)
#define STOR_LIST_DIR_CC            STOR_CCVAL(LIST_DIR)
#define STOR_LIST_ALL_CC            STOR_CCVAL(LIST_ALL)
#define STOR_SEND_STATS_CC          STOR_CCVAL(SEND_STATS)
#define STOR_CLOSE_FILES_CC         STOR_CCVAL(CLOSE_FILES)
#define STOR_TRANSFER_CC            STOR_CCVAL(TRANSFER)
#define STOR_ABORT_TRANSFER_CC      STOR_CCVAL(ABORT_TRANSFER)

#endif
