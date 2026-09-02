import ast
import copy
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
RENDERER_PATH = REPOSITORY / "scripts/render-release-components.py"
RENDERER = runpy.run_path(str(RENDERER_PATH))
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))

PHASE8_PYTHON_BUILD_DIGESTS = {
    "implementation/python-cp311-build-policy": "7d4d34401dab8c74b4d1f87e3704f0f66a4a199d317ef82e5a3e29f340e5f0b1",
    "python/cp311-source": "b0484e934292f1239bae108756f41293f82e671603fa0aa5a1fef7b30ba012ce",
    "python/cp311-native-build": "d329b20de620e9d6be4d82dd847a41c2ce9a114a5754c60122af573c8e19106b",
    "python/cp311-x86_64-build": "90cb4a2b26ad4624c6a5710a35b28d3c47d5d9823e1d75caa530e8c71f0b1774",
    "python/cp311-aarch64-build": "6008a45ee300380d6c2a2da86a1297f2e0b03b04c54aabacad48b5c4d5400973",
    "implementation/python-cp312-build-policy": "c40ff777eff73d7e6bb3a3bf3f110cbed8e5cc287e3830ebe13b6f02c2fc7f23",
    "python/cp312-source": "22cef15279ae8c045f80d22ca5d7c96fb34e5313a4e51607f0261fd2c45fe651",
    "python/cp312-native-build": "4152d187113fd903a542f2758c3adfca7079feb3c229b83b7c31d48d8e31e15e",
    "python/cp312-x86_64-build": "bcbd28b151216ee2355cf76cf42cae7fd5c26c384419447a530b51757c5c1481",
    "python/cp312-aarch64-build": "2930c213e853c924bce2e112526142401fc96cbc69219a7a4c77c658e258156d",
    "implementation/python-cp313-build-policy": "5f42fcd5d6d207a68cf958a58673d31023cc54c36275269698db5a72bccda576",
    "python/cp313-source": "645286459f503ee163911bbeb6d6609c35885108021a1b442eb0e88ad3767652",
    "python/cp313-native-build": "a428511db9354cf7798ee6c934863cc359d1837b9dd8f90bc3bab34177ab57c0",
    "python/cp313-x86_64-build": "ab87916b414cd783300275c033b227496ac08e75e6ec55d1e91167db353f6a1b",
    "python/cp313-aarch64-build": "05889c0e1d523c7e406581871c10358ae786455514c599cc0a46fbcda81759a1",
    "implementation/python-cp314-build-policy": "8585593866ed19ab7203b188b6d91b6a4ad0d2257e4e2a502d352f4fd2f59906",
    "python/cp314-source": "b9d0c494516360089e8e61d4fb809d75c9d233d8c780f1a838c46489aea9034f",
    "python/cp314-native-build": "ffdb1374ce4ab354e80dde8df21a15eda0875f6c0fb4fe2b10f8058986dc8202",
    "python/cp314-x86_64-build": "67a6e878e8fc9e592765ce0318a709b2686683b132900431ace217b774c712ae",
    "python/cp314-aarch64-build": "f28bfec58ec2bddafe6dcace24e970db9003135fb59a2eb3c784a30dd639683d",
}
PHASE8_PYTHON_QUALIFICATION_DIGESTS = {
    "implementation/python-qualification-policy": "5ae0b9f1e9e99c0001bc1285cbbcd529cd7f77a10f574d2e3b6ffc718f2e3f73",
    "python/qualification": "5de116bc3feef260b2ec3532da145050b3cc820d46760bd606235674d4ac4985",
}


def component_documents(documents):
    return {
        document["component"]: document
        for document in documents.values()
        if document.get("kind") == "crossforge-release-component"
    }


def changed(before, after):
    return {
        name
        for name in set(before) & set(after)
        if RENDERER["canonical_sha256"](before[name])
        != RENDERER["canonical_sha256"](after[name])
    }


class ReleaseComponentProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = VALIDATOR["load_json"](REPOSITORY / "config/release.json")
        cls.documents = RENDERER["render_documents"](REPOSITORY, cls.release)
        cls.components = component_documents(cls.documents)
        cls.component_schema = VALIDATOR["load_json"](
            REPOSITORY / "config/schemas/release-component.schema.json"
        )
        cls.binding_schema = VALIDATOR["load_json"](
            REPOSITORY / "config/schemas/release-binding.schema.json"
        )
        cls.rows = tuple(RENDERER["IMPLEMENTED_ROWS"])
        cls.row_names = tuple(row["row"] for row in cls.rows)

    def render_mutation(self, mutate, rows=None):
        release = copy.deepcopy(self.release)
        mutate(release)
        return RENDERER["render_component_documents"](
            release, self.rows if rows is None else rows
        )

    def test_tracked_projection_set_and_binding_are_current(self):
        self.assertEqual(RENDERER["output_drift"](REPOSITORY, self.documents), [])
        binding = self.documents[RENDERER["BINDING_PATH"]]
        RENDERER["validate_binding"](
            self.release, self.components, binding, self.rows
        )
        records = {record["component"]: record for record in binding["components"]}
        self.assertEqual(set(records), set(self.components))
        self.assertEqual(
            binding["release"]["canonical_sha256"],
            RENDERER["canonical_sha256"](self.release),
        )

    def test_every_file_has_strict_schema_and_exact_envelope(self):
        for schema in (self.component_schema, self.binding_schema):
            VALIDATOR["validate_schema_subset"](schema)
        for name, document in self.components.items():
            with self.subTest(component=name):
                self.assertEqual(set(document), RENDERER["COMPONENT_KEYS"])
                VALIDATOR["validate"](
                    document, self.component_schema, self.component_schema, "$"
                )
        VALIDATOR["validate"](
            self.documents[RENDERER["BINDING_PATH"]],
            self.binding_schema,
            self.binding_schema,
            "$",
        )
        candidate = copy.deepcopy(self.components["sources/gcc"])
        candidate["extra"] = True
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate"](
                candidate, self.component_schema, self.component_schema, "$"
            )

    def test_schema_rejects_unsafe_component_material_and_binding_paths(self):
        for value in ("a//b", "a/", "a/../b", "../a"):
            with self.subTest(component=value):
                candidate = copy.deepcopy(self.components["sources/gcc"])
                candidate["component"] = value
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    VALIDATOR["validate"](
                        candidate, self.component_schema, self.component_schema, "$"
                    )
        for value in ("/a//b", "/a/", "/a/../b", "/.."):
            with self.subTest(material=value):
                candidate = copy.deepcopy(self.components["sources/gcc"])
                candidate["materials"][0]["path"] = value
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    VALIDATOR["validate"](
                        candidate, self.component_schema, self.component_schema, "$"
                    )
        for value in (
            "config/generated/components/a//b.json",
            "config/generated/components/a/../b.json",
            "config/generated/components/a/.json",
        ):
            with self.subTest(binding=value):
                candidate = copy.deepcopy(
                    self.documents[RENDERER["BINDING_PATH"]]
                )
                candidate["components"][0]["path"] = value
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    VALIDATOR["validate"](
                        candidate, self.binding_schema, self.binding_schema, "$"
                    )

    def test_policy_registry_exactly_covers_record_fields(self):
        self.assertEqual(
            set(RENDERER["POLICY_FIELD_SCOPES"]),
            set(RENDERER["RECORD_FIELDS"]),
        )
        self.assertEqual(
            RENDERER["QUALIFICATION_POLICY_FIELDS"],
            tuple(RENDERER["RECORD_FIELDS"]),
        )
        self.assertNotIn("zstd", RENDERER["BUILD_POLICY_FIELDS"])
        self.assertIn("zstd", RENDERER["QUALIFICATION_POLICY_FIELDS"])
        self.assertNotIn("hash_algorithm", RENDERER["BUILD_POLICY_FIELDS"])
        self.assertIn(
            "hash_algorithm", RENDERER["QUALIFICATION_POLICY_FIELDS"]
        )
        for row in self.row_names:
            materials = self.components[
                "implementation/python-%s-build-policy" % row
            ]["materials"]
            self.assertFalse(
                any(material["path"].endswith("/zstd") for material in materials)
            )
            self.assertFalse(
                any(
                    material["path"].endswith("/hash_algorithm")
                    for material in materials
                )
            )
        malformed = [copy.deepcopy(row) for row in self.rows]
        malformed[0]["extra"] = True
        with self.assertRaisesRegex(RENDERER["ProjectionError"], "RECORD_FIELDS"):
            RENDERER["render_component_documents"](self.release, tuple(malformed))
        malformed = [copy.deepcopy(row) for row in self.rows]
        malformed[0]["zstd"] = 0
        with self.assertRaisesRegex(RENDERER["ProjectionError"], "zstd policy"):
            RENDERER["render_component_documents"](self.release, tuple(malformed))

    def test_policy_and_python_dependency_matrix_is_exact(self):
        expected_qualification = {
            "implementation/python-qualification-policy",
            "toolchain/x86_64-qualification",
            "toolchain/aarch64-qualification",
        }
        for record in self.rows:
            row = record["row"]
            policy = "implementation/python-%s-build-policy" % row
            source = "python/%s-source" % row
            native = "python/%s-native-build" % row
            self.assertEqual(
                {item["component"] for item in self.components[source]["dependencies"]},
                {policy},
            )
            expected_native = {
                "rpm/host-build-common",
                "rpm/host-python-build",
                source,
            }
            if record["zstd"]:
                expected_native.add("zstd/host-build")
            self.assertEqual(
                {item["component"] for item in self.components[native]["dependencies"]},
                expected_native,
            )
            for arch in ("x86_64", "aarch64"):
                target = "python/%s-%s-build" % (row, arch)
                expected_target = {native, "toolchain/%s-build" % arch}
                if record["zstd"]:
                    expected_target.add("zstd/%s-build" % arch)
                self.assertEqual(
                    {
                        item["component"]
                        for item in self.components[target]["dependencies"]
                    },
                    expected_target,
                )
                expected_qualification.add(target)
        self.assertEqual(
            {
                item["component"]
                for item in self.components["python/qualification"]["dependencies"]
            },
            expected_qualification,
        )

    def test_python_qualification_component_identity_api_is_exact(self):
        expected = {
            "policy": {
                "component": "implementation/python-qualification-policy",
                "canonical_sha256": RENDERER["canonical_sha256"](
                    self.components[
                        "implementation/python-qualification-policy"
                    ]
                ),
            },
            "aggregate": {
                "component": "python/qualification",
                "canonical_sha256": RENDERER["canonical_sha256"](
                    self.components["python/qualification"]
                ),
            },
        }
        observed = RENDERER["python_qualification_components"](self.release)
        self.assertEqual(observed, expected)
        self.assertEqual(
            RENDERER["python_qualification_components"](
                self.release, self.rows
            ),
            expected,
        )
        self.assertEqual(
            RENDERER["validate_python_qualification_components"](
                copy.deepcopy(observed), self.release
            ),
            expected,
        )
        self.assertEqual(
            RENDERER["bind_python_qualification_components"](
                self.release,
                expected["policy"]["canonical_sha256"],
                expected["aggregate"]["canonical_sha256"],
            ),
            expected,
        )

        malformed = []
        extra_role = copy.deepcopy(expected)
        extra_role["extra"] = copy.deepcopy(expected["policy"])
        malformed.append(extra_role)
        wrong_name = copy.deepcopy(expected)
        wrong_name["policy"]["component"] = "python/qualification"
        malformed.append(wrong_name)
        wrong_digest = copy.deepcopy(expected)
        wrong_digest["aggregate"]["canonical_sha256"] = "0" * 64
        malformed.append(wrong_digest)
        extra_field = copy.deepcopy(expected)
        extra_field["policy"]["scope"] = "qualification"
        malformed.append(extra_field)
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(RENDERER["ProjectionError"]):
                    RENDERER[
                        "validate_python_qualification_components"
                    ](value, self.release, self.rows)

        phase8 = RENDERER["python_qualification_components"](
            self.release, self.rows[:-1]
        )
        phase8_documents = RENDERER["render_component_documents"](
            self.release, self.rows[:-1]
        )
        self.assertEqual(
            phase8,
            {
                "policy": {
                    "component": "implementation/python-qualification-policy",
                    "canonical_sha256": RENDERER["canonical_sha256"](
                        phase8_documents[
                            "implementation/python-qualification-policy"
                        ]
                    ),
                },
                "aggregate": {
                    "component": "python/qualification",
                    "canonical_sha256": RENDERER["canonical_sha256"](
                        phase8_documents["python/qualification"]
                    ),
                },
            },
        )
        self.assertNotEqual(phase8, expected)

    def test_every_release_leaf_has_its_classified_scope_owner(self):
        classifications = RENDERER["classify_release_leaves"](
            self.release, self.rows
        )
        owners = RENDERER["validate_component_set"](
            self.release, self.components, self.rows
        )
        self.assertEqual(set(classifications), set(owners))
        for path, scope in classifications.items():
            with self.subTest(path=path):
                self.assertTrue(
                    any(
                        self.components[owner]["scope"] == scope
                        for owner in owners[path]
                    )
                )

    def test_new_release_leaf_fails_closed_even_before_schema_validation(self):
        release = copy.deepcopy(self.release)
        release["new_contract"] = {"value": True}
        with self.assertRaisesRegex(
            RENDERER["ProjectionError"], "no explicit semantic classification"
        ):
            RENDERER["render_component_documents"](release, self.rows)

    def test_required_dependency_deletion_is_rejected(self):
        components = copy.deepcopy(self.components)
        report = components["python/qualification"]
        report["dependencies"] = [
            item
            for item in report["dependencies"]
            if item["component"] != "python/cp312-aarch64-build"
        ]
        with self.assertRaises(RENDERER["ProjectionError"]):
            RENDERER["validate_component_set"](self.release, components, self.rows)

    def test_component_set_scope_and_dependency_allow_matrix_fail_closed(self):
        missing = copy.deepcopy(self.components)
        del missing["python/cp312-source"]
        extra = copy.deepcopy(self.components)
        extra["attack/extra"] = copy.deepcopy(extra["sources/gcc"])
        extra["attack/extra"]["component"] = "attack/extra"
        wrong_scope = copy.deepcopy(self.components)
        wrong_scope["python/cp312-source"]["scope"] = "future"
        extra_edge = copy.deepcopy(self.components)
        extra_edge["sources/binutils"]["dependencies"].append(
            {
                "component": "sources/gcc",
                "canonical_sha256": RENDERER["canonical_sha256"](
                    extra_edge["sources/gcc"]
                ),
            }
        )
        for candidate in (missing, extra, wrong_scope, extra_edge):
            with self.subTest(candidate=set(candidate)):
                with self.assertRaises(RENDERER["ProjectionError"]):
                    RENDERER["validate_component_set"](
                        self.release, candidate, self.rows
                    )

    def test_policy_forgery_is_rejected_even_with_coordinated_digest(self):
        components = copy.deepcopy(self.components)
        policy = components["implementation/python-qualification-policy"]
        for material in policy["materials"]:
            if material["path"] == "/@implementation/python_rows/cp313/gil_policy":
                material["value"] = "absent"
        forged = RENDERER["canonical_sha256"](policy)
        for dependency in components["python/qualification"]["dependencies"]:
            if dependency["component"] == policy["component"]:
                dependency["canonical_sha256"] = forged
        with self.assertRaises(RENDERER["ProjectionError"]):
            RENDERER["validate_component_set"](self.release, components, self.rows)

    def test_binding_rejects_coordinated_component_and_digest_tampering(self):
        components = copy.deepcopy(self.components)
        source = components["sources/gcc"]
        source["materials"][0]["value"] = "forged"
        binding = copy.deepcopy(self.documents[RENDERER["BINDING_PATH"]])
        for record in binding["components"]:
            if record["component"] == "sources/gcc":
                record["canonical_sha256"] = RENDERER["canonical_sha256"](source)
        with self.assertRaises(RENDERER["ProjectionError"]):
            RENDERER["validate_binding"](
                self.release, components, binding, self.rows
            )

    def test_duplicate_material_and_dependency_cycle_are_rejected(self):
        components = copy.deepcopy(self.components)
        document = components["toolchain/x86_64-build"]
        document["materials"].append(copy.deepcopy(document["materials"][0]))
        document["materials"].sort(key=lambda record: record["path"])
        with self.assertRaisesRegex(RENDERER["ProjectionError"], "repeat"):
            RENDERER["validate_component_set"](self.release, components, self.rows)
        components = copy.deepcopy(self.components)
        document = components["sources/gcc"]
        document["dependencies"].append(
            {
                "component": "sources/gcc",
                "canonical_sha256": RENDERER["canonical_sha256"](document),
            }
        )
        with self.assertRaisesRegex(RENDERER["ProjectionError"], "cycle"):
            RENDERER["validate_component_set"](self.release, components, self.rows)

    def test_material_reader_requires_complete_canonical_graph(self):
        values = RENDERER["material_index"](
            self.release, self.components, "sources/gcc", self.rows
        )
        self.assertEqual(
            values["/gts/source/sha256"], self.release["gts"]["source"]["sha256"]
        )
        forged = copy.deepcopy(self.components)
        forged["sources/gcc"]["materials"][0]["value"] = "forged"
        with self.assertRaises(RENDERER["ProjectionError"]):
            RENDERER["material_index"](
                self.release, forged, "sources/gcc", self.rows
            )

    def test_cp312_patch_has_exact_row_local_impact(self):
        after = self.render_mutation(
            lambda release: release["python"]["versions"][3]["patches"][0].__setitem__(
                "sha256", "0" * 64
            )
        )
        self.assertEqual(
            changed(self.components, after),
            {
                "python/cp312-source",
                "python/cp312-native-build",
                "python/cp312-x86_64-build",
                "python/cp312-aarch64-build",
                "python/qualification",
            },
        )

    def test_cp310_append_preserves_existing_build_component_identities(self):
        self.assertEqual(self.rows[-1]["row"], "cp310")
        phase8_rows = self.rows[:-1]
        before = RENDERER["render_component_documents"](
            self.release, phase8_rows
        )
        after = self.components

        protected = set()
        for row in ("cp311", "cp312", "cp313", "cp314"):
            protected.update(
                {
                    "implementation/python-%s-build-policy" % row,
                    "python/%s-source" % row,
                    "python/%s-native-build" % row,
                    "python/%s-x86_64-build" % row,
                    "python/%s-aarch64-build" % row,
                }
            )
        for component in sorted(protected):
            with self.subTest(component=component):
                self.assertEqual(
                    RENDERER["canonical_sha256"](before[component]),
                    RENDERER["canonical_sha256"](after[component]),
                )
                self.assertEqual(
                    RENDERER["canonical_sha256"](after[component]),
                    PHASE8_PYTHON_BUILD_DIGESTS[component],
                )
        self.assertEqual(protected, set(PHASE8_PYTHON_BUILD_DIGESTS))

        for component in (
            "implementation/python-qualification-policy",
            "python/qualification",
        ):
            with self.subTest(component=component):
                self.assertNotEqual(
                    RENDERER["canonical_sha256"](before[component]),
                    RENDERER["canonical_sha256"](after[component]),
                )
                self.assertNotEqual(
                    RENDERER["canonical_sha256"](after[component]),
                    PHASE8_PYTHON_QUALIFICATION_DIGESTS[component],
                )
        for component in (
            "toolchain/x86_64-qualification",
            "toolchain/aarch64-qualification",
        ):
            self.assertEqual(
                RENDERER["canonical_sha256"](before[component]),
                RENDERER["canonical_sha256"](after[component]),
            )
        self.assertIn("future/python-cp310", before)
        self.assertNotIn("future/python-cp310", after)
        self.assertIn("python/cp310-source", after)

    def test_support_and_sigstore_have_no_build_impact(self):
        release = copy.deepcopy(self.release)
        release["python"]["versions"][3]["support"] = "bugfix"
        support = RENDERER["render_component_documents"](release, self.rows)
        self.assertEqual(changed(self.components, support), {"python/qualification"})
        release = copy.deepcopy(self.release)
        release["python"]["versions"][3]["source"]["sigstore"][
            "bundle_sha256"
        ] = "0" * 64
        sigstore = RENDERER["render_component_documents"](release, self.rows)
        self.assertEqual(
            changed(self.components, sigstore),
            {"python/qualification", "supply/evidence"},
        )
        for documents in (support, sigstore):
            self.assertFalse(
                any(
                    self.components[name]["scope"] == "build"
                    for name in changed(self.components, documents)
                )
            )

    def test_aarch64_sysroot_has_exact_single_arch_impact(self):
        after = self.render_mutation(
            lambda release: release["targets"][1]["sysroot"].__setitem__(
                "canonical_sha256", "0" * 64
            )
        )
        expected = {
            "rpm/sysroot-aarch64",
            "toolchain/aarch64-build",
            "toolchain/aarch64-qualification",
            "python/qualification",
            "zstd/aarch64-build",
        }
        expected.update(
            "python/%s-aarch64-build" % row for row in self.row_names
        )
        self.assertEqual(changed(self.components, after), expected)

    def test_x86_64_sysroot_has_exact_single_arch_impact(self):
        after = self.render_mutation(
            lambda release: release["targets"][0]["sysroot"].__setitem__(
                "canonical_sha256", "0" * 64
            )
        )
        expected = {
            "rpm/sysroot-x86_64",
            "toolchain/x86_64-build",
            "toolchain/x86_64-qualification",
            "python/qualification",
            "zstd/x86_64-build",
        }
        expected.update(
            "python/%s-x86_64-build" % row for row in self.row_names
        )
        self.assertEqual(changed(self.components, after), expected)

    def test_qemu_cpu_changes_qualification_only(self):
        after = self.render_mutation(
            lambda release: release["qemu"]["executor"].__setitem__("cpu", "max")
        )
        self.assertEqual(
            changed(self.components, after),
            {"toolchain/aarch64-qualification", "python/qualification"},
        )

    def test_host_python_lock_has_exact_python_host_impact(self):
        after = self.render_mutation(
            lambda release: release["host_locks"]["host-python-build"].__setitem__(
                "canonical_sha256", "0" * 64
            )
        )
        expected = {"rpm/host-python-build", "python/qualification"}
        for row in self.row_names:
            expected.update(
                {
                    "python/%s-native-build" % row,
                    "python/%s-x86_64-build" % row,
                    "python/%s-aarch64-build" % row,
                }
            )
        self.assertEqual(changed(self.components, after), expected)

    def test_trust_changes_all_rpm_srpm_and_exact_consumers(self):
        after = self.render_mutation(
            lambda release: release["trust"]["rocky_rpm_key"].__setitem__(
                "sha256", "0" * 64
            )
        )
        expected = {
            name
            for name in self.components
            if name.startswith("rpm/")
            or (name.startswith("sources/") and name != "sources/zstd")
        }
        expected.update(
            {
                "toolchain/x86_64-build",
                "toolchain/aarch64-build",
                "toolchain/x86_64-qualification",
                "toolchain/aarch64-qualification",
                "python/qualification",
                "zstd/host-build",
                "zstd/x86_64-build",
                "zstd/aarch64-build",
            }
        )
        for row in self.row_names:
            expected.update(
                {
                    "python/%s-native-build" % row,
                    "python/%s-x86_64-build" % row,
                    "python/%s-aarch64-build" % row,
                }
            )
        self.assertEqual(changed(self.components, after), expected)

    def test_zstd_source_and_policy_are_isolated_from_python_rows(self):
        source = self.components["sources/zstd"]
        self.assertEqual(source["scope"], "build")
        self.assertEqual(source["dependencies"], [])
        self.assertTrue(
            all(
                material["path"].startswith("/python/zstd/")
                for material in source["materials"]
            )
        )
        policy = self.components["implementation/zstd-build-policy"]
        self.assertEqual(policy["scope"], "build")
        self.assertEqual(policy["dependencies"], [])
        self.assertEqual(
            {material["path"] for material in policy["materials"]},
            {
                "/@implementation/zstd/exclude_archive_symbols",
                "/@implementation/zstd/linkage",
                "/@implementation/zstd/multithread",
                "/@implementation/zstd/no_trace",
                "/@implementation/zstd/position_independent_code",
                "/@implementation/zstd/private",
                "/@implementation/zstd/selected_license",
                "/@implementation/zstd/visibility",
            },
        )
        expected_dependencies = {
            "zstd/host-build": {
                "rpm/host-build-common",
                "sources/zstd",
                "implementation/zstd-build-policy",
            },
            "zstd/x86_64-build": {
                "rpm/host-build-common",
                "toolchain/x86_64-build",
                "sources/zstd",
                "implementation/zstd-build-policy",
            },
            "zstd/aarch64-build": {
                "rpm/host-build-common",
                "toolchain/aarch64-build",
                "sources/zstd",
                "implementation/zstd-build-policy",
            },
        }
        for component, expected in expected_dependencies.items():
            self.assertEqual(
                {
                    dependency["component"]
                    for dependency in self.components[component]["dependencies"]
                },
                expected,
            )
        expected_python_edges = {
            "python/cp314-native-build": {"zstd/host-build"},
            "python/cp314-x86_64-build": {"zstd/x86_64-build"},
            "python/cp314-aarch64-build": {"zstd/aarch64-build"},
        }
        for component, document in self.components.items():
            if not component.startswith("python/cp"):
                continue
            actual = {
                dependency["component"]
                for dependency in document["dependencies"]
                if dependency["component"].startswith(("zstd/", "sources/zstd"))
            }
            self.assertEqual(actual, expected_python_edges.get(component, set()))

    def test_zstd_metadata_change_has_exact_build_impact(self):
        mutations = (
            lambda zstd: zstd["source"].__setitem__("sha256", "0" * 64),
            lambda zstd: zstd["source"]["signature"].__setitem__(
                "evidence", "evidence/gpg/changed.b64"
            ),
            lambda zstd: zstd["license"].__setitem__(
                "license_sha256", "0" * 64
            ),
        )
        for mutation in mutations:
            release = copy.deepcopy(self.release)
            mutation(release["python"]["zstd"])
            after = RENDERER["render_component_documents"](release, self.rows)
            self.assertEqual(
                changed(self.components, after),
                {
                    "sources/zstd",
                    "zstd/host-build",
                    "zstd/x86_64-build",
                    "zstd/aarch64-build",
                    "python/cp314-native-build",
                    "python/cp314-x86_64-build",
                    "python/cp314-aarch64-build",
                    "python/qualification",
                },
            )

    def test_future_python_change_does_not_pollute_current_matrix(self):
        after = self.render_mutation(
            lambda release: release["python"]["versions"][0]["source"].__setitem__(
                "sha256", "0" * 64
            )
        )
        self.assertEqual(changed(self.components, after), {"future/python-cp39"})

    def test_cp314_implementation_preserves_existing_row_build_digests(self):
        prior_rows = tuple(
            row for row in self.rows if row["introduced_phase"] <= 7
        )
        phase8_rows = tuple(
            row for row in self.rows if row["introduced_phase"] <= 8
        )
        before = RENDERER["render_component_documents"](
            self.release, prior_rows
        )
        after = RENDERER["render_component_documents"](
            self.release, phase8_rows
        )
        self.assertEqual(
            set(after) - set(before),
            {
                "implementation/python-cp314-build-policy",
                "python/cp314-source",
                "python/cp314-native-build",
                "python/cp314-x86_64-build",
                "python/cp314-aarch64-build",
            },
        )
        self.assertEqual(set(before) - set(after), {"future/python-cp314"})
        self.assertEqual(
            changed(before, after),
            {
                "implementation/python-qualification-policy",
                "python/qualification",
                "supply/evidence",
            },
        )
        for row in ("cp313", "cp311", "cp312"):
            for suffix in ("source", "native-build", "x86_64-build", "aarch64-build"):
                name = "python/%s-%s" % (row, suffix)
                self.assertEqual(
                    RENDERER["canonical_sha256"](before[name]),
                    RENDERER["canonical_sha256"](after[name]),
                )
            policy = "implementation/python-%s-build-policy" % row
            self.assertEqual(
                RENDERER["canonical_sha256"](before[policy]),
                RENDERER["canonical_sha256"](after[policy]),
            )

    def test_writer_rejects_escape_paths_before_any_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            original_key = next(
                key for key in self.documents if key != RENDERER["BINDING_PATH"]
            )
            for malicious in (Path("../escaped.json"), root / "absolute.json"):
                with self.subTest(path=str(malicious)):
                    documents = copy.deepcopy(self.documents)
                    document = documents.pop(original_key)
                    documents[malicious] = document
                    with self.assertRaises(RENDERER["ProjectionError"]):
                        RENDERER["write_documents"](
                            repository, self.release, documents, self.rows
                        )
                    self.assertEqual(list(repository.iterdir()), [])
                    self.assertFalse((root / "escaped.json").exists())
                    self.assertFalse((root / "absolute.json").exists())

    def test_writer_rejects_forged_binding_and_path_component_mismatch(self):
        mutations = []
        forged = copy.deepcopy(self.documents)
        forged[RENDERER["BINDING_PATH"]]["components"][0][
            "canonical_sha256"
        ] = "0" * 64
        mutations.append(forged)
        mismatched = copy.deepcopy(self.documents)
        original = next(
            key for key in mismatched if key != RENDERER["BINDING_PATH"]
        )
        mismatched[
            Path("config/generated/components/mismatched/component.json")
        ] = mismatched.pop(original)
        mutations.append(mismatched)
        for documents in mutations:
            with self.subTest(keys=len(documents)):
                with tempfile.TemporaryDirectory() as directory:
                    repository = Path(directory)
                    with self.assertRaises(RENDERER["ProjectionError"]):
                        RENDERER["write_documents"](
                            repository, self.release, documents, self.rows
                        )
                    self.assertEqual(list(repository.iterdir()), [])

    def test_writer_rejects_symlink_output_boundary_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            outside = root / "outside"
            repository.mkdir()
            outside.mkdir()
            generated = repository / "config/generated"
            generated.mkdir(parents=True)
            (generated / "components").symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaisesRegex(
                RENDERER["ProjectionError"], "symlink|outside"
            ):
                RENDERER["write_documents"](
                    repository, self.release, self.documents, self.rows
                )
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((generated / "release-binding.json").exists())

    def test_atomic_writer_skips_unchanged_and_commits_binding_last(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            first = RENDERER["write_documents"](
                repository, self.release, self.documents, self.rows
            )
            self.assertEqual(first[-1], RENDERER["BINDING_PATH"])
            binding = repository / RENDERER["BINDING_PATH"]
            before = binding.stat().st_mtime_ns
            self.assertEqual(
                RENDERER["write_documents"](
                    repository, self.release, self.documents, self.rows
                ),
                [],
            )
            self.assertEqual(binding.stat().st_mtime_ns, before)

    def test_atomic_failure_rolls_back_first_replace_and_keeps_old_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            RENDERER["write_documents"](
                repository, self.release, self.documents, self.rows
            )
            binding = repository / RENDERER["BINDING_PATH"]
            old_binding = binding.read_bytes()
            release = copy.deepcopy(self.release)
            release["python"]["versions"][3]["patches"][0]["sha256"] = "0" * 64
            changed_documents = RENDERER["render_documents"](REPOSITORY, release)
            writer = RENDERER["write_documents"]
            globals_map = writer.__globals__
            original = globals_map["_replace_staged"]
            calls = []
            first_replaced = []

            def fail_after_first_replace(temporary, path):
                calls.append((temporary, path))
                if len(calls) == 1:
                    before = path.read_bytes()
                    original(temporary, path)
                    first_replaced.append(path.read_bytes() != before)
                    return
                if len(calls) == 2:
                    raise OSError("injected component write failure")
                return original(temporary, path)

            globals_map["_replace_staged"] = fail_after_first_replace
            try:
                with self.assertRaises(OSError):
                    writer(repository, release, changed_documents, self.rows)
            finally:
                globals_map["_replace_staged"] = original
            self.assertEqual(first_replaced, [True])
            self.assertEqual(binding.read_bytes(), old_binding)
            self.assertEqual(
                RENDERER["output_drift"](repository, self.documents), []
            )
            written = writer(repository, release, changed_documents, self.rows)
            self.assertEqual(written[-1], RENDERER["BINDING_PATH"])
            self.assertEqual(
                RENDERER["output_drift"](repository, changed_documents), []
            )

    def test_successful_writer_cleans_stale_and_abandoned_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            RENDERER["write_documents"](
                repository, self.release, self.documents, self.rows
            )
            stale = repository / "config/generated/components/stale.json"
            stale.write_text("{}\n", encoding="utf-8")
            abandoned = repository / "config/generated/.abandoned.tmp"
            abandoned.write_text("partial", encoding="utf-8")
            RENDERER["write_documents"](
                repository, self.release, self.documents, self.rows
            )
            self.assertFalse(stale.exists())
            self.assertFalse(abandoned.exists())

    def test_binding_replace_failure_rolls_back_all_components(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            RENDERER["write_documents"](
                repository, self.release, self.documents, self.rows
            )
            release = copy.deepcopy(self.release)
            release["python"]["versions"][3]["patches"][0]["sha256"] = "0" * 64
            changed_documents = RENDERER["render_documents"](REPOSITORY, release)
            writer = RENDERER["write_documents"]
            globals_map = writer.__globals__
            original = globals_map["_replace_staged"]
            binding_path = repository / RENDERER["BINDING_PATH"]
            reached_binding = []

            def fail_binding(temporary, path):
                if path == binding_path:
                    reached_binding.append(True)
                    raise OSError("injected binding replace failure")
                return original(temporary, path)

            globals_map["_replace_staged"] = fail_binding
            try:
                with self.assertRaises(OSError):
                    writer(repository, release, changed_documents, self.rows)
            finally:
                globals_map["_replace_staged"] = original
            self.assertEqual(reached_binding, [True])
            self.assertEqual(
                RENDERER["output_drift"](repository, self.documents), []
            )

    def test_atomic_writer_uses_unique_same_directory_temporaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested/component.json"
            function = RENDERER["_atomic_write_if_changed"]
            module = function.__globals__["tempfile"]
            original = module.mkstemp
            temporaries = []

            def recording_mkstemp(*args, **kwargs):
                descriptor, name = original(*args, **kwargs)
                temporaries.append(Path(name))
                return descriptor, name

            module.mkstemp = recording_mkstemp
            try:
                self.assertTrue(function(path, "first\n"))
                self.assertTrue(function(path, "second\n"))
            finally:
                module.mkstemp = original
            self.assertEqual(len(temporaries), 2)
            self.assertNotEqual(temporaries[0], temporaries[1])
            self.assertTrue(all(item.parent == path.parent for item in temporaries))

    def test_platform_python_stage_runs_component_drift_check(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(encoding="utf-8")
        block = dockerfile.split("FROM config-validate AS platform-python-check", 1)[1].split(
            "FROM config-validate AS toolchain-plan", 1
        )[0]
        self.assertIn(
            "/usr/libexec/platform-python ./scripts/render-release-components.py --check",
            block,
        )

    def test_renderer_is_python36_syntax_compatible(self):
        ast.parse(
            RENDERER_PATH.read_text(encoding="utf-8"),
            filename=str(RENDERER_PATH),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
