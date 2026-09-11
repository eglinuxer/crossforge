"""Validate a prior toolchain report without importing SDK or Python row policy."""

import hashlib
import json
import re
import runpy
from pathlib import Path


POLICY = runpy.run_path(str(Path(__file__).with_name("toolchain_policy.py")))


class QualificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise QualificationError(message)


def load_json(path):
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, "duplicate JSON key: %s" % key)
            result[key] = value
        return result
    def constant(value):
        raise QualificationError("nonfinite JSON value: %s" % value)
    try:
        with Path(path).open(encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=pairs, parse_constant=constant)
        require(type(value) is dict, "toolchain report must be an object")
        return value
    except (OSError, ValueError) as error:
        raise QualificationError(str(error)) from error


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_report(arch, target, report_path):
    require(arch in ("x86_64", "aarch64") and target == arch + "-unknown-linux-gnu",
            "unsupported toolchain qualification target")
    require(
        report_path.is_file() and not report_path.is_symlink(),
        "%s toolchain qualification report is missing or unsafe" % arch,
    )
    return load_json(report_path)


def _qualify_report(arch, target, report_path, report, gcc, binutils,
                    sysroot_sha256, component, policy=None, release_sha256=None):
    scoped = "input_binding" in report
    require(scoped or "qualification_schema_version" not in report or
            (arch == "aarch64" and type(report["qualification_schema_version"]) is int and
             report["qualification_schema_version"] == 1), "unsupported toolchain qualification schema")
    if scoped:
        try:
            require(policy is not None, "scoped toolchain report requires authenticated policy")
            POLICY["require_binding"](report, policy)
        except (ValueError, KeyError, TypeError) as error:
            raise QualificationError(str(error)) from error
        require(type(report.get("qualification_schema_version")) is int and
                report["qualification_schema_version"] == 2 and
                report.get("report_kind") == "crossforge-toolchain-qualification" and
                report.get("runtime_executor") == policy["runtime_executor"] and
                report.get("runtime_base") == policy["runtime_base"] and
                type(report.get("abi_baseline")) is dict and
                report["abi_baseline"].get("canonical_sha256") == policy["abi_baseline"]["canonical_sha256"],
                "scoped toolchain qualification policy differs")
    require(type(report.get("binutils_version")) is str,
            "toolchain binutils version is missing")
    require(
        report.get("target") == target
        and (scoped or report.get("release_sha256") == release_sha256)
        and report.get("sysroot_sha256") == sysroot_sha256
        and report.get("compiler_version") == gcc["version"]
        and re.search(
            r"(?<![0-9.])%s(?![0-9.])"
            % re.escape(binutils["version"]),
            report.get("binutils_version", ""),
        )
        is not None
        and report.get("sources")
        == {
            "gcc": gcc["source"],
            "binutils": binutils["source"],
        }
        and report.get("qualification_component") == component,
        "%s prior toolchain qualification does not match current inputs" % arch,
    )
    if arch == "aarch64":
        require(
            type(report.get("qualification_schema_version")) is int
            and report["qualification_schema_version"] == (2 if scoped else 1)
            and report.get("report_kind")
            == "crossforge-toolchain-qualification"
            and type(report.get("locked_sysroot_execution")) is dict
            and report["locked_sysroot_execution"].get("status")
            == "passed"
            and type(report.get("clean_runtime_execution")) is dict
            and report["clean_runtime_execution"].get("status")
            == "passed",
            "aarch64 prior runtime qualification did not pass",
        )
    else:
        require(type(report.get("locked_sysroot_execution")) is dict and
                report["locked_sysroot_execution"].get("status") == "passed",
                "x86_64 locked-sysroot qualification did not pass")
        clean_marker = report_path.with_name("x86_64-clean-runtime.ok")
        require(
            clean_marker.is_file()
            and not clean_marker.is_symlink()
            and clean_marker.read_bytes() == b"passed\n",
            "x86_64 clean-runtime qualification marker differs",
        )
    return {
        "component": component,
        "report_sha256": sha256_file(report_path),
    }


def qualify_policy_toolchain_report(report_path, policy):
    """Check a scoped report using policy authenticated by load/from_release.

    The caller supplies expected policy, never policy inferred from the report.
    A legacy report requires the complete-release entry point below.
    """
    try:
        require(type(policy) is dict, "toolchain policy must be an object")
        arch = policy["target"]["arch"]
        validated = POLICY["_policy"](arch, policy["component"], policy["target"],
            policy["abi_baseline"], policy["gcc"], policy["binutils"],
            policy["runtime_base"], policy["runtime_executor"])
        require(POLICY["component"].canonical_sha256(policy) ==
                POLICY["component"].canonical_sha256(validated), "toolchain policy fields differ")
    except (ValueError, KeyError, TypeError) as error:
        raise QualificationError(str(error)) from error
    target = validated["target"]["triple"]
    report_path = Path(report_path)
    report = _read_report(arch, target, report_path)
    require("input_binding" in report, "component policy requires a scoped toolchain report")
    return _qualify_report(arch, target, report_path, report, validated["gcc"], validated["binutils"],
        validated["target"]["sysroot"]["canonical_sha256"], validated["component"], policy=validated)


def qualify_prior_toolchain_report(
    arch, target, report_path, release, release_sha256, sysroot_sha256, component,
):
    """Compatibility adapter for consumers with complete release expectations."""
    report_path = Path(report_path)
    report = _read_report(arch, target, report_path)
    policy = None
    if "input_binding" in report:
        try:
            policy = POLICY["from_release"](release, arch, component)
        except (ValueError, KeyError, TypeError) as error:
            raise QualificationError(str(error)) from error
    return _qualify_report(arch, target, report_path, report,
        {"version": release["gts"]["gcc_version"], "source": release["gts"]["source"]},
        {"version": release["binutils"]["version"], "source": release["binutils"]["source"]},
        sysroot_sha256, component, policy=policy, release_sha256=release_sha256)
