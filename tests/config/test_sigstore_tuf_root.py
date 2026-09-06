import argparse
import ast
import copy
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/verify-sigstore-tuf-root.py"
VERIFIER = runpy.run_path(str(SCRIPT))
EVIDENCE = REPOSITORY / "evidence/sigstore/tuf"


class SigstoreTUFRootTests(unittest.TestCase):
    def arguments(self):
        return argparse.Namespace(
            root_directory=EVIDENCE,
            initial_version=5,
            final_version=15,
            initial_root_sha256=(
                "e2a930b2d1d4053dd56e8faf66fd113658545d522e35d222"
                "ccf58fea87ccccf4"
            ),
            targets=EVIDENCE / "14.targets.json.b64",
            targets_sha256=(
                "6a697f7f8908c8ab26c11786ecb490b54acec97fa8c802e3"
                "99f065f8a0cc1acd"
            ),
            trusted_root=EVIDENCE / "trusted_root.json.b64",
            trusted_root_sha256=(
                "6494e21ea73fa7ee769f85f57d5a3e6a08725eae1e38c755"
                "fc3517c9e6bc0b66"
            ),
            base64_envelopes=True,
        )

    def test_pinned_root_rotations_authorize_the_trusted_root(self):
        self.assertEqual(
            VERIFIER["verify_arguments"](self.arguments()),
            {
                "initial_root_version": 5,
                "final_root_version": 15,
                "targets_version": 14,
                "trusted_root_sha256": (
                    "6494e21ea73fa7ee769f85f57d5a3e6a08725eae1e38c7"
                    "55fc3517c9e6bc0b66"
                ),
            },
        )

    def test_wrong_bootstrap_and_trusted_root_digests_fail_closed(self):
        for field in (
            "initial_root_sha256",
            "targets_sha256",
            "trusted_root_sha256",
        ):
            with self.subTest(field=field):
                arguments = self.arguments()
                setattr(arguments, field, "0" * 64)
                with self.assertRaises(VERIFIER["TUFError"]):
                    VERIFIER["verify_arguments"](arguments)

    def test_signature_threshold_rejects_missing_keyholder_signatures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = EVIDENCE / "5.root.json.b64"
            target = root / "5.root.json"
            VERIFIER["decode_base64_evidence"](
                source, target, "TUF root 5"
            )
            document = VERIFIER["validate_root"](
                VERIFIER["load_json"](target), 5
            )
            incomplete = copy.deepcopy(document)
            incomplete["signatures"] = incomplete["signatures"][:2]
            with self.assertRaisesRegex(
                VERIFIER["TUFError"], "threshold is 3"
            ):
                VERIFIER["verify_signatures"](
                    incomplete,
                    document,
                    "root",
                    "incomplete root",
                )

    def test_canonical_encoder_preserves_olpc_string_rules(self):
        self.assertEqual(
            VERIFIER["canonical_text"](
                {"z": "line\nnext", "a": '\\"'}
            ),
            '{"a":"\\\\\\\"","z":"line\nnext"}',
        )

    def test_malformed_base64_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.b64"
            source.write_text("%%%\n", encoding="utf-8")
            with self.assertRaisesRegex(
                VERIFIER["TUFError"], "invalid base64"
            ):
                VERIFIER["decode_base64_evidence"](
                    source,
                    Path(directory) / "output",
                    "test evidence",
                )

    def test_verifier_is_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
