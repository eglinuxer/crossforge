import ast
import copy
import tempfile
import unittest
from pathlib import Path
import runpy


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/license-file-inventory.py"
INVENTORY = runpy.run_path(str(SCRIPT))
SCHEMA = REPOSITORY / "config/schemas/license-file-inventory.schema.json"
RELEASE = REPOSITORY / "config/release.json"
RELEASE_SCHEMA = REPOSITORY / "config/schemas/release.schema.json"


class LicenseFileInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = INVENTORY["load_schema_document"](
            RELEASE, RELEASE_SCHEMA
        )

    def fixture_root(self, directory):
        root = Path(directory)
        payloads = {
            "opt/crossforge/host-tools/ninja/1.13.2/share/licenses/ninja/COPYING": b"ninja\n",
            "opt/crossforge/python/cp314/build/lib/python3.14/LICENSE.txt": b"python\n",
            "opt/crossforge/vcpkg/root/ports/zlib/copyright": b"port metadata\n",
            "usr/share/licenses/glibc/COPYING": b"glibc\n",
        }
        for relative, payload in payloads.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        unrelated = root / "opt/crossforge/bin/tool"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(b"not a license\n")
        return root

    def test_inventory_is_sorted_release_bound_and_machine_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture_root(directory)
            output = root / "opt/crossforge/LICENSES.json"
            document = INVENTORY["build_inventory"](root, self.release)
            digest = INVENTORY["validate_inventory"](
                document, SCHEMA, document
            )
            self.assertEqual(digest, INVENTORY["canonical_sha256"](document))
            self.assertEqual(document["entry_count"], 4)
            self.assertEqual(
                [entry["path"] for entry in document["entries"]],
                sorted(entry["path"] for entry in document["entries"]),
            )
            components = {
                entry["path"]: entry["component"]
                for entry in document["entries"]
            }
            self.assertEqual(
                components["/usr/share/licenses/glibc/COPYING"],
                "rpm/host/glibc",
            )
            self.assertEqual(
                components[
                    "/opt/crossforge/vcpkg/root/ports/zlib/copyright"
                ],
                "vcpkg/port/zlib",
            )
            ignored_target = root / "opt/crossforge/ignored-target"
            ignored_target.mkdir()
            ignored_link = root / "opt/crossforge/ignored-link"
            ignored_link.symlink_to(ignored_target, target_is_directory=True)
            self.assertEqual(
                INVENTORY["build_inventory"](root, self.release), document
            )
            INVENTORY["write_once"](output, document)
            expected = INVENTORY["build_inventory"](root, self.release)
            loaded = INVENTORY["VALIDATOR"]["load_json"](output)
            INVENTORY["validate_inventory"](loaded, SCHEMA, expected)

    def test_tampering_symlinks_and_empty_license_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture_root(directory)
            document = INVENTORY["build_inventory"](root, self.release)
            for mutate in (
                lambda value: value["entries"][0].__setitem__(
                    "sha256", "0" * 64
                ),
                lambda value: value.__setitem__("entry_count", 3),
                lambda value: value["entries"][0].__setitem__(
                    "unexpected", True
                ),
            ):
                changed = copy.deepcopy(document)
                mutate(changed)
                with self.assertRaises(INVENTORY["InventoryError"]):
                    INVENTORY["validate_inventory"](
                        changed, SCHEMA, document
                    )
            empty = root / "opt/crossforge/share/licenses/empty/LICENSE"
            empty.parent.mkdir(parents=True)
            empty.touch()
            with self.assertRaisesRegex(
                INVENTORY["InventoryError"], "license file is empty"
            ):
                INVENTORY["build_inventory"](root, self.release)
            empty.unlink()
            target = root / "outside"
            target.write_bytes(b"outside\n")
            link = root / "opt/crossforge/share/licenses/escape/LICENSE"
            link.parent.mkdir(parents=True)
            link.symlink_to(target)
            with self.assertRaisesRegex(
                INVENTORY["InventoryError"], "license path is a symlink"
            ):
                INVENTORY["build_inventory"](root, self.release)

    def test_schema_and_script_remain_python36_compatible(self):
        schema = INVENTORY["VALIDATOR"]["load_json"](SCHEMA)
        INVENTORY["VALIDATOR"]["validate_schema_subset"](schema)
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )

    def test_complete_sdk_embeds_and_revalidates_the_inventory(self):
        dockerfile = (
            REPOSITORY / "docker/packaging.Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("LICENSE-APACHE LICENSE-MIT", dockerfile)
        self.assertIn("license-file-inventory.schema.json", dockerfile)
        self.assertIn("scripts/license-file-inventory.py", dockerfile)
        self.assertIn("--output /opt/crossforge/LICENSES.json", dockerfile)
        self.assertEqual(
            dockerfile.count("/work/scripts/license-file-inventory.py"), 2
        )
        self.assertIn("validate --root /", dockerfile)


if __name__ == "__main__":
    unittest.main()
