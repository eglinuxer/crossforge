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


def qualify_prior_toolchain_report(
    arch,
    target,
    report_path,
    release,
    release_sha256,
    sysroot_sha256,
    component,
):
    require(arch in ("x86_64", "aarch64") and target == arch + "-unknown-linux-gnu",
            "unsupported toolchain qualification target")
    require(
        report_path.is_file() and not report_path.is_symlink(),
        "%s toolchain qualification report is missing or unsafe" % arch,
    )
    report = load_json(report_path)
    scoped = "input_binding" in report
    require(scoped or "qualification_schema_version" not in report or
            (arch == "aarch64" and type(report["qualification_schema_version"]) is int and
             report["qualification_schema_version"] == 1), "unsupported toolchain qualification schema")
    if scoped:
        try:
            policy = POLICY["from_release"](release, arch, component)
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
        and report.get("compiler_version") == release["gts"]["gcc_version"]
        and re.search(
            r"(?<![0-9.])%s(?![0-9.])"
            % re.escape(release["binutils"]["version"]),
            report.get("binutils_version", ""),
        )
        is not None
        and report.get("sources")
        == {
            "gcc": release["gts"]["source"],
            "binutils": release["binutils"]["source"],
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
