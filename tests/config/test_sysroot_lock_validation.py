import copy
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-sysroot-lock.py"))


def synthetic_lock():
    plan = VALIDATOR["load_json"](
        REPOSITORY / "config/sysroots/el8-x86_64.plan.json"
    )
    fingerprint = "1" * 40
    baseurl = "https://packages.example.invalid/el8/x86_64/"
    packages = []
    for root in plan["roots"]:
        name = root["name"]
        nevra = "%s-0:1.0-1.el8.x86_64" % name
        packages.append(
            {
                "name": name,
                "epoch": 0,
                "version": "1.0",
                "release": "1.el8",
                "arch": "x86_64",
                "nevra": nevra,
                "repo_id": "baseos",
                "location": "Packages/%s.rpm" % nevra,
                "url": "%sPackages/%s.rpm" % (baseurl, nevra),
                "repository_checksum": {
                    "algorithm": "sha256",
                    "value": "2" * 64,
                },
                "sha256": "2" * 64,
                "size": 1,
                "install_size": 1,
                "source_rpm": "%s-1.0-1.el8.src.rpm" % name,
                "signing_key_fingerprint": fingerprint,
                "reason": "root",
            }
        )
    packages.sort(key=lambda package: package["nevra"])
    return {
        "$schema": "https://crossforge.dev/schemas/sysroot-lock.schema.json",
        "schema_version": 1,
        "kind": "sysroot-lock",
        "identity": copy.deepcopy(plan["identity"]),
        "solver": {
            "implementation": "dnf",
            "image": "quay.io/rockylinux/rockylinux:8",
            "image_digest": "sha256:" + "3" * 64,
            "dnf_version": "test",
            "libdnf_version": "test",
            "rpm_version": "test",
            "allowed_arches": ["x86_64", "noarch"],
            "install_weak_deps": False,
            "best": True,
            "strict": True,
            "allow_erasing": False,
        },
        "repositories": [
            {
                "id": "baseos",
                "baseurl": baseurl,
                "repomd_sha256": "4" * 64,
                "metadata": [
                    {
                        "type": "primary",
                        "location": "repodata/primary.xml.gz",
                        "checksum": {
                            "algorithm": "sha256",
                            "value": "5" * 64,
                        },
                    }
                ],
                "gpg_key": {
                    "sha256": "6" * 64,
                    "fingerprint": fingerprint,
                },
            }
        ],
        "roots": copy.deepcopy(plan["roots"]),
        "packages": packages,
    }


class SysrootLockValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = VALIDATOR["load_json"](
            REPOSITORY / "config/sysroots/el8-x86_64.plan.json"
        )
        cls.plan_schema = VALIDATOR["load_json"](
            REPOSITORY / "config/schemas/sysroot-plan.schema.json"
        )
        cls.lock_schema = VALIDATOR["load_json"](
            REPOSITORY / "config/schemas/sysroot-lock.schema.json"
        )

    def test_planning_manifest_is_valid_but_not_a_lock(self):
        VALIDATOR["validate_document"](self.plan, self.plan_schema)
        self.assertEqual(self.plan["kind"], "sysroot-plan")
        self.assertNotIn("packages", self.plan)

    def test_synthetic_complete_lock_is_valid(self):
        VALIDATOR["validate_document"](synthetic_lock(), self.lock_schema)

    def test_current_lock_is_bound_to_release(self):
        lock_path = REPOSITORY / "locks/sysroot-el8-x86_64.json"
        lock = VALIDATOR["load_json"](lock_path)
        VALIDATOR["validate_release_binding"](
            lock, lock_path, REPOSITORY / "config/release.json"
        )

    def test_unknown_plan_field_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["packages"] = []
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](plan, self.plan_schema)

    def test_wrong_root_arch_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["roots"][0]["arch"] = "aarch64"
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](plan, self.plan_schema)

    def test_empty_lock_package_list_is_rejected(self):
        lock = synthetic_lock()
        lock["packages"] = []
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](lock, self.lock_schema)

    def test_non_sha256_checksum_is_rejected(self):
        lock = synthetic_lock()
        lock["packages"][0]["repository_checksum"]["algorithm"] = "sha1"
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](lock, self.lock_schema)

    def test_malformed_sha256_is_rejected(self):
        lock = synthetic_lock()
        lock["packages"][0]["sha256"] = "not-a-sha256"
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](lock, self.lock_schema)

    def test_duplicate_nevra_is_rejected(self):
        lock = synthetic_lock()
        duplicate = copy.deepcopy(lock["packages"][0])
        duplicate["sha256"] = "7" * 64
        lock["packages"].append(duplicate)
        lock["packages"].sort(key=lambda package: package["nevra"])
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](lock, self.lock_schema)

    def test_duplicate_rpm_filename_is_rejected(self):
        lock = synthetic_lock()
        package = copy.deepcopy(lock["packages"][0])
        package["name"] = "extra-package"
        package["nevra"] = "extra-package-0:1.0-1.el8.noarch"
        package["arch"] = "noarch"
        package["reason"] = "dependency"
        lock["packages"].append(package)
        lock["packages"].sort(key=lambda item: item["nevra"])
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](lock, self.lock_schema)

    def test_forbidden_dependency_package_is_rejected(self):
        lock = synthetic_lock()
        package = copy.deepcopy(lock["packages"][0])
        package.update(
            {
                "name": "gcc",
                "nevra": "gcc-0:1.0-1.el8.x86_64",
                "location": "Packages/g/gcc-1.0-1.el8.x86_64.rpm",
                "url": "https://packages.example.invalid/el8/x86_64/Packages/g/gcc-1.0-1.el8.x86_64.rpm",
                "source_rpm": "gcc-1.0-1.el8.src.rpm",
                "reason": "dependency",
            }
        )
        lock["packages"].append(package)
        lock["packages"].sort(key=lambda item: item["nevra"])
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](lock, self.lock_schema)

    def test_package_arch_outside_target_is_rejected(self):
        lock = synthetic_lock()
        package = lock["packages"][0]
        package["arch"] = "aarch64"
        package["nevra"] = "%s-0:%s-%s.aarch64" % (
            package["name"],
            package["version"],
            package["release"],
        )
        lock["packages"].sort(key=lambda item: item["nevra"])
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](lock, self.lock_schema)

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["STRICT_JSON"]["reject_duplicate_keys"](
                [("same", 1), ("same", 2)]
            )


if __name__ == "__main__":
    unittest.main()
