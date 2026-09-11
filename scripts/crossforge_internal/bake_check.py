"""Native Docker checks for same-file, scratch-export Bake links.

Buildx 0.36.1 cannot pass a check result through a target: context (upstream
docker/buildx#3992). For checking only, reconnect proven-equivalent local stages.
The production graph is never changed, and every resolved target is checked.
Remove this adapter after the pinned Buildx supports linked check requests.
"""

import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from . import bake_materials
from .identity import parse_json, require


def _merge(left, right, label):
    require(all(key not in left or left[key] == value for key, value in right.items()),
            "conflicting linked check %s" % label)
    return dict(left, **right)


def prepare(root, graph, directory):
    """Write temporary lint recipes; reject links whose equivalence is unknown."""
    root, directory = Path(root), Path(directory)
    targets = graph["target"]
    require(targets, "no resolved Bake targets to check")
    result = copy.deepcopy(graph)
    for name, target in targets.items():
        links = {key: value[7:] for key, value in target.get("contexts", {}).items()
                 if value.startswith("target:")}
        if not links:
            continue
        original = bake_materials.source_closure(root, graph, name, {"purpose": "static-check"})
        parameters = original["parameters"]
        dockerfile = root / target["dockerfile"]
        source = dockerfile.read_text(encoding="utf-8")
        _, stages, _ = bake_materials.recipe(source)
        args, contexts = dict(target.get("args", {})), dict(target.get("contexts", {}))
        replacements = {}
        for context, provider_name in links.items():
            require(provider_name in targets, "missing linked check provider")
            provider = targets[provider_name]
            require(not any(value.startswith("target:") for value in provider.get("contexts", {}).values()),
                    "nested linked checks require explicit support")
            excluded = bake_materials.IGNORED_BAKE_FIELDS | {"args", "contexts", "target"}
            require({key: value for key, value in target.items() if key not in excluded} ==
                    {key: value for key, value in provider.items() if key not in excluded},
                    "linked checks require the same Dockerfile, context and execution options")
            exported = provider["target"]
            block = stages[exported]["instructions"]
            require(block[0] == "FROM scratch AS " + exported and len(block) > 1 and
                    all(line.startswith("COPY ") for line in block[1:]),
                    "linked check provider must be a COPY-only scratch export")
            args = _merge(args, provider.get("args", {}), "arguments")
            contexts = _merge(contexts, provider.get("contexts", {}), "contexts")
            selected = parameters["recipes"][name]["stages"]
            matches = []
            for stage, lines in selected.items():
                for line in lines:
                    if re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(context) + r"(?![A-Za-z0-9_.-])", line):
                        require(line == "FROM %s AS %s" % (context, stage),
                                "linked check context must be a plain FROM alias")
                        require(list(stages).index(exported) < list(stages).index(stage),
                                "linked export must precede its local alias")
                        matches.append(line)
            require(matches, "unused linked check context")
            for line in matches:
                replacements[line] = line.replace("FROM " + context + " AS ", "FROM " + exported + " AS ", 1)

        # Adding provider arguments or image bindings must not alter the original
        # consumer OR any provider closure, including implicit BuildKit/proxy args.
        combined = copy.deepcopy(graph)
        for member in [name, *links.values()]:
            combined["target"][member]["args"] = args
            combined["target"][member]["contexts"] = _merge(
                combined["target"][member].get("contexts", {}),
                {key: value for key, value in contexts.items() if not value.startswith("target:")},
                "contexts")
        require(bake_materials.source_closure(root, combined, name, {"purpose": "static-check"}) == original,
                "merged arguments or contexts change a linked source closure")

        # Keep line numbers, comments, frontend directives and Docker ignore rules.
        for before, after in replacements.items():
            source, count = re.subn(r"^" + re.escape(before) + r"$", after, source, flags=re.M)
            require(count == 1, "linked FROM must occupy one unique physical line")
        destination = directory / (name + ".Dockerfile")
        destination.write_text(source, encoding="utf-8")
        ignore = Path(str(dockerfile) + ".dockerignore")
        if ignore.exists():
            shutil.copyfile(ignore, str(destination) + ".dockerignore")
        adapted = result["target"][name]
        adapted["dockerfile"] = str(destination.resolve())
        adapted["args"] = args
        adapted["contexts"] = {key: value for key, value in contexts.items() if key not in links}
    require(not any(value.startswith("target:") for target in result["target"].values()
                    for value in target.get("contexts", {}).values()), "unresolved linked check context")
    return result


def check(root, selected, builder=None):
    command = ["docker", "buildx", "bake"]
    if builder:
        command += ["--builder", builder]
    graph = parse_json(subprocess.check_output(command + ["--print", *selected], cwd=root))
    with tempfile.TemporaryDirectory(prefix="crossforge-bake-check-") as directory:
        adapted = prepare(root, graph, directory)
        path = Path(directory) / "check.bake.json"
        path.write_text(json.dumps(adapted), encoding="utf-8")
        names = sorted(graph["target"])
        print("Native Docker checks (including linked providers): " + ", ".join(names), flush=True)
        return subprocess.call(command + ["-f", str(path), "--allow=fs.read=" + directory,
                                          "--check", *names], cwd=root)
