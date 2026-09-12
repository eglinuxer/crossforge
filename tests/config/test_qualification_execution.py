import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import qualification_execution as execution
    from crossforge_internal.identity import IdentityError
finally:
    sys.path.pop(0)


class QualificationExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "progress.jsonl"
        self.vertex = {"digest": "sha256:" + "a" * 64, "name": "[gate qualification 3/3] RUN /test",
                       "started": "2026-09-10T00:00:01.123456789Z", "completed": "2026-09-10T00:00:02.000Z"}

    def verify(self, vertices, runs=None):
        self.path.write_text("\n".join(json.dumps({"vertexes": [vertex]}) for vertex in vertices))
        return execution.fresh_vertices(self.path, runs or {"qualification": 1},
            "2026-09-10T00:00:00Z", "2026-09-10T00:00:03Z")

    def test_fresh_completed_run_retains_original_times_and_identity(self):
        starting = {key: value for key, value in self.vertex.items() if key != "completed"}
        result = self.verify([starting, self.vertex])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["digest"], self.vertex["digest"])
        self.assertEqual(result[0]["started"], self.vertex["started"])
        self.assertFalse(result[0]["cached"])

    def test_cached_failed_incomplete_outside_interval_and_wrong_stage_rejected(self):
        for change in ({"cached": True}, {"error": "failed"}, {"completed": None},
                       {"started": "2026-09-09T00:00:00Z"}, {"completed": "2026-09-11T00:00:00Z"},
                       {"name": "[gate other 3/3] RUN /test"}, {"name": "[gate qualification 3/3] COPY /test /out"}):
            with self.subTest(change=change), self.assertRaises(IdentityError):
                self.verify([dict(self.vertex, **change)])

    def test_later_update_cannot_hide_cached_or_failed_execution(self):
        for change in ({"cached": True}, {"error": "failed"}):
            with self.assertRaises(IdentityError):
                self.verify([dict(self.vertex, **change), self.vertex])

    def test_missing_extra_or_empty_run_coverage_rejected(self):
        for vertices, runs in (([], {"qualification": 1}), ([self.vertex], {"qualification": 2}),
                               ([self.vertex, dict(self.vertex, digest="sha256:" + "b" * 64)], {"qualification": 1})):
            with self.assertRaises(IdentityError):
                self.verify(vertices, runs)


if __name__ == "__main__":
    unittest.main()
