#!/usr/bin/env bash
set -Eeuo pipefail

root=$1
mkdir -p "$root"
# Capture state before any Buildx command can bootstrap a stopped builder.
timeout 30s docker ps -aq --filter name=buildx_buildkit > "$root/containers.txt" 2>&1 || true
while IFS= read -r container; do
    [[ "$container" =~ ^[0-9a-f]+$ ]] || continue
    timeout 30s docker inspect --format \
        '{"state":{{json .State}},"restart_count":{{.RestartCount}}}' "$container" \
        > "$root/buildkit-$container-state.json" 2>&1 || true
    timeout 30s docker logs --timestamps "$container" \
        > "$root/buildkit-$container.log" 2>&1 || true
done < "$root/containers.txt"
# Hosted Ubuntu permits noninteractive sudo. Keep failure evidence even when
# the journal is unavailable; diagnostics must never replace the build result.
timeout 30s sudo -n journalctl -k --no-pager --since '-6 hours' \
    --grep='Out of memory|Killed process|oom-kill' > "$root/kernel-oom.log" 2>&1 || true
timeout 120s docker buildx history export --all --output "$root/build.dockerbuild" \
    > "$root/history-export.log" 2>&1 || true
timeout 30s docker buildx du > "$root/buildkit-disk.txt" 2>&1 || true
