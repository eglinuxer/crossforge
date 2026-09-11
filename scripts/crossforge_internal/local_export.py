"""Bound local OCI extraction; timed-out transfers never supply consumer files."""

import copy
import json
import os
from pathlib import Path
import re
import signal
import subprocess

from .identity import require


TRANSFER_TIMEOUT = 600
STOP_TIMEOUT = 10


def execute(command, directory, timeout=TRANSFER_TIMEOUT):
    """Cancel the complete Docker client process group if a transfer times out."""
    process = subprocess.Popen(command, cwd=str(directory), start_new_session=True)
    try:
        status = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
        finally:
            # Docker may exit before its plugin. Never leave a timed-out
            # Buildx child still writing into the failed attempt directory.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise
    if status:
        raise subprocess.CalledProcessError(status, command)


def extract(graph, target, directory, command):
    """Retry only a timed-out COPY export, with the same immutable OCI input."""
    directory = Path(directory)
    definition = graph.get('target', {}).get(target, {})
    require(set(graph) == {'target'} and set(graph['target']) == {target}, 'extraction graph must have one target')
    require(definition.get('output') == [{'type': 'local', 'dest': str(directory / 'files')}],
            'extraction must write its new local files directory')
    contexts = definition.get('contexts', {})
    require(len(contexts) == 1 and all(re.fullmatch(r'oci-layout://.+@sha256:[0-9a-f]{64}', reference)
            for reference in contexts.values()), 'extraction requires one digest-pinned local OCI input')
    instructions = [line for line in definition.get('dockerfile-inline', '').splitlines()
                    if line.strip() and not line.startswith('#')]
    require(len(instructions) > 1 and instructions[0] == 'FROM scratch' and
            all(line.startswith('COPY --from=' + next(iter(contexts)) + ' ') for line in instructions[1:]),
            'extraction retry cannot run build or qualification instructions')
    for attempt in (1, 2):
        destination = directory / ('files' if attempt == 1 else 'files-attempt2')
        destination.mkdir()
        selected = copy.deepcopy(graph)
        selected['target'][target]['output'][0]['dest'] = str(destination)
        recipe = directory / ('extract.bake.json' if attempt == 1 else 'extract-attempt2.bake.json')
        with recipe.open('x', encoding='utf-8') as stream:
            json.dump(selected, stream, indent=2, sort_keys=True)
            stream.write('\n')
        invocation = command + ['--allow=fs.write=' + str(destination), '-f', str(recipe), target, '--progress=plain']
        try:
            execute(invocation, directory)
        except subprocess.TimeoutExpired as error:
            with (directory / ('export-timeout-%d.json' % attempt)).open('x', encoding='utf-8') as stream:
                json.dump({'attempt': attempt, 'timeout_seconds': error.timeout, 'target': target,
                    'destination': str(destination), 'status': 'timed-out'}, stream, indent=2, sort_keys=True)
                stream.write('\n')
            if attempt == 2:
                raise
            print('Local OCI export timed out; retaining partial files and retrying the same input in a new directory', flush=True)
        else:
            return destination
