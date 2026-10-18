# Task 5 acceptance pack plan

Grading is per subsystem (S1 STOR base, S2 STOR transfer, S3 CMDVAL/CMDI, S4 TLMO,
S5 GW, S6 SFI, S7 CTF system tests, N non-functional) and then overall. Feedback to
the agent names the failing subsystem and the spec term. Every result on this task
is labelled preliminary until human review. Check ids are prefixed by subsystem.

## Plumbing every live check depends on

- Every command to a node is a CMDI frame to UDP 1234 (cpu1) or 1235 (cpu2),
  built with the default table key, SourceId on the allow-list, and onboard
  seconds read from TLMO telemetry. There is no raw CI_LAB injection.
- Telemetry is read on 2234 (cpu1) or 2235 (cpu2) from TLMO with no enable step.
- Verification packets are command-type on 0x18E0-0x18EF (cpu1) and 0x19E0-0x19EF
  (cpu2); SFI watch payload counters sit at payload byte 8.
- Live checks run on a scratch copy of the installed tree, in the run's private
  network namespace, with cpu1 and cpu2 started by tools/run_cfs.sh (CFS_CPU=cpu2),
  distinct RAM-disk paths, clocks within the age window.
- The pack carries its own independent frame encoder (HMAC-SHA-256, CRC-16/CCITT-FALSE
  with check value 0x29B1) and compares the agent's tools/udp_harness.py functions
  against it.
- GW links: every unit sent to a GW connection is a CMDI frame, marker-framed
  (1A CF FC 1D) on TCP and SERIAL; every unit received from GW is verified as a
  frame and unwrapped before matching.

## Per-subsystem checks

### build / N
- prep, install and runtest pass; -Werror build.
- cpu1 and cpu2 reach OPERATIONAL with the INIT events of STOR, CMDI, TLMO, GW, SFI;
  neither startup script lists CI_LAB or TO_LAB.
- gcov line coverage >= 95 percent per new source file for the six components,
  including code inherited from the lab apps.
- DESIGN.md per component, at most 80 lines; docs/reports/system.md with its six
  sections, the system-tests section referring to ctf/run_system_tests.sh.
- Unit-test count reflects CI_LAB and TO_LAB tests no longer being built.

### S1 STOR base
- Reuse Task 4's hidden tests and live checks, re-plumbed through frames and TLMO;
  add purge-order tests with .arc files. Table load and file-area commands go
  through frames.

### S2 STOR transfer
- Hidden unit tests: TRANSFER rejections, segment fields, ack OK/NAK/timeout,
  receiver NAK PATH/EXISTS/QUOTA/SEQUENCE/CRC, the three success actions.
- Live ground transfer with DELETE: reassemble from STOR_XFER_TLM (TLMO class 3),
  compare CRC. Live telemetry-area transfer with MARK_ARCHIVED.
- Pack-relayed cpu1-to-cpu2 transfer: TLMO ADD_STREAM of the SEG MID on cpu1 and
  the ACK MID on cpu2, relay by framing each packet to the other node's CMDI;
  byte-identical file on cpu2 (path relative to cpu2's run dir), source deleted on
  cpu1, XfersCompleted and FilesReceived counts. EXISTS NAK path; ABORT_TRANSFER.

### S3 CMDVAL / CMDI
- Unit tests: CRC check value; FIPS SHA-256 vectors; RFC 4231 HMAC cases 1-7; age
  boundaries at +-MaxAge and +-(MaxAge+1); source list including 0;
  CMDVAL_ValidateFrame ordering matrix and each FORMAT sub-case.
- Live: a valid frame executes SAMPLE_APP NOOP; one frame per rejection reason,
  each checked for counter, event and non-execution; an unframed packet on 1234 is
  a FORMAT rejection; SET_MAX_AGE and SET_ENABLE_MASK sent unframed or with a bad
  MAC change nothing; the mask command disables HMAC; SET_MAX_AGE widens the window;
  the same valid frame works on cpu2 port 1235.
- tools/udp_harness.py: node_ports, crc16_ccitt_false, build_frame, onboard_seconds,
  send_frame, send_framed_command match the independent encoder; `send-framed` and
  `recv --node` CLI work.

### S4 TLMO
- Unit tests: eviction, drain order, budget 0, full runtime stream list with
  TLMO_MAX_STREAMS 64.
- Live hold-and-inject (bursts of <= 8, about 0.6 s apart): exact drop-count
  arithmetic per class, then datagram order on 2234.
- Live: TO_LAB-equivalent default telemetry arrives with no enable step (cFE ES, EVS,
  SB, TBL, TIME housekeeping, SAMPLE_APP, new apps, events); SET_CLASS; SET_BUDGET;
  table load via cFE TBL LOAD/ACTIVATE; class map packet; ADD_STREAM and
  REMOVE_STREAM including error cases and being dropped on table update; command
  error cases; cpu2 telemetry on 2235.

### S5 GW
- Unit tests: connection validation; deframer (marker, 16-byte header, L in 8..1024,
  out-of-range L resumes hunting); table share and its retry; rejection for every
  reason; egress framing.
- Live UDP: egress of a subscribed verification packet (as a verified frame);
  ingress of a framed SAMPLE_APP NOOP; a NOOP must arrive as a valid frame to
  execute; one live check per rejection reason with GW counters and event 25; a bad
  MAC raises HmacErrors by 1; loop drop on the embedded command.
- Live TCP: listener appears late, connection reaches UP, marker-framed traffic both
  ways, split frame, garbage before a marker, bad MAC.
- Live PTY (raw mode): same as TCP over a PTY pair; GW keeps its heartbeat and
  answers HK with an idle PTY.
- Restart: GW STOP_APP then START_APP (/cf/gw.so, GW_AppMain, priority 70, stack
  131072) after a SERIAL connection was used, then a new SERIAL connection on a
  fresh PTY carrying traffic both ways.
- A key change in CMDI.ValTable is followed by GW on ingress and egress;
  ValTableShared in HK; ENABLE, DISABLE, REMOVE, RELOAD and error events; HK and
  CONN_TLM fields.

### S6 SFI
- Unit tests: SFI_ComputeClusters vectors.
- Live: ES STOP_APP of GW (through a frame) gives a miss event within N+2 s and an
  onset in STATUS; START_APP restores it. SFI_T1-T3 heartbeat injection gives
  CLUSTERED size 2; CLEAR_HISTORY; spaced misses give UNCORRELATED. 0x18EC STALL and
  0x18ED RATE fault and clear, including counter reset.

### S7 CTF system tests
- Run ctf/setup_ctf.sh fresh (patches under ctf/shims/patches/ applied, plugins
  under ctf/plugins/ loaded via additional_plugins_path).
- CCDD layout probe for every new app plus ES START_APP/STOP_APP.
- `ctf --pluginInfo` lists every pinned instruction: SendCmdiCommand,
  SendCmdiRawPacket; StartTlmRecording, CheckTlmOrder, StopTlmRecording;
  OpenUdpLink, OpenTcpServer, WaitLinkConnected, OpenPtyPair, SendLinkPacket,
  SendLinkCmdiCommand, CheckLinkPacket, CheckNoLinkPacket, CloseLink; StopApp,
  StartApp, WaitForEvent; CheckNodeFile, CompareNodeFiles, RemoveNodeFile.
- Run the two ported smoke scripts and each st_*.json (st_gw_commands,
  st_cmdval_hmac, st_tlmo_priority, st_gw_udp, st_gw_tcp, st_gw_serial,
  st_sfi_heartbeat, st_stor_transfer) by name with run_ctf.sh, CFS_RUN_DIR and
  CFS_RUN_DIR_CPU2 on scratch copies, 300 s limit each; ctf/run_system_tests.sh
  exists. Whether a test tests what it claims is left to human review.

## Pilot must confirm (pack should probe these and fail loudly, not silently)
- Zero-timeout reads on a PTY fd through OSAL.
- GW restart over an existing /dev/pts mapping with leftover OSAL resources.
- cpu1 and cpu2 do not share one RAM-disk path.
- CTF's telemetry listener tolerates command-type packets on telemetry ports.
- TLMO pipe depth against 48 default streams.
- CFE_TBL_Share when GW starts before CMDI registers, and across a CMDI restart.
- Node clocks within the age window for GW-to-GW frames.
