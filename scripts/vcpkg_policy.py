"""Authenticate vcpkg qualification inputs without a partial release document.

The SDK build root and two qualification roots are independent trusted pins.
Each subsequent qualification root binds that SDK and its predecessor. This
input policy does not assert that any command ran or authorize receipt reuse.
"""

import copy
from pathlib import Path
import re
import runpy

TOOLCHAIN = runpy.run_path(str(Path(__file__).with_name("toolchain_policy.py")))
READER = TOOLCHAIN["component"]
ARCHES = ("x86_64", "aarch64")
STAGES = ("sdk", "contract", "tier1", "tier2", "tier3")
NAMES = dict(zip(STAGES, ("vcpkg/sdk-build", "vcpkg/contract-qualification",
    "vcpkg/upstream-tier1-qualification", "vcpkg/upstream-tier2-qualification", "vcpkg/upstream-tier3-qualification")))
BASE_MATERIALS = [{"path": "/baseline", "value": "el8"}] + [
    {"path": "/platforms/" + key, "value": "linux/amd64"} for key in ("build", "image", "tool_host")]
REPORT_FIELDS = {
    "sdk": {"environment", "source", "ninja", "cmake_host_tool", "integration", "triplets", "cmake", "toolchain_report_sha256"},
    "contract": {"components", "dependencies", "fixture_files", "patchelf_asset", "results"},
    "upstream": {"components", "dependencies", "assets", "fixture_files", "ports", "results"},
}


class PolicyError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PolicyError(message)


def _identity(document):
    return {"component": document["component"], "canonical_sha256": READER.canonical_sha256(document)}


def _dependencies(document, names):
    dependencies = {item["component"]: item["canonical_sha256"] for item in document["dependencies"]}
    require(set(dependencies) == set(names), "%s dependency set differs" % document["component"])
    return dependencies


def host_tools_from_release(release):
    ninja, cmake = (release["host_tools"][name] for name in ("ninja", "cmake"))
    payloads = [item for item in cmake["payloads"] if item["path"] == "bin/cmake"]
    require(len(payloads) == 1, "CMake binary payload must be unique")
    return {"ninja": {"version": ninja["version"], "binary_sha256": ninja["binary"]["extracted_sha256"],
                      "license_sha256": ninja["license"]["sha256"]},
            "cmake": {"version": cmake["version"], "binary_sha256": payloads[0]["sha256"],
                      "url": cmake["binary"]["url"], "sha512": cmake["binary"]["sha512"]}}


def _host_tool(source, name):
    require(not source["dependencies"], "host tool source cannot have dependencies")
    def leaf(suffix):
        return READER.material_value(source, source["component"], "build", READER.canonical_sha256(source),
                                     "/host_tools/" + name + "/" + suffix, "string")
    require(leaf("binary/status") == "locked", "host tool binary must be locked")
    version = leaf("version")
    require(re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", version), "invalid host tool version")
    if name == "ninja":
        result = {"version": version, "binary_sha256": leaf("binary/extracted_sha256"),
                  "license_sha256": leaf("license/sha256")}
        READER.validate_canonical_sha256(result["license_sha256"])
    else:
        matches = [item["path"][:-len("/path")] for item in source["materials"] if
                   re.fullmatch(r"/host_tools/cmake/payloads/(0|[1-9][0-9]*)/path", item["path"]) and item["value"] == "bin/cmake"]
        require(len(matches) == 1, "CMake binary payload must be unique")
        suffix = matches[0][len("/host_tools/cmake/"):] + "/sha256"
        result = {"version": version, "binary_sha256": leaf(suffix), "url": leaf("binary/url"), "sha512": leaf("binary/sha512")}
        require(result["url"].startswith("https://") and re.fullmatch(r"[0-9a-f]{128}", result["sha512"]),
                "CMake download identity differs")
    READER.validate_canonical_sha256(result["binary_sha256"])
    return result


def _load(read, toolchain_policy, stage, trusted_sha256, toolchain_sha256=None):
    require(stage in STAGES, "unsupported vcpkg policy stage")
    if stage == "sdk":
        require(type(toolchain_sha256) is dict and set(toolchain_sha256) == set(ARCHES),
                "vcpkg SDK requires both independent toolchain qualification pins")
        pins = copy.deepcopy(toolchain_sha256)
    else:
        require(toolchain_sha256 is None, "later vcpkg stages derive toolchain pins from their qualification root")
        pins = None
    identities = {}
    current, digest = stage, trusted_sha256
    while True:
        document = read(NAMES[current], "build" if current == "sdk" else "qualification", digest)
        require(document["materials"] == BASE_MATERIALS, "vcpkg platform material set differs")
        identities[current] = _identity(document)
        if current == "sdk":
            sdk = document
            break
        previous = STAGES[STAGES.index(current) - 1]
        implementation = "implementation/vcpkg-" + ("contract" if current == "contract" else "upstream-" + current) + "-qualification"
        expected = {implementation, NAMES[previous]}
        if current == "contract":
            expected.update("toolchain/%s-qualification" % arch for arch in ARCHES)
        dependencies = _dependencies(document, expected)
        if current == "contract":
            pins = {arch: dependencies["toolchain/%s-qualification" % arch] for arch in ARCHES}
        current, digest = previous, dependencies[NAMES[previous]]
    dependencies = _dependencies(sdk, {"rpm/host-runtime", "sources/vcpkg", "host-tools/ninja", "host-tools/cmake",
        "implementation/vcpkg-integration", "toolchain/x86_64-build", "toolchain/aarch64-build"})
    toolchains = {}
    for arch in ARCHES:
        document = read("toolchain/%s-qualification" % arch, "qualification", pins[arch])
        links = _dependencies(document, {"toolchain/%s-build" % arch, "abi/%s-baseline" % arch})
        require(links["toolchain/%s-build" % arch] == dependencies["toolchain/%s-build" % arch],
                "vcpkg SDK and toolchain qualification build inputs differ")
        toolchains[arch] = toolchain_policy(arch, pins[arch])
    host_tools = {}
    for name in ("ninja", "cmake"):
        document = read("host-tools/" + name, "build", dependencies["host-tools/" + name])
        expected = {"rpm/host-runtime", "sources/" + name, "implementation/" + name + "-host-tool"}
        if name == "cmake":
            expected.add("host-tools/ninja")
        links = _dependencies(document, expected)
        require(links["rpm/host-runtime"] == dependencies["rpm/host-runtime"], "vcpkg host runtime input differs")
        if name == "cmake":
            require(links["host-tools/ninja"] == dependencies["host-tools/ninja"], "CMake and vcpkg Ninja inputs differ")
        source = read("sources/" + name, "build", links["sources/" + name])
        host_tools[name] = _host_tool(source, name)
    return {"schema_version": 1, "kind": "crossforge-vcpkg-input-policy", "stage": stage,
            "components": identities, "toolchains": toolchains, "host_tools": host_tools}


def load(directory, stage, trusted_sha256, toolchain_sha256=None):
    directory = Path(directory)
    try:
        return _load(lambda name, scope, digest: READER.load_component(directory / (name + ".json"), name, scope, digest),
                     lambda arch, digest: TOOLCHAIN["load"](directory, arch, digest), stage, trusted_sha256, toolchain_sha256)
    except (READER.ComponentError, TOOLCHAIN["PolicyError"]) as error:
        raise PolicyError(str(error)) from error


def from_release(release, stage):
    """For final/legacy consumers; renderer imports stay outside scoped stages."""
    extension = runpy.run_path(str(Path(__file__).with_name("release-components-vcpkg.py")))
    core = runpy.run_path(str(Path(__file__).with_name("release-components-core.py")),
                         init_globals={"COMPONENT_EXTENSIONS": (extension["extend_component_graph"],)})
    try:
        documents = core["render_component_documents"](release)
        def read(name, scope, digest):
            document = documents[name]
            READER.validate_component(document, name, scope)
            require(READER.canonical_sha256(document) == digest, "vcpkg release component digest differs")
            return document
        pins = {arch: READER.canonical_sha256(documents["toolchain/%s-qualification" % arch]) for arch in ARCHES}
        policy = _load(read, lambda arch, digest: TOOLCHAIN["from_release"](release, arch,
            {"component": "toolchain/%s-qualification" % arch, "canonical_sha256": digest}),
            stage, READER.canonical_sha256(documents[NAMES[stage]]), pins if stage == "sdk" else None)
        require(policy["host_tools"] == host_tools_from_release(release), "projected vcpkg host tools differ from release")
        return policy
    except (READER.ComponentError, TOOLCHAIN["PolicyError"], core["ProjectionError"], KeyError, TypeError) as error:
        raise PolicyError(str(error)) from error


def prior(policy, stage):
    require(stage in STAGES and STAGES.index(stage) <= STAGES.index(policy["stage"]), "vcpkg predecessor is not bound")
    result = copy.deepcopy(policy)
    result["stage"] = stage
    result["components"] = {key: result["components"][key] for key in STAGES[:STAGES.index(stage) + 1]}
    return result


def binding(policy):
    return {"schema_version": 1, "kind": "crossforge-vcpkg-input-binding", "policy_sha256": READER.canonical_sha256(policy)}


def require_binding(report, policy):
    stage = policy["stage"]
    kind = "crossforge-vcpkg-" + (stage if stage in ("sdk", "contract") else "upstream-" + stage) + "-qualification"
    fields = REPORT_FIELDS[stage if stage in ("sdk", "contract") else "upstream"]
    require(type(report) is dict and set(report) == fields | {"schema_version", "kind", "status", "input_binding"} and
            type(report.get("schema_version")) is int and report["schema_version"] == 2 and
            report.get("kind") == kind and report.get("status") == "passed" and "release_sha256" not in report,
            "scoped vcpkg report schema or release claim differs")
    require(READER.canonical_sha256(report.get("input_binding")) == READER.canonical_sha256(binding(policy)),
            "vcpkg report input binding differs")
