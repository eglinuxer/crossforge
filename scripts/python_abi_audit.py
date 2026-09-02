#!/usr/bin/env python3
"""Strict ELF dependency ownership primitives for CPython qualification.

The frozen ABI baseline remains authoritative for core versioned imports.
Versioned imports from Python's separately locked runtime providers must exist
in the actual provider DSO.  Strong unversioned imports must have exactly one
owner in the Python executable's global scope plus the artifact's recursive
DT_NEEDED closure.  Unresolved weak hooks are evidence, never implicit success.

This module intentionally uses only Python 3.6-compatible standard-library
interfaces so it can run in the locked EL8 qualification environment.
"""

import re

import abi_contract


ELF_RECORD_KEYS = {
    "identity",
    "soname",
    "needed",
    "versioned_exports",
    "unversioned_exports",
    "default_exports",
    "versioned_imports",
    "unversioned_imports",
}
VERSIONED_EXPORT_KEYS = {"name", "version", "default"}
VERSIONED_IMPORT_KEYS = {"provider", "name", "version", "binding"}
UNVERSIONED_IMPORT_KEYS = {"name", "binding"}
IMPORT_BINDINGS = {"GLOBAL", "WEAK"}
EXPORT_BINDINGS = {"GLOBAL", "WEAK", "UNIQUE"}
EXPORT_VISIBILITIES = {"DEFAULT", "PROTECTED"}
NEEDED_RE = re.compile(r"\(NEEDED\).*?\[([^\]]+)\]")
SONAME_RE = re.compile(r"\(SONAME\).*?\[([^\]]+)\]")


class PythonAbiAuditError(ValueError):
    """Python ELF dependency ownership is incomplete or contradictory."""


def require(condition, message):
    if not condition:
        raise PythonAbiAuditError(message)


def _validate_symbol(value, label):
    try:
        return abi_contract._validate_symbol_name(value, label)
    except abi_contract.AbiContractError as error:
        raise PythonAbiAuditError(str(error)) from error


def _validate_soname(value, label):
    try:
        return abi_contract._validate_soname(value, label)
    except abi_contract.AbiContractError as error:
        raise PythonAbiAuditError(str(error)) from error


def _split_defined_symbol(value):
    if "@@" in value:
        name, version = value.rsplit("@@", 1)
        default = True
    elif "@" in value:
        name, version = value.rsplit("@", 1)
        default = False
    else:
        return value, None, True
    require(name and version, "malformed versioned export: %r" % value)
    require(not any(character.isspace() for character in version), "export version contains whitespace")
    return name, version, default


def parse_dynamic_identities(dynamic_section, expected_soname=None):
    """Return ordered DT_NEEDED entries and the optional exact DT_SONAME."""
    require(type(dynamic_section) is str, "readelf dynamic section must be text")
    needed = NEEDED_RE.findall(dynamic_section)
    for soname in needed:
        _validate_soname(soname, "DT_NEEDED")
    require(len(needed) == len(set(needed)), "ELF repeats a DT_NEEDED provider")
    sonames = SONAME_RE.findall(dynamic_section)
    require(len(sonames) <= 1, "ELF exposes multiple DT_SONAME entries")
    if sonames:
        _validate_soname(sonames[0], "DT_SONAME")
    observed = sonames[0] if sonames else None
    if expected_soname is not None:
        _validate_soname(expected_soname, "expected DT_SONAME")
        require(observed == expected_soname, "ELF DT_SONAME differs from provider identity")
    return needed, observed


def elf_record_from_readelf(
    identity,
    dynamic_symbols,
    version_info,
    dynamic_section,
    expected_soname=None,
):
    """Build a deterministic import/export record while preserving ``@@``."""
    require(
        type(identity) is str
        and identity
        and not any(character.isspace() for character in identity),
        "ELF identity must be nonempty text without whitespace",
    )
    needed, soname = parse_dynamic_identities(dynamic_section, expected_soname)
    try:
        rows = abi_contract._symbol_rows(dynamic_symbols)
        version_needs = abi_contract.parse_readelf_version_needs(version_info)
    except abi_contract.AbiContractError as error:
        raise PythonAbiAuditError(str(error)) from error

    version_nodes = set(re.findall(r"\bName:\s+(\S+)", version_info))
    versioned_exports = set()
    unversioned_exports = set()
    default_exports = set()
    versioned_imports = set()
    unversioned_imports = set()
    for row in rows:
        raw_name = row["name"]
        if row["index"] == "UND":
            name, version = abi_contract._split_symbol_version(raw_name)
            _validate_symbol(name, "undefined symbol")
            binding = row["binding"]
            require(binding in IMPORT_BINDINGS, "unsupported undefined symbol binding")
            if version is None:
                unversioned_imports.add((name, binding))
                continue
            require(row["version_index"] is not None, "versioned import has no numeric index")
            require(
                row["version_index"] in version_needs,
                "versioned import has no provider mapping",
            )
            need = version_needs[row["version_index"]]
            require(need["version"] == version, "import version differs from provider mapping")
            versioned_imports.add((need["provider"], name, version, binding))
            continue

        if row["binding"] not in EXPORT_BINDINGS:
            continue
        if row["visibility"] not in EXPORT_VISIBILITIES:
            continue
        if row["index"] == "ABS" and raw_name in version_nodes:
            continue
        name, version, default = _split_defined_symbol(raw_name)
        _validate_symbol(name, "defined symbol")
        if version is not None and name == version and row["index"] == "ABS":
            continue
        if version is None:
            unversioned_exports.add(name)
            default_exports.add(name)
        else:
            versioned_exports.add((name, version, default))
            if default:
                default_exports.add(name)

    record = {
        "identity": identity,
        "soname": soname,
        "needed": list(needed),
        "versioned_exports": [
            {"name": name, "version": version, "default": default}
            for name, version, default in sorted(versioned_exports)
        ],
        "unversioned_exports": sorted(unversioned_exports),
        "default_exports": sorted(default_exports),
        "versioned_imports": [
            {
                "provider": provider,
                "name": name,
                "version": version,
                "binding": binding,
            }
            for provider, name, version, binding in sorted(versioned_imports)
        ],
        "unversioned_imports": [
            {"name": name, "binding": binding}
            for name, binding in sorted(unversioned_imports)
        ],
    }
    validate_elf_record(record, expected_soname)
    return record


def validate_elf_record(record, expected_soname=None):
    require(type(record) is dict and set(record) == ELF_RECORD_KEYS, "ELF record fields differ")
    identity = record["identity"]
    require(
        type(identity) is str
        and identity
        and not any(character.isspace() for character in identity),
        "ELF record identity is invalid",
    )
    soname = record["soname"]
    require(soname is None or type(soname) is str, "ELF record SONAME is invalid")
    if soname is not None:
        _validate_soname(soname, "ELF record SONAME")
    if expected_soname is not None:
        require(soname == expected_soname, "ELF record SONAME differs from expected")
        require(identity == expected_soname, "provider record identity differs from SONAME")

    needed = record["needed"]
    require(type(needed) is list, "ELF record needed must be an array")
    for value in needed:
        _validate_soname(value, "ELF record DT_NEEDED")
    require(len(needed) == len(set(needed)), "ELF record repeats DT_NEEDED")

    exports = record["versioned_exports"]
    require(type(exports) is list, "versioned exports must be an array")
    export_keys = []
    for item in exports:
        require(type(item) is dict and set(item) == VERSIONED_EXPORT_KEYS, "versioned export fields differ")
        _validate_symbol(item["name"], "versioned export name")
        require(
            type(item["version"]) is str
            and item["version"]
            and "@" not in item["version"]
            and not any(character.isspace() for character in item["version"]),
            "versioned export version is invalid",
        )
        require(type(item["default"]) is bool, "versioned export default must be boolean")
        export_keys.append((item["name"], item["version"], item["default"]))
    require(export_keys == sorted(export_keys), "versioned exports are not sorted")
    require(len(export_keys) == len(set(export_keys)), "versioned exports repeat")

    unversioned_exports = record["unversioned_exports"]
    require(type(unversioned_exports) is list, "unversioned exports must be an array")
    for name in unversioned_exports:
        _validate_symbol(name, "unversioned export")
    require(
        unversioned_exports == sorted(set(unversioned_exports)),
        "unversioned exports are not canonical",
    )

    defaults = record["default_exports"]
    require(type(defaults) is list, "default exports must be an array")
    for name in defaults:
        _validate_symbol(name, "default export")
    require(defaults == sorted(set(defaults)), "default exports are not canonical")
    expected_defaults = set(unversioned_exports)
    expected_defaults.update(
        item["name"] for item in exports if item["default"]
    )
    require(
        defaults == sorted(expected_defaults),
        "default exports differ from unversioned and @@ exports",
    )

    versioned_imports = record["versioned_imports"]
    require(type(versioned_imports) is list, "versioned imports must be an array")
    versioned_keys = []
    for item in versioned_imports:
        require(type(item) is dict and set(item) == VERSIONED_IMPORT_KEYS, "versioned import fields differ")
        _validate_soname(item["provider"], "versioned import provider")
        _validate_symbol(item["name"], "versioned import name")
        require(
            type(item["version"]) is str
            and item["version"]
            and "@" not in item["version"]
            and not any(character.isspace() for character in item["version"]),
            "versioned import version is invalid",
        )
        require(item["binding"] in IMPORT_BINDINGS, "versioned import binding is invalid")
        versioned_keys.append(
            (item["provider"], item["name"], item["version"], item["binding"])
        )
    require(versioned_keys == sorted(versioned_keys), "versioned imports are not sorted")
    require(len(versioned_keys) == len(set(versioned_keys)), "versioned imports repeat")

    unversioned = record["unversioned_imports"]
    require(type(unversioned) is list, "unversioned imports must be an array")
    unversioned_keys = []
    for item in unversioned:
        require(type(item) is dict and set(item) == UNVERSIONED_IMPORT_KEYS, "unversioned import fields differ")
        _validate_symbol(item["name"], "unversioned import name")
        require(item["binding"] in IMPORT_BINDINGS, "unversioned import binding is invalid")
        unversioned_keys.append((item["name"], item["binding"]))
    require(unversioned_keys == sorted(unversioned_keys), "unversioned imports are not sorted")
    require(len(unversioned_keys) == len(set(unversioned_keys)), "unversioned imports repeat")
    return record


def validate_provider_catalog(baseline, external_providers, catalog):
    """Validate the exact core+external provider universe."""
    try:
        abi_contract.validate_baseline(baseline)
    except abi_contract.AbiContractError as error:
        raise PythonAbiAuditError(str(error)) from error
    require(type(external_providers) in (list, tuple), "external providers must be an array")
    external = list(external_providers)
    for soname in external:
        _validate_soname(soname, "external provider")
    require(external == sorted(set(external)), "external providers are not canonical")
    core = set(baseline["providers"])
    require(not core.intersection(external), "external providers overlap the core ABI")
    expected = core.union(external)
    require(type(catalog) is dict and set(catalog) == expected, "provider catalog membership differs")
    for soname in sorted(catalog):
        validate_elf_record(catalog[soname], soname)
        for dependency in catalog[soname]["needed"]:
            require(dependency in expected, "provider dependency is outside the owned universe: %s" % dependency)
    return catalog


def _dependency_closure(roots, catalog):
    state = {}
    result = []

    def visit(soname):
        require(soname in catalog, "DT_NEEDED provider is not owned: %s" % soname)
        status = state.get(soname, 0)
        require(status != 1, "provider DT_NEEDED graph contains a cycle")
        if status == 2:
            return
        state[soname] = 1
        if soname not in result:
            result.append(soname)
        for dependency in catalog[soname]["needed"]:
            visit(dependency)
        state[soname] = 2

    for root in roots:
        visit(root)
    return result


def audit_python_elf(
    baseline,
    external_providers,
    catalog,
    python_global,
    artifact,
):
    """Audit one Python executable/module with no unresolved strong imports."""
    validate_provider_catalog(baseline, external_providers, catalog)
    validate_elf_record(python_global)
    validate_elf_record(artifact)
    require(python_global["soname"] is None, "Python global record must be an executable")

    global_closure = _dependency_closure(python_global["needed"], catalog)
    artifact_closure = _dependency_closure(artifact["needed"], catalog)
    scope = []
    for identity in [python_global["identity"]] + global_closure + artifact_closure:
        if identity not in scope:
            scope.append(identity)
    reachable = set(scope[1:])
    core_allowlist = {
        provider: {
            (record["name"], record["version"])
            for record in exports
        }
        for provider, exports in baseline["providers"].items()
    }
    external = set(external_providers)
    provider_exports = {
        soname: {
            (record["name"], record["version"])
            for record in details["versioned_exports"]
        }
        for soname, details in catalog.items()
    }
    core_versioned = []
    external_versioned = []
    for item in artifact["versioned_imports"]:
        provider = item["provider"]
        name = item["name"]
        version = item["version"]
        require(provider in reachable, "versioned import provider is outside the loader scope")
        if provider in core_allowlist:
            classification = abi_contract.classify_version(version, provider)
            require(
                classification == "public",
                "core import uses a %s version node" % classification,
            )
            require(
                (name, version) in core_allowlist[provider],
                "core ABI import is not in the frozen baseline: %s:%s@%s"
                % (provider, name, version),
            )
            require(
                (name, version) in provider_exports[provider],
                "core ABI import is absent from the locked provider DSO",
            )
            core_versioned.append(dict(item))
        else:
            require(provider in external, "versioned import provider is not explicitly owned")
            require(
                (name, version) in provider_exports[provider],
                "external versioned import is absent from the locked provider DSO",
            )
            external_versioned.append(dict(item))

    strong = []
    optional_weak = []
    for item in artifact["unversioned_imports"]:
        name = item["name"]
        owners = []
        if name in python_global["default_exports"]:
            owners.append(python_global["identity"])
        for soname in scope[1:]:
            if name in catalog[soname]["default_exports"] and soname not in owners:
                owners.append(soname)
        if item["binding"] == "GLOBAL":
            require(owners, "strong unversioned import has no owner: %s" % name)
            require(
                len(owners) == 1,
                "strong unversioned import has multiple owners: %s" % name,
            )
            strong.append({"name": name, "owner": owners[0]})
        else:
            optional_weak.append(
                {
                    "name": name,
                    "resolution": "resolved" if owners else "optional-unresolved-weak",
                    "owners": owners,
                }
            )

    core_versioned.sort(
        key=lambda item: (
            item["provider"], item["name"], item["version"], item["binding"]
        )
    )
    external_versioned.sort(
        key=lambda item: (
            item["provider"], item["name"], item["version"], item["binding"]
        )
    )
    strong.sort(key=lambda item: (item["name"], item["owner"]))
    optional_weak.sort(key=lambda item: item["name"])
    return {
        "status": "passed",
        "artifact": artifact["identity"],
        "needed": list(artifact["needed"]),
        "provider_closure": sorted(reachable),
        "core_versioned": core_versioned,
        "external_versioned": external_versioned,
        "strong_unversioned": strong,
        "optional_weak": optional_weak,
    }
