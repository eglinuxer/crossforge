# GitHub-hosted quality and delivery

Crossforge uses GitHub-hosted `ubuntu-24.04` build runners and
`ubuntu-24.04-arm` native release runners. The published SDK remains a single
`linux/amd64` image with both EL8 cross targets.

## Required checks and affected builds

`ci.yml` runs on main pushes, PRs and manual dispatch. Shared quick checks live in
`verify-quick.yml`, which is reusable with contents:read and no registry permission.
PR updates cancel stale runs. New main pushes replace outdated selected builds;
quick checks for distinct commits can run immediately. `quick` validates locks, configuration,
generated files, unit tests, every shell script, and the Bake and Actions
definitions. `scripts/ci-component-plan.py` compares immutable base/head Git
snapshots after checking all three generators. It uses the component material
inventory to compare reachable Docker stages, COPY inputs, arguments and pinned
contexts. Deleted and renamed paths are read from both sides of the diff. Missing
baselines, unsupported material syntax, unknown paths or changed root catalogs
select the complete existing SDK/GCC stage set. Qt qualification remains opt-in.
A PR uses the merge base against the checked-out merge commit.

Each main push runs the selected roots and does not publish a public SDK candidate.
Missing internal toolchain components are produced separately for those roots.
Dispatch `candidate.yml` on main when preparing a candidate or release:
it calls `verify-quick.yml` with `plan-components: false`, then the complete component gate selection and candidate
publication. Candidate quick checks, manual CI and push-selected builds have
separate concurrency groups, so development updates cannot cancel a candidate.
Avoid starting a second candidate for the same SHA while one is active.

| Changes | Selection from the current material graph |
| --- | --- |
| Documentation and configuration unit tests only | Quick checks |
| Full GCC baseline bytes | GCC smoke/full evidence roots; no compiler input changes |
| Shared cross-Python build script | All cross-Python builds, affected row gates and SDK |
| Candidate supply policy | Relevant input/policy checks; no compiler input changes |
| SDK acquisition, recovery or orchestration implementation | Canonical SDK roots, with explicit orchestration reasons; compiler inputs change only when their actual materials change |
| cp39 patch and its release pin | Only cp39 native/cross compiler inputs change; several qualification roots still change because they copy the entire release document |
| Unknown path or unavailable comparison base | Complete SDK/GCC stage set |

The detailed `component-plan` artifact records changed files and parameter
identities per root, selected stage targets and any fallback reason. Its compact
job output drives `verify-incremental.yml`. Toolchain, Python and GCC matrices
contain only selected stages; each stage may run a subset of its canonical Bake
roots. Source selection does not authorize component or qualification reuse.
On original-repository main pushes and main manual CI, `verify-main-incremental.yml`
finds the raw component boundaries in the selected graph. It calls
`produce-toolchain.yml` separately for each needed architecture. That workflow
authenticates current input availability, builds and publishes only missing roles,
signs their same-run receipts in an OIDC-only job, then persists the signed catalog
in a separate package-writer job. Existing entries keep their original producer;
they are not re-signed or claimed as new qualification.

The raw toolchain and Python workflows also preserve successful predecessor jobs
when only failed jobs are rerun. Signing receives the original producer invocation,
handoff SHA256 and artifact ID; storage receives the original signer artifact ID
and independent SHA256 values for the catalog and signature bundle. Guards require
the same trusted run/source and ordered attempts (producer ≤ signer ≤ retry)
before downloading artifacts. A signing retry keeps the original receipt producer;
a storage retry checks the exact signed bytes and does not sign again. The pinned
catalog verifier remains mandatory. These jobs and their reusable-workflow callers
grant actions:read for explicit same-run artifact-ID downloads; signer and package
writer permissions remain separate. This path requires successful predecessor job
outputs and uploaded artifacts, and still needs live GitHub retry acceptance.

After all planned producers finish, the wrapper explicitly reduces the actual
consumer jobs to contents:read + packages:read. They capture current inputs,
verify the signed catalog and OCI bytes, and substitute fixed component contexts
in selected toolchain, Python, vcpkg, GCC and SDK stages. These jobs require the
prepared components: a missing index fails with diagnostics instead of compiling
another copy. Authentication, transfer and artifact verification failures also
stop the stage. The main caller grants the maximum writer/OIDC permissions only
to this wrapper; each leaf job receives the permissions for its own operation.
PRs, forks and non-main dispatches use a separate contents:read-only caller and
retain the complete source dependency graph without registry credentials.

The SDK job now calls `run-component-sdk` and `scripts/ci-sdk.py`. It independently
acquires two raw toolchains, thirty raw Python parts and six row qualifications.
Matching signed rows retain their original producer. Only a missing qualification
input index permits fresh row qualification on the SDK worker; missing raw parts,
authentication errors, transfer errors or rejected evidence stop the job.
Physical environment matching remains strict. At most two independent Python
processes qualify missing rows using the same builder; all rows must pass before
the existing SDK executor rechecks their files and freshly runs append/final gates.
Process isolation avoids the documented shared-interpreter mutation hazards of
[`runpy.run_path`](https://docs.python.org/3/library/runpy.html).

This is a transitional consumer route. Existing selected Python jobs and required
status dependencies remain, so their work may overlap the SDK worker's fresh
qualification. The signed `produce-python-row.yml` workflow is not yet called by
the main matrix, and fresh local row artifacts are not published. Candidate
prequalification calls this same build workflow; candidate image publication
retains its existing raw-component route and fresh qualification policy. These
paths still need actual Docker and GitHub acceptance; no speed or disk reduction
has been measured for this change.

Before row execution, SDK diagnostics preserve all thirty-two raw selections in
`execution/component-recovery.json`, plus a separate canonical SHA256 in
`execution/recovery-reference.json`. The job summary identifies the uploaded
diagnostic artifact and SHA256. This is the existing raw recovery format, usable
through the raw recovery interface under its original source/root/environment
checks; it is not a complete six-qualified-row checkpoint. Automatic recovery of
the new SDK job and persistence of its fresh local rows remain rollout work.
OCI blobs and installed trees stay outside the diagnostic artifact.

Python target/runtime/row, GCC and all five vcpkg qualification stages now consume
authenticated scoped policy inputs. Final SDK integration still binds the complete
release document, and GCC aggregate policy still binds smoke/full and both
architectures. The planner preserves these actual dependencies. Full
qualification continues daily, manually and for explicitly requested candidates.
The new dynamic workflow is locally checked but has not yet run on GitHub.

The stable required check is **`pr-required`**. It succeeds only if `quick`
and the selected reusable build workflow both succeed. An intermediate `builds`
gate verifies the event's expected permission branch succeeded and the other was
skipped. The reusable workflow separately checks
that every selected stage succeeded and every unselected stage was skipped;
a cancelled or unexpectedly skipped selected job fails the check. Configure
main branch protection/rulesets to require `pr-required`, require PRs and
restrict bypasses according to the maintainer policy. Workflow files cannot
activate repository branch protection by themselves. Enable the required
check after the changed workflow has produced that check on GitHub.

## Explicit source compiler rebuild

Dispatch `replay-sources.yml` on the original repository's main branch to rebuild
one toolchain or one Python row from its locked source inputs. The workflow runs
quick preflight, uses the existing pinned builder and has only contents:read.
It produces cache-only local results and diagnostics, with no registry export,
catalog signing or component publication.

| Stage | Forced compiler scope |
| --- | --- |
| `toolchain-x86_64` / `toolchain-aarch64` | That architecture's binutils and GCC compilation |
| `python-cp39` through `python-cp314` | That row's build Python and both target CPython compilations |

For local Docker/Bake, the corresponding command is:

```sh
python3 scripts/ci-build.py run python-cp39 \
  --directory /tmp/crossforge-source-replay-new \
  --rebuild-sources --source-builder <pinned-builder-name>
```

Use a new diagnostics directory. The complete canonical stage still runs its
existing gates, but the source observation only asserts fresh execution of its
selected compiler stages. The plan requires their original reachable recipes
and compiler RUNs. It forces every RUN in those stages, then checks owning
BuildKit events, timestamps, cache/failure aliases, current source and actual
execution identity. A successful Docker exit without this evidence fails.
The stage filter uses Docker's documented
[`target.no-cache-filter`](https://docs.docker.com/build/bake/reference/#targetno-cache-filter)
behavior; diagnostics separately establish whether the required RUNs occurred.

Source replay cannot consume replacement build components, export caches or be
combined with qualification replay. It does not force source downloads, prepared
inputs, unselected compilers or all qualification gates. For example, Python row
rebuild does not force GCC; ordinary missing dependencies can still build through
the canonical graph. Optional global `--cold` also removes remote cache imports,
while the explicit source filters remain responsible for proving recompilation.
This is neither a fully empty-cache build nor reusable qualification evidence.
Local graph/fixture checks have passed; actual compiler replay and GitHub event
acceptance remain pending.

## Explicit qualification replay

Dispatch `replay-qualification.yml` on the original repository's main branch and
choose one supported CI stage. Its quick preflight and package reader use read
permissions; it does not publish images, export registry caches or sign results.
The selected stage requires existing authenticated toolchain components and,
for Python/SDK, raw Python components. If one is missing, run normal main CI to
prepare it before replaying. A missing or invalid component cannot silently
fall back to a source compiler build.

| Stage | Forced scope |
| --- | --- |
| `toolchain-x86_64` / `toolchain-aarch64` | That target's static and clean-runtime toolchain gates |
| `gcc-smoke` | Both targets' GCC smoke gates and their final-compiler probes |
| `gcc-full` | x86_64 GCC full gate and its final-compiler probe |
| `python-cp39` through `python-cp314` | That row's two static gates, both runtime tiers per target, row finalizer and SDK append |
| `vcpkg` | Contract plus all three existing locked-source upstream qualification tiers |
| `sdk` | Six row append validations, Python SDK final validation and complete SDK final validation |

The CLI equivalent adds `--replay-qualification` to `ci-build.py run` with
`--require-components` and the normal component reader options; Python/SDK also
requires `--python-components`. It requires the complete canonical roots of the
selected stage. Unsupported stages, partial root selections, `--cold` and cache
writes are rejected. Ordinary `--cold` retains its existing meaning of removing
remote cache imports; it does not prove that tests were newly executed.

The plan derives each required RUN count from the reachable Docker recipe and
sets `no-cache-filter` only on the selected qualification stages. SDK append
steps are forced once in the Python SDK solve, then consumed by the complete SDK
solve. Upstream source compiler stages are not selected for replay. This option
does not implement a clean source rebuild, nor does an SDK-only replay freshly
execute every upstream row qualification.

Diagnostics retain `replay-plan.json`, per-solve raw BuildKit events and
`replay-result.json`. Success requires every owning RUN to complete within this
execution, with no cached/failed alias, missing coverage, changed source graph or
changed observed execution environment. A zero Docker exit with bad evidence
fails CI. These are CI execution observations, not signed reusable qualification
receipts or candidate/native ARM evidence. The workflow still needs actual
GitHub replay acceptance; socketless graph checks cannot provide it.

### Recover the original component selection

Main component consumers and manual replays write `component-recovery.json`
before starting the first Bake solve. It fixes each selected catalog, artifact
and receipt digest, input identity and original producer. The uploaded build
diagnostics artifact remains available for seven days; the job summary reports
its immutable artifact ID, run ID and the recovery document's canonical SHA256.
An acquisition failure before a complete selection produces no recovery document.
A subsequent build failure retains the complete selection for a retry.

To replay using that selection, provide all three optional dispatch inputs:
`recovery-run-id`, `recovery-artifact-id` and `recovery-sha256`. The workflow
downloads the specified artifact from the original repository using read-only
Actions access. Cross-run download requires an explicit token, run and artifact
selection, as documented by the pinned
[download-artifact action](https://github.com/actions/download-artifact/blob/d3f86a106a0bac45b974a628896c90dbdf5c8093/README.md).
Choose the same stage and source commit. If main has advanced, the recovery is
rejected; this entry point does not check out an older source. A partial-root
incremental selection cannot be used for a full-stage manual replay.

For the CLI, add `--record-component-recovery` to record a selection, or pass
`--component-recovery /path/component-recovery.json` together with
`--component-recovery-sha256 <original-canonical-sha256>` to recover one. Both
require the authenticated reader options and `--require-components`; use
`--python-components` when the original selection included Python parts. Use new
diagnostics and acquisition directories for every attempt. A local invocation
without `GITHUB_SHA` binds the source material inventory instead of claiming a
Git commit identity; it still requires authenticated GitHub-produced components.

Recovery verifies the independently selected document SHA256, exact source
inventory, build execution inputs, stage, roots and component set before any
lookup. Each original catalog is then fetched by digest, authenticated again,
and its OCI bytes and receipt checked against current inputs. Missing or changed
catalogs, receipts, artifacts or producers fail; they do not trigger a replacement
producer. Source and execution inputs are rechecked after acquisition and after
successful execution. New local paths and run IDs do not rewrite original
producer records. This preserves raw component selection for CI gate retries;
candidate/source-image recovery and new native ARM evidence remain separate work.

## Build stages

`verify-incremental.yml` serves daily CI and inherits the caller's permissions for
build jobs: contents:read on the ordinary path, plus packages:read only on the
trusted main path. Its plan, input preparation and final summary jobs explicitly
reduce permissions to contents:read. It preserves
phase ordering while allowing deliberately unselected dependencies to be skipped.
Every direct and transitive phase prerequisite is included in `needs`, and a
failed or cancelled prerequisite blocks dependent work. The final gate recomputes
the expected flags and matrices from the selection and rejects missing jobs,
changed outputs, unexpected skips and unplanned successful jobs.

`verify-builds.yml` retains the complete qualification/manual profiles and inherits
the caller's permissions. The trusted qualification wrapper grants cache writes.
Both workflows use the same bounded `ci-build.py` executor and canonical stage
catalog; selected targets are validated against that stage's resolved Bake roots.
The skip/matrix ordering follows [GitHub's job condition semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idif).

The split is at the caller because reusable workflows can only maintain or reduce
their caller's token permissions, as documented in [GitHub's reusable workflow
permission rules](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).
The shared quick workflow has no nested package-reading jobs, so candidate and
pilot preflight remain callable with contents:read only. The component reader
also checks repository, GitHub server, main ref and push/dispatch event before
registry login; read credentials are supplied through stdin and logged out after
the stage. Original catalogs and receipts enter the diagnostic artifact, while
large downloaded OCI layouts stay in a separate temporary directory.

Before enabling this workflow on main, the manual component pilot must establish
the internal package and its repository access, then demonstrate a signed catalog
build/reuse cycle. A missing index is recoverable through source producers; a
registry access error is not an absent index and intentionally fails. This rollout
has local tests and Docker execution evidence, but no live GitHub acceptance yet.

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
qualification selects stages 1–5; Qt runs only through an explicit manual
`qualification.yml` dispatch with `profile=qt`, or local Bake commands. Matrix jobs use `fail-fast: false` to retain
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

Only `qualification.yml`, on a trusted main schedule or manual dispatch, writes
these caches. Candidate prequalification calls that same wrapper. Its shared
concurrency group serializes all writers with `queue: max`, so up to 100
waiting qualifications/candidates are retained rather than replaced by a
new scheduled run. Ordering follows entry into the concurrency queue, not
necessarily workflow dispatch order. See [GitHub's concurrency contract](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
Pinned actionlint 1.7.12 does not yet understand this field; only its specific
unsupported-queue diagnostic is ignored, and a regression requires this exact
setting with cancellation disabled on the qualification workflow. Running
qualification and candidate publication are not automatically cancelled.
Main/PR/manual CI and candidate publication only import caches. The cache writer
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

The source and SDK publication jobs also save the publication Buildx output in
`candidate-build-diagnostics/build.log`, emit heartbeat/log-size updates, and
collect BuildKit state, logs and history on success or failure. Separate
`candidate-source-build-<run>-<attempt>` and `candidate-sdk-build-<run>-<attempt>`
artifacts retain these diagnostics and any Buildx metadata for 90 days. The
publication command's exit status still controls the gate. A runner loss can
prevent collection here too; these artifacts do not replace a successful
publication checkpoint or qualification evidence.

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

`candidate.yml` is manually dispatched on main when preparing a candidate or release.
After quick checks it calls `verify-main-incremental.yml` with an unconditional
`profile: full` for the exact candidate checkout. This selects every canonical
release gate, centrally prepares missing raw toolchain/Python components, and
uses authenticated readers in the consuming jobs. Candidate preparation no
longer joins the shared qualification cache-writer queue. Scheduled/manual
`qualification.yml` cache writers retain their existing serialized policy.
Raw producers accept only the exact main CI caller or a manual main candidate
caller with matching workflow/source SHA; package writers and OIDC signers
remain separate jobs with the existing role-restricted catalog policy.

SDK publication independently authenticates and verifies all required raw
components, reparses the bound Bake graph, and rejects any remaining GCC or
CPython source compiler input. Missing components fail before SDK publication;
they do not trigger a source fallback. `candidate-components.py` binds the
source bundle, source inventory, graph, execution identity and original component
selections, then rechecks them after the image build. Ordinary cache imports
remain available for other dependencies and existing gates. This does not
replace raw components with qualified-row receipts or change qualification policy.
The final SDK still validates its GCC full evidence and all existing contracts.
Qt build/runtime evidence is optional and is not required by candidate signing
or stable promotion. Native AArch64 compiler probes remain mandatory.

After pushing the unique candidate, anonymous consumers, native AArch64 and
signature checks run as before. Stable promotion and rollback continue to use
digests without rebuilding. A warm build is not a previously qualified image:
only the actual published candidate digest can acquire release evidence.

Prepare a candidate first with `gh workflow run candidate.yml --ref main`.
The workflow binds the commit selected at dispatch; later main pushes do not
change that candidate's source identity.

Source publication, SDK publication and final anonymous consumer checks run as
three separate jobs. The first two seal a strict metadata checkpoint after
their push and identity binding succeed. Downstream jobs download the successful
producer's immutable artifact ID and verify its independently supplied canonical
SHA256 before restoring inputs. The checkpoint binds the original source commit,
run/attempt, release, raw OCI index and Buildx metadata, archive identity and
locked SBOM generator report. The SDK checkpoint embeds the original source
checkpoint and preserves its files byte for byte. SDK checkpoint schema 2 also
preserves `component-selection.json`, including original catalog, receipt and OCI
digests and producers. These checkpoints record
publication identity; qualification and anonymous byte retrieval still run in
the downstream gates.

If SDK publication fails after the source checkpoint succeeds, or final consumer
checks fail after the SDK checkpoint succeeds, use `gh run rerun RUN_ID --failed`
to keep those original image digests. The final `publish` consumer job has only
package read permission. The same retry command applies when native ARM or
signing fails. GitHub preserves the
original source commit and ref on a partial retry, as described in its
[rerun documentation](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs?tool=cli).
Native and signing consumers use the successful upstream jobs' immutable
artifact IDs. They do not construct names from the new attempt number. A signing
retry therefore keeps the original candidate/source digests, probe bundle and
successful native report; a native retry executes the real ARM probes again.

The signature artifact includes `candidate-recovery.json`, binding the candidate
manifest SHA256, original probe/report byte hashes, run ID and each final-consumer,
native and signing attempt. Recovery schema 2 also records the original source
and SDK publication attempts and checkpoint SHA256 values; their order must be
source ≤ SDK ≤ final consumer ≤ native ≤ signing. Promotion retrieves the exact latest successful
signing attempt, checks the selected upstream IDs against
[GitHub's artifact metadata](https://docs.github.com/en/rest/actions/artifacts),
then downloads those original artifacts and revalidates all existing semantics
and public signatures. Missing, expired, duplicated, wrong-run or wrong-source
artifacts fail. The records use the
[pinned action's artifact ID support](https://github.com/actions/download-artifact/blob/d3f86a106a0bac45b974a628896c90dbdf5c8093/README.md).

Promotion schema 2 embeds the original producer lineage in
`release-promotion.json`, so the durable archive preserves it after Actions
artifacts expire. Legacy schema 1 and candidates without a recovery record
retain the existing same-attempt contract; no search for an older successful
artifact is introduced. The byte hashes of native probes and report are also
revalidated when creating or reading the durable archive. A recovery record does
not itself establish qualification. Partial retry behavior still needs live
GitHub acceptance; local fixtures are not native ARM execution evidence.

Recovery schema 3 additionally embeds the complete original component selection.
Signing checks its independent SHA256 from the SDK checkpoint and its source
commit before any external signature is made. It travels inside the existing
promotion record and fourteen-file durable archive, so component pins remain
available after the diagnostic artifacts expire. Legacy checkpoint/recovery
schemas remain readable under their original strict contracts.

An image push followed by failure before its producer successfully seals and
uploads the checkpoint is not recoverable through this path. The workflow does
not infer a replacement checkpoint from a tag. Rerunning all jobs starts the
publication jobs again. Recovery across different candidate runs, qualification
receipt reuse, live component-producer retry acceptance and live candidate
acceptance remain pending. Local graph tests use explicit resolver fixtures;
they are not proof of a public candidate build or its performance.

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


## Internal component handoff rollout pilot

`component-pilot.yml` is a separate manual main workflow for the first x86_64
registry handoff. Its default `mode: build` runs the existing quick preflight, publishes the installation
and GCC test-context components to `ghcr.io/eglinuxer/crossforge-components`, then
uses a separate reader job to verify and consume the pinned artifacts. The
reader runs toolchain/runtime and GCC smoke qualification plus the cp39 x86_64
cross build. The raw producer and the separate catalog storage job have package
write permission; the reader has package read permission. Only the catalog
signer has OIDC permission. Transport uses pinned BuildKit and ORAS tools.

The producer job supplies an immutable Actions artifact ID and an independent
canonical handoff SHA256. The reader checks exact source commit, run and attempt,
then recaptures the expected component inputs. It cannot use another run's
receipt or a mutable registry tag as qualification evidence. A partial job rerun
with an older attempt's handoff is currently rejected by this same-run reader.

After successful consumption, the signer creates a canonical catalog and Sigstore
bundle. A separate storage job downloads that exact artifact ID, verifies the
signature with the pinned Cosign verifier and trusted root, and publishes the two
files as an OCI artifact. A full-digest retention tag preserves the catalog before
input lookup tags are updated. Those tags only locate a catalog; consumers must
verify its fixed manifest and blob digests, signature and exact expected inputs.
The lookup CLI also accepts an explicit catalog digest for recovery. No automatic
deletion or expiry of registry catalogs is introduced; candidate/release references
must remain protected when a retention policy is added. The seven-day Actions
artifact is diagnostic output, not the durable catalog store.

Dispatch `mode: reuse` to exercise the catalog consumer in another run. It
discovers each of the two artifacts by independently captured current inputs,
or uses the optional digest-only `catalog-reference` for both. It verifies the
catalog signature, downloads and checks the original component OCI bytes, then
uses the same fresh toolchain/runtime and GCC smoke gates and cp39 cross build.
Original component producer details remain separate from this run's new test
records. This mode has packages:read and skips production, signing and storage;
the final gate checks the exact expected success/skipped jobs for the chosen
mode. A missing input index reports that a producer is required and fails this
read-only pilot. Daily main CI has a separate centralized missing-component path.
Failed authentication, transfer or artifact verification is never a build miss.

This pilot does not yet supply artifacts to `verify-incremental.yml`,
`verify-builds.yml` or candidate qualification. It covers one cross target, not the complete Python
row or final SDK. Production cross-run routing and a successful GitHub-issued
signature remain unverified. Local registry roundtrip and consumer execution are recorded
in [the registry pilot observation](research/registry-handoff-pilot-2026-09-10.json);
the new workflow has not yet been dispatched on GitHub. See
[internal component commands](internal-components.md) for local Docker operation.
