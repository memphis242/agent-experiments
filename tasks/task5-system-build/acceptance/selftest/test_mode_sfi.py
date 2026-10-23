"""Focused tests of review-owned ABI parsers and independently generated stimuli."""
from __future__ import annotations

from dataclasses import replace
import struct
import unittest
import frames
import spec
import tables
from common import Fail
import live_sfi
import live_mode


def telemetry(mid: int, payload: bytes) -> bytes:
    data=bytearray(16+len(payload))
    struct.pack_into('>HHH',data,0,mid,0xC000,len(data)-7)
    data[16:]=payload
    return bytes(data)


def fault(sequence: int,kind: int=4,source: int=6,active: int=1) -> bytes:
    return struct.pack(spec.SFI_FAULT_FMT,sequence,kind,source,2,active,1,1,b'',100,0x80000000,101,3)


class FaultParsing(unittest.TestCase):
    def test_slot_identity_and_immutable_bytes(self) -> None:
        for kind,source,slot in ((1,15,15),(2,4,20),(3,5,21),(4,6,22),(5,15,47),(6,7,55),(7,0,56)):
            raw=fault(0xFFFFFFFF,kind,source)
            report=live_sfi.parse_fault(raw)
            self.assertEqual((report.slot,report.sequence,report.raw),(slot,0xFFFFFFFF,raw))
        with self.assertRaises(Fail):live_sfi.parse_fault(bytes(47))
        with self.assertRaises(Fail):live_sfi.parse_fault(bytes(49))
        bad=bytearray(fault(1));bad[12:32]=b'x'*20
        with self.assertRaises(Fail):live_sfi.parse_fault(bytes(bad))

    def test_exact_log_header_chronology_and_wrap(self) -> None:
        rows=(fault(0xFFFFFFFF),fault(0,active=0),fault(1))
        data=struct.pack('<IHHII',0x5346494C,1,48,3,1)+b''.join(rows)
        sequence,reports=live_sfi.parse_fault_log(data)
        self.assertEqual((sequence,tuple(report.raw for report in reports)),(1,rows))
        for offset,value in ((0,0),(4,2),(6,47),(8,65),(12,2)):
            bad=bytearray(data)
            struct.pack_into('<H' if offset in (4,6) else '<I',bad,offset,value)
            with self.assertRaises(Fail):live_sfi.parse_fault_log(bytes(bad))
        for invalid in (data[:-1],data+b'\0',data[:16],data[:16]+rows[1]+rows[0]+rows[2]):
            with self.assertRaises(Fail):live_sfi.parse_fault_log(invalid)
        self.assertEqual(live_sfi.parse_fault_log(struct.pack('<IHHII',0x5346494C,1,48,0,0)),(0,()))

    def test_expanded_status_exact_full_size(self) -> None:
        body=bytearray(spec.SFI_STATUS_SIZE)
        struct.pack_into('<BBHHH',body,0,0,2,1,1,2)
        struct.pack_into(spec.SFI_APP_STATUS_FMT,body,8+3*36,b'GW',1,0,1,100,1,1,6,0)
        struct.pack_into(spec.SFI_MISS_RECORD_FMT,body,584,255,100,0,4,6,0)
        struct.pack_into('<HH',body,1032,8,64)
        body[1036+22]=1;body[1093+22]=1;body[1152+22*48:1200+22*48]=fault(1)
        packet=telemetry(0x08DD,body)
        status=live_sfi.parse_status(packet)
        self.assertEqual((status.app('GW').event_count,status.history[0][3:5],status.largest_watches_mask,status.faults[22].slot),(6,(4,6),64,22))
        for invalid in (packet[:-1],packet+b'\0'):
            with self.assertRaises(Fail):live_sfi.parse_status(invalid)


class ModeParsing(unittest.TestCase):
    def test_exact_status_and_response_offsets(self) -> None:
        body=bytearray(spec.MODE_STATUS_SIZE)
        struct.pack_into('<8B3I4B',body,0,1,1,1,1,2,0,56,1,100,101,102,1,65,255,0)
        struct.pack_into('<4B2I',body,24+56*12,1,3,0,0,132,0xFFFFFFFF)
        status=live_mode.parse_status(telemetry(0x08F1,body))
        self.assertEqual((status.restart_slot,status.profile,status.faults[56].next_eligible,status.faults[56].last_sequence),(56,(1,65,255,0),132,0xFFFFFFFF))
        for invalid in (body[:-1],body+b'\0'):
            with self.assertRaises(Fail):live_mode.parse_status(telemetry(0x08F1,invalid))
        for offset,value in ((0,3),(6,57),(7,3)):
            bad=bytearray(body);bad[offset]=value
            with self.assertRaises(Fail):live_mode.parse_status(telemetry(0x08F1,bad))

    def test_native_default_table_payloads(self) -> None:
        table=tables.ModeTable();body=table.pack()
        self.assertEqual(len(body),1500)
        self.assertEqual(body[:16],bytes((1,1,10,20,0,80,255,0,1,65,255,0,2,50,3,0)))
        for index,(name,entry,path,stack,priority) in enumerate(spec.MODE_DEFAULT_RESTARTS):
            row=struct.unpack_from(spec.MODE_RESTART_FMT,body,16+index*112)
            self.assertEqual(tuple(value.split(b'\0',1)[0].decode() for value in row[:3]),(name,entry,path))
            self.assertEqual(row[3:],(stack,priority,0))
        response=struct.unpack_from(spec.MODE_RESPONSE_FMT,body,464+4*148)
        self.assertEqual(response[:12],(1,0,1,1,0,0,1,200,30,3,0,0x0801))
        self.assertEqual(response[12:16],(0,0,0,0))
        self.assertEqual(response[16].split(b'\0',1)[0],b'mode/response.bin')
        changed=replace(table,recovery=37).pack()
        self.assertEqual(changed[:3]+changed[4:],body[:3]+body[4:])

    def test_expanded_sfi_table_round_trip(self) -> None:
        rows=tuple((spec.verif_mid(mid) if isinstance(mid,int) else spec.named_tlm_mid(mid),off,width,spec.SFI_RULE[rule],limit)
                   for mid,off,width,rule,limit in spec.SFI_DEFAULT_WATCH_ROWS)
        table=tables.SfiTable(watches=rows)
        body=table.pack()
        self.assertEqual(len(body),780)
        self.assertEqual(body[8+7*24:12+7*24],b'MODE')
        limit=struct.unpack_from('<IHBBIIIBBBB',body,392+6*24)
        self.assertEqual(limit,(0x18EF,16,4,3,0,10,20,3,255,2,1))
        self.assertEqual(tables.SfiTable.unpack(body).pack(),body)


if __name__=='__main__':unittest.main()
