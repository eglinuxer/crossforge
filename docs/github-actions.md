# GitHub-hosted quality and delivery

Crossforge uses GitHub-hosted `ubuntu-24.04` build runners and
`ubuntu-24.04-arm` native release runners. The published SDK remains a single
`linux/amd64` image with both EL8 cross targets.

## Required checks and affected builds

`ci.yml` runs on PRs and manual dispatch, and is callable for quick checks.
PR updates cancel stale runs. `quick` validates locks, configuration,
generated files, unit tests, every shell script, and the Bake and Actions
definitions. `scripts/ci-plan.py` selects a build profile from the complete
Git diff, including deleted paths. Unknown paths and shared inputs select
`full`; manual dispatch without a diff base also selects `full`.
A PR uses the merge base against the checked-out merge commit.

Each main push starts only `candidate.yml`: it calls CI with `quick-only: true`
(the reusable build summary selects no heavy stages and has a separate
concurrency lock from selected builds), then one full
qualification, then candidate publication. No separate push CI duplicates
that qualification. Manual candidate dispatch is a recovery path; do not
start a second candidate for the same SHA while its automatic run is active.

| Changes | Build profile |
| --- | --- |
| Documentation and configuration unit tests only | `none` (quick checks still run) |
| Launcher, packaging, integration or consumer fixtures | `sdk` |
| Python implementation or runtime fixtures | `python` |
| Qt implementation or runtime fixtures | `qt` |
| Locks, release policy, workflows, common scripts, unknown or mixed domains | `full` |

The first implementation deliberately runs all six Python rows for `sdk` and
`python`. Narrowing individual rows needs explicit dependency mapping; it must
not silently miss changes to shared Python logic. Main pushes always qualify the full graph before publishing. Full qualification
also runs daily and manually as a separate replay.

The stable required check is **`pr-required`**. It succeeds only if `quick`
and the reusable build workflow both succeed. That workflow separately checks
that every selected stage succeeded and every unselected stage was skipped;
a cancelled or unexpectedly skipped selected job fails the check. Configure
main branch protection/rulesets to require `pr-required`, require PRs and
restrict bypasses according to the maintainer policy. Workflow files cannot
activate repository branch protection by themselves. Enable the required
check after the changed workflow has produced that check on GitHub.

## Build stages

`verify-builds.yml` is callable only by another workflow. It inherits the
caller's token permissions: CI grants only `contents: read`; the trusted
qualification wrapper grants cache writes. It is shared by CI and release
prequalification.

1. `inputs`: locked RPM/source verification, sysroots and host tools.
2. Two independent toolchain jobs.
3. Six independent dual-target Python rows, plus a separate vcpkg tier3 job.
4. Complete Python/packaging/SDK integration after those jobs succeed.
5. Separate GCC smoke and full jobs after toolchain jobs succeed.
6. Qt-only RPM/source inputs and host xcb preparation, then a host WebEngineCore
   build job and a separate complete host qualification job, in parallel with
   toolchain builds. The complete job reuses the WebEngine build cache and
   still builds all modules, installs and runs all host checks. Both compilation
   logs are included in the existing build-log evidence. This split gives cache
   writers a checkpoint before completing the host build; read-only or cold
   runs without that cache still build the dependency locally. Target runtime jobs wait for both the
   host qualification and toolchains; their Bake graphs still build and verify
   the corresponding target xcb libraries. The all-target xcb group must not
   run before toolchain caches are ready, because it would rebuild both GCCs.
   SDK-only changes do not build these Qt inputs.

The SDK profiles select stages 1–4; the Qt profile selects 1, 2 and 6. Full
qualification selects everything. Matrix jobs use `fail-fast: false` to retain
both target results, with at most two concurrent members per matrix. BuildKit
runs at most one build vertex at a time within each runner. Its Go runtime
uses `GOMEMLIMIT=4GiB` to encourage earlier collection of solver/cache metadata
and leave headroom on the 16 GiB host; this is a
[soft GC target](https://go.dev/doc/gc-guide#Memory_limit), not a hard container
limit or a guarantee against OOM. Compiler jobs keep
their existing limit of four. Cross stages still cannot execute target code.
QEMU smoke and heavy compiler builds do not share a runner.

Each stage executes its concrete Bake roots sequentially on the same builder,
preserving linked dependencies and local cache reuse. A failing root stops the
stage immediately. Sequential roots alone cannot prevent session sharing
between linked Bake targets. BuildKit is pinned to v0.33.0, which includes
[the upstream registry-cache session rebinding fix](https://github.com/moby/buildkit/pull/7047).
This addresses the inactive-session lazy-layer fetch seen in hosted inputs and
vcpkg builds with v0.32.2. Hosted warm-cache runs must verify the fix here.

All build jobs have a six-hour timeout; build commands share a 330-minute budget
(with a further one-minute forced-stop grace) to leave time for diagnostics.
This is a ceiling, not evidence that a
cold stage fits it. Runtime and disk measurements from hosted runs determine
whether inputs, Qt or another stage needs a further split. In particular,
cache import does not solve final SDK image size or source archive disk needs.

## Cache ownership and replay

The dedicated registry cache is
`ghcr.io/eglinuxer/crossforge-buildcache:main-<concrete-bake-target>`.
Buildx resolves group names to concrete targets before exporting caches.
Component targets use `mode=max`; the aggregate `python-dev` and
`sdk-complete-dev` targets use `mode=min` to avoid compressing all intermediate
compiler and Python build trees again on one hosted runner. All build and
qualification dependencies still execute; this only limits exported cache
layers. These final aggregate caches are imported only by their own targets;
they are not propagated to upstream inputs that need intermediate stages.
The first aggregate Python export exhausted the hosted disk with
`mode=max`. See [Docker's registry cache modes](https://docs.docker.com/build/cache/backends/registry/).
Different matrix members have different export destinations. Shared
imports include internal Dockerfile prerequisites and Python row caches. Each
linked target starts with exports from its own Dockerfile; Python row targets
exclude direct exports for other rows. Parent imports also flow to their linked
inputs transitively, because a parent's `mode=max` export contains those input
layers. Consumers also import their transitive dependencies' own caches so
shared stages can resolve those records within the consuming solve. This
addresses the remote-cache pattern described in
[Buildx issue 414](https://github.com/docker/buildx/issues/414); actual reuse
must still be confirmed from hosted build logs. Unrelated targets keep their
own imports. Cache misses execute the original build and qualification graph.

Only `qualification.yml`, on a trusted main push, schedule or manual dispatch, writes
these caches. Candidate prequalification calls that same wrapper. Its shared
concurrency group serializes all writers with `queue: max`, so up to 100
waiting qualifications/candidates are retained rather than replaced by a
new scheduled run. Ordering follows entry into the concurrency queue, not
necessarily workflow dispatch order. See [GitHub's concurrency contract](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
Pinned actionlint 1.7.12 does not yet understand this field; only its specific
unsupported-queue diagnostic is ignored, and a regression requires this exact
setting with cancellation disabled on the qualification workflow. Running
qualification and candidate publication are not automatically cancelled.
PR/manual CI and candidate publication only import caches. The cache writer
also checks the repository, event and ref before allowing an export.

The first cache export creates the GHCR cache package. Configure that package
as public for anonymous PR/fork cache reads and link it to this repository.
Every successful cache writer now checks each exported manifest using an
isolated empty Docker config and the pinned Buildx executable. If anonymous
reads fail, the job fails before dependent stages start and retains
`cache-access.json`. This distinguishes a successful upload from a cache that
PRs can actually use. The report checks manifest availability, not a full
layer download or release qualification. Until anonymous reads work, existing
read-only CI runs may fall back to cold builds. Do not grant
PRs registry credentials to work around a private cache. Keep package write
access limited to trusted qualification. Caches are disposable acceleration;
no cache reference is a release identity or proof of qualification. They are
not installable SDK publications, and normal Bake outputs remain cache-only.

A manual full qualification has a `cold` input. It imports no remote caches on
any fresh runner, but retains within-job BuildKit reuse. This deliberately
measures each stage's cold ability; it can repeat prerequisite compilation
across jobs. A normal qualification uses shared caches. Record and compare
both before declaring the hosted topology qualified. Cold builds can still
export newly built caches for later warm runs.

To inspect a cache override without building or uploading anything:

```sh
python3 scripts/ci-build.py cache --output /tmp/crossforge-cache.json \
  python-matrix gcc-testsuite-full-qualified

docker buildx bake -f docker-bake.hcl -f docker-bake.override.json \
  -f /tmp/crossforge-cache.json --print \
  python-matrix gcc-testsuite-full-qualified
```

## Diagnostics and retries

`ci-build.py` runs the existing heartbeat wrapper, retaining full Buildx output
in `build.log` and printing elapsed time/log size every minute. Every 30 seconds
it records available memory, swap, load and free workspace disk. It records
exit status, elapsed time and source commit in `result.json`; these are
operational observations, not release qualification reports. Per-root Buildx
metadata (`metadata-<target>.json`), the resolved graph and BuildKit container
logs are retained too. Diagnostics capture container state and restart count
before Buildx can restart a stopped daemon, plus available kernel OOM journal
records. This distinguishes a daemon restart under memory pressure from a
transport-only EOF; unavailable diagnostics never replace the build failure.
A failure prints the final 100 log
lines without replacing the original failure code.

An `always()` artifact step uploads diagnostics for seven days, named by
stage, run ID and run attempt. A hard runner loss or timeout can prevent final
observations/upload; already emitted Actions logs remain useful. Files inside
a failed Docker RUN are not automatically exported as standalone test reports.
Use the full build log/build record to investigate those failures.

Use GitHub's rerun-failed-jobs operation for failed stages. Reused cache entries
must still satisfy the current Bake graph and its existing report validators.
Cache misses rebuild; they must not skip a required gate. All-failed/cancelled
and unexpected-skip states remain failures in the summary.

Locked RPM downloads make at most three attempts for transient transport
failures (including TLS `UNEXPECTED_MESSAGE`) and HTTP 408, 429, 500, 502, 503
and 504, waiting two then four seconds. TLS retries use a fresh connection with
normal certificate verification; certificate failures and other TLS errors
remain fatal.
Each attempt starts a fresh temporary file. Content size/checksum failures,
permanent HTTP failures and local filesystem errors remain fatal; retries do
not alter the lock or bypass subsequent signature verification.

## Candidate and stable delivery

`candidate.yml` runs automatically on main pushes, with manual dispatch retained
for recovery. After quick checks it calls the full qualification workflow for the exact
candidate checkout. It then imports those caches into its existing source and
SDK build graph, without changing output, attestation or qualification policy.
The final SDK still validates its GCC full evidence and all existing contracts.
Qt native input also waits for full qualification.

After pushing the unique candidate, anonymous consumers, native AArch64 and
signature checks run as before. Stable promotion and rollback continue to use
digests without rebuilding. A warm build is not a previously qualified image:
only the actual published candidate digest can acquire release evidence.

Push a stable `vX.Y.Z` Git tag to request a formal release. The tag must point
to a commit in main and match `product.version` in that commit's
`config/release.json`; lightweight and annotated tags are supported.
`release-request.yml` finds the newest candidate run for that exact commit.
It accepts only successful upstream main candidates, and does not substitute
an older success for a newer failed or pending attempt. Missing candidates,
missing/expired evidence and failed qualification stop release; no fallback
build is started.

If the candidate is still running, the tag request exits without occupying a
runner. The candidate's successful completion event checks tags at its commit
and resumes promotion. When no release tag exists this event does no publishing.
The coordinator dispatches `promote.yml` on main with the exact candidate run
and existing tag. Promotion revalidates the tag after production approval,
checks all immutable candidate evidence, and assigns version/channel tags to
the same OCI digests without rebuilding SDK or source images. Duplicate
requests remain subject to the existing serialized, idempotent promotion checks.

Tag triggering retains the production environment's required review and its
main-only deployment policy. The production secret `RELEASE_ADMIN_TOKEN`
(repository-scoped fine-grained PAT with Administration read) remains required
for live control-plane verification; a workflow token cannot replace it.
Automatic triggering does not remove this approval or secret requirement.
GitHub permits the coordinator's `GITHUB_TOKEN` to trigger a
[workflow dispatch](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

First deployment acceptance requires a successful warm qualification, a
successful cold qualification, a complete signed candidate run, anonymous
consumer execution, and digest-only stable promotion. Local unit tests,
actionlint and `bake --print` establish wiring, not those execution results.
