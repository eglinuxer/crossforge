"""Bound local OCI extraction; timed-out transfers never supply consumer files."""

import copy
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tarfile

from .identity import require


TRANSFER_TIMEOUT = 600
STOP_TIMEOUT = 10


def unpack(archive_path, destination):
    """Extract one completed transport archive into its empty private directory.

    BuildKit still applies the OCI layers. This only extracts its COPY result;
    the caller must verify the original receipt and installed-tree contents.
    Validate the complete namespace before using Python's upstream extractor,
    including on platform-python versions without tarfile's data filter.
    """
    destination = Path(destination)
    require(destination.is_dir() and not destination.is_symlink() and not any(destination.iterdir()),
            'tar extraction requires a new empty directory')
    with tarfile.open(str(archive_path), mode='r:') as archive:
        members = archive.getmembers()
        paths = {}
        for member in members:
            path = Path(member.name)
            require(not path.is_absolute() and '..' not in path.parts and str(path) not in paths,
                    'unsafe or duplicate export member')
            require(member.isfile() or member.isdir() or member.issym() or member.islnk(),
                    'unsupported export file type')
            require(path.parts or member.isdir(), 'export root must be a directory')
            paths[str(path)] = member
        for name, member in paths.items():
            for parent in Path(name).parents:
                require(str(parent) not in paths or paths[str(parent)].isdir(),
                        'export member traverses a non-directory')
            if member.issym():
                link = Path(member.linkname)
                require(not link.is_absolute() and bool(member.linkname), 'unsafe export symlink')
                resolved = (destination / Path(name).parent / link).resolve()
                require(destination.resolve() in (resolved,) + tuple(resolved.parents),
                        'export symlink escapes the destination')
            elif member.islnk():
                link = Path(member.linkname)
                require(not link.is_absolute() and '..' not in link.parts
                        and str(link) in paths and paths[str(link)].isfile(),
                        'export hardlink must reference a regular archive member')
        # Files and directories precede links, so extraction never writes
        # through an archive-created symlink or order-dependent hardlink.
        ordered = [copy.copy(member) for member in members if not (member.issym() or member.islnk())]
        ordered += [copy.copy(member) for member in members if member.islnk()]
        ordered += [copy.copy(member) for member in members if member.issym()]
        for member in ordered:
            member.uid = member.gid = member.uname = member.gname = None
        options = {'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}
        archive.extractall(str(destination), members=ordered, **options)
        for name, member in paths.items():
            path = destination / name
            if member.issym():
                resolved = path.resolve()
                require(destination.resolve() in (resolved,) + tuple(resolved.parents),
                        'export symlink chain escapes the destination')
        # Newer tarfile data filters intentionally narrow modes. The receipt
        # verifier needs the original modes, including private report files.
        for name, member in sorted(paths.items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
            if not member.issym():
                os.chmod(str(destination / name), member.mode)
                os.utime(str(destination / name), (member.mtime, member.mtime))


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
    """Transfer a COPY result as a tar stream, then verify it through the caller.

    Avoid the per-file local-export receive protocol that can stall on large
    rows. Retry only timeouts, using the same immutable OCI input and a new
    archive/directory; a partial archive never supplies consumer files.
    """
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
        archive = directory / ('export.tar' if attempt == 1 else 'export-attempt2.tar')
        require(not archive.exists() and not archive.is_symlink(), 'export archive already exists')
        selected = copy.deepcopy(graph)
        selected['target'][target]['output'] = [{'type': 'tar', 'dest': str(archive)}]
        recipe = directory / ('extract.bake.json' if attempt == 1 else 'extract-attempt2.bake.json')
        with recipe.open('x', encoding='utf-8') as stream:
            json.dump(selected, stream, indent=2, sort_keys=True)
            stream.write('\n')
        invocation = command + ['--allow=fs.write=' + str(archive), '-f', str(recipe), target, '--progress=plain']
        try:
            execute(invocation, directory)
        except subprocess.TimeoutExpired as error:
            with (directory / ('export-timeout-%d.json' % attempt)).open('x', encoding='utf-8') as stream:
                json.dump({'attempt': attempt, 'timeout_seconds': error.timeout, 'target': target,
                    'destination': str(destination), 'archive': str(archive), 'status': 'timed-out'},
                    stream, indent=2, sort_keys=True)
                stream.write('\n')
            if attempt == 2:
                raise
            print('Local OCI export timed out; retaining partial files and retrying the same input in a new directory', flush=True)
        else:
            unpack(archive, destination)
            return destination
