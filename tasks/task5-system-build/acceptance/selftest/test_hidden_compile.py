"""Compile hidden vectors with an existing pinned cFS coverage build's native headers.

Set T5_COMPILE_COMMANDS to its compile_commands.json to enable this optional gate.
No cFS build is started and no source or build-tree files are changed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import sys
import unittest

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK))

import hidden_glue as hg


class NativeRunnerFeedback(unittest.TestCase):
    def test_native_truncated_labels_preserve_failures_and_reached_cases(self) -> None:
        name = "Keep-alive validation and frame accounting"
        output = f"[BEGIN] 01 {name[:39]}\n[ FAIL] actual native failure\n"
        seen, failed = hg.normalize_case_output(*hg.parse_output(output), [name])
        self.assertEqual(seen, [name])
        self.assertEqual(failed, {name: ["actual native failure"]})

    def test_only_the_exact_pinned_label_is_normalized(self) -> None:
        name = "Enabled mask partial failure and no-op preservation"
        seen, failed = hg.normalize_case_output([name[:37], "SETUP"], {"SETUP": ["bad setup"]}, [name])
        self.assertEqual(seen, [name[:37], "SETUP"])
        self.assertEqual(failed, {"SETUP": ["bad setup"]})
        reached, _ = hg.normalize_case_output([name[:39].rstrip()], {}, [name])
        self.assertEqual(reached, [name])
        with self.assertRaisesRegex(AssertionError, "ambiguous"):
            hg.normalize_case_output([], {}, [name, name[:39] + " other"])

    def test_all_runner_registrations_have_unique_native_labels(self) -> None:
        by_runner: dict[tuple[str, str], list[str]] = {}
        for cid, spec in hg.load_case_specs(PACK).items():
            by_runner.setdefault(hg.HIDDEN_CHECKS[cid], []).extend(spec.cases)
        for names in by_runner.values():
            hg.normalize_case_output([], {}, names)


class NativeHiddenCompile(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("T5_COMPILE_COMMANDS"), "set T5_COMPILE_COMMANDS for native C compilation")
    def test_all_hidden_translation_units(self) -> None:
        database = json.loads(Path(os.environ["T5_COMPILE_COMMANDS"]).read_text())
        coverage = [row for row in database if "hidden_acceptance/coveragetest_" in row["file"]]
        self.assertTrue(coverage, "database must contain native hidden coverage compiler invocations")
        sources = tuple(sorted((PACK / "hidden_tests").glob("*/*.c")))
        self.assertTrue(sources)

        def compile_one(source: Path) -> tuple[Path, int, str]:
            row = next((row for row in coverage if Path(row["file"]).name == source.name), coverage[0])
            args = row.get("arguments") or shlex.split(row["command"])
            command = [args[0], "-I" + str(PACK / "hidden_tests")]
            skip_next = False
            for arg in args[1:]:
                if skip_next:
                    skip_next = False
                    continue
                if arg == "-o":
                    skip_next = True
                    continue
                if arg in ("-c", "-MD", "-MMD", "-MP") or arg == row["file"]:
                    continue
                if arg in ("-MF", "-MT", "-MQ"):
                    skip_next = True
                    continue
                command.append(arg)
            command += ["-fsyntax-only", str(source)]
            # Temporary cwd also contains any compiler scratch files, never the source tree.
            with tempfile.TemporaryDirectory(prefix="task5-native-compile-") as work:
                proc = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=60)
            return source, proc.returncode, proc.stdout + proc.stderr

        with ThreadPoolExecutor(max_workers=16) as pool:
            for source, rc, output in pool.map(compile_one, sources):
                with self.subTest(source=source.relative_to(PACK)):
                    self.assertEqual(rc, 0, output)


if __name__ == "__main__":
    unittest.main()
