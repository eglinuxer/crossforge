import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class BuildkitDiagnosticsTests(unittest.TestCase):
    def test_state_precedes_bootstrap_and_failed_export_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = root / 'bin'
            commands.mkdir()
            docker = commands / 'docker'
            docker.write_text('''#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$1" in
    ps) printf 'abcdef123456\\n' ;;
    inspect) printf '{"state":{"OOMKilled":true,"ExitCode":137},"restart_count":1}\\n' ;;
    logs) printf 'daemon restarted\\n' ;;
    buildx) echo 'builder unavailable' >&2; exit 1 ;;
    *) exit 2 ;;
esac
''')
            docker.chmod(0o755)
            sudo = commands / 'sudo'
            sudo.write_text('#!/usr/bin/env bash\necho "Killed process 42 (buildkitd)"\n')
            sudo.chmod(0o755)
            destination = root / 'evidence'
            calls = root / 'calls'
            result = subprocess.run(
                ['bash', str(ROOT / 'scripts/collect-buildkit-diagnostics.sh'), str(destination)],
                env={**os.environ, 'PATH': str(commands) + ':' + os.environ['PATH'],
                     'CALL_LOG': str(calls)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads((destination / 'buildkit-abcdef123456-state.json').read_text())
            self.assertTrue(state['state']['OOMKilled'])
            self.assertEqual(state['restart_count'], 1)
            sequence = calls.read_text().splitlines()
            self.assertLess(next(i for i, call in enumerate(sequence) if call.startswith('inspect ')),
                            next(i for i, call in enumerate(sequence) if call.startswith('buildx ')))
            self.assertIn('daemon restarted', (destination / 'buildkit-abcdef123456.log').read_text())
            self.assertIn('Killed process', (destination / 'kernel-oom.log').read_text())
            self.assertIn('builder unavailable', (destination / 'history-export.log').read_text())
            self.assertIn('builder unavailable', (destination / 'buildkit-disk.txt').read_text())

    def test_unavailable_docker_and_journal_do_not_hide_original_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('docker', 'sudo'):
                path = root / name
                path.write_text('#!/usr/bin/env bash\necho unavailable >&2\nexit 1\n')
                path.chmod(0o755)
            destination = root / 'evidence'
            result = subprocess.run(
                ['bash', str(ROOT / 'scripts/collect-buildkit-diagnostics.sh'), str(destination)],
                env={**os.environ, 'PATH': directory + ':' + os.environ['PATH']},
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(list(destination.glob('*-state.json')))
            self.assertIn('unavailable', (destination / 'kernel-oom.log').read_text())


if __name__ == '__main__':
    unittest.main()
