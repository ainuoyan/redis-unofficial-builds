from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class LifecycleFailureTests(unittest.TestCase):
    def run_shell(self, backend: str, code: str, **environment: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", f'source "$1"\n{code}', "bash",
             str(ROOT / f"packaging/{backend}/scripts/common.sh")],
            env={**os.environ, **environment}, text=True, capture_output=True, timeout=10,
        )

    def test_update_rolls_back_nested_copy_failure_and_explicit_exit(self) -> None:
        for backend in ("musl", "macos"):
            script = (ROOT / f"packaging/{backend}/scripts/update.sh").read_text()
            tail = script[script.index("\nrollback() {"):]
            for failure in ("false", "exit 74", "kill -TERM $$"):
                with self.subTest(backend=backend, failure=failure):
                    # Execute the actual transaction and rollback, mocking every
                    # mutation and service operation; no host service/files touched.
                    code = '''
backup=/audit-fixture/backup
package_root=/audit-fixture/package
new_version=7.4.11
old_version=7.4.10
new_status=release
was_running=true
recovering_uninstalled=false
stop_service() { echo STOP; }
start_service() { echo START; }
rc-service() { echo "SERVICE $2"; }
rc-update() { :; }
release_lock() { echo UNLOCK; }
install() { :; }
rm() { echo UNEXPECTED_DELETE; return 99; }
tar() { echo RESTORED; }
write_state() { :; }
wait_ready() { return 0; }
''' + f'install_program_files() {{ echo PARTIAL_COPY; {failure}; }}\n' + tail
                    result = self.run_shell(backend, code)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("PARTIAL_COPY", result.stdout)
                    self.assertIn("RESTORED", result.stdout, result.stderr)
                    self.assertIn("UNLOCK", result.stdout, result.stderr)
                    self.assertNotIn("UNEXPECTED_DELETE", result.stdout)
                    self.assertTrue("START" in result.stdout or "SERVICE start" in result.stdout)

    def test_uninstall_stop_failure_never_deletes(self) -> None:
        for backend in ("musl", "macos"):
            script = (ROOT / f"packaging/{backend}/scripts/uninstall.sh").read_text()
            body = script.split("\nvalidate_state\n", 1)[1]
            for purge in ("false", "true"):
                with self.subTest(backend=backend, purge=purge):
                    result = self.run_shell(backend, f'purge={purge}\n' + '''
rc-service() { return 73; }
rc-update() { :; }
stop_service() { return 73; }
launchctl() { return 73; }
rm() { echo WOULD_DELETE; }
''' + body)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("WOULD_DELETE", result.stdout)

    def test_authenticated_service_response_is_ready(self) -> None:
        for backend in ("musl", "macos"):
            for response in ("PONG", "NOAUTH Authentication required.", "NOPERM no permissions"):
                with self.subTest(backend=backend, response=response):
                    # Mock only the bounded transport, not the readiness decision.
                    result = self.run_shell(backend, '''
probe_ready() { printf '%s\n' "$RESPONSE"; }
sleep() { :; }
wait_ready /audit-fixture
''', RESPONSE=response)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_version_updates_are_not_skipped(self) -> None:
        for backend in ("musl", "macos", "windows"):
            name = "Update-Redis.ps1" if backend == "windows" else "update.sh"
            script = (ROOT / f"packaging/{backend}/scripts/{name}").read_text()
            self.assertNotIn("already installed; no changes were made", script)

    def test_probe_quotes_custom_socket_and_drops_inherited_password(self) -> None:
        for backend in ("musl", "macos"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "bin").mkdir()
                (root / "scratch").mkdir()
                cli = root / "bin/redis-cli"
                cli.write_text('''#!/bin/bash
[[ -z "${REDISCLI_AUTH+x}" && "$#" == 3 && "$1" == -s && "$2" == "$EXPECTED_SOCKET" && "$3" == ping ]] || exit 1
printf 'PONG\n'
''')
                cli.chmod(0o755)
                result = self.run_shell(backend, 'probe_ready "$FIXTURE_ROOT"',
                    FIXTURE_ROOT=str(root), TMPDIR=str(root / "scratch"),
                    REDIS_READY_SOCKET=str(root / "socket with spaces"),
                    EXPECTED_SOCKET=str(root / "socket with spaces"), REDISCLI_AUTH="must-not-leak")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "PONG")
                self.assertEqual(list((root / "scratch").iterdir()), [])

    def test_probe_timeout_reaps_its_child_and_cleans_temporary_files(self) -> None:
        for backend in ("musl", "macos"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "bin").mkdir()
                (root / "scratch").mkdir()
                cli = root / "bin/redis-cli"
                cli.write_text('#!/bin/bash\necho $$ > "$PID_FILE"\nwhile :; do :; done\n')
                cli.chmod(0o755)
                result = self.run_shell(backend, 'probe_ready "$FIXTURE_ROOT"',
                    FIXTURE_ROOT=str(root), TMPDIR=str(root / "scratch"), PID_FILE=str(root / "pid"))
                self.assertNotEqual(result.returncode, 0)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((root / "pid").read_text()), 0)
                self.assertEqual(list((root / "scratch").iterdir()), [])

    def test_unknown_readiness_responses_fail(self) -> None:
        for backend in ("musl", "macos"):
            result = self.run_shell(backend, '''
probe_ready() { printf 'ERR unknown command\n'; }
sleep() { :; }
wait_ready /audit-fixture
''')
            self.assertNotEqual(result.returncode, 0)

    def test_stop_guard_rejects_live_processes_and_probe_errors(self) -> None:
        for backend in ("musl", "macos"):
            for status in (0, 1, 2):
                with self.subTest(backend=backend, status=status):
                    result = self.run_shell(backend, '''
pgrep() { return "$MOCK_STATUS"; }
sleep() { :; }
assert_service_stopped
''', MOCK_STATUS=str(status))
                    self.assertEqual(result.returncode == 0, status == 1, result.stderr)


if __name__ == "__main__":
    unittest.main()
