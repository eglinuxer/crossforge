import json
import re
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
RENDERER = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))


class ZstdDockerGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        cls.binding = json.loads(
            (REPOSITORY / "config/generated/release-binding.json").read_text(
                encoding="utf-8"
            )
        )
        cls.records = {item["component"]: item for item in cls.binding["components"]}
        cls.document = json.loads(RENDERER["render"](REPOSITORY))
        cls.targets = cls.document["target"]
        cls.dockerfile = (REPOSITORY / "docker/zstd.Dockerfile").read_text(
            encoding="utf-8"
        )

    def digest(self, component):
        return self.records[component]["canonical_sha256"]

    def test_hcl_defines_only_the_zstd_common_template(self):
        hcl = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        self.assertEqual(hcl.count('target "_zstd_common"'), 1)
        self.assertIn('dockerfile = "docker/zstd.Dockerfile"', hcl)
        for name in ("zstd-source", "zstd-host-build", "zstd-x86_64-build", "zstd-aarch64-build"):
            self.assertNotIn('target "%s"' % name, hcl)

    def test_renderer_binds_exact_components_and_contexts(self):
        source = self.targets["zstd-source"]
        self.assertEqual(source["inherits"], ["_zstd_common"])
        self.assertEqual(source["target"], "zstd-source")
        self.assertEqual(source["args"]["ZSTD_SOURCE_COMPONENT_SHA256"], self.digest("sources/zstd"))
        host = self.targets["zstd-host-build"]
        self.assertEqual(host["target"], "zstd-host-build-export")
        self.assertEqual(host["args"]["ZSTD_BUILD_COMPONENT_SHA256"], self.digest("zstd/host-build"))
        self.assertEqual(set(host["contexts"]), {"crossforge_host_common", "crossforge_zstd_source"})
        for arch, triple in RENDERER["PYTHON_TARGETS"].items():
            target = self.targets["zstd-%s-build" % arch]
            self.assertEqual(target["target"], "zstd-target-build-export")
            self.assertEqual(target["args"]["ZSTD_TARGET_TRIPLE"], triple)
            self.assertEqual(target["args"]["ZSTD_BUILD_COMPONENT_SHA256"], self.digest("zstd/%s-build" % arch))
            self.assertEqual(target["contexts"]["crossforge_toolchain"], "target:toolchain-%s-build-export" % arch)
        for name in ("zstd-host-build", "zstd-x86_64-build", "zstd-aarch64-build"):
            self.assertEqual(self.targets[name]["args"]["ZSTD_BUILD_POLICY_COMPONENT_SHA256"], self.digest("implementation/zstd-build-policy"))
            self.assertEqual(self.targets[name]["output"], ["type=cacheonly"])

    def test_source_and_build_stages_have_minimal_security_boundaries(self):
        source = self.dockerfile.split("FROM crossforge_rocky_amd64 AS zstd-source", 1)[1].split("FROM crossforge_host_common AS zstd-host-build", 1)[0]
        for required in ("sources/zstd.json", "ZSTD-RELEASE-KEY.asc", "zstd-1.5.7.tar.gz.sig.b64", "base64 --decode", "prepare-zstd-source.py", "/out/materials/zstd.tar.gz", "/out/materials/zstd.tar.gz.sig"):
            self.assertIn(required, source)
        for forbidden in ("config/release.json", "release.schema.json", "evidence/oci", "patches/"):
            self.assertNotIn(forbidden, source)
        for stage in ("zstd-host-build", "zstd-target-build"):
            block = re.split(r"^FROM ", self.dockerfile.split(" AS %s" % stage, 1)[1], maxsplit=1, flags=re.MULTILINE)[0]
            self.assertIn("RUN --network=none", block)
            self.assertIn("sources-zstd.json", block)
            self.assertIn("zstd-build-policy.json", block)
            self.assertIn("prepare-zstd-source.py", block)
            self.assertIn("build-zstd.sh", block)
            self.assertIn("/work/prepared/materials/zstd.tar.gz", block)
            self.assertIn("/work/prepared/materials/zstd.tar.gz.sig", block)
            self.assertRegex(
                block,
                r"/work/config/sources-zstd\.json \"\$ZSTD_SOURCE_COMPONENT_SHA256\"",
            )
        self.assertIn("FROM scratch AS zstd-host-build-export", self.dockerfile)
        self.assertIn("FROM scratch AS zstd-target-build-export", self.dockerfile)

    def test_generated_target_edges_have_no_missing_reference_or_cycle(self):
        hcl = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        all_targets = set(self.targets) | set(re.findall(r'^target\s+"([^"]+)"', hcl, re.MULTILINE))
        edges = {
            name: {value[7:] for value in target.get("contexts", {}).values() if value.startswith("target:")}
            for name, target in self.targets.items()
        }
        self.assertFalse({dep for values in edges.values() for dep in values if dep not in all_targets})
        visiting = set()
        visited = set()
        def visit(name):
            self.assertNotIn(name, visiting)
            if name in visited:
                return
            visiting.add(name)
            for dependency in edges.get(name, ()):
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
        for name in edges:
            visit(name)


if __name__ == "__main__":
    unittest.main()
