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
 *   Specification for the CFE Executive Services (CFE_ES) command function codes
 *
 * @note
 *   This file should be strictly limited to the command/function code (CC)
 *   macro definitions.  Other definitions such as enums, typedefs, or other
 *   macros should be placed in the msgdefs.h or msg.h files.
 */
#ifndef DEFAULT_STOR_FCNCODE_VALUES_H
#define DEFAULT_STOR_FCNCODE_VALUES_H

/************************************************************************
 * Macro Definitions
 ************************************************************************/

#define STOR_CCVAL(x) STOR_FunctionCode_##x

enum STOR_FunctionCode_
{
    STOR_FunctionCode_NOOP                = 0,
    STOR_FunctionCode_RESET_COUNTERS      = 1,
    STOR_FunctionCode_SET_PRIORITY        = 2,
    STOR_FunctionCode_SET_DECIMATION      = 3,
    STOR_FunctionCode_SET_PURGE_THRESHOLD = 4,
    STOR_FunctionCode_ADD_FILE            = 5,
    STOR_FunctionCode_DELETE_FILE         = 6,
    STOR_FunctionCode_LIST_DIR            = 7,
    STOR_FunctionCode_LIST_ALL            = 8,
    STOR_FunctionCode_SEND_STATS          = 9,
    STOR_FunctionCode_CLOSE_FILES         = 10,
    STOR_FunctionCode_TRANSFER            = 11,
    STOR_FunctionCode_ABORT_TRANSFER      = 12,
};

#endif
