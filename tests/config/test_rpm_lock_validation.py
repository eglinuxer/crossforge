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
            REPOSITORY / "config/rpm/host-qt-build-el8-x86_64.plan.json",
            REPOSITORY / "config/rpm/qt-target-el8-x86_64.plan.json",
            REPOSITORY / "config/rpm/qt-target-el8-aarch64.plan.json",
            REPOSITORY / "config/rpm/qt-runtime-el8-x86_64.plan.json",
            REPOSITORY / "config/rpm/qt-runtime-el8-aarch64.plan.json",
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
        cls.qt_transactions = [
            REPOSITORY / "locks/transactions/host-qt-build-el8-x86_64.json",
            REPOSITORY / "locks/transactions/qt-target-el8-x86_64.json",
            REPOSITORY / "locks/transactions/qt-target-el8-aarch64.json",
            REPOSITORY / "locks/transactions/qt-runtime-el8-x86_64.json",
            REPOSITORY / "locks/transactions/qt-runtime-el8-aarch64.json",
        ]
        cls.qt_locks = [
            REPOSITORY / "locks/host-qt-build-el8-x86_64.json",
            REPOSITORY / "locks/qt-target-el8-x86_64.json",
            REPOSITORY / "locks/qt-target-el8-aarch64.json",
            REPOSITORY / "locks/qt-runtime-el8-x86_64.json",
            REPOSITORY / "locks/qt-runtime-el8-aarch64.json",
        ]

    def test_current_plans_are_strict_and_semantically_valid(self):
        for path in self.plans:
            VALIDATOR["validate_document"](VALIDATOR["load_json"](path))

    def test_qt_plans_separate_host_tools_from_target_libraries(self):
        host, x86_64, aarch64 = [
            VALIDATOR["load_json"](REPOSITORY / path)
            for path in (
                "config/rpm/host-qt-build-el8-x86_64.plan.json",
                "config/rpm/qt-target-el8-x86_64.plan.json",
                "config/rpm/qt-target-el8-aarch64.plan.json",
            )
        ]
        self.assertEqual(
            host["solver_policy"]["enabled_modules"],
            VALIDATOR["QT_HOST_MODULES"],
        )
        self.assertEqual(
            {record["name"] for record in host["roots"]},
            VALIDATOR["HOST_QT_ROOTS"],
        )
        self.assertTrue(
            {"nodejs", "python38", "python3-html5lib", "gperf"}.issubset(
                VALIDATOR["HOST_QT_ROOTS"]
            )
        )
        for plan in (x86_64, aarch64):
            self.assertEqual(plan["identity"]["role"], "qt-target")
            self.assertEqual(plan["solver_policy"]["enabled_modules"], [])
            self.assertEqual(
                {record["name"] for record in plan["roots"]},
                VALIDATOR["QT_TARGET_ROOTS"],
            )
            self.assertIn("libatomic", VALIDATOR["QT_TARGET_ROOTS"])
            self.assertNotIn("libatomic", VALIDATOR["HOST_QT_ROOTS"])
            self.assertFalse(
                {"nodejs", "python38", "python3-html5lib", "gperf"}.intersection(
                    {record["name"] for record in plan["roots"]}
                )
            )
            self.assertEqual(
                plan["base"]["parent_lock"],
                "locks/sysroot-el8-%s.json" % plan["identity"]["arch"],
            )
        self.assertEqual(
            [record["name"] for record in x86_64["roots"]],
            [record["name"] for record in aarch64["roots"]],
        )

    def test_qt_plan_rejects_host_tools_in_a_target_or_unlocked_parent(self):
        target = VALIDATOR["load_json"](
            REPOSITORY / "config/rpm/qt-target-el8-x86_64.plan.json"
        )
        target["roots"].append(
            {"name": "nodejs", "arch": "target", "purpose": "qt-target"}
        )
        target["base"]["parent_sha256"] = "0" * 64
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_document"](target)

    def test_qt_target_resolver_requires_an_explicit_non_root_parent(self):
        target = VALIDATOR["load_json"](
            REPOSITORY / "config/rpm/qt-target-el8-x86_64.plan.json"
        )
        with self.assertRaises(RESOLVER["ResolutionError"]):
            RESOLVER["validated_parent_root"](target, None)
        with self.assertRaises(RESOLVER["ResolutionError"]):
            RESOLVER["validated_parent_root"](target, Path("/"))
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                RESOLVER["validated_parent_root"](target, Path(temporary)),
                Path(temporary).resolve(),
            )
            host = VALIDATOR["load_json"](
                REPOSITORY / "config/rpm/host-qt-build-el8-x86_64.plan.json"
            )
            with self.assertRaises(RESOLVER["ResolutionError"]):
                RESOLVER["validated_parent_root"](host, Path(temporary))

    def test_qt_runtime_plans_are_target_only_and_development_free(self):
        plans = [
            VALIDATOR["load_json"](
                REPOSITORY / ("config/rpm/qt-runtime-el8-%s.plan.json" % arch)
            )
            for arch in ("x86_64", "aarch64")
        ]
        for plan in plans:
            self.assertEqual(plan["identity"]["role"], "qt-runtime")
            self.assertEqual(plan["base"]["mode"], "empty")
            self.assertEqual(plan["solver_policy"]["enabled_modules"], [])
            self.assertEqual(
                {root["name"] for root in plan["roots"]},
                VALIDATOR["QT_RUNTIME_ROOTS"],
            )
            self.assertTrue(
                all(
                    root["arch"] == "target"
                    and root["purpose"] == "qt-runtime"
                    and not root["name"].endswith(
                        ("-devel", "-headers", "-static")
                    )
                    for root in plan["roots"]
                )
            )
        self.assertEqual(
            [root["name"] for root in plans[0]["roots"]],
            [root["name"] for root in plans[1]["roots"]],
        )

    def test_qt_runtime_resolvers_start_from_empty_target_roots(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        bake = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        for arch in ("x86_64", "aarch64"):
            stage = dockerfile.split(
                " AS rpm-resolve-qt-runtime-%s" % arch, 1
            )[1].split("\nFROM ", 1)[0]
            self.assertIn(
                "--plan ./config/rpm/qt-runtime-el8-%s.plan.json" % arch,
                stage,
            )
            self.assertNotIn("--parent-root", stage)
            self.assertIn("RUN --network=none", stage)
            self.assertIn(
                'target "rpm-lock-qt-runtime-%s"' % arch,
                bake,
            )

    def test_qt_target_resolver_stages_bind_each_materialized_sysroot(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        for arch in ("x86_64", "aarch64"):
            stage = dockerfile.split(
                " AS rpm-resolve-qt-target-%s" % arch, 1
            )[1].split("\nFROM ", 1)[0]
            root = "/opt/crossforge/sysroots/el8/%s" % arch
            self.assertIn("--from=sysroot-%s" % arch, stage)
            self.assertIn("--parent-root " + root, stage)
            self.assertIn(
                "--plan ./config/rpm/qt-target-el8-%s.plan.json" % arch,
                stage,
            )

    def test_qt_locked_stages_install_without_repository_access(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        host = dockerfile.split(" AS host-qt-build-locked", 1)[1].split(
            "\nFROM ", 1
        )[0]
        self.assertIn("RUN --network=none", host)
        self.assertIn("host-qt-build-rpms", host)
        for arch in ("x86_64", "aarch64"):
            target = dockerfile.split(
                " AS qt-target-%s-locked" % arch, 1
            )[1].split("\nFROM ", 1)[0]
            self.assertIn("RUN --network=none", target)
            self.assertIn("install-overlay", target)
            self.assertIn("--from=qt-target-rpms-%s" % arch, target)
            self.assertIn("/qt-rpm-bundle/", target)
            self.assertIn("--bundle /qt-rpm-bundle", target)

    def test_current_transactions_encode_valid_dnf_algebra(self):
        for path in self.transactions + self.qt_transactions:
            VALIDATOR["validate_document"](VALIDATOR["load_json"](path))

    def test_current_locks_are_bound_to_release_and_signed_metadata(self):
        release = REPOSITORY / "config/release.json"
        for path in self.locks + self.qt_locks:
            lock = VALIDATOR["load_json"](path)
            VALIDATOR["validate_release_binding"](lock, path, release)

    def test_locked_qt_closures_have_reviewed_architecture_difference(self):
        host, x86_64, aarch64, runtime_x86_64, runtime_aarch64 = [
            VALIDATOR["load_json"](path) for path in self.qt_transactions
        ]
        self.assertEqual(len(host["items"]), 204)
        self.assertEqual(len(x86_64["items"]), 205)
        self.assertEqual(len(aarch64["items"]), 202)
        pair = runpy.run_path(
            str(REPOSITORY / "scripts/validate-qt-qualification.py")
        )["validate_target_pair"]([x86_64, aarch64])
        self.assertEqual(pair["x86_64_packages"], 205)
        self.assertEqual(pair["aarch64_packages"], 202)
        runtime_pair = runpy.run_path(
            str(REPOSITORY / "scripts/validate-qt-qualification.py")
        )["validate_runtime_pair"]([runtime_x86_64, runtime_aarch64])
        self.assertEqual(
            runtime_pair,
            {
                "x86_64_packages": 134,
                "aarch64_packages": 132,
                "x86_64_only": ["hwdata", "libpciaccess"],
            },
        )

    def test_locked_qt_runtime_closures_exclude_development_packages(self):
        for path in self.qt_transactions[-2:]:
            transaction = VALIDATOR["load_json"](path)
            VALIDATOR["validate_locked_qt_runtime_contract"](transaction)
            names = {item["name"] for item in transaction["items"]}
            self.assertFalse(
                {
                    name
                    for name in names
                    if name.endswith(("-devel", "-headers", "-static"))
                }
            )

    def test_locked_qt_target_rejects_power_tools_origin_drift(self):
        transaction = VALIDATOR["load_json"](self.qt_transactions[1])
        item = next(
            item for item in transaction["items"] if item["repo_id"] == "powertools"
        )
        item["repo_id"] = "appstream"
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_locked_qt_target_contract"](transaction)

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
            {"dejagnu", "expect", "gcc"},
        )
        self.assertEqual(
            {
                (item["name"], item["action"], item["reason"], item["repo_id"])
                for item in transaction["items"]
            },
            {
                ("annobin", "install", "dependency", "appstream"),
                ("cpp", "install", "dependency", "appstream"),
                ("dejagnu", "install", "user", "powertools"),
                ("expect", "install", "user", "baseos"),
                ("gcc", "install", "user", "appstream"),
                ("gcc-plugin-annobin", "install", "dependency", "appstream"),
                ("isl", "install", "dependency", "appstream"),
                ("libgcc", "remove", "unknown", "@System"),
                ("libgcc", "upgrade", "unknown", "baseos"),
            },
        )
        self.assertEqual(len(lock["packages"]), 8)
        self.assertEqual(
            transaction["manifests"]["remove"]["packages"],
            ["libgcc-0:8.5.0-22.el8_10.x86_64"],
        )

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
        plan = VALIDATOR["load_json"](self.plans[6])
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
