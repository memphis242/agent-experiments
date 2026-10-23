# Task 5 acceptance pack plan

Grading is per subsystem (S1 STOR base, S2 STOR transfer, S3 CMDVAL/CMDI, S4 TLMO,
S5 GW, S6 SFI, S7 MODE, S8 CTF system tests, N non-functional) and then overall. Feedback to
the agent names the failing subsystem and the spec term. Every result on this task
is labelled preliminary until human review. Check ids are prefixed by subsystem.

All additions remain in this one task. Task 5 runs with an enlarged
budget (more rounds/time than T1-T4) and is the auto-compaction test bed.
Round counts and time budgets are TBD by owner.

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
- Before live checks, verify effective kernel queue capacity in each node's
  IPC namespace: the harness sets fs.mqueue.msg_max to 512; require msg_max
  >= 512 and sufficient RLIMIT_MSGQUEUE for both nodes, counting kernel
  per-message overhead and other queues charged to the same user. Preflight
  allocation probes use the final non-root node identity and the actual
  queue inventory; verify mq_getattr capacities, including TLMO/MODE >=128
  and SFI >=256. Boot success is insufficient. A kernel/resource-limit
  shortfall is an environment prerequisite failure, not an implementation
  failure. Permissive truncation never satisfies a pipe-depth requirement.
  Nested check namespaces must repeat the setup/preflight, since a newly
  created IPC namespace has its own limits.
- The pack carries its own independent frame encoder (HMAC-SHA-256, CRC-16/CCITT-FALSE
  with check value 0x29B1) and compares the agent's tools/udp_harness.py functions
  against it.
- GW links: every unit sent to a GW connection is a CMDI frame, marker-framed
  (1A CF FC 1D) on TCP and SERIAL; every unit received from GW is verified as a
  frame and unwrapped before matching.

- Use the native classic MID mapping. Audit every spec C.3 MID against the
  bundled apps/cFE and mission overrides; compare full MIDs, not just topics
  (CF_CH0_RX command topic 0xC8 and STOR_FILE_INFO telemetry 0xC8 are distinct).
  New IDs: STOR FILE_INFO 0x08C8, CMDI SOURCE 0x08D1, TLMO STREAM 0x08D6,
  SFI FAULT 0x18F3 / mirror 0x08DE, MODE CMD/SEND_HK/WAKEUP 0x18F0/F1/F2,
  MODE HK/STATUS 0x08F0/F1; global health/mode/keepalive 0x187A/B/C (topics
  0x1A/B/C). Local cpu2 adds 0x100. Verification topics 0xE0-0xEF stay reserved.
- Extend the independent encoder with a monotonic per-node/source serial
  allocator shared by Ground, table-load helpers, ES helpers and CTF plugins;
  resetting app counters never resets it. Isolate deliberate replay tests from
  normal commands with source 1 for stimuli and source 2 for observations.
- C.7 timing is normative: synthetic exact-time hidden tests; 1 Hz deadlines
  allow +2 s, 10 Hz +1 s unless the subsystem gives an explicit bound; add
  one HK period for HK observation. Use raw telemetry/event timestamps to
  bound transitions; MODE recovery is [20,25] s from SFI clear. All request
  telemetry must arrive within 2 s at a sufficient nonzero budget.
- Timing-sensitive live checks run in a host-coordinated serialized lane
  with reserved CPU capacity and no concurrent builds, including builds
  from other reviews. Record independent host scheduling-watchdog evidence: a detected
  scheduling stall invalidates the attempt and triggers one clean serialized
  rerun. If that rerun also stalls, report an environment failure. Exact
  deadlines are graded with synthetic-time tests, not the live tolerance.
- Live burst envelope: injected verification packets have total SB length
  <=1024 bytes, at most 8 per burst, with burst starts at least 0.6 s apart.
  Expanded SFI status, STOR HK and TLMO stream-statistics requests use their
  exact layout-probed packet lengths (record byte counts, all <=8192), one
  outstanding large request at a time; await capture and at least 0.6 s
  before the next, with budget 256 and no held output. Reserved-space
  SEND_STREAM_STATS snapshots in the S4 held-output checks are the exception:
  one outstanding snapshot, restore budget through CMDI before awaiting it.
  No unpaced burst of
  maximum-size packets is graded live. Maximum-size boundary/burst stress
  uses hidden component tests. Verify SB allocation-failure counters/logs
  stay clear within this live envelope, including background traffic.
  C.6 permits mission SB pool sizing overrides; include descriptors and
  allocator buckets, not just packet bytes. Pipe depth alone implies no
  lossless burst guarantee; TLMO's copied queue does not retain SB buffers.
- Disable MODE automatic responses with SET_ENABLE 0 and wait for HK before
  isolated S1-S6 setup. Before isolated-test overrides, also await completion
  of initial profile application, or two MODE wakeups after all profile
  targets register. Complete scheduled profile reapplications before resetting
  counters and beginning assertions. Enable MODE only after peer/route setup in its
  live checks. Arming tests explicitly arrange first peer contact; no node-loss
  timer is armed by an absent peer at startup.
- Acceptance never restarts SFI while MODE remains running. SFI recovery
  restarts the node; peer-loss tests use relay cuts, not an in-run SFI restart.
  Per-slot report serial history and SFI report numbering share that lifetime.
- Add raw TLMO collection of SFI_FAULT, SFI_HEALTH and MODE_PEER command packets;
  decode mirrored SFI_FAULT_TLM too, requiring byte-identical report payloads.
  No new live check injects raw SB packets or uses an unauthenticated path.
  Live fault-log content verification uses STOR transfer telemetry, not local
  filesystem access. GW-only test endpoints retain authenticated frame plumbing.

## Per-subsystem checks

### build / N
- prep, install and runtest pass; -Werror build.
- cpu1 and cpu2 reach OPERATIONAL with the INIT events of STOR, CMDI, TLMO, GW, SFI, MODE;
  neither startup script lists CI_LAB or TO_LAB.
- gcov line coverage >= 95 percent per new source file for all seven components (six apps and cmdval_lib),
  including code inherited from the lab apps.
- DESIGN.md per component, at most 80 lines; docs/reports/system.md with its six
  sections, the system-tests section referring to ctf/run_system_tests.sh
  and reporting all fifteen st_ scripts plus both smoke scripts.
- Static mission checks: OSAL_CONFIG_MAX_FILE_NAME = 32 and
  OSAL_CONFIG_QUEUE_MAX_DEPTH = 512 for both CPUs; allowed mission SB pool
  overrides support the declared live burst envelope. MODE app/table installed on both CPUs, prefix/entry
  MODE_/MODE_AppMain, startup after SFI at priority 70/stack 131072,
  mode.table include/definition wiring, scheduler MODE SEND_HK 57 and WAKEUP
  10 ticks; 22 - 2 + 12 = 32 slots, MAX_SCHEDULE_ENTRIES 32, 0 spare.
  Count actual initializers including the two SBN entries, not just source
  lines; no separate timers added as SCH entries. Assert MODE heartbeat
  and SFI's default MODE app slot. All new table types/defaults validated.
- N: require apps/mode/DESIGN.md <=80 lines, unit-test module coverage >=95%,
  all seven design notes, six report sections with complete MID inventory,
  state/profile/restart/lifetime decisions and 17 CTF results. New per-source
  code is included in the same -Werror/runtest/coverage gates.
- Unit-test count reflects CI_LAB and TO_LAB tests no longer being built.

### S1 STOR base
- Reuse Task 4's hidden tests and live checks, re-plumbed through frames and TLMO;
  add purge-order tests with .arc files. Table load and file-area commands go
  through frames.
- Hidden quota tests: zero file quota, telemetry quota below MaxPacketFileBytes,
  atomic no-change rejection, lower-than-used acceptance/purge, immediate drops
  and restoration on table activation. FILE_INFO path/area/unterminated/open/
  missing/directory and OSAL errors; ISO-HDLC CRC of empty/123456789/binary files.
- Live SET_QUOTA through CMDI, FILE_INFO size/CRC through default TLMO stream,
  no file mutation, PriorityBytes[0..255] sum equals telemetry usage across
  table changes, SET_PRIORITY, archive/delete and purge. Include large HK
  packet size in the default-stream plumbing check.

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

- Hidden replay tests (CMDI, leaving CMDVAL stateless): first 0, duplicates,
  unseen out-of-order 63 behind accepted/64 rejected, jumps >=64, exactly 2^31,
  FFFFFFFF->0 wrap, independent sources, source 0 with SOURCE disabled, eight
  runtime slots then ninth source, no slot/sequence consumption on any earlier
  failure. An app-rejected command still consumes the serial. Verify RESET
  retains window, table activation clears it, SWITCH_KEY retains it.
- Hidden key tests: NEXT validation/all-zero/equal/NextValid ranges, ACTIVE-only
  validation, SWITCH_KEY no NEXT, mandatory switch HMAC even with HMAC mask off in CMDI and GW ingress,
  check before replay in CMDI and before publishing in GW, no NEXT leakage in events/stats, table staging and shared
  working-copy CFE_TBL_Modified calls. Existing config API and validation order
  stay ABI-compatible; reference stubs must model shared key changes.
- Hidden source stats: each accepted/reason maps to exactly one AllowedSources
  row or OTHER, FORMAT with <6 bytes and readable listed source, repeated SOURCE
  and replay failures, exact reset/table semantics and full 9-row payload.
- Live `s3-replay-window`: send explicit serials on source 1, use source 2 for
  polling, compare all source-1 counters plus aggregate HK/event/non-execution;
  explicitly accept 100, execute unseen 37, reject unseen 36 as REPLAY and
  duplicate 100 as REPLAY; include wrap on a clean node. Same accepted bytes
  on cpu2 are independent.
- Live `s3-key-rotation`: stage NEXT using authenticated cFE TBL load/validate/
  activate with ACTIVE retained, switch in an ACTIVE frame, reject a retired-key
  frame and replay under the new key, accept a fresh NEXT-key frame. Query HK,
  source stats and GW egress under the new key. Test mask-disabled SWITCH_KEY
  with bad HMAC through both CMDI and GW separately, then restore key/mask through accepted frames.
- Hidden same-wakeup ordering vector: with HMAC enabled, before one CMDI
  wakeup queue SWITCH_KEY, a retired-key ordinary command and a fresh-key
  ordinary command. Require
  immediate changed-key validation (retired rejected, fresh executes). Repeat
  for mask and age changes without intermediate polling, with vectors whose
  outcomes distinguish the changed settings. Exercise this queued-datagram
  order live using a controlled pre-wakeup batch and source-separated counters;
  hidden tests enforce deterministic same-wakeup dispatch.

### S4 TLMO
- Unit tests: eviction, drain order, budget 0, full runtime stream list with
  TLMO_MAX_STREAMS 64.
- Live hold-and-inject (<=8 packets of <=1024 bytes, burst starts at least
  0.6 s apart): exact drop-count arithmetic per class, then datagram order
  on 2234.
- Live: TO_LAB-equivalent default telemetry arrives with no enable step (cFE ES, EVS,
  SB, TBL, TIME housekeeping, SAMPLE_APP, new apps, events); SET_CLASS; SET_BUDGET;
  table load via cFE TBL LOAD/ACTIVATE; class map packet; ADD_STREAM and
  REMOVE_STREAM including error cases and being dropped on table update; command
  error cases; cpu2 telemetry on 2235.

- Hidden decimation/profile tests: table N=0 and class maps/budgets bounds,
  N=1/255 and phase 0 first packet, SET_DECIMATION phase reset, ADD_STREAM N=1,
  phases retained on counter reset and cleared on table load. Decimated packets
  do not become queue drops. Per-stream Received/Sent/Dropped/Decimated/
  SendErrors and Queued attribution covers eviction by another stream,
  oversized packet and failed send, class/profile changes with queued packets,
  remove/table discard, empty/full SEND_STREAM_STATS snapshot, zero tail and slot ordering.
  Queue MID A with slots A/B, activate slots B/A, require retained A packets
  owned by A's new slot with their queued class unchanged, cumulative counters
  and phases reset, and Queued recomputed. Repeat as a live held-output table
  reorder with the replacement table budget also 0, reserved reply space
  and the budget-restoration sequence below.
- Live `s4-stream-profiles`: two reserved streams with differing decimation,
  use an isolated stream table including STREAM_TLM at Decimation 1 and
  reserve queue space for the statistics reply. While budget is 0, inject
  known counts and issue SEND_STREAM_STATS.
  Restore a sufficient nonzero budget through CMDI without awaiting telemetry,
  then inspect the captured snapshot and verify drain order/totals/class order.
  Select all three profiles and inspect target
  budgets/maps. Repeat SET_CLASS/SET_BUDGET override and table activation rules.
  One full snapshot is on default STREAM_TLM; default count 57 within capacity 64,
  pipe >=128 and max packet 8192. Hidden tests cover full 64-slot runtime list.

### S5 GW
- Unit tests: connection validation; deframer (marker, 16-byte header, L in 8..1024,
  out-of-range L resumes hunting); table share and its retry; rejection for every
  reason; egress framing.
- Live UDP: egress of a subscribed verification packet (as a verified frame);
  ingress of a framed SAMPLE_APP NOOP; a NOOP must arrive as a valid frame to
  execute; one live check per rejection reason with GW counters and event 25; a bad
  MAC raises HmacErrors by 1; loop drop on the embedded command.
- Live TCP: listener appears late, connection reaches UP within one 5 s attempt period,
  marker-framed traffic both ways, split frame, garbage before a marker, bad MAC; after
  the peer closes, a new connect cycle with an attempt at once.
- Live TCP connect cycle (s5-tcp-down): against a peer that never listens, DOWN (3) with
  one GW_CONN_TIMEOUT_ERR_EID about 20 s after ADD_CONN (accepted window 18-26 s),
  Enabled stays 1, egress while DOWN counts EgressErrors, no attempt once DOWN even when a
  listener appears, and DISABLE_CONN then ENABLE_CONN starts a fresh cycle with an
  attempt at once.
- Live PTY (raw mode): same as TCP over a PTY pair; GW keeps its heartbeat and
  answers HK with an idle PTY.
- Restart: GW STOP_APP then START_APP (/cf/gw.so, GW_AppMain, priority 70, stack
  131072) after a SERIAL connection was used, then a new SERIAL connection on a
  fresh PTY carrying traffic both ways.
- A key change in CMDI.ValTable is followed by GW on ingress and egress;
  ValTableShared in HK; ENABLE, DISABLE, REMOVE, RELOAD and error events; HK and
  CONN_TLM fields.

- Hidden route/keep-alive tests: definition boolean/peer/local-port constraints,
  route index/MID/duplicate/endpoints, unused-route removal, endpoint removal,
  SET_ENABLED_MASK partial open-failure exception; no-op states preserved.
  6 s UP timeout; exact-boundary valid keep-alive pending on the deadline
  wakeup refreshes before expiry, producing no DOWN/UP edge, SFI link report
  or transient route switch. Assert inbound -> deadlines -> routes -> outbound
  ordering. Reply=0/1 handling and no reply loop,
  wrong peer/destination/length/FC/Reply, disabled keep-alive, DOWN probe/recovery,
  TCP connect exhaustion emits only CONN_TIMEOUT_ERR (28); keep-alive expiry
  emits only KEEPALIVE_ERR (29), once per corresponding edge, with no overlap.
  Explicit disable clears. Capture SFI
  REPORT_LINK exact MID/FC12/payload on edges only. Byte/frame counters include
  rejected frames and exclude stream markers; data packet counters exclude KA.
- Hidden routing: primary/backup/no-UP selection, immediate failure, 10 s
  continuous-primary failback with interruptions, preference override/release,
  no duplicate fanout, bounded inbound health/mode cache (64 entries/2 s),
  ordinary loop guard unchanged for other MIDs, table/reload preference reset.
- Live `s5-link-health`: paired endpoints send/ack valid probes, then go silent
  without ICMP. Assert DOWN 6 s from last valid receipt (live [5,8] s from a steady relay cut), one timeout/report, repeated silence no
  extra edge, valid probe recovery, malformed probes do not refresh. Compare
  both-direction frames/bytes/errors via CONN_TLM; confirm health event in SFI.
- Live `s5-route-failover`: use the S8.6 dual relay layout, prove backup-only
  delivery with unique verification packets and no duplicates, no premature
  failback, return to primary after [10,12] s from continuous UP (or preference
  release for overridden routes). Keep the existing TCP no-listener 18-26 s
  cycle test with keep-alives disabled. MODE disabled for isolated GW cases.
- Out of scope (owner decision): dedicated hidden vectors for an interrupted
  primary stability interval, both endpoints unavailable, table-activation
  preference reset and the full TCP timeout versus keep-alive distinction;
  adversarial plugin checks beyond `s8-plugin-negative` basics.

### S6 SFI
- Unit tests: SFI_ComputeClusters vectors.
- Live: ES STOP_APP of GW (through a frame) gives a miss event within N+2 s and an
  onset in STATUS; START_APP restores it. SFI_T1-T3 heartbeat injection gives
  CLUSTERED size 2; CLEAR_HISTORY; spaced misses give UNCORRELATED. 0x18EC STALL and
  0x18ED RATE fault and clear, including counter reset.

- Hidden fault model: exact 57-slot mapping, report payload order/types,
  onset/clear sequence/onset retention/node/severity/name, latch independent of
  physical State, active ACK rejection and inactive ACK success, duplicate
  trigger suppression, ring overwrite of 64 reports. RESET/CLEAR_HISTORY/ACK
  must not erase log or reset report serials. Table activation emits old-source
  clears before rearming; enable commands preserve latch and reset baselines.
- Hidden LIMIT: inclusive min/max at all widths, max representability,
  persistence 1/255, first sample comparison, consecutive reset, clear on first
  in-range packet, exact table defaults including watch 6 (topic EF).
- Hidden event-rate: only ERROR, per-app matching/enables, exact (now-W,now]
  boundaries/subseconds, EventLimit+1 cap and recent-timestamp retention across
  arbitrarily large bursts, expiry on wakeup, single onset/clear and no
  SFI-generated feedback. RATE/STALLED and event-rate are separate identities.
- Hidden clustering: existing SFI_ComputeClusters vectors/const contract plus
  mixed heartbeat/watch onset ring, repeats count in size but deduplicate masks,
  earliest-cluster tie rules, associated/unassociated watches and H/W edges.
  Old heartbeat-only vectors stay meaningful when no watch fault is injected.
- Hidden health: global MID/header/FC/payload, Src/Dst/length checks, own echo
  ignored, first-contact arming, 10 s default timeout/clear, disable/re-enable,
  publication while unarmed and table override. Continuous valid health
  packets while PeerEnabled 0 must neither arm nor refresh; re-enable remains
  unarmed until a later valid receipt. Link reports drive slots 48..55.
- Hidden dump: exact little-endian header + oldest-first records, zero/64/wrapped
  ring, overwrite, all open/write/close errors delete partial output, counter
  semantics; ACK changes current latch array, not historical record bytes.
- Live `s6-limit-latch-enable`: frames on EF (9,9,10,9,9,9,10) prove persistence,
  report pairing, critical severity, latch and ACK guards, watch/app enable
  controls and baseline resets. Observe through SFI FAULT_TLM/STATUS_TLM.
- Live `s6-event-rate`: six intentionally invalid GW commands generate GW ERROR
  events inside 10 s, require EVENT_RATE onset then expiry clear (default limit
  5); INFORMATION/DEBUG stimuli do not trigger it. Keep MODE disabled.
- Live `s6-mixed-cluster`: combine an armed verification heartbeat miss with
  STALL/LIMIT onsets inside W and separated outside W, check exact history and
  masks and tie selection using synthetic-time hidden tests for boundary cases.
- Live `s6-peer-health`: configure bidirectional GW health/mode, arm both; cut
  both paths using relay cuts. Assert NODE_LOSS in [10,12] s from the last
  valid onboard receipt, or [9,12] s from a steady relay cut, and clear on
  contact; synthetic-time tests enforce the exact 10 s deadline;
  unknown/malformed/own-source frames must not arm/refresh. One-node startup
  with no contact stays unarmed. Use relay filtering of MODE_PEER to isolate
  its separate peer-mode deadline in S7 checks.
- Live `s6-log-ground`: capture report payloads, dump after onset/clear, STOR
  ADD_FILE, FILE_INFO and TRANSFER KEEP. Independently parse reassembled bytes,
  exact header/count/order/onset/clear payload equality, size and CRC; do not
  read the dump directly from disk. Exercise >64 edge wrap in hidden tests.

### S7 MODE
- Static app/mission/default table checks as N, full command/telemetry layout
  probe and all event ID/type assignments. Hidden tests target each source file
  and assert exact target MID/FC/native payload values for profile and actions.
- Hidden table bounds: every boolean/range, restart allow-list/name/entry/path/
  priority/stack, duplicate targets, response targets/cooldown/retry, valid
  STOR dump payload, conditional field validation and all pinned defaults.
- Hidden state machine: startup profile application after app registrations,
  initial/updated/same-mode/explicit profile rules; simultaneous SAFE, critical,
  warning, commands and peer loss; last accepted command; guarded SAFE exits;
  report validation/per-slot serial wrap, duplicate/stale ignore; active versus
  latched recovery, 20 s continuous health, quiet timer reset, disabled response
  monitoring and re-enable evaluation, all RESET_COUNTERS non-effects.
  Test isolated overrides only after initial profile completion; a scheduled
  table/profile reapplication must complete before counter reset/assertions.
- Hidden responses: ascending slots, one restart resource, no consumption while
  waiting, commands in pinned order and exact payloads, present-app STOP then
  absent-app START, already stopped app no STOP, each 5 s timeout, registration
  versus SFI-clear distinction; transmit failures counted and actionable events.
  Cooldown 30 s across clears/ACK/reset, cap 3, no fourth START, exhaustion only
  at eligible time while still active, persistent SAFE/exhaustion and explicit
  inactive RESET_RESPONSE. Missing restart match does not target another app.
  Activate a changed MODE table during WAIT_STOP: the subsequent START uses
  the dispatched attempt's snapshotted target/entry/path/priority/stack, and
  a later attempt uses the new table. Existing cooldown deadlines are unchanged.
  Shared route requests release SET_PREFERENCE 0 exactly once on the last
  requesting slot's clear, including during SET_ENABLE 0; count the cleanup
  transmission normally, with no deferred duplicate on re-enable.
- Hidden peer: MODE global publication every wakeup, invalid/own message handling,
  first-contact arming, 10 s mode timer independent of SFI's NODE_LOSS, no forced
  matching of peer mode, recovery/disable semantics and both timer telemetry.
  Continuous valid messages while PeerEnabled 0 do not arm/refresh; PeerMode
  and PeerSequence diagnostics still update and publication continues.
  Re-enable requires a subsequently received valid message before arming.
- Live `s7-fault-response`: S8.6 stopped-GW sequence; assert SFI report, MODE
  DEGRADED, automatic ES START, restored heartbeat, one attempt, no premature
  retry, 20 s quiet return. Compare budget/purge/mask fields on target apps for
  all profiles, including SAFE mask with configured slots 2+ disabled.
- Live `s7-cooldown-cap`: recur GW stops, wait eligibility, assert attempt start
  times >=30 s apart, stop again after the third successful recovery; the next eligible
  recurrence exhausts the slot without a fourth START,
  RestartsSent <=3, RETRY_ERR once and SAFE. Clear active fault by accepted
  START if needed; counter reset/ACK cannot free cap, RESET_RESPONSE can, then
  explicit SET_MODE NOMINAL exits SAFE. Bound duration to 150 s plus startup.
- Live `s7-response-actions`: table-select each action using known fixture
  mode/response.bin, confirm STOR priority 200, transfer KEEP contents/CRC,
  exact GW preference and release, target rejection visible for absent file,
  profile and action errors/counters as specified. Include last-requester
  route release during SET_ENABLE 0 and live table activation during WAIT_STOP,
  checking the START snapshot and unchanged cooldown, then new parameters on
  the next attempt. Table changes do not erase attempt state. Per-class tests
  use fresh nodes/explicit response reset.
- Live `s7-peer-mode`: keep health flowing while dropping only MODE_PEER at
  the test relay (valid CMDI controls configure the routing), require DEGRADED
  in [10,12] s from the last valid onboard MODE receipt, or [9,12] s from
  a steady relay cut, peer loss/recovery events and no NODE_LOSS; synthetic
  tests enforce the exact 10 s deadline. Reverse the relay filter for
  SFI health. Dual primary cut with backup surviving makes both nodes DEGRADED
  via LINK within 10 s (DOWN within [5,8] s from the cut), then healthy primary/failback/recovery as S8.6. TLMO only observes.

### S8 CTF system tests
- Run ctf/setup_ctf.sh fresh (patches under ctf/shims/patches/ applied, plugins
  under ctf/plugins/ loaded via additional_plugins_path).
- CCDD layout probe for every new app plus ES START_APP/STOP_APP and the
  native TBL LOAD/VALIDATE/ACTIVATE commands used to stage NEXT.
- `ctf --pluginInfo` lists every pinned instruction: SendCmdiCommand,
  SendCmdiRawPacket, ReplayCmdiFrame, PrepareCmdiKeyTable, ActivateCmdiKeyTable;
  StartTlmRecording, CheckTlmOrder, CheckTlmCounters, StopTlmRecording;
  OpenUdpLink, OpenTcpServer, WaitLinkConnected, OpenPtyPair, SendLinkPacket,
  SendLinkCmdiCommand, CheckLinkPacket, CheckNoLinkPacket, CloseLink, OpenGwRelay, SetGwRelayEnabled; StopApp,
  StartApp, WaitForEvent; CheckNodeFile, CompareNodeFiles, RemoveNodeFile; StartStorGroundCapture,
  CheckStorGroundTransfer.
- Run the two ported smoke scripts and each st_*.json (st_gw_commands,
  st_cmdval_hmac, st_tlmo_priority, st_gw_udp, st_gw_tcp, st_gw_serial,
  st_sfi_heartbeat, st_stor_transfer, st_mode_fault_response, st_gw_failover,
  st_cmdi_replay, st_fault_log_transfer, st_safe_mode_node_loss, st_key_switch,
  st_tlmo_profiles) by name with run_ctf.sh, CFS_RUN_DIR and
  CFS_RUN_DIR_CPU2 on scratch copies, 300 s limit each; ctf/run_system_tests.sh
  exists and invokes both smoke scripts then the fifteen st_ scripts in order
  (17 results). Each script must finish within the spec's 180 s budget;
  retain the 300 s per-script grading limit. The host-coordinated serialized
  timing lane, watchdog/rerun policy, raw timestamp bounds, initial MODE
  disable/profile barrier and burst/large-request rules above apply to these
  runs too. Scripts 13-15 never restart SFI while MODE runs.
- New script content checks mirror S8.6, not just existence: stopped GW has no
  manual START, DEGRADED/profile/recovery assertions and one restart; failover
  uses two transparent UDP relays and verifies both-node mode and duplicate-free
  backup delivery; replay saves and resends identical bytes with source-separated
  observation counters; log transfer independently reconstructs and validates
  binary records and FILE_INFO CRC. Run plugin negative-argument/timeouts tests
  and CCDD layout probe for all globals, MODE and expanded structs. Re-run
  setup idempotently with no changes under ignored CTF/venv trees.
- Verify StartStorGroundCapture uses the existing raw TLMO tap, saves ground
  artifact, catches corrupt/missing/duplicate segments and malformed log. Replay
  preserves MAC/time/serial bytes. Relay false consumes/discards rather than
  closing sockets; restoring it recovers keep-alives. All instructions return
  failure with a logged reason on argument/I/O/timeout errors. Human review
  checks that each script demonstrates its named behaviour.
- Run and review `st_safe_mode_node_loss.json` (order 13): two nodes,
  script 10's exact relay ports/health-mode routes; critical LIMIT slot 22
  forces cpu1 SAFE, with actual TLMO profile 2/budget 4 and STOR threshold
  50, and rejects NOMINAL/DEGRADED exits. Cut both relay paths, not the
  apps: verify links 48/49, independent peer-mode loss and SFI NODE_LOSS
  slot 56 with C.7/S8.6 bounds. cpu1 stays SAFE despite the warning
  demands, cpu2 becomes DEGRADED. Clear LIMIT, reject NOMINAL while loss
  remains, accept guarded DEGRADED; restore relays by cut+20 s and clear
  each fault before its retry deadline. Review continuous quiet recovery,
  final profiles and exact SFI/MODE counters/attempts in S8.6, ACKs and
  cleanup. A latched inactive fault must not block recovery; no manual
  NOMINAL shortcut, node stop or SFI restart may substitute for this path.
- Run and review `st_key_switch.json` (order 14): two prebuilt native
  CMDI.ValTable fixtures, K0->K1 through CMDI and K1->K2 through GW;
  all staging is authenticated native TBL LOAD/VALIDATE/ACTIVATE. Review
  retained ACTIVE/NEXT validation, KEY_SWITCH_INF/KeySwitches/NextValid,
  unchanged old-frame HMAC rejection and fresh-key duplicate-serial REPLAY
  rejection within 30 s, then fresh commands executing through both paths.
  GW must share the new key for ingress and egress. Require source-1
  Accepted/HmacErrors/ReplayErrors 2/1/1, source-2 Accepted 7/6 by lane,
  zero other source errors/rows, CMDI HK totals and exact GW frame/packet
  counters specified in S8.6. No table activation/reset/restart may erase
  the replay window between the stimulus, switch and replay assertions.
  Review prepared-image removal, active-key cleanup and link closure.
- Run and review `st_tlmo_profiles.json` (order 15): MODE first disabled,
  initial barrier complete, factors 2/3 at phase 0 on default slots 38/42.
  Inject 12 then 6 packets each with pinned counters/pacing. Verify full
  SEND_STREAM_STATS snapshots Received/Sent/Decimated 12/6/6 and 12/4/8,
  then 18/9/9 and 18/6/12, zero drops/errors/queues, exact raw delivered
  counter lists via CheckTlmCounters, 57 ordered used slots/zero tail.
  MODE SET_MODE DEGRADED, without direct SELECT_PROFILE, must change
  actual budget 16->8 and classes 2/3->2/2; factor/phase/counters persist.
  Observe budget 8 before temporarily raising to 256 for the full reply;
  APPLY_PROFILE restores 8. Verify PacketsDecimated 21, restore factors
  and NOMINAL budget/threshold, and close raw recording on cleanup.
- Plugin review/negative tests: PrepareCmdiKeyTable rejects malformed or
  equal/zero keys, unsafe paths, existing destinations and running targets;
  probe the native image/header/layout. ActivateCmdiKeyTable uses authenticated
  native commands in completion order, fails on TBL rejection/timeouts and
  never silently changes the configured key or default disk table. Confirm
  source/serial allocation is shared with the other plugins. CheckTlmCounters
  must fail on missing/extra/out-of-order/short packets and raw-tap errors,
  including a duplicate that a minimum-count assertion would miss.

## Pilot must confirm (pack should probe these and fail loudly, not silently)
- Zero-timeout reads on a PTY fd through OSAL.
- GW restart over an existing /dev/pts mapping with leftover OSAL resources.
- cpu1 and cpu2 do not share one RAM-disk path.
- CTF's telemetry listener tolerates command-type packets on telemetry ports.
- TLMO pipe depth against 57 default streams.
- CFE_TBL_Share when GW starts before CMDI registers, and across a CMDI restart.
- Node clocks within the age window for GW-to-GW frames.
- TLMO sends the full expanded STOR HK and SFI status (not truncated to 1024);
  CTF decodes them and the raw tap accepts the command-type peer/fault streams.
- CMDI SWITCH_KEY/CFE_TBL_Modified is observed by a shared GW handle without a
  disk rewrite; successful TBL activation explicitly resets CMDI replay slots.

## Pack implementation notes

Layout: `run.sh` (entry point) runs `check.py` (check table `CHECKS`, `RUN_ORDER` per lane, retry policy,
result file and summary). Live plumbing is `node.py` (scratch node copies started with the agent's
`tools/run_cfs.sh`, `Ground` receiver on the TLMO port, framed commands, housekeeping requests, table loads
through cFE TBL LOAD/VALIDATE/ACTIVATE), with the independent codec in `frames.py`, pinned identifiers and
layouts in `spec.py`, table images in `tables.py`. Checks live in `static_checks.py` (C.2, C.6, N, default
tables), `harness_checks.py` + `harness_probe.py` (S3.5), `live_stor.py` (S1, S2), `live_cmdi.py` (S3),
`live_tlmo.py` (S4), `live_gw.py` (S5), `live_sfi.py` (S6), `live_nodes.py` (C.1 probes), `ctf_checks.py`
(S8), `live_mode.py` (S7) and `hidden_glue.py` (component behaviour tests in `hidden_tests/`, sharing the model `t5_model.h`).

Implementation constraints:
- Result file: the contract (enforced by harness/acceptance.py) allows exactly `passed` and `checks` at the
  top level, so the per-subsystem summary (checks, run, passed, failed, fraction, passed_all, overall,
  `preliminary: true`) is written next to it as `<out-stem>.subsystems.json` and printed to stdout.
- Parallelism: the node ports are fixed, so every check that binds them (live checks, the harness CLI, CTF
  scripts) runs in the "isolated" lane, in groups that each get their own nested unprivileged user, network
  (own loopback), mount (private /dev/shm, the OSAL RAM disk), IPC (POSIX queues, named after PIDs) and PID
  namespaces (`isolation.py`; `unshare --kill-child` under `setpriv --pdeathsig`, so a group's processes die
  with it, and pgrep/pkill in a group see only that group). The group's code runs in a further user
  namespace mapping the caller's uid back, so cFS never runs as uid 0. `live_worker.py` runs a group's
  checks in order in a reconstructed context and writes their results after each check. Groups: checks that
  share nodes (TLMO hold, GW UDP, GW SERIAL, STOR peer transfer, two-node probes) and the CTF scripts (they
  share the one set-up ctf/ tree, and wait until s8-setup-fresh, including its idempotence re-run, is over)
  are one group each; every other check is its own group. Timing-sensitive live groups and CTF scripts
  use a host-coordinated serialized lane with reserved CPU capacity, shared across reviews. Finish all
  builds (including CTF setup and hidden-test compilation) before entering that lane; coordinate with
  other reviews so no concurrent builds run. Scheduling-watchdog evidence invalidates a stalled attempt
  and permits one clean serialized rerun; a second stall is an environment failure. Synthetic-time hidden
  tests grade exact deadlines. `T5_PACK_JOBS` may parallelize only non-timing groups outside that lane;
  namespace isolation alone does not establish scheduling isolation. Node start-up failures are shared
  through a locked file, so after two failed starts of a node the remaining groups report it at once.
  Without nested namespaces (or when the build failed) checks run in-process one at a time. The contract
  permits nesting under `ACCEPT_NO_NAMESPACE` (harness/ACCEPTANCE_CONTRACT.md). The build and "cpu" lane
  (unit tests, coverage, behaviour tests built with `make -k -j16`) retain build parallelism outside the
  timing lane. The "misc" lane (static checks, fresh CTF setup, plugin listing, CCDD probe) must finish
  any builds before timing-sensitive live work. In each fresh IPC namespace, namespace-root sets
  /proc/sys/fs/mqueue/msg_max to 512 before remapping to the non-root node UID; repeat capacity and
  RLIMIT_MSGQUEUE preflight there before live checks. Do not rely on an ancestor IPC namespace's setting.
- S1 tables: nodes boot STOR with an entry-less StorageTable and each scenario table is then loaded through
  cFE Table Services, so no default-entry packet files distort the arithmetic Task 4 relied on.
- Events are read from TLMO's event telemetry; only the start-up INIT events are read from the node's console
  log, because they precede TLMO's subscriptions.
- CMDI and GW behaviour tests carry a reference cmdval_lib (in `t5_model.h`) that takes precedence over the
  component's stub library (`-Wl,--allow-multiple-definition`), which isolates them from the agent's library
  as "linked against the cmdval_lib stubs" intends without depending on how the agent wrote its stubs.
- Pilot items are named checks that fail loudly: `n-ram-disks-distinct`, `n-node-clocks`,
  `s4-pipe-depth-probe` (SB PipeOverflowErrorCounter/MsgLimitErrorCounter), `s5-pty-idle-probe`,
  `s5-restart-serial-probe`, `s5-valtable-share-probe` (GW moved before CMDI in the scratch startup script,
  then ES RESTART_APP of CMDI) and `s8-listener-probe` (a review-owned CTF script while command-type and short
  datagrams arrive on UDP 2234).
- Time budget: enlarged Task 5 round/time budgets are TBD by owner. Set the pack's new-work deadline,
  run.sh limit and harness/run.py --acceptance-timeout consistently once assigned, allowing the serialized
  lane and one clean rerun; Task 5 is the auto-compaction test bed with more rounds/time than T1-T4.
- Checks that pass on the unmodified template by design: `n-build-werror`, `n-unit-tests` (the template has
  no CI_LAB/TO_LAB unit tests), `n-change-scope` and `s8-setup-fresh`.
- Command counters (owner decision on C.4): in every app, every rejected command (unknown function code, wrong
  length, and a message with an unexpected MID) increments `CommandErrorCounter` once and nothing else, and every
  accepted command, including request commands such as SEND_STATS, SEND_CLASS_MAP, SEND_CONN_STATUS, SEND_STATUS
  and CLEAR_HISTORY, increments `CommandCounter`. Protocol heartbeats/transfers/fault/health/mode packets and sampled streams
  do not increment CommandCounter; SEND_HK and WAKEUP messages are not commands; RESET_COUNTERS
  leaves both counters at 0. The behaviour tests of all six apps and the live command checks assert this.
- TCP connect cycle (owner decision on S5.2, pinned in the spec): OSAL only, with `OS_SocketConnect(..., 0)`
  and an in-progress attempt polled each WAKEUP with `OS_SelectSingle(READABLE|WRITABLE, 0)` (the pinned OSAL
  has no asynchronous connect completion, and a second `OS_SocketConnect` on the same socket fails). Four
  attempts start 5 s apart (`GW_TCP_ATTEMPT_SEC`, 1 + `GW_TCP_MAX_RETRIES`); an attempt undecided after 5 s
  fails; DOWN (3) follows at the end of the fourth period (20 s) with one `GW_CONN_TIMEOUT_ERR_EID` (28). A
  refused attempt on loopback fails at once, so the attempts are spaced by start time; otherwise DOWN would
  come in milliseconds and the 18-26 s window would not hold. Failed attempts are counted in no connection
  counter. A cycle begins on ADD_CONN, ENABLE_CONN, RELOAD, a table update and a drop from UP; from DOWN only
  DISABLE_CONN then ENABLE_CONN (ENABLE_CONN of an enabled connection stays rejected).
- S6.3 `SFI_ComputeClusters` takes `const` onsets and sorts a local copy of at most `SFI_HISTORY_LEN` entries
  with no dynamic allocation; the clusters behaviour test checks the caller's array is unchanged and its
  feedback quotes that sentence.
