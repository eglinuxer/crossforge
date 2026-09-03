import copy
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-rpm-lock.py"))
RESOLVER = runpy.run_path(
    str(REPOSITORY / "scripts/resolve-rpm-transaction.py")
)


class RpmLockValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plans = [
            REPOSITORY / "config/rpm/sysroot-el8-x86_64.plan.json",
            REPOSITORY / "config/rpm/sysroot-el8-aarch64.plan.json",
            REPOSITORY / "config/rpm/host-build-common-el8-x86_64.plan.json",
            REPOSITORY / "config/rpm/host-gcc-build-el8-x86_64.plan.json",
            REPOSITORY / "config/rpm/host-gcc-test-el8-x86_64.plan.json",
            REPOSITORY / "config/rpm/host-python-build-el8-x86_64.plan.json",
            REPOSITORY / "config/rpm/host-runtime-el8-x86_64.plan.json",
        ]
        cls.transactions = [
            REPOSITORY / "locks/transactions/sysroot-el8-x86_64.json",
            REPOSITORY / "locks/transactions/sysroot-el8-aarch64.json",
            REPOSITORY / "locks/transactions/host-build-common-el8-x86_64.json",
            REPOSITORY / "locks/transactions/host-gcc-build-el8-x86_64.json",
            REPOSITORY / "locks/transactions/host-gcc-test-el8-x86_64.json",
            REPOSITORY / "locks/transactions/host-python-build-el8-x86_64.json",
            REPOSITORY / "locks/transactions/host-runtime-el8-x86_64.json",
        ]
        cls.locks = [
            REPOSITORY / "locks/sysroot-el8-x86_64.json",
            REPOSITORY / "locks/sysroot-el8-aarch64.json",
            REPOSITORY / "locks/host-build-common-el8-x86_64.json",
            REPOSITORY / "locks/host-gcc-build-el8-x86_64.json",
            REPOSITORY / "locks/host-gcc-test-el8-x86_64.json",
            REPOSITORY / "locks/host-python-build-el8-x86_64.json",
            REPOSITORY / "locks/host-runtime-el8-x86_64.json",
        ]

    def test_current_plans_are_strict_and_semantically_valid(self):
        for path in self.plans:
            VALIDATOR["validate_document"](VALIDATOR["load_json"](path))

    def test_current_transactions_encode_valid_dnf_algebra(self):
        for path in self.transactions:
            VALIDATOR["validate_document"](VALIDATOR["load_json"](path))

    def test_current_locks_are_bound_to_release_and_signed_metadata(self):
        release = REPOSITORY / "config/release.json"
        for path in self.locks:
            lock = VALIDATOR["load_json"](path)
            VALIDATOR["validate_release_binding"](lock, path, release)

    def test_sysroot_user_set_exactly_matches_roots(self):
        for path in self.transactions[:2]:
            transaction = VALIDATOR["load_json"](path)
            root_nevras = {
                request["resolved_nevra"] for request in transaction["requests"]
            }
            user_nevras = {
                item["nevra"]
                for item in transaction["items"]
                if item["action"] == "install" and item["reason"] == "user"
            }
            self.assertEqual(root_nevras, user_nevras)

    def test_sysroot_package_closures_have_matching_name_and_evr(self):
        transactions = [
            VALIDATOR["load_json"](path) for path in self.transactions[:2]
        ]
        identities = [
            {
                (item["name"], item["epoch"], item["version"], item["release"])
                for item in transaction["items"]
            }
            for transaction in transactions
        ]
        self.assertEqual(identities[0], identities[1])
        self.assertEqual(len(identities[0]), 78)

    def test_host_common_and_gcc_delta_are_distinct(self):
        common = VALIDATOR["load_json"](self.transactions[2])
        gcc = VALIDATOR["load_json"](self.transactions[3])
        common_names = {
            item["name"] for item in common["items"] if item["action"] != "remove"
        }
        gcc_names = {
            item["name"] for item in gcc["items"] if item["action"] != "remove"
        }
        self.assertNotIn("libzstd-devel", common_names)
        self.assertEqual(gcc_names, {"bison", "flex", "libzstd-devel", "m4"})

    def test_python_build_delta_has_only_declared_development_roots(self):
        python = VALIDATOR["load_json"](self.transactions[5])
        self.assertEqual(
            {request["name"] for request in python["requests"]},
            {
                "bzip2-devel",
                "libffi-devel",
                "libuuid-devel",
                "openssl-devel",
                "sqlite-devel",
                "xz-devel",
            },
        )
        self.assertNotIn(
            "libzstd-devel",
            {item["name"] for item in python["items"]},
        )

    def test_gcc_test_delta_is_exact_and_test_only(self):
        plan = VALIDATOR["load_json"](self.plans[4])
        transaction = VALIDATOR["load_json"](self.transactions[4])
        lock = VALIDATOR["load_json"](self.locks[4])
        self.assertEqual(
            [repository["id"] for repository in plan["repositories"]],
            ["baseos", "appstream", "powertools"],
        )
        self.assertEqual(
            {request["name"] for request in transaction["requests"]},
            {"dejagnu", "expect"},
        )
        self.assertEqual(
            {
                item["name"]: item["repo_id"]
                for item in transaction["items"]
            },
            {"dejagnu": "powertools", "expect": "baseos"},
        )
        self.assertEqual(len(lock["packages"]), 2)
        self.assertEqual(transaction["manifests"]["remove"]["packages"], [])

    def test_locked_gcc_test_contract_rejects_origin_and_purpose_tampering(self):
        transaction = VALIDATOR["load_json"](self.transactions[4])
        wrong_origin = copy.deepcopy(transaction)
        wrong_origin["items"][0]["repo_id"] = "baseos"
        wrong_purpose = copy.deepcopy(transaction)
        wrong_purpose["requests"][0]["purpose"] = "host-runtime"
        wrong_parent = copy.deepcopy(transaction)
        wrong_parent["base"]["parent_sha256"] = "0" * 64
        for candidate in (wrong_origin, wrong_purpose, wrong_parent):
            with self.assertRaises(VALIDATOR["ValidationError"]):
                VALIDATOR["validate_locked_transaction_semantics"](candidate)

    def test_host_runtime_is_an_independent_user_tool_closure(self):
        plan = VALIDATOR["load_json"](self.plans[-1])
        RESOLVER["validate_plan_semantics"](plan)
        self.assertEqual(plan["identity"]["role"], "host-runtime")
        self.assertEqual(
            plan["base"],
            {"mode": "image", "parent_lock": None, "parent_sha256": None},
        )
        self.assertEqual(
            [repository["id"] for repository in plan["repositories"]],
            ["baseos", "appstream", "powertools"],
        )
        roots = {item["name"] for item in plan["roots"]}
        self.assertEqual(roots, VALIDATOR["HOST_RUNTIME_ROOTS"])
        self.assertTrue(
            {
                "cmake",
                "meson",
                "ninja-build",
                "git-core",
                "gcc-toolset-15-gcc-c++",
                "perl-IPC-Cmd",
                "perl-Time-Piece",
            }.issubset(roots)
        )
        self.assertFalse(
            {
                "rpm-build",
                "redhat-rpm-config",
                "scl-utils-build",
                "libzstd-devel",
                "openssl-devel",
            }.intersection(roots)
        )

        for mutation in ("lock-base", "missing-powertools", "extra-root"):
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(plan)
                if mutation == "lock-base":
                    candidate["base"] = {
                        "mode": "lock",
                        "parent_lock": "locks/host-build-common-el8-x86_64.json",
                        "parent_sha256": "0" * 64,
                    }
                else:
                    if mutation == "missing-powertools":
                        candidate["repositories"].pop()
                    else:
                        extra = copy.deepcopy(candidate["roots"][0])
                        extra["name"] = "forged-root"
                        candidate["roots"].append(extra)
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    VALIDATOR["validate_document"](candidate)
                with self.assertRaises(RESOLVER["ResolutionError"]):
                    RESOLVER["validate_plan_semantics"](candidate)

    def test_host_runtime_transaction_is_clean_and_origin_scoped(self):
        runtime = VALIDATOR["load_json"](self.transactions[-1])
        common = VALIDATOR["load_json"](self.transactions[2])
        lock = VALIDATOR["load_json"](self.locks[-1])
        self.assertEqual(len(runtime["requests"]), 43)
        self.assertEqual(len(runtime["items"]), 173)
        self.assertEqual(len(lock["packages"]), 152)
        self.assertEqual(
            len(runtime["manifests"]["result"]["packages"]), 279
        )
        self.assertEqual(
            runtime["manifests"]["base"], common["manifests"]["base"]
        )
        self.assertNotEqual(
            runtime["manifests"]["base"], common["manifests"]["result"]
        )
        self.assertEqual(
            {
                item["name"]
                for item in runtime["items"]
                if item["action"] != "remove"
                and item["repo_id"] == "powertools"
            },
            {"meson", "ninja-build"},
        )
        result_names = {
            VALIDATOR["nevra_name_arch"](nevra)[0]
            for nevra in runtime["manifests"]["result"]["packages"]
        }
        self.assertFalse(
            result_names.intersection(VALIDATOR["HOST_RUNTIME_FORBIDDEN"])
        )
        self.assertTrue(
            {
                "bzip2-libs",
                "libffi",
                "libuuid",
                "openssl-libs",
                "sqlite-libs",
                "xz-libs",
                "zlib",
            }.issubset(result_names)
        )

    def test_locked_host_runtime_policy_is_plan_independent_and_fail_closed(self):
        runtime = VALIDATOR["load_json"](self.transactions[-1])
        VALIDATOR["validate_locked_transaction_semantics"](runtime)

        wrong_origin = copy.deepcopy(runtime)
        next(
            item
            for item in wrong_origin["items"]
            if item["repo_id"] == "powertools"
        )["repo_id"] = "appstream"

        wrong_root = copy.deepcopy(runtime)
        wrong_root["requests"][0]["name"] = "forged-root"

        lock_base = copy.deepcopy(runtime)
        lock_base["base"] = {
            "mode": "lock",
            "parent_lock": "locks/host-build-common-el8-x86_64.json",
            "parent_sha256": "0" * 64,
        }

        weak_dependencies = copy.deepcopy(runtime)
        weak_dependencies["solver_policy"]["install_weak_deps"] = True

        forbidden = copy.deepcopy(runtime)
        fake = "rpm-build-0:4.14.3-31.el8.x86_64"
        for name in ("base", "result"):
            manifest = forbidden["manifests"][name]
            manifest["packages"].append(fake)
            manifest["packages"].sort()
            manifest["canonical_sha256"] = VALIDATOR["canonical_sha256"](
                manifest["packages"]
            )

        extra_devel = copy.deepcopy(runtime)
        fake = "forged-devel-0:1-1.el8.x86_64"
        for name in ("base", "result"):
            manifest = extra_devel["manifests"][name]
            manifest["packages"].append(fake)
            manifest["packages"].sort()
            manifest["canonical_sha256"] = VALIDATOR["canonical_sha256"](
                manifest["packages"]
            )

        for candidate in (
            wrong_origin,
            wrong_root,
            lock_base,
            weak_dependencies,
            forbidden,
            extra_devel,
        ):
            with self.subTest(candidate=candidate["manifests"]["result"]):
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    VALIDATOR["validate_locked_transaction_semantics"](
                        candidate
                    )

    def test_unknown_plan_field_is_rejected(self):
        plan = VALIDATOR["load_json"](self.plans[0])
        plan["unexpected"] = True
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](plan)

    def test_wrong_sysroot_root_arch_is_rejected(self):
        plan = VALIDATOR["load_json"](self.plans[0])
        plan["roots"][0]["arch"] = "any"
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](plan)

    def test_sysroot_identity_and_repository_are_role_bound(self):
        for field, value in (
            ("name", "sysroot-el8-forged"),
            ("baseurl", "https://example.invalid/rocky/"),
        ):
            plan = VALIDATOR["load_json"](self.plans[0])
            if field == "name":
                plan["identity"][field] = value
            else:
                plan["repositories"][0][field] = value
            with self.subTest(field=field):
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    VALIDATOR["validate_document"](plan)

    def test_duplicate_transaction_nevra_is_rejected(self):
        transaction = VALIDATOR["load_json"](self.transactions[0])
        transaction["items"].append(copy.deepcopy(transaction["items"][0]))
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](transaction)

    def test_weak_dependency_reason_is_rejected(self):
        transaction = VALIDATOR["load_json"](self.transactions[0])
        dependency = next(
            item for item in transaction["items"] if item["reason"] == "dependency"
        )
        dependency["reason"] = "weak-dependency"
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](transaction)

    def test_unsupported_transaction_action_is_rejected(self):
        transaction = VALIDATOR["load_json"](self.transactions[0])
        transaction["items"][0]["action"] = "obsolete"
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](transaction)

    def test_package_url_must_match_repository_location(self):
        transaction = VALIDATOR["load_json"](self.transactions[0])
        forward = next(item for item in transaction["items"] if item["action"] == "install")
        forward["url"] = "https://example.invalid/forged.rpm"
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](transaction)

    def test_package_url_does_not_reinterpret_location_as_a_url(self):
        transaction = VALIDATOR["load_json"](self.transactions[0])
        forward = next(item for item in transaction["items"] if item["action"] == "install")
        repository = transaction["repositories"][0]
        forward["location"] = "https:forged.rpm"
        forward["url"] = repository["baseurl"] + "forged.rpm"
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](transaction)

    def test_repository_locations_reject_uri_and_control_syntax(self):
        for location in (".", "https:forged.rpm", "pkg.rpm?x", "pkg.rpm#x", "bad\0.rpm"):
            with self.subTest(location=location):
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    VALIDATOR["safe_posix_location"](location, "test location")

    def test_signed_repomd_must_match_transaction_metadata(self):
        transaction = VALIDATOR["load_json"](self.transactions[0])
        repository = copy.deepcopy(transaction["repositories"][0])
        metadata_root = (
            REPOSITORY
            / "locks/metadata"
            / transaction["identity"]["name"]
            / repository["id"]
        )
        repomd_path = VALIDATOR["checked_metadata_path"](
            metadata_root, repository["repomd"]["location"], REPOSITORY
        )
        VALIDATOR["validate_repomd_claim"](repository, repomd_path)
        repository["metadata"][0]["checksum"]["value"] = "0" * 64
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_repomd_claim"](repository, repomd_path)

    def test_repomd_signature_claim_must_match_release_trust(self):
        transaction = VALIDATOR["load_json"](self.transactions[0])
        repository = copy.deepcopy(transaction["repositories"][0])
        release = VALIDATOR["load_json"](REPOSITORY / "config/release.json")
        repository["repomd"]["signature"]["fingerprint"] = "0" * 40
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_repository_trust"](
                repository, release["trust"]["rocky_rpm_key"]
            )

    def test_metadata_path_rejects_escape_and_symlink_components(self):
        with tempfile.TemporaryDirectory() as temporary:
            anchor = Path(temporary)
            root = anchor / "metadata"
            root.mkdir()
            with self.assertRaises(VALIDATOR["ValidationError"]):
                VALIDATOR["checked_metadata_path"](
                    root, "../repomd.xml", anchor
                )
            target = anchor / "target"
            target.mkdir()
            (root / "linked").symlink_to(target, target_is_directory=True)
            with self.assertRaises(VALIDATOR["ValidationError"]):
                VALIDATOR["checked_metadata_path"](
                    root, "linked/repomd.xml", anchor
                )
            real_root = anchor / "real" / "name" / "repo"
            real_root.mkdir(parents=True)
            (anchor / "alias").symlink_to(anchor / "real", target_is_directory=True)
            with self.assertRaises(VALIDATOR["ValidationError"]):
                VALIDATOR["checked_metadata_path"](
                    anchor / "alias" / "name" / "repo",
                    "repodata/repomd.xml",
                    anchor,
                )

    def test_result_manifest_must_match_transaction_algebra(self):
        transaction = VALIDATOR["load_json"](self.transactions[0])
        transaction["manifests"]["result"]["packages"].pop()
        transaction["manifests"]["result"]["canonical_sha256"] = VALIDATOR[
            "canonical_sha256"
        ](transaction["manifests"]["result"]["packages"])
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](transaction)

    def test_root_cannot_resolve_to_another_package(self):
        transaction = VALIDATOR["load_json"](self.transactions[0])
        transaction["requests"][0]["resolved_nevra"] = transaction["requests"][1][
            "resolved_nevra"
        ]
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](transaction)

    def test_lock_payload_digest_must_match_repository_checksum(self):
        lock = VALIDATOR["load_json"](self.locks[0])
        lock["packages"][0]["received_sha256"] = "0" * 64
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](lock)

    def test_lock_header_must_match_transaction(self):
        lock = VALIDATOR["load_json"](self.locks[0])
        lock["packages"][0]["header"]["source_rpm"] = "wrong-1.src.rpm"
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](lock)

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["STRICT"]["reject_duplicate_keys"](
                [("same", 1), ("same", 2)]
            )


if __name__ == "__main__":
    unittest.main()
