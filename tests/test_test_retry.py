from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RETRY_SCRIPT = ROOT / "scripts/run-test-with-one-retry.sh"


class TestRetryTests(unittest.TestCase):
    def run_probe(
        self, failures_before_success: int
    ) -> tuple[subprocess.CompletedProcess[str], int]:
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            counter = temp_root / "counter"
            probe = temp_root / "probe.sh"
            probe.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
count=0
if [[ -f "$1" ]]; then
  read -r count <"$1"
fi
count=$((count + 1))
printf '%s\n' "$count" >"$1"
(( count > $2 ))
""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "bash",
                    str(RETRY_SCRIPT),
                    "bash",
                    str(probe),
                    str(counter),
                    str(failures_before_success),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            attempts = int(counter.read_text(encoding="utf-8").strip())
            return result, attempts

    def test_successful_suite_is_not_retried(self) -> None:
        result, attempts = self.run_probe(0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(attempts, 1)
        self.assertNotIn("retrying", result.stderr)

    def test_failed_suite_is_retried_once(self) -> None:
        result, attempts = self.run_probe(1)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(attempts, 2)
        self.assertIn("retrying the complete suite once", result.stderr)

    def test_second_failure_remains_fatal(self) -> None:
        result, attempts = self.run_probe(2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(attempts, 2)

    def test_command_is_required(self) -> None:
        result = subprocess.run(
            ["bash", str(RETRY_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("test command is required", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
