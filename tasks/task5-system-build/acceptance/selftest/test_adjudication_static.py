from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK))

import live_nodes as ln
import static_checks as st
from common import EnvironmentFail, Fail


class AdjudicationFixes(unittest.TestCase):
    def test_ram_volume_identity_accepts_osal_volume_suffix_and_rejects_ambiguity(self) -> None:
        self.assertEqual(ln.resolve_ram_volume_identity([("dev/shm/osal:RAM1", (1, 10))]),
                         ("dev/shm/osal:RAM1", (1, 10)))
        with self.assertRaises(Fail):
            ln.resolve_ram_volume_identity([("dev/shm/osal:RAM1", (1, 10)),
                                            ("tmp/osal:RAM2", (1, 11))], 42)

    def test_install_custom_expands_literal_foreach_and_items_forms(self) -> None:
        calls = st.expand_simple_foreach("""
foreach(APP tlmo stor sfi mode)
  target_include_directories(${APP}.table INTERFACE ${APP_INCLUDE_TABLE_LIST})
endforeach()
foreach(APP IN ITEMS tlmo stor sfi mode)
  target_compile_definitions(${APP}.table INTERFACE ${APP_DEFINITION_TABLE_LIST})
endforeach()
""")
        for app in ("tlmo", "stor", "sfi", "mode"):
            self.assertIn(f"target_include_directories({app}.table", calls)
            self.assertIn(f"target_compile_definitions({app}.table", calls)
        self.assertIn("${APP_INCLUDE_TABLE_LIST}", calls)
        self.assertIn("${APP_DEFINITION_TABLE_LIST}", calls)
        complete = ("set(APP_DEFINITION_TABLE_LIST)\nset(APP_INCLUDE_TABLE_LIST\n"
                    + " $<TARGET_PROPERTY:sample_app,INCLUDE_DIRECTORIES>\n"
                    + " $<TARGET_PROPERTY:hk,INCLUDE_DIRECTORIES>\n"
                    + " $<TARGET_PROPERTY:cmdval_lib,INCLUDE_DIRECTORIES>\n)\n"
                    + "foreach(APP stor cmdi tlmo gw sfi mode)\n"
                    + " $<TARGET_PROPERTY:${APP},INCLUDE_DIRECTORIES>\n"
                    + "endforeach()\n"
                    + "foreach(APP tlmo stor sfi mode)\n"
                    + " target_include_directories(${APP}.table INTERFACE ${APP_INCLUDE_TABLE_LIST})\n"
                    + " target_compile_definitions(${APP}.table INTERFACE ${APP_DEFINITION_TABLE_LIST})\n"
                    + "endforeach()\n")
        self.assertEqual(st.judge_install_custom(complete, "cpu1"), [])

    def test_schedule_probe_checks_effective_macro(self) -> None:
        source = st.schedule_capacity_probe_source()
        self.assertIn('#include "sch_lab_interface_cfg.h"', source)
        self.assertIn("_Static_assert(SCH_LAB_MISSION_MAX_SCHEDULE_ENTRIES == 32", source)

    def test_active_core_modules_excludes_disabled_module_and_applies_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "cfe/cmake").mkdir(parents=True)
            (tree / "sample_defs").mkdir()
            (tree / "cfe/cmake/mission_defaults.cmake").write_text(
                'set(MISSION_CORE_MODULES "es" "sb" "msg")\n')
            (tree / "sample_defs/targets.cmake").write_text(
                'list(REMOVE_ITEM MISSION_CORE_MODULES msg)\nlist(APPEND MISSION_CORE_MODULES ta)\n')
            self.assertEqual(st.active_cfe_modules(tree), ("es", "sb", "ta"))

    def test_mid_probe_limits_apps_to_mission_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "sample_defs").mkdir()
            (tree / "sample_defs/targets.cmake").write_text(
                "list(APPEND MISSION_GLOBAL_APPLIST sample_app stor cmdi)\n"
                "set(cpu1_APPLIST sch_lab ci_lab)\nset(cpu2_APPLIST sch_lab to_lab)\n")
            self.assertEqual(st.active_mission_apps(tree), ("sample_app", "stor", "cmdi", "sch_lab"))

    def test_osal_stress_preflight_reports_quota_as_environment(self) -> None:
        self.assertEqual(len(st.osal_test_queue_inventory()), 64)
        self.assertTrue(all(q.depth == 256 and q.message_size == 4 for q in st.osal_test_queue_inventory()))
        with patch.object(Path, "read_text", return_value="512"), \
             patch.object(st.preflight.resource, "getrlimit", return_value=(819200, 819200)), \
             patch.object(st.preflight.os, "geteuid", return_value=1000):
            with self.assertRaises(EnvironmentFail):
                st.verify_osal_test_queue_quota()


if __name__ == "__main__":
    unittest.main()
