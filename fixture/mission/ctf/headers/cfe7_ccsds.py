"""
CCSDS header types for cFE 7 builds (message ids carry the secondary-header flag,
8-byte command header, 16-byte telemetry header: 6 primary + 6 time + 4 spare).

CTF's stock ccsds_v1 module describes exactly this wire format but its telemetry
packet class lacks get_msg_size(), which CTF's telemetry parser calls on every packet.
This module adds that one method and re-exports the three names CTF imports.
Selected by ccsds:CCSDS_header_path in configs/mission.ini.
"""
from core_plugins.ccsds_plugin.cfe.ccsds_v1.ccsds_v1 import (
    CcsdsV1CmdPacket,
    CcsdsV1PrimaryHeader,
    CcsdsV1TlmPacket,
)


class Cfe7TlmPacket(CcsdsV1TlmPacket):
    """Telemetry header with the total-size accessor CTF requires."""

    def get_msg_size(self):
        """Total packet size in bytes: the CCSDS length field counts bytes after the primary header, minus one."""
        return self.pheader.get_packet_length() + 7


CcsdsPrimaryHeader = CcsdsV1PrimaryHeader
CcsdsCommand = CcsdsV1CmdPacket
CcsdsTelemetry = Cfe7TlmPacket
