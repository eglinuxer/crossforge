import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/print-build-log-diagnostics.py"


class BuildLogDiagnosticsTests(unittest.TestCase):
    def test_reports_bounded_error_context_from_the_middle_of_a_large_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build.log"
            lines = ["warning %d" % index for index in range(5000)]
            lines[2499] = "FAILED: object.o"
            lines[2500] = "compiler: fatal error: Killed signal"
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(log), "--limit", "20"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAILED: object.o", result.stderr)
        self.assertIn("fatal error: Killed signal", result.stderr)
        self.assertNotIn("warning 4999", result.stderr)
        self.assertLess(len(result.stderr), 5000)

    def test_falls_back_to_a_bounded_tail_without_error_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build.log"
            log.write_text(
                "\n".join("line %d" % index for index in range(50)) + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(log), "--limit", "5"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("line 44", result.stderr)
        self.assertIn("line 45", result.stderr)
        self.assertIn("line 49", result.stderr)


if __name__ == "__main__":
    unittest.main()
