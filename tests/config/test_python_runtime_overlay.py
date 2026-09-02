import copy
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
OVERLAY = runpy.run_path(str(REPOSITORY / "scripts/assemble-python-runtime.py"))


class PythonRuntimeOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = OVERLAY["MATERIALIZER"]["load_lock"](
            REPOSITORY / "locks/sysroot-el8-x86_64.json"
        )
        cls.release = OVERLAY["load_json"](REPOSITORY / "config/release.json")
        cls.selected = OVERLAY["select_runtime_packages"](
            cls.context, cls.context["packages"]
        )

    def evidence(self):
        base = [("bash", "x86_64", "bash-0:4.4.20-6.el8_10.x86_64")]
        installed = [
            (item["item"]["name"], item["item"]["arch"], item["item"]["nevra"])
            for item in self.selected
        ]
        return OVERLAY["build_evidence"](
            self.context,
            self.release,
            self.release["base_image"]["manifests"]["amd64"],
            self.selected,
            base,
            sorted(base + installed, key=lambda row: row[2]),
            "1" * 64,
        )

    def test_evidence_identity_is_canonical_and_strict(self):
        evidence = self.evidence()
        OVERLAY["validate_evidence"](evidence)
        self.assertEqual(
            evidence["identity_sha256"],
            OVERLAY["canonical_sha256"](evidence["identity"]),
        )
        forged = copy.deepcopy(evidence)
        forged["unexpected"] = True
        with self.assertRaises(OVERLAY["ValidationError"]):
            OVERLAY["validate_evidence"](forged)

    def test_overlay_transaction_is_explicitly_non_deployable(self):
        arguments = OVERLAY["installation_arguments"](
            Path("/runtime-root"), "foreign-test-arch", [Path("runtime.rpm")], True
        )
        self.assertIn("--nodeps", arguments)
        self.assertIn("--noscripts", arguments)
        self.assertIn("--notriggers", arguments)
        self.assertIn("--ignorearch", arguments)
        self.assertIn("--test", arguments)


if __name__ == "__main__":
    unittest.main()
