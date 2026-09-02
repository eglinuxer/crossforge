import json
import re
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
RENDERER = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))


def docker_stages(document):
    matches = list(
        re.finditer(
            r"^FROM(?:\s+--platform=\S+)?\s+(\S+)\s+AS\s+([a-z0-9_-]+)\s*$",
            document,
            re.MULTILINE,
        )
    )
    stages = {}
    seen = set()
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(
            document
        )
        base = match.group(1)
        name = match.group(2)
        block = document[match.start():end]
        dependencies = set()
        if base in seen:
            dependencies.add(base)
        for source in re.findall(r"--from=([a-zA-Z0-9_.-]+)", block):
            if source in seen:
                dependencies.add(source)
        for source in re.findall(
            r"(?:^|,)from=([a-zA-Z0-9_.-]+)", block
        ):
            if source in seen:
                dependencies.add(source)
        stages[name] = {"block": block, "dependencies": dependencies}
        seen.add(name)
    return stages


def dependency_closure(edges, root):
    result = set()
    pending = [root]
    while pending:
        current = pending.pop()
        for dependency in edges.get(current, ()):
            if dependency in result:
                continue
            result.add(dependency)
            pending.append(dependency)
    return result


class ToolchainBuildExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.override = json.loads(RENDERER["render"](REPOSITORY))
        cls.targets = cls.override["target"]
        cls.hcl = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        cls.dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        cls.stages = docker_stages(cls.dockerfile)

    def test_build_exports_are_scratch_and_contain_one_architecture(self):
        for arch in ("x86_64", "aarch64"):
            stage = "toolchain-%s-build-export" % arch
            block = self.stages[stage]["block"]
            code = "\n".join(
                line
                for line in block.splitlines()
                if not line.lstrip().startswith("#")
            )
            self.assertTrue(block.startswith("FROM scratch AS %s" % stage))
            self.assertEqual(code.count("COPY --from="), 3)
            self.assertEqual(code.count("COPY --from=gcc-%s" % arch), 1)
            self.assertEqual(code.count("COPY --from=binutils-%s" % arch), 1)
            self.assertEqual(code.count("COPY --from=sysroot-%s" % arch), 1)
            self.assertIn("/work/binutils-stage/opt/ /opt/", code)
            self.assertIn("/work/gcc-stage/opt/ /opt/", code)
            self.assertIn("/opt/crossforge/sysroots/el8/%s/" % arch, code)
            other = "aarch64" if arch == "x86_64" else "x86_64"
            self.assertNotIn("/targets/%s" % other, code)
            self.assertNotIn("/sysroots/el8/%s" % other, code)
            for forbidden in (
                "RUN ",
                "release.json",
                "qualification",
                "qemu",
            ):
                self.assertNotIn(forbidden, code)

    def test_bake_declares_both_cache_only_export_targets(self):
        for arch in ("x86_64", "aarch64"):
            name = "toolchain-%s-build-export" % arch
            match = re.search(
                r'target "%s"\s*\{(?P<body>.*?)^\}' % re.escape(name),
                self.hcl,
                re.MULTILINE | re.DOTALL,
            )
            self.assertIsNotNone(match, name)
            body = match.group("body")
            self.assertIn('inherits = ["_common"]', body)
            self.assertIn('target   = "%s"' % name, body)
            self.assertIn('output   = ["type=cacheonly"]', body)

    def test_every_python_cross_uses_build_export_but_final_sdk_uses_dev(self):
        reverse_consumers = {"x86_64": set(), "aarch64": set()}
        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            row = contract["row"]
            for arch in RENDERER["PYTHON_TARGETS"]:
                cross = self.targets["cpython-cross-%s-%s" % (row, arch)]
                self.assertEqual(
                    cross["contexts"]["crossforge_toolchain"],
                    "target:toolchain-%s-build-export" % arch,
                )
        for name, target in self.targets.items():
            for value in target.get("contexts", {}).values():
                for arch in reverse_consumers:
                    if value == "target:toolchain-%s-build-export" % arch:
                        reverse_consumers[arch].add(name)
        for arch, consumers in reverse_consumers.items():
            self.assertEqual(
                consumers,
                {
                    "cpython-cross-%s-%s" % (contract["row"], arch)
                    for contract in RENDERER["IMPLEMENTED_ROWS"]
                }
                | {"zstd-%s-build" % arch},
            )
        sdk = self.targets["sdk-toolchains-dev"]["contexts"]
        self.assertEqual(
            sdk["crossforge_toolchain_x86_64"],
            "target:toolchain-x86_64-dev",
        )
        self.assertEqual(
            sdk["crossforge_toolchain_aarch64"],
            "target:toolchain-aarch64-dev",
        )

    def test_build_export_internal_closures_exclude_qualification_and_qemu(self):
        edges = {
            name: stage["dependencies"]
            for name, stage in self.stages.items()
        }
        forbidden = (
            "config-validate",
            "qualify",
            "runtime-smoke",
            "qemu",
            "toolchain-x86_64-dev",
            "toolchain-aarch64-dev",
        )
        for arch in ("x86_64", "aarch64"):
            stage = "toolchain-%s-build-export" % arch
            closure = dependency_closure(edges, stage)
            with self.subTest(stage=stage):
                self.assertIn("gcc-%s" % arch, closure)
                self.assertIn("binutils-%s" % arch, closure)
                self.assertIn("sysroot-%s" % arch, closure)
                self.assertFalse(
                    {
                        dependency
                        for dependency in closure
                        if any(token in dependency for token in forbidden)
                    }
                )

    def test_direct_cross_target_graph_has_no_missing_reference_or_cycle(self):
        hcl_targets = set(
            re.findall(r'^target\s+"([^"]+)"\s*\{', self.hcl, re.MULTILINE)
        )
        all_targets = set(self.targets) | hcl_targets
        edges = {
            name: {
                value[len("target:"):]
                for value in target.get("contexts", {}).values()
                if value.startswith("target:")
            }
            for name, target in self.targets.items()
        }
        self.assertFalse(
            {
                dependency
                for dependencies in edges.values()
                for dependency in dependencies
                if dependency not in all_targets
            }
        )
        visiting = set()
        visited = set()

        def visit(name):
            self.assertNotIn(name, visiting, "target cycle at %s" % name)
            if name in visited:
                return
            visiting.add(name)
            for dependency in edges.get(name, ()):
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in edges:
            visit(name)

        forbidden = (
            "config-validate",
            "qualify",
            "runtime-smoke",
            "qemu",
            "toolchain-x86_64-dev",
            "toolchain-aarch64-dev",
        )
        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            for arch in RENDERER["PYTHON_TARGETS"]:
                name = "cpython-cross-%s-%s" % (contract["row"], arch)
                closure = dependency_closure(edges, name)
                self.assertIn("toolchain-%s-build-export" % arch, closure)
                self.assertFalse(
                    {
                        dependency
                        for dependency in closure
                        if any(token in dependency for token in forbidden)
                    },
                    name,
                )


if __name__ == "__main__":
    unittest.main()
