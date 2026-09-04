import ast
import hashlib
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/resolve_candidate_image.py"
RESOLVER = runpy.run_path(str(SCRIPT))


class CandidateImageResolutionTests(unittest.TestCase):
    def index(self, manifests=None):
        if manifests is None:
            manifests = [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + "2" * 64,
                    "size": 123,
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + "3" * 64,
                    "size": 456,
                    "platform": {"os": "unknown", "architecture": "unknown"},
                },
            ]
        return {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": manifests,
        }

    def raw(self, document):
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )

    def digest(self, payload):
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def test_buildx_metadata_resolves_target_digest(self):
        digest = "sha256:" + "1" * 64
        metadata = {"sdk-candidate": {"containerimage.digest": digest}}
        self.assertEqual(
            RESOLVER["buildx_digest"](metadata, "sdk-candidate"), digest
        )
        self.assertEqual(
            RESOLVER["buildx_digest"](
                {"containerimage.digest": digest}, "sdk-candidate"
            ),
            digest,
        )

    def test_platform_manifest_is_unique_and_index_bytes_are_bound(self):
        payload = self.raw(self.index())
        self.assertEqual(
            RESOLVER["platform_manifest_digest"](
                payload, self.digest(payload), "linux/amd64"
            ),
            "sha256:" + "2" * 64,
        )
        with self.assertRaisesRegex(
            RESOLVER["ImageIdentityError"], "raw OCI index digest differs"
        ):
            RESOLVER["platform_manifest_digest"](
                payload, "sha256:" + "0" * 64, "linux/amd64"
            )

    def test_missing_and_duplicate_platforms_are_rejected(self):
        arm = {
            "digest": "sha256:" + "4" * 64,
            "platform": {"os": "linux", "architecture": "arm64"},
        }
        missing = self.raw(self.index([arm]))
        with self.assertRaisesRegex(
            RESOLVER["ImageIdentityError"], "exactly one linux/amd64"
        ):
            RESOLVER["platform_manifest_digest"](
                missing, self.digest(missing), "linux/amd64"
            )

        descriptor = self.index()["manifests"][0]
        duplicate = self.raw(self.index([descriptor, dict(descriptor)]))
        with self.assertRaisesRegex(
            RESOLVER["ImageIdentityError"], "found 2"
        ):
            RESOLVER["platform_manifest_digest"](
                duplicate, self.digest(duplicate), "linux/amd64"
            )

    def test_manifest_instead_of_index_is_rejected(self):
        manifest = self.raw(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": "sha256:" + "5" * 64},
                "layers": [],
            }
        )
        with self.assertRaisesRegex(
            RESOLVER["ImageIdentityError"], "not an OCI index"
        ):
            RESOLVER["platform_manifest_digest"](
                manifest, self.digest(manifest), "linux/amd64"
            )

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "metadata.json"
            path.write_text(
                '{"sdk-candidate":{},"sdk-candidate":{}}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(
                RESOLVER["ImageIdentityError"], "duplicate JSON key"
            ):
                RESOLVER["load_json"](path)

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
