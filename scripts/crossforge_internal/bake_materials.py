"""Conservative source closure for Crossforge's resolved Bake graph.

This inventories inputs, not Docker build semantics. BuildKit still executes the
recipe. Unsupported source syntax fails closed; it cannot silently omit inputs.
Ignore patterns are deliberately not applied: extra files may invalidate reuse,
but excluded files cannot hide an input. Selected recipe blocks, their declared
args, pinned image contexts, and execution identity are part of the key.
"""

from pathlib import Path
import re
import shlex

from . import component_inputs
from .identity import digest_value, exact_fields, file_record, relative_path, require


FROM = re.compile(r"FROM(?:\s+--platform=\S+)?\s+(\S+)\s+AS\s+([A-Za-z0-9_.-]+)\Z")
VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
IGNORED_BAKE_FIELDS = {"cache-from", "cache-to", "output", "tags", "attest", "no-cache", "no-cache-filter"}
SUPPORTED_BAKE_FIELDS = {"context", "dockerfile", "target", "platforms", "args", "contexts"} | IGNORED_BAKE_FIELDS
PROXY_ARGS = {name for upper in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "NO_PROXY", "ALL_PROXY")
              for name in (upper, upper.lower())}


def instructions(text):
    """Read the repository's default-escape, non-heredoc Dockerfile dialect."""
    result, pending = [], ""
    for line in text.splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            require(not re.match(r"\s*#\s*escape=", line, re.I), "custom Dockerfile escape is unsupported")
            continue
        require("<<" not in line, "Dockerfile heredoc requires explicit material support")
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
        else:
            result.append((pending + stripped).strip())
            pending = ""
    require(not pending, "incomplete Dockerfile continuation")
    return result


def recipe(text):
    header, stages, current = [], {}, None
    for instruction in instructions(text):
        if instruction.startswith("FROM "):
            match = FROM.fullmatch(instruction)
            require(match is not None, "component Docker stages must have explicit names")
            base, name = match.groups()
            require(name not in stages, "duplicate Docker stage")
            stages[name] = {"base": base, "instructions": [instruction]}
            current = stages[name]["instructions"]
        elif current is None:
            require(instruction.startswith("ARG "), "unsupported instruction before FROM")
            header.append(instruction)
        else:
            current.append(instruction)
    frontend = re.findall(r"^# syntax=(\S+)\s*$", text, re.M)
    require(len(frontend) == 1 and "@" in frontend[0], "Dockerfile frontend must be pinned")
    digest_value(frontend[0].rsplit("@", 1)[1], "Dockerfile frontend digest", oci=True)
    return header, stages, frontend[0]


def _expand(value, args):
    def replace(match):
        name = match.group(1) or match.group(2)
        require(name in args and type(args[name]) is str, "undefined source argument: %s" % name)
        return args[name]
    result = VARIABLE.sub(replace, value)
    require("$" not in result, "unsupported source argument expansion")
    return result


def _copy(instruction):
    words = shlex.split(instruction)
    require(words and words[0] == "COPY", "expected COPY instruction")
    flags = {}
    while len(words) > 1 and words[1].startswith("--"):
        flag = words.pop(1)[2:]
        key, separator, value = flag.partition("=")
        require(separator and key in ("from", "chmod", "chown") and key not in flags,
                "unsupported component COPY option: %s" % flag)
        flags[key] = value
    require(len(words) >= 3 and not words[1].startswith("["),
            "component COPY must use supported shell source syntax")
    return flags, words[1:-1]


def _source_files(root, pattern, directories):
    relative_path(pattern.rstrip("/"))
    require(not any(c in pattern for c in ("[", "]", "?")) and "**" not in pattern,
            "unsupported material glob")
    result = set()
    matches = list(root.glob(pattern.rstrip("/")))
    require(matches, "material source pattern has no matches: %s" % pattern)
    for path in matches:
        require(not path.is_symlink(), "symlink material source is unsupported")
        if path.is_dir():
            children = list(path.rglob("*"))
            for directory in [path] + [child for child in children if child.is_dir()]:
                require(not directory.is_symlink(), "symlink material directory is unsupported")
                directories[str(directory.relative_to(root))] = "%04o" % (directory.stat().st_mode & 0o7777)
            files = [child for child in children if not child.is_dir() or child.is_symlink()]
            result.update(str(child.relative_to(root)) for child in files)
        else:
            result.add(str(path.relative_to(root)))
    return result


def _inventory(root, graph, target, execution, artifacts=None):
    root = Path(root).resolve()
    require(type(graph) is dict and type(graph.get("target")) is dict, "expected resolved Bake graph")
    require(type(execution) is dict and execution, "component execution identity is required")
    files, directories = {".dockerignore"}, {}
    artifacts = artifacts if artifacts is not None else {}
    require(type(artifacts) is dict and all(type(name) is str for name in artifacts),
            "component context identities must be a named mapping")
    dependencies, used_artifacts = {}, set()
    for record in artifacts.values():
        exact_fields(record, ("component", "inputs_sha256", "artifact_digest"), "component context identity")
        require(type(record["component"]) is str and
                component_inputs.COMPONENT_NAME.fullmatch(record["component"]), "invalid component context name")
        digest_value(record["inputs_sha256"], "component context inputs SHA256")
        digest_value(record["artifact_digest"], "component context artifact digest", oci=True)
        require(record["component"] not in dependencies or dependencies[record["component"]] == record,
                "conflicting identities for the same component")
        dependencies[record["component"]] = record
    recipes, target_records, visited, visiting = {}, {}, set(), set()

    def visit_target(name):
        if name in visited:
            return
        require(name not in visiting, "cyclic Bake target context")
        require(name in graph["target"], "missing Bake target context: %s" % name)
        visiting.add(name)
        definition = graph["target"][name]
        require(type(definition) is dict and not set(definition) - SUPPORTED_BAKE_FIELDS,
                "unsupported Bake target fields: %s" % name)
        require(definition.get("context") == ".", "component source context must be repository root")
        require(definition.get("platforms") == ["linux/amd64"], "component target must be linux/amd64")
        dockerfile = relative_path(definition.get("dockerfile"))
        require(not (root / dockerfile).is_symlink(), "Dockerfile cannot be a symlink")
        header, stages, frontend = recipe((root / dockerfile).read_text(encoding="utf-8"))
        ignore = dockerfile + ".dockerignore"
        if (root / ignore).exists():
            files.add(ignore)
        args = dict(definition.get("args", {}))
        require(all(type(k) is str and type(v) is str for k, v in args.items()),
                "Bake arguments must be explicit strings")
        require("BUILDKIT_SYNTAX" not in args, "frontend overrides require explicit material support")
        contexts = definition.get("contexts", {})
        require(type(contexts) is dict, "Bake contexts must be an object")
        require(not set(contexts) & set(stages), "named contexts cannot shadow Docker stages")
        selected, active, external = set(), set(), {}
        # Defaults are only used to resolve source names; the complete selected
        # recipe and explicit args remain bound even when a RUN uses an arg.
        defaults = {}
        environment_names = set()
        for instruction in header + [line for stage in stages.values() for line in stage["instructions"]]:
            if instruction.startswith("ARG "):
                key, separator, value = instruction[4:].partition("=")
                if separator:
                    if key in defaults:
                        require(defaults[key] == value, "ambiguous Docker ARG default")
                    defaults[key] = value
            elif instruction.startswith("ENV "):
                words = shlex.split(instruction[4:])
                environment_names.update(word.split("=", 1)[0] for word in words if "=" in word)
                if words and "=" not in words[0]:
                    environment_names.add(words[0])
        defaults.update(args)

        def source_expand(value):
            names = {match.group(1) or match.group(2) for match in VARIABLE.finditer(value)}
            require(not names & environment_names, "ENV-based source paths require explicit material support")
            return _expand(value, defaults)

        def dependency(source):
            source = _expand(source, defaults)
            if source in stages:
                visit_stage(source)
            elif source != "scratch":
                require(source in contexts, "unbound component Docker source: %s" % source)
                context = contexts[source]
                require(type(context) is str, "context reference must be a string")
                external[source] = context
                if source in artifacts:
                    record = artifacts[source]
                    require(context.startswith(("oci-layout://", "docker-image://")) and
                            context.count("@") == 1 and
                            context.rsplit("@", 1)[1] == record["artifact_digest"],
                            "component context does not use the verified artifact digest")
                    used_artifacts.add(source)
                    # Transport location is not part of the subject's identity.
                    external[source] = {"component": record["component"],
                                        "artifact_digest": record["artifact_digest"]}
                elif context.startswith("target:"):
                    visit_target(context[7:])
                elif context.startswith("docker-image://"):
                    require(context.count("@") == 1, "component base image must be pinned")
                    digest_value(context.rsplit("@", 1)[1], "component base image digest", oci=True)
                else:
                    require("://" not in context and not context.startswith("/"),
                            "unsupported component context source")
                    files.update(_source_files(root, context, directories))

        def visit_stage(stage_name):
            if stage_name in selected:
                return
            require(stage_name in stages and stage_name not in active, "missing or cyclic Docker stage")
            active.add(stage_name)
            stage = stages[stage_name]
            dependency(stage["base"])
            for instruction in stage["instructions"][1:]:
                command = instruction.split(" ", 1)[0]
                require(command in {"ARG", "ENV", "WORKDIR", "COPY", "RUN", "LABEL", "USER", "SHELL"},
                        "unsupported component Docker instruction: %s" % command)
                if command == "COPY":
                    flags, sources = _copy(instruction)
                    if "from" in flags:
                        dependency(flags["from"])
                    else:
                        for source in sources:
                            files.update(_source_files(root, source_expand(source), directories))
                elif command == "RUN":
                    for mount in re.findall(r"--mount=([^\s]+)", instruction):
                        options = dict(item.split("=", 1) if "=" in item else (item, "")
                                       for item in mount.split(","))
                        require(options.get("type", "bind") in ("bind", "tmpfs"),
                                "cache/secret/ssh mounts require explicit component identity support")
                        if "from" in options:
                            dependency(options["from"])
                        elif options.get("type", "bind") == "bind":
                            files.update(_source_files(root, source_expand(options.get("source", options.get("src", "."))), directories))
            active.remove(stage_name)
            selected.add(stage_name)

        visit_stage(definition.get("target"))
        declared = {line[4:].split("=", 1)[0] for key in selected
                    for line in stages[key]["instructions"] if line.startswith("ARG ")}
        from_args = {match.group(1) or match.group(2) for key in selected
                     for match in VARIABLE.finditer(stages[key]["instructions"][0])}
        header_names = declared | from_args
        while True:
            used_header = [line for line in header if line[4:].split("=", 1)[0] in header_names]
            expanded = header_names | {match.group(1) or match.group(2) for line in used_header
                                       for match in VARIABLE.finditer(line)}
            if expanded == header_names:
                break
            header_names = expanded
        recipes[name] = {"dockerfile": dockerfile, "frontend": frontend, "header": used_header,
                         "stages": {key: stages[key]["instructions"] for key in sorted(selected)}}
        target_records[name] = {"args": {key: value for key, value in args.items()
                                         if key in declared | header_names | PROXY_ARGS | {"SOURCE_DATE_EPOCH"}
                                         or key.startswith("BUILDKIT_")},
                                "contexts": external, "target": definition["target"],
                                "platforms": definition["platforms"]}
        visiting.remove(name)
        visited.add(name)

    visit_target(target)
    require(used_artifacts == set(artifacts), "declared component context is not consumed by the graph")
    # Version the material contract when its meaning changes. The implementation
    # of the planner/transport is not a compiler input unless COPY actually uses
    # it. Consumers independently recapture this complete declaration.
    return sorted(files), {"material_model": 2, "root_target": target,
                           "recipes": recipes, "bake_targets": target_records,
                           "directories": directories, "execution": execution}, [
                               dependencies[name] for name in sorted(dependencies)]


def source_closure(root, graph, target, execution):
    """Selection-only source inventory, without artifact or qualification claims."""
    paths, parameters, dependencies = _inventory(root, graph, target, execution)
    require(not dependencies, "source inventory cannot declare artifact subjects")
    return {"parameters": parameters, "files": [file_record(root, path) for path in paths]}


def capture(root, graph, target, component, role, targets, execution, artifacts=None):
    """Capture a source-build closure; resolved graph must come from checked Bake."""
    require(role in ("toolchain-install", "gcc-test-context", "python-row", "qualification"),
            "unsupported component material role")
    paths, parameters, dependencies = _inventory(root, graph, target, execution, artifacts)
    parameters["role"] = role
    return component_inputs.capture(root, component, "qualification" if role == "qualification" else "build",
                                    paths, targets, parameters=parameters, dependencies=dependencies)
