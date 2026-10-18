#include "cfe.h"
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
