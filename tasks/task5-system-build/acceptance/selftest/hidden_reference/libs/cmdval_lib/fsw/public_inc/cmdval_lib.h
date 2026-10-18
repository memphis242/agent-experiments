#ifndef CMDVAL_LIB_H
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
