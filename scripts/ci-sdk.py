#!/usr/bin/env python3
"""Qualify missing rows and integrate main's SDK using prepared raw components."""

import argparse
import json
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time

from crossforge_internal import ci_sdk, component_build, python_sdk_catalog
from crossforge_internal.identity import IdentityError, parse_json, require


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "qualify-row":
        parser = argparse.ArgumentParser(description="Run one SDK row in an independent interpreter", allow_abbrev=False)
        parser.add_argument("--request", type=Path, required=True)
        parser.add_argument("--request-sha256", required=True)
        worker = parser.parse_args(argv[1:])
        try:
            ci_sdk.qualify_request(worker.request, worker.request_sha256)
            return 0
        except (IdentityError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            print("error: %s" % error, file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--targets-json", required=True)
    parser.add_argument("--builder", required=True)
    parser.add_argument("--oras", type=Path, required=True)
    parser.add_argument("--cosign", type=Path, required=True)
    parser.add_argument("--docker-config", type=Path)
    parser.add_argument("--component-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    stop, monitor = threading.Event(), None
    started, status = time.monotonic(), 1
    try:
        targets = parse_json(args.targets_json)
        ci_sdk.selected_root(targets)
        python_sdk_catalog.directories(args.component_directory, args.output)
        require(not any((args.output.parent / name).exists() or (args.output.parent / name).is_symlink()
                        for name in ("run.json", "resources.jsonl")), "SDK run diagnostics must be new")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        build = runpy.run_path(str(ROOT / "scripts/ci-build.py"))
        monitor = threading.Thread(target=build["monitor_resources"], args=(args.output.parent / "resources.jsonl", stop), daemon=True)
        monitor.start()
        value = ci_sdk.execute(ROOT, targets, args.component_directory, args.output, args.builder, args.oras, args.cosign, args.docker_config)
        print(json.dumps(value, sort_keys=True, indent=2))
        status = 0
    except (IdentityError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print("error: %s" % error, file=sys.stderr)
    finally:
        stop.set()
        if monitor is not None:
            monitor.join()
            component_build.write_json(args.output.parent / "run.json", {"exit_code": status,
                "elapsed_seconds": round(time.monotonic() - started, 3)})
    return status


if __name__ == "__main__":
    raise SystemExit(main())
