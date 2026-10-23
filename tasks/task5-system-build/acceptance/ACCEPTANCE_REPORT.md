# Task 5 acceptance expansion report

Status: the requested deferred GW boundary cases and adversarial plugin check are implemented. All 25 check ids explicitly named in PLAN.md exist, with 139 registered checks overall and 196 registered component cases. The four new GW cases are included in `s5-hidden-gw`; `s8-plugin-negative` now calls `ctf_checks.c_plugin_adversarial`. Registration alone is not a claim of full PLAN semantic review or production CTF execution.

## Verification

- `./selftest.sh`: PASS. Final run with `T5_COMPILE_COMMANDS` set passed all 130 tests across 11 modules, including native C compilation with no skips. A plain run also passed (129 tests before the final additional fault-log fixture test, with the optional native compiler test skipped). The runner exercises review-owned logic and optional native C compilation, without building cFS or launching flight nodes.
- `git diff --check`: PASS; new source/report files are ASCII. Both installed CPU1/CPU2 native CMDI image headers were also accepted by the semantic header validator (208-byte images).
- Native C: all nine hidden translation units compile with `-std=c99 -pedantic -Wall -Werror` against the existing pinned native coverage build's headers and this pack's `t5_model.h`. Reproduce with `T5_COMPILE_COMMANDS=<native-cpu1-compile_commands.json> ./selftest.sh`. The optional compiler gate puts this pack's model include path first and generates no source-tree output.
- GW behavior: compiled the current hidden runner and the retained GW subject from `/workspace/t5-ref-build/component/tree/apps/gw/fsw/src/gw_app.c` into temporary objects, linked the existing pinned cFE/OSAL test libraries, and ran all 42 cases successfully. All cases were reached after native-label normalization. Four independent temporary mutations were rejected by their intended new cases: retained primary stability across interruption, double egress-error counting with both endpoints down, retained preference on table activation, and CONN_TIMEOUT substituted for KEEPALIVE_ERR. No retained subject or native build-tree file was edited.
- The attempted `py_compile` command was rejected before shell startup; it was not retried. Full `python -m py_compile` verification therefore remains uncompleted under the blocked-action constraint. See the exact command and rejection in Blocked operations. Behavioral selftests independently import and exercise the Python modules, including the generated CTF adapter.
- No git writes, mission builds, full production CTF/plugin run or live flight-node scenarios were performed in this completion. Temporary native compiler/runner artifacts stay under `/tmp` and are removed. All source edits are under acceptance/; spec.md and PLAN.md are unchanged.

## Implemented changes

MODE has a new live module, native table/status builders and a production-entry-point hidden runner. SFI now covers expanded fault slots, LIMIT/latches/enables, event windows, mixed clustering, health, binary logs and MODE response interactions. CMDI gains replay/key/source/order vectors and live scenarios. TLMO gains profiles, decimation, ownership/reordering, statistics and maximum-size component vectors. STOR gains quota, FILE_INFO/error/CRC and priority accounting checks. GW gains keep-alive, routing, native layouts and dual relays. CTF is S8 with fifteen system scripts, two smoke scripts, instruction/layout/content checks and public-dispatch adversarial plugin checks. The registered plugin check constructs 71 CTF scenarios: 28 missing-argument cases and 43 semantic/lifecycle vectors, including one vector with five native activation cases.

Shared plumbing includes per-node/source serial allocation; source 2 observations; authenticated ES/table/control helpers; paced <=8-packet, <=1024-byte live injection; serialized large requests and held-snapshot budget restoration; initial MODE disable/profile barrier; raw fault/health/mode collection and payload mirror equality; and SB allocation evidence. Queue preflight sets msg_max in each fresh IPC namespace, checks final non-root identity and per-process RLIMIT_MSGQUEUE, inventories unlinked actual OSAL queues through process descriptors, verifies mq_getattr depths, and allocation-probes the full learned two-node inventory after stopping the complete node lifetime. CTF performs this inventory preflight before its script clock and monitors actual script queues too. Environment failures are ungraded separately in subsystem summaries; truncation cannot pass depth requirements.

Live groups run serially after all builds/static/unit work, under a host-wide lock. `T5_RESERVED_CPUS` must name at least two reserved CPUs and `T5_HOST_BUILDS_QUIESCENT=1` records the coordinator's confirmation that other reviews are not building. A separate watchdog records host monotonic scheduling evidence, invalidates a stalled attempt and permits one clean rerun. A second stall is ENVIRONMENT. Evidence is retained in `<out-stem>.evidence.json`, including queue inventory and native MID/pool probe output. Exact deadlines are covered by synthetic-time component vectors; Python tests verify the timing and rerun machinery itself.

The default new-work budget is provisionally 180 minutes, run.sh permits 10920 seconds and timeout_s is 11000. PLAN leaves the owner-assigned round/time budget TBD; an external harness timeout must be at least 11000 seconds or use the task timeout file. No external harness configuration was edited.

## Remaining coverage and review work

The current request supersedes PLAN.md's historical out-of-scope bullet for these GW/plugin items. They are now covered:

- `gw_boundary_cases.h`: interrupted primary stability, including simultaneous primary expiry/failback deadline; no selected route and one error per packet with both endpoints unavailable; identical used-route table activation clearing preferences; TCP connect exhaustion versus an UP keep-alive expiry, retained transport, exact SFI fault/recovery payloads, transport-failure reporting, reconnect exhaustion and administrative clearing.
- `ctf_adversarial.py` and `ctf_checks.c_plugin_adversarial`: public CTF dispatch with positive controls and strict instruction-level boolean/exception/log adjudication; malformed, missing, duplicate/conflicting STOR segments and invalid file CRC/offset/padding; malformed fault logs and exact report matching; missing/extra/out-of-order/short raw counters and an injected listener failure; invalid keys/paths, existing/running targets and native-image write failure; CMDI/ES/link serial sharing, explicit serial advancement and replay preservation.
- `ctf_native_probe.py`: full native image payload/header validation, separate LOAD/VALIDATE/ACTIVATE rejection, one whole-instruction timeout and ordered successful completion; commands are independently authenticated/decoded, operation completion uses the public telemetry listener, and failures must surface their reason. Default image/configured-key preservation and RemoveNodeFile cleanup are checked. Generated header description/timestamps are permitted to differ.
- Native compilation exposed acceptance-side MODE issues: four misleading-indentation warnings and an unavailable cFE status constant are corrected. Native runtime exposed OSAL's 39-character case-name truncation; hidden_glue now normalizes exact, unambiguous native labels back to registered names while retaining failure attribution.

Outstanding review limits:

- S8 script content gates and the 17 per-script executions are registered. Full S8.6 semantic review remains preliminary pending a reference implementation and human review, as the spec requires.
- Production SFI ReportSequence wrap is not reached by the bounded black-box runner: it would require 2^32 report edges or a nonpublic state mutation hook. Python binary-log wrap parsing and MODE's public report-serial wrap vectors are covered instead. This is a testability limit, not a spec contradiction.
- Apart from the GW native run described above, the other production hidden runners, independent native MID/pool probe, actual queue allocation/inventory, live timing, both-node routing/restart behavior and CTF plugin/script executions remain unexercised in this completion. Pack-side native compiler, adapter and transport tests do not establish full flight-system or production CTF conformance. The retained partial reference/fake drivers are not an expanded reference solution.

## Spec issues

No concrete contradiction in spec.md was found. S5.2 lines 1343-1360 defines initial TCP connect exhaustion and S5.4 lines 1532-1543 defines timer/transport-failure reports, but neither explicitly states whether a never-UP TCP connection also reports a link fault on its first connect exhaustion. The spec now pins it: a never-UP TCP connection sends no REPORT_LINK on connect exhaustion. The new vector grades CONN_TIMEOUT versus KEEPALIVE_ERR and the zero keep-alive counter there. S8.3 lines 2475-2478 requires instruction-level failure and continuation, so process exit status alone is not treated as a rejection oracle. S8.3 lines 2569-2571 does not pin header creation metadata; native-image checks validate semantic native header fields and payload, permitting timestamps/descriptions to vary. C.7 does not quantify simultaneous background traffic, so static SB pool sizing computes a necessary descriptor/bucket-rounded envelope bound and the live allocator counters/log gate supplies the background sufficiency check. Exhaustive SFI report-number wrap is not feasible through the bounded public interface, as described above.

## Historical blocked operations (prior expansion)

Two broad read-only searches encountered the same unrelated protected directory and were stopped without retrying that search line:

1. `rg --files /workspace | rg '/(cmdi_plugin|tlm_order_plugin|link_plugin|stor_ground_plugin)/' | head -25`
2. `rg -n 'CFE_ES_APPID_C' /workspace -g 'cfe_es_api_typedefs.h' -g 'cfe_es_extern_typedefs.h' -g 'cfe_es_api.h' --max-count 1`

Both reported: `rg: /workspace/explore-pgdata/_workspace_worktree_the-founders-app_debates_debates-clean: Permission denied (os error 13)`.

The delegated request to complete the remaining GW boundary vectors and plugin adversarial checks ended with: `Agent errored: This content was flagged for possible cybersecurity risk. If this seems wrong, try rephrasing your request. If you're doing authorized security work that requires more cyber permissive safeguards, apply for Daybreak access via https://platform.openai.com/settings/organization/status-and-access before retrying.` The blocked work was not retried or rephrased.

## Files added

- `ACCEPTANCE_REPORT.md`
- `hidden_tests/cmdi/expanded_cmdi.h`
- `hidden_tests/mode/CMakeLists.txt`
- `hidden_tests/mode/cases.json`
- `hidden_tests/mode/coveragetest_mode_hidden.c`
- `hidden_tests/mode/mode_action_cases.h`
- `hidden_tests/sfi/sfi_expanded_cases.h`
- `hidden_tests/stor/expanded_stor.h`
- `hidden_tests/tlmo/expanded_tlmo.h`
- `live_mode.py`
- `preflight.py`
- `selftest/test_expanded_gw_ctf.py`
- `selftest/test_expanded_subsystems.py`
- `selftest/test_mode_sfi.py`
- `selftest/test_plumbing.py`
- `selftest/test_pool_checks.py`
- `spec_sfi_mode.py`
- `tables_sfi_mode.py`
- `timing.py`

## Files changed

- `check.py`
- `common.py`
- `ctf_checks.py`
- `frames.py`
- `hidden_glue.py`
- `hidden_tests/cmdi/cases.json`
- `hidden_tests/cmdi/coveragetest_cmdi_hidden.c`
- `hidden_tests/gw/cases.json`
- `hidden_tests/gw/coveragetest_gw_hidden.c`
- `hidden_tests/sfi/cases.json`
- `hidden_tests/sfi/coveragetest_sfi_hidden.c`
- `hidden_tests/stor/cases.json`
- `hidden_tests/stor/coveragetest_stor_hidden.c`
- `hidden_tests/t5_model.h`
- `hidden_tests/tlmo/cases.json`
- `hidden_tests/tlmo/coveragetest_tlmo_hidden.c`
- `isolation.py`
- `live_cmdi.py`
- `live_common.py`
- `live_gw.py`
- `live_nodes.py`
- `live_sfi.py`
- `live_stor.py`
- `live_tlmo.py`
- `node.py`
- `run.sh`
- `selftest.sh`
- `selftest/hidden_reference/apps/gw/fsw/src/gw_app.c`
- `selftest/test_judges.py`
- `selftest/test_schema_scoring.py`
- `spec.py`
- `static_checks.py`
- `tables.py`
- `timeout_s`

## Explicit PLAN check-id mapping

| Check id | Implementing function |
| --- | --- |
| `n-build-werror` | `static_checks.c_build_werror` |
| `n-change-scope` | `static_checks.c_change_scope` |
| `n-node-clocks` | `live_nodes.c_node_clocks` |
| `n-ram-disks-distinct` | `live_nodes.c_ram_disks_distinct` |
| `n-unit-tests` | `static_checks.c_unit_tests` |
| `s3-key-rotation` | `live_cmdi.c_key_rotation` |
| `s3-replay-window` | `live_cmdi.c_replay_window` |
| `s4-pipe-depth-probe` | `live_tlmo.c_pipe_depth_probe` |
| `s4-stream-profiles` | `live_tlmo.c_stream_profiles` |
| `s5-link-health` | `live_gw.c_link_health` |
| `s5-pty-idle-probe` | `live_gw.c_pty_idle_probe` |
| `s5-restart-serial-probe` | `live_gw.c_restart_serial_probe` |
| `s5-route-failover` | `live_gw.c_route_failover` |
| `s5-valtable-share-probe` | `live_gw.c_valtable_share_probe` |
| `s6-event-rate` | `live_sfi.c_event_rate` |
| `s6-limit-latch-enable` | `live_sfi.c_limit_latch_enable` |
| `s6-log-ground` | `live_sfi.c_log_ground` |
| `s6-mixed-cluster` | `live_sfi.c_mixed_cluster` |
| `s6-peer-health` | `live_sfi.c_peer_health` |
| `s7-cooldown-cap` | `live_mode.c_cooldown_cap` |
| `s7-fault-response` | `live_mode.c_fault_response` |
| `s7-peer-mode` | `live_mode.c_peer_mode` |
| `s7-response-actions` | `live_mode.c_response_actions` |
| `s8-listener-probe` | `ctf_checks.c_listener_probe` |
| `s8-setup-fresh` | `ctf_checks.c_setup_fresh` |

## Complete registered check mapping

| Check id | Implementing function |
| --- | --- |
| `n-build-werror` | `static_checks.c_build_werror` |
| `n-component-layout` | `static_checks.c_layout` |
| `n-targets` | `static_checks.c_targets` |
| `n-startup-scripts` | `static_checks.c_startup_scripts` |
| `n-install-custom` | `static_checks.c_install_custom` |
| `n-schedule` | `static_checks.c_schedule` |
| `n-readme` | `static_checks.c_readme` |
| `n-change-scope` | `static_checks.c_change_scope` |
| `n-unit-tests` | `static_checks.c_unit_tests` |
| `n-coverage-tests` | `static_checks.c_coverage_tests_present` |
| `n-coverage` | `static_checks.c_coverage` |
| `n-design-notes` | `static_checks.c_design_notes` |
| `n-system-report` | `static_checks.c_system_report` |
| `n-run-scripts` | `live_nodes.c_run_scripts` |
| `n-nodes-operational` | `live_nodes.c_nodes_operational` |
| `n-ram-disks-distinct` | `live_nodes.c_ram_disks_distinct` |
| `n-node-clocks` | `live_nodes.c_node_clocks` |
| `s1-default-table` | `static_checks.c_default_tables(ctx, "STOR")` |
| `s1-scheduled-housekeeping` | `live_stor.c_scheduled_housekeeping` |
| `s1-startup-and-hk` | `live_stor.c_startup_and_hk` |
| `s1-decimation-and-stats` | `live_stor.c_decimation_and_stats` |
| `s1-packet-file-naming` | `live_stor.c_packet_file_naming` |
| `s1-rollover` | `live_stor.c_rollover` |
| `s1-idle-file-close` | `live_stor.c_idle_file_close` |
| `s1-purge-order` | `live_stor.c_purge_order` |
| `s1-purge-archived-first` | `live_stor.c_purge_archived_first` |
| `s1-quota-drop` | `live_stor.c_quota_drop` |
| `s1-restart-scan` | `live_stor.c_restart_scan` |
| `s1-set-param-errors` | `live_stor.c_set_param_errors` |
| `s1-file-add-list-delete` | `live_stor.c_file_add_list_delete` |
| `s1-file-path-rejection` | `live_stor.c_file_path_rejection` |
| `s1-file-exists-rejection` | `live_stor.c_file_exists_rejection` |
| `s1-file-quota-rejection` | `live_stor.c_file_quota_rejection` |
| `s1-reset-counters` | `live_stor.c_reset_counters` |
| `s1-table-validation-live` | `live_stor.c_table_validation_live` |
| `s1-hidden-stor` | `hidden_glue.HiddenTests.verdict("s1-hidden-stor") - stor/hidden` |
| `s2-ground-transfer` | `live_stor.c_ground_transfer` |
| `s2-tlm-transfer-archive` | `live_stor.c_tlm_transfer_archive` |
| `s2-peer-transfer` | `live_stor.c_peer_transfer` |
| `s2-peer-exists-nak` | `live_stor.c_peer_exists_nak` |
| `s2-abort-and-rejections` | `live_stor.c_transfer_abort_and_rejections` |
| `s2-hidden-stor-transfer` | `hidden_glue.HiddenTests.verdict("s2-hidden-stor-transfer") - stor/hidden` |
| `s3-default-table` | `static_checks.c_default_tables(ctx, "CMDI")` |
| `s3-cmdval-self-contained` | `static_checks.c_cmdval_self_contained` |
| `s3-hidden-cmdval-lib` | `hidden_glue.HiddenTests.verdict("s3-hidden-cmdval-lib") - cmdval_lib/hidden` |
| `s3-hidden-cmdi` | `hidden_glue.HiddenTests.verdict("s3-hidden-cmdi") - cmdi/hidden` |
| `s3-valid-frame-executes` | `live_cmdi.c_valid_frame_executes` |
| `s3-unframed-format` | `live_cmdi.c_unframed_format` |
| `s3-rejection-reasons` | `live_cmdi.c_rejection_reasons` |
| `s3-set-enable-mask` | `live_cmdi.c_set_enable_mask` |
| `s3-set-max-age` | `live_cmdi.c_set_max_age` |
| `s3-cmdi-commands` | `live_cmdi.c_cmdi_commands` |
| `s3-harness-functions` | `harness_checks.c_harness_functions` |
| `s3-harness-cli` | `harness_checks.c_harness_cli` |
| `s3-harness-live` | `live_cmdi.c_harness_live` |
| `s3-cpu2-frame` | `live_cmdi.c_cpu2_frame` |
| `s4-default-table` | `static_checks.c_default_tables(ctx, "TLMO")` |
| `s4-hidden-tlmo` | `hidden_glue.HiddenTests.verdict("s4-hidden-tlmo") - tlmo/hidden` |
| `s4-default-telemetry` | `live_tlmo.c_default_telemetry` |
| `s4-hold-drop-arithmetic` | `live_tlmo.c_hold_drop_arithmetic` |
| `s4-drain-order` | `live_tlmo.c_drain_order` |
| `s4-set-class` | `live_tlmo.c_set_class` |
| `s4-set-budget` | `live_tlmo.c_set_budget` |
| `s4-add-remove-stream` | `live_tlmo.c_add_remove_stream` |
| `s4-table-update` | `live_tlmo.c_table_update` |
| `s4-class-map` | `live_tlmo.c_class_map` |
| `s4-tlmo-commands` | `live_tlmo.c_tlmo_commands` |
| `s4-cpu2-telemetry` | `live_tlmo.c_cpu2_telemetry` |
| `s4-pipe-depth-probe` | `live_tlmo.c_pipe_depth_probe` |
| `s5-default-table` | `static_checks.c_default_tables(ctx, "GW")` |
| `s5-hidden-gw` | `hidden_glue.HiddenTests.verdict("s5-hidden-gw") - gw/hidden` |
| `s5-udp-egress` | `live_gw.c_udp_egress` |
| `s5-udp-ingress` | `live_gw.c_udp_ingress` |
| `s5-ingress-rejections` | `live_gw.c_ingress_rejections` |
| `s5-loop-drop` | `live_gw.c_loop_drop` |
| `s5-valtable-key-change` | `live_gw.c_valtable_key_change` |
| `s5-tcp` | `live_gw.c_tcp` |
| `s5-tcp-down` | `live_gw.c_tcp_down` |
| `s5-serial` | `live_gw.c_pty` |
| `s5-pty-idle-probe` | `live_gw.c_pty_idle_probe` |
| `s5-restart-serial-probe` | `live_gw.c_restart_serial_probe` |
| `s5-conn-commands` | `live_gw.c_conn_commands` |
| `s5-valtable-share-probe` | `live_gw.c_valtable_share_probe` |
| `s6-default-table` | `static_checks.c_default_tables(ctx, "SFI")` |
| `s6-hidden-sfi-clusters` | `hidden_glue.HiddenTests.verdict("s6-hidden-sfi-clusters") - sfi/hidden_clusters` |
| `s6-hidden-sfi` | `hidden_glue.HiddenTests.verdict("s6-hidden-sfi") - sfi/hidden` |
| `s6-apps-ok-after-startup` | `live_sfi.c_apps_ok_after_startup` |
| `s6-gw-stop-miss` | `live_sfi.c_gw_stop_miss` |
| `s6-cluster-and-clear` | `live_sfi.c_cluster_and_clear` |
| `s6-stall-rate` | `live_sfi.c_stall_rate` |
| `s6-sfi-commands` | `live_sfi.c_sfi_commands` |
| `s8-setup-fresh` | `ctf_checks.c_setup_fresh` |
| `s8-plugin-layout` | `ctf_checks.c_plugin_layout` |
| `s8-plugin-info` | `ctf_checks.c_plugin_info` |
| `s8-ccdd-layout` | `ctf_checks.c_ccdd_layout` |
| `s8-run-system-tests` | `ctf_checks.c_run_system_tests_script` |
| `s8-listener-probe` | `ctf_checks.c_listener_probe` |
| `s8-smoke-sample-app` | `ctf_checks.c_smoke_sample_app` |
| `s8-smoke-housekeeping` | `ctf_checks.c_smoke_housekeeping` |
| `s8-st-gw-commands` | `ctf_checks.c_gw_commands` |
| `s8-st-cmdval-hmac` | `ctf_checks.c_cmdval_hmac` |
| `s8-st-tlmo-priority` | `ctf_checks.c_tlmo_priority` |
| `s8-st-gw-udp` | `ctf_checks.c_gw_udp` |
| `s8-st-gw-tcp` | `ctf_checks.c_gw_tcp` |
| `s8-st-gw-serial` | `ctf_checks.c_gw_serial` |
| `s8-st-sfi-heartbeat` | `ctf_checks.c_sfi_heartbeat` |
| `s8-st-stor-transfer` | `ctf_checks.c_stor_transfer` |
| `s8-st-mode-fault-response` | `ctf_checks.c_mode_fault_response` |
| `s8-st-gw-failover` | `ctf_checks.c_gw_failover` |
| `s8-st-cmdi-replay` | `ctf_checks.c_cmdi_replay` |
| `s8-st-fault-log-transfer` | `ctf_checks.c_fault_log_transfer` |
| `s8-st-safe-mode-node-loss` | `ctf_checks.c_safe_mode_node_loss` |
| `s8-st-key-switch` | `ctf_checks.c_key_switch` |
| `s8-st-tlmo-profiles` | `ctf_checks.c_tlmo_profiles` |
| `n-queue-capacity` | `live_nodes.c_queue_capacity` |
| `n-mid-inventory` | `static_checks.c_mid_inventory` |
| `n-mode-integration` | `static_checks.c_mode_integration` |
| `n-sb-pool-sizing` | `static_checks.c_sb_pool_sizing` |
| `s1-quota-file-info` | `live_stor.c_quota_file_info` |
| `s3-replay-window` | `live_cmdi.c_replay_window` |
| `s3-key-rotation` | `live_cmdi.c_key_rotation` |
| `s3-same-wakeup-order` | `live_cmdi.c_same_wakeup_order` |
| `s4-stream-profiles` | `live_tlmo.c_stream_profiles` |
| `s4-stream-reorder` | `live_tlmo.c_stream_reorder` |
| `s5-link-health` | `live_gw.c_link_health` |
| `s5-route-failover` | `live_gw.c_route_failover` |
| `s6-limit-latch-enable` | `live_sfi.c_limit_latch_enable` |
| `s6-event-rate` | `live_sfi.c_event_rate` |
| `s6-mixed-cluster` | `live_sfi.c_mixed_cluster` |
| `s6-peer-health` | `live_sfi.c_peer_health` |
| `s6-log-ground` | `live_sfi.c_log_ground` |
| `s7-default-table` | `static_checks.c_default_tables(ctx, "MODE")` |
| `s7-hidden-mode` | `hidden_glue.HiddenTests.verdict("s7-hidden-mode") - mode/hidden` |
| `s7-fault-response` | `live_mode.c_fault_response` |
| `s7-cooldown-cap` | `live_mode.c_cooldown_cap` |
| `s7-response-actions` | `live_mode.c_response_actions` |
| `s7-peer-mode` | `live_mode.c_peer_mode` |
| `s8-script-content` | `ctf_checks.c_script_content` |
| `s8-plugin-negative` | `ctf_checks.c_plugin_adversarial` |

## Component case inventories

| Check id | Runner | Registered cases |
| --- | --- | --- |
| `s3-hidden-cmdi` | `cmdi/hidden` | 22 |
| `s3-hidden-cmdval-lib` | `cmdval_lib/hidden` | 12 |
| `s5-hidden-gw` | `gw/hidden` | 42 |
| `s7-hidden-mode` | `mode/hidden` | 17 |
| `s6-hidden-sfi` | `sfi/hidden` | 25 |
| `s6-hidden-sfi-clusters` | `sfi/hidden_clusters` | 12 |
| `s1-hidden-stor` | `stor/hidden` | 26 |
| `s2-hidden-stor-transfer` | `stor/hidden` | 14 |
| `s4-hidden-tlmo` | `tlmo/hidden` | 26 |

## New GW case mapping

| Existing check id | Case | Implementing function |
| --- | --- | --- |
| `s5-hidden-gw` | Interrupted primary stability interval | `Test_InterruptedPrimaryStability` |
| `s5-hidden-gw` | Route with both endpoints unavailable | `Test_RouteBothUnavailable` |
| `s5-hidden-gw` | Table activation preference reset | `Test_TableActivationPreferenceReset` |
| `s5-hidden-gw` | TCP connect vs keep-alive reports | `Test_TcpTimeoutAndKeepAliveReports` |

No new top-level check ids were introduced. The existing `s8-plugin-negative` id is retained and runs with the serialized CTF group and live timing prerequisites.

## Files touched in this completion

- `ACCEPTANCE_REPORT.md`, `check.py`, `ctf_checks.py`, `ctf_adversarial.py`, `ctf_native_probe.py`
- `hidden_glue.py`, `hidden_tests/gw/cases.json`, `hidden_tests/gw/coveragetest_gw_hidden.c`, `hidden_tests/gw/gw_boundary_cases.h`
- `hidden_tests/mode/coveragetest_mode_hidden.c`, `hidden_tests/mode/mode_action_cases.h`
- `selftest.sh`, `selftest/test_expanded_gw_ctf.py`, `selftest/test_hidden_compile.py`, `selftest/test_ctf_adversarial.py`, `selftest/test_ctf_native_probe.py`

## Blocked operation during this completion

A delegated verification tool call failed before shell startup. Exact command:

```sh
rg -n '^HOST|def judge_adversarial|outcome = re.search' spec.py ctf_checks.py ctf_adversarial.py && python3 -B -m py_compile ctf_checks.py ctf_adversarial.py selftest/test_ctf_adversarial.py
```

Requested workdir: `/workspace/worktree/agent-experiments/task5-expand/tasks/task5-system-build/acceptance`, default zsh/login shell. Exact rejection:

```text
exec_command failed: CreateProcess { message: "Rejected(\"Failed to create unified exec process: No such file or directory (os error 2)\")" }
```

This syntax-check line was stopped without retrying it through another shell, path, interpreter, or tool. Independent behavioral and native compiler tests continued. The earlier historical blocked-operations section is retained from the prior expansion report.
