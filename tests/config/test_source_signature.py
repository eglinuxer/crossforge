import ast
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/source_signature.py"
SIGNATURE = runpy.run_path(str(SCRIPT))


class SourceSignatureTests(unittest.TestCase):
    def setUp(self):
        self.primary = "cba23971357c2e6590d9efd3ec8fef3a7bfb4eda"
        self.signing = "c6c265324bbebdc350b513d02d2cef1034921684"
        self.policy = {
            "key_sha256": "1" * 64,
            "primary_fingerprint": self.primary,
            "signing_fingerprint": self.signing,
            "signing_key_expires_at": "2024-08-12T16:30:38Z",
            "signature_time": "2026-07-09T18:21:38Z",
            "status": "cryptographically-valid-expired-key",
            "exception": "upstream-signing-subkey-expired-before-signing",
        }
        self.status = (
            "[GNUPG:] EXPKEYSIG 2D2CEF1034921684 Brad King\n"
            "[GNUPG:] VALIDSIG C6C265324BBEBDC350B513D02D2CEF1034921684 "
            "2026-07-09 1783621298 0 4 0 1 10 00 "
            "CBA23971357C2E6590D9EFD3EC8FEF3A7BFB4EDA\n"
        )

    def run_with_status(self, status, policy=None):
        function = SIGNATURE["verify_expired_key_signature"]
        globals_ = function.__globals__
        original = {
            name: globals_[name]
            for name in ("file_identity", "key_fingerprints", "gpg_status")
        }
        globals_["file_identity"] = lambda _path: {
            "sha256": self.policy["key_sha256"],
            "size": 1,
        }
        globals_["key_fingerprints"] = lambda _gpg, _key, _home: [
            self.primary,
            self.signing,
        ]
        globals_["gpg_status"] = lambda command, _home: (
            (status, "") if "--verify" in command else ("", "")
        )
        try:
            return function(
                Path("gpg"),
                Path("signed"),
                Path("signature"),
                Path("key"),
                policy or self.policy,
            )
        finally:
            globals_.update(original)

    def test_exact_valid_and_expired_records_are_accepted(self):
        result = self.run_with_status(self.status)
        self.assertEqual(
            result,
            {
                "status": "cryptographically-valid-expired-key",
                "signature_time": "2026-07-09T18:21:38Z",
                "exception": "upstream-signing-subkey-expired-before-signing",
                "primary_fingerprint": self.primary,
                "signing_fingerprint": self.signing,
                "signing_key_expires_at": "2024-08-12T16:30:38Z",
            },
        )

    def test_rejected_missing_or_wrong_identity_status_fails_closed(self):
        mutations = (
            self.status.replace(
                "[GNUPG:] EXPKEYSIG 2D2CEF1034921684 Brad King\n", ""
            ),
            "[GNUPG:] BADSIG 2D2CEF1034921684 Brad King\n" + self.status,
            self.status.replace(self.primary.upper(), ("0" * 40).upper()),
        )
        for status in mutations:
            with self.subTest(status=status):
                with self.assertRaises(SIGNATURE["SignatureError"]):
                    self.run_with_status(status)

    def test_signature_must_be_after_the_declared_expiration(self):
        policy = dict(self.policy)
        policy["signing_key_expires_at"] = "2027-01-01T00:00:00Z"
        with self.assertRaises(SIGNATURE["SignatureError"]):
            self.run_with_status(self.status, policy)

    def test_module_is_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
