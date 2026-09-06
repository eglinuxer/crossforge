import argparse
import ast
import runpy
import subprocess
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/run-with-heartbeat.py"
RUNNER = runpy.run_path(str(SCRIPT))


class FakeProcess:
    def __init__(self, returncode=0):
        self.pid = 1234
        self.returncode = returncode
        self.waits = 0
        self.forwarded = []

    def wait(self, timeout):
        self.waits += 1
        if self.waits == 1:
            raise subprocess.TimeoutExpired(["command"], timeout)
        return self.returncode

    def poll(self):
        return None if self.waits < 2 else self.returncode

    def send_signal(self, signum):
        self.forwarded.append(signum)


class RunWithHeartbeatTests(unittest.TestCase):
    def test_command_exit_code_and_periodic_liveness_are_preserved(self):
        process = FakeProcess(returncode=7)
        reports = []
        clock = iter((10.0, 71.0))

        def report(message, flush=False):
            reports.append((message, flush))

        returncode = RUNNER["execute"](
            ["example", "--flag"],
            "qualification",
            60,
            popen=lambda command: process,
            clock=lambda: next(clock),
            reporter=report,
            install_signal_handlers=False,
        )
        self.assertEqual(returncode, 7)
        self.assertEqual(
            reports,
            [
                (
                    "heartbeat: qualification elapsed=61s "
                    "pid=1234 status=running",
                    True,
                )
            ],
        )

    def test_signals_are_forwarded_and_signal_exits_are_normalized(self):
        process = FakeProcess(returncode=-15)
        state = {"signal": None}
        RUNNER["forward_signal"](process, state, 15, None)
        self.assertEqual(state["signal"], 15)
        self.assertEqual(process.forwarded, [15])
        self.assertEqual(RUNNER["normalize_returncode"](-15), 143)

        def child_already_exited(_signum):
            raise ProcessLookupError()

        process.send_signal = child_already_exited
        RUNNER["forward_signal"](process, state, 2, None)
        self.assertEqual(state["signal"], 2)

    def test_cli_requires_a_bounded_interval_and_command(self):
        arguments = RUNNER["parse_arguments"](
            [
                "--label",
                "full-sdk",
                "--interval",
                "90",
                "--",
                "docker",
                "buildx",
            ]
        )
        self.assertEqual(arguments.interval, 90)
        self.assertEqual(arguments.command, ["docker", "buildx"])
        for value in ("0", "3601", "invalid"):
            with self.assertRaises(argparse.ArgumentTypeError):
                RUNNER["positive_interval"](value)

    def test_script_remains_python36_syntax_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
