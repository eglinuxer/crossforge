# Internal component handoff

Component producers use the canonical Docker/Bake build stages and export OCI
layouts with embedded contracts. They support a single local `docker-container`
BuildKit node. Both toolchains, all six Python rows, qualified-row consumption,
and digest-preserving registry transfer are implemented. Main CI prepares missing
components and integrates verified rows; candidate publication independently
consumes raw components and retains its full qualification gates. Live GitHub
signing, cross-run reuse and candidate execution still require acceptance.
See the [acceptance index](research/ci-refactoring-acceptance.md) for the exact
source versions and completed local Docker gates.

## Selecting changed source work

Inside the local Docker tooling environment, run the planner from the repository
with full Git commit IDs and the pinned Buildx CLI available:

```sh
python3 scripts/ci-component-plan.py select \
  --base "$BASE_COMMIT" --head "$HEAD_COMMIT" \
  --output /output/component-plan.json
```

The command exports temporary base/head source snapshots, checks the generators,
and compares the same material closures used by the component interfaces. It
prints a compact `plan=...` job output and saves detailed reasons to a new JSON
file. Omitting the base requests conservative full selection. A source snapshot
with unsupported materials also selects full work instead of an empty matrix.
No Docker daemon, registry credentials, or artifact publication is required for
this selection command; `bake --print` resolves the checked graph locally.

`source_closure` exposes selection-only files and parameters; it has no artifact
digest or qualification assertion. Existing `capture` identities retain material
model 2. A selected consumer still needs independent receipt, expected-input and
digest verification before artifact use. Trusted main CI prepares missing raw
toolchains centrally and consumes their verified digests. PRs retain source
dependencies. See [the rollout observations](research/incremental-plan-2026-09-10.json)
for the remaining broad qualification dependencies.

## Identities and roles

`scripts/component-artifact.py` keeps three boundaries separate:

- `component-inputs.schema.json` binds source files and modes, selected Docker
  instructions, resolved Bake arguments and image contexts, and the observed
  BuildKit execution identity. The source commit is provenance, not a build key.
- `component-artifact.schema.json` embeds those inputs, the artifact role, and
  the original producer in `/component/contract.json`.
- `component-receipt.schema.json` binds that contract and metadata bytes to the
  actual OCI root, platform manifest, and config digests. The receipt SHA256 is
  over canonical JSON, not the formatting of `receipt.json`.

`toolchain-install` contains `/opt/crossforge/` from the corresponding build
export. `gcc-test-context` contains the prepared GCC source and GCC build tree;
it is a separate component and does not substitute for an installation. Each
component covers exactly one cross target. The producer strips registry outputs,
cache exports, and tags, and writes only a new local OCI directory.

A receipt proves byte and input bindings once its provenance is trusted. It
does not certify ABI, GCC full, Python, or release qualification. Existing
qualification gates remain required. A consumer must obtain the expected receipt
SHA256 through its own trusted handoff and independently capture the full current
input set. Reading a SHA256 from the same untrusted artifact is not that handoff.

## Local Docker workflow

Incremental selection uses runtime source dependencies and explicit controller
mappings. A file read only by `platform-python-check` has syntax coverage, not
an established runtime scope. Changes to such unmapped host controllers select
the complete CI graph. The SDK and Python installation controller mappings keep
their narrower scope; ordinary build inputs still select their actual consumers
even when the syntax check also reads them.

Run the CLI inside the local Docker tooling environment with the Docker CLI,
pinned Buildx plugin, and access to the task's builder. Keep source mounted read
only. OCI paths must be visible at the same absolute paths to the producer and
consumer. Use new output directories so a failed or repeated invocation cannot
replace the original record.

Local OCI metadata and qualified-row extraction allow ten minutes per transfer.
A timeout stops the Docker client and its Buildx children, retains the partial
directory, and retries once into a new directory with the same digest and COPY
recipe. Other export errors fail immediately. Both attempts remain available
until normal staging cleanup; row CI preserves their recipes and timeout records
in its diagnostics. Consumers accept only the successful directory and still perform
the original byte, contract, report, and installed-tree checks. This retry does
not rerun qualification or turn an interrupted producer into a successful one.

First run the three release renderers with `--check`, then obtain the resolved
canonical graph and actual execution identity:

```sh
docker buildx bake -f docker-bake.hcl -f docker-bake.override.json \
  --print toolchain-x86_64-build-export > /output/source-graph.json
python3 scripts/component-artifact.py execution-identity \
  --builder component-producer > /output/execution.json
```

Create a producer JSON with `kind: local`, the original `source_commit`, an honest
`source_dirty` boolean, a unique `urn:crossforge:local:...` invocation, and a UTC
`started_at` such as `2026-09-10T00:00:00Z`. Source bytes and directory modes must
stay fixed throughout the build. For CI-like local snapshots, normalize files to
their Git executable mode and directories to `0755` before capturing inputs.

```sh
python3 scripts/component-artifact.py produce-toolchain \
  --source /source --graph /output/source-graph.json \
  --arch x86_64 --role toolchain-install \
  --execution /output/execution.json --producer /output/producer.json \
  --output /output/toolchain-install --builder component-producer
```

The command validates the running BuildKit environment, builds the component,
recaptures the source closure, verifies the OCI blobs, and uses a COPY-only
BuildKit graph to read its embedded contract. It emits the receipt path,
canonical receipt SHA256, input SHA256, and actual OCI identities. An interrupted
invocation leaves its directory for diagnosis. `plan-toolchain` emits the inputs,
contract, and Bake graph without building.

For a GCC test context, use `--role gcc-test-context` and obtain the source graph
with `--print gcc-x86_64-test-context-export`. ARM uses `--arch aarch64` and the matching target names.
Consumers must verify each role separately before assembling their test stage.

On the consumer, capture expected inputs from its independently checked source
graph and the producer execution policy, then verify the trusted receipt:

```sh
python3 scripts/component-artifact.py toolchain-inputs \
  --source /source --graph /output/source-graph.json \
  --arch x86_64 --role toolchain-install \
  --execution /output/execution.json > /output/expected-inputs.json
python3 scripts/component-artifact.py verify-local \
  --receipt /output/toolchain-install/receipt.json \
  --receipt-sha256 "$TRUSTED_RECEIPT_SHA256" \
  --expected-inputs /output/expected-inputs.json --role toolchain-install \
  --layout /output/toolchain-install/oci \
  --frontend "$PINNED_DOCKERFILE_FRONTEND" --builder component-consumer \
  --consumer-target cpython-cross-cp39-x86_64 \
  --context-name crossforge_toolchain > /output/consumer.bake.json
docker buildx bake -f docker-bake.hcl -f docker-bake.override.json \
  -f /output/consumer.bake.json cpython-cross-cp39-x86_64
```

Verification hashes the selected OCI manifest/config/layer blobs and extracts
metadata with BuildKit; Python does not reimplement tar, whiteout, or symlink
application. A wrong digest, role, target, recipe, sysroot input, or metadata file
fails before an override is emitted. The override refers to an immutable platform
manifest; it has no fallback to a tag or source-build target.

The canonical toolchain and GCC qualification stages now expose these inputs:

| Consumer | Named component contexts |
|---|---|
| `toolchain-x86_64-dev`, `runtime-smoke-x86_64` | `crossforge_toolchain_x86_64_install` |
| `toolchain-aarch64-dev`, `runtime-smoke-aarch64` | `crossforge_toolchain_aarch64_install` |
| `gcc-testsuite-x86_64-smoke`, `gcc-testsuite-x86_64-full-qualified` | `crossforge_toolchain_x86_64_install` and `crossforge_toolchain_x86_64_test_context` |
| `gcc-testsuite-aarch64-smoke` | `crossforge_toolchain_aarch64_install` and `crossforge_toolchain_aarch64_test_context` |

Each context defaults to its source-built export. Verify each artifact using the
corresponding role and generate a consumer override with that exact context name.
Two override files can be passed to Bake to bind both GCC inputs. The renderer
adds bindings only to targets that actually reach the contexts, keeping exports
independent and avoiding producer self-dependencies. Compiler smoke, clean-runtime
execution, GCC probes, and exact baseline comparisons remain in their existing
qualification stages; an installation export still has no qualification claim.

## Material closure limits

The source inventory follows reachable Docker stages and named Bake target
contexts. Local COPY sources, directory entries and modes, and relevant pinned
image contexts enter the identity. The implementation supports the repository's
current default-escape Dockerfile dialect. Unsupported source syntax, unpinned
images, shadowed stage names, secret/SSH/cache mounts, and unknown Bake fields
fail closed. It is an inventory, not a replacement Docker interpreter.

Material model 2 binds stage-declared arguments, inherited global defaults,
arguments used by selected FROM instructions, proxy arguments, and implicit
frontend arguments such as SOURCE_DATE_EPOCH. Frontend overrides are rejected.
Transport CLI implementation is not a compiler input unless the recipe copies
it; qualification acceptance code is a qualification input. This intentionally
changes the keys from model 1; old receipts are not relabeled. Ignore patterns
are not applied to selected directory copies, so extra ignored files can still
invalidate reuse. The main/PR planner now compares `source_closure` inventories
from checked base/head snapshots. Unknown changes or an unprovable inventory
select full work; artifact reuse still requires independent current-input and
receipt verification. Keep generated files checked before planning, and do not
provide independently edited graphs to trusted producers.


## Fresh qualification and verified prior execution

Use `qualification-execution-identity` to observe the BuildKit policy together
with Docker/kernel/CPU identity and the builder container's resource and security
configuration. The producer checks that identity before and after execution.
CPU features, kernel versions, or runner resource configuration differences
invalidate this local qualification identity. The environment model supports the
single local docker-container boundary; it is not a portable claim of equivalent
native ARM hardware or a substitute for candidate integration.

Supported profiles are `toolchain`, `gcc-smoke`, and x86_64-only `gcc-full`.
The first needs an installation subject; GCC profiles require the installation
and its distinct test context. Obtain the checked canonical graph including the
qualification root and the corresponding subject producer targets:

```sh
docker buildx bake -f docker-bake.hcl -f docker-bake.override.json \
  --print gcc-testsuite-x86_64-smoke toolchain-x86_64-build-export \
  gcc-x86_64-test-context-export > /output/qualification-graph.json
python3 scripts/component-artifact.py qualification-execution-identity \
  --builder component-consumer > /output/qualification-execution.json
```

Create `/output/subjects.json` with exactly the required roles. Replace the
receipt hashes below with values received through your trusted local handoff:

```json
{
  "toolchain-install": {
    "receipt": "/output/toolchain-install/receipt.json",
    "receipt_sha256": "<independently trusted canonical SHA256>",
    "layout": "/output/toolchain-install/oci"
  },
  "gcc-test-context": {
    "receipt": "/output/gcc-test-context/receipt.json",
    "receipt_sha256": "<independently trusted canonical SHA256>",
    "layout": "/output/gcc-test-context/oci"
  }
}
```

```sh
python3 scripts/component-artifact.py produce-qualification \
  --source /source --graph /output/qualification-graph.json \
  --arch x86_64 --profile gcc-smoke \
  --execution /output/qualification-execution.json \
  --subjects /output/subjects.json --producer /output/producer.json \
  --output /output/gcc-smoke --builder component-consumer
```

The command independently recaptures the subject producer inputs and verifies
both OCI layouts before replacing named contexts. The qualification input
closure stops at those verified component digests, retaining their input hashes
as dependencies. It binds test scripts, policy, baselines, report validators,
report copy paths, and the observed execution environment.

The producer applies stage-specific `no-cache-filter`, preserves raw BuildKit
JSON progress, and requires every RUN in the selected qualification stages to
complete without a cache hit or error during the recorded interval. Compiler
source stages are outside this graph. RUN cache does not establish fresh test
execution; this behavior follows Docker's documented [cache invalidation rules](https://docs.docker.com/build/cache/invalidation/).

The existing toolchain report validator is shared with final SDK validation
through `toolchain_report.py`, without importing SDK/Python-row policy into the
report module. GCC reports are regenerated from raw summaries and the exact
frozen baseline, then compared using canonical JSON; tested policy and make-log
bindings are also checked. Unknown/changed execution records fail verification.

Only after those checks does the producer seal a scratch OCI artifact containing
`component/contract.json`, `component/qualification.json`, the original
`component/execution.jsonl`, and the explicit reports/logs. The external generic
receipt binds all these bytes. A role label alone does not assert qualification;
use the qualification-specific verifier:

```sh
python3 scripts/component-artifact.py verify-qualification \
  --source /source --graph /output/qualification-graph.json \
  --arch x86_64 --profile gcc-smoke \
  --execution /output/qualification-execution.json \
  --subjects /output/subjects.json --builder component-consumer \
  --receipt /output/gcc-smoke/receipt.json \
  --receipt-sha256 "$TRUSTED_QUALIFICATION_RECEIPT_SHA256" \
  --layout /output/gcc-smoke/oci --temporary-parent /output
```

It independently recaptures expected inputs, extracts and verifies all metadata,
revalidates reports and raw execution events, and returns
`mode: verified-prior-execution` with the original producer and execution times.
It does not relabel a prior run as a new execution. `qualification-inputs` performs
the same subject verification and emits only the expected input document.

The producer always replays the selected tests; prior execution is consumed only
through the separate verifier. An interrupted attempt retains its own output
folder for diagnosis and cannot overwrite a previous receipt. Local receipt
verification does not establish trusted GitHub provenance, registry retention,
full SDK integration, or the required new-candidate native ARM execution.


## Registry transfer and the CI pilot

`component-registry.py` uses the content-locked ORAS tool in
`.github/locked-tools/oras.json`. This is CI tooling policy, like the existing
Buildx/BuildKit action pins; it does not change SDK product version inputs.
The installer verifies both the release archive and extracted executable hashes,
and extracts only the expected regular binary. The executable is checked again
before transfer.

```sh
python3 scripts/component-registry.py install-tool --output /output/oras-tool
python3 scripts/component-registry.py publish \
  --receipt /output/toolchain-install/receipt.json \
  --receipt-sha256 "$TRUSTED_COMPONENT_RECEIPT_SHA256" \
  --expected-inputs /output/expected-inputs.json --role toolchain-install \
  --layout /output/toolchain-install/oci \
  --repository ghcr.io/eglinuxer/crossforge-components \
  --oras /output/oras-tool/oras --builder component-producer \
  --frontend "$PINNED_DOCKERFILE_FRONTEND"
```

The publisher first verifies the trusted receipt, independently supplied expected
inputs, OCI bytes and extracted metadata. It then uses upstream
[ORAS OCI-layout copying](https://oras.land/docs/commands/oras_cp/) to transfer the
existing graph and verifies the remote root manifest bytes. No Docker build or
image exporter changes the sealed manifest during transfer. The returned
`reference` is digest-only; `retention_tag` is derived from the full root digest
and is used to keep the artifact reachable, not as a consumer identity.

```sh
python3 scripts/component-registry.py fetch \
  --receipt /output/toolchain-install/receipt.json \
  --receipt-sha256 "$TRUSTED_COMPONENT_RECEIPT_SHA256" \
  --expected-inputs /output/expected-inputs.json --role toolchain-install \
  --reference "$TRUSTED_COMPONENT_REGISTRY_REFERENCE" \
  --output /output/downloaded-toolchain \
  --oras /output/oras-tool/oras --builder component-consumer \
  --frontend "$PINNED_DOCKERFILE_FRONTEND"
```

Fetching requires a new local directory and an exact registry digest matching
the trusted receipt. It verifies the downloaded blobs and metadata before
returning `local_reference`, suitable for a Bake named context. The registry and
local references identify the same image bytes. Registry authentication uses
`--registry-config /path/to/config.json`; tokens are never passed as CLI arguments.
HTTPS is the normal transport. `--loopback-http` is restricted to localhost or
127.0.0.1 for local Docker registry experiments.

The generic transfer interface does not establish report qualification. Run
`verify-qualification` with independently recaptured qualification inputs after
fetching a qualification artifact. Receipt trust also remains independent from
the OCI transport path.

`.github/workflows/component-pilot.yml` is a manually dispatched, main-only
rollout pilot. Its default `build` mode publishes x86_64 installation and GCC test-context
artifacts to the separate internal package repository. It passes the small
handoff document via an immutable Actions artifact ID and its canonical SHA256
via a producer job output. The consumer has packages:read, verifies that
independent digest and the exact clean commit/run/attempt, recaptures component
inputs, and downloads both OCI artifacts. It freshly executes toolchain/runtime
and GCC smoke gates, then builds cp39 x86_64 from the verified installation.
Every selected job must succeed; skipped or cancelled jobs fail the final gate.

The job-output handoff remains limited to the same run and attempt. After the
consumer gates succeed, a separate job signs a canonical component catalog with
the existing pinned, TUF-authenticated Cosign tool. Only that job has
`id-token:write`; it has no registry write permission. Catalog construction
rechecks the independent handoff digest and exact clean checkout/run/attempt.
Every entry must retain the catalog producer, original receipt and exact
internal registry digest. A catalog cannot reissue another run's receipts as its
own. The final workflow gate also requires signing and immediate verification
to succeed in build mode.

## Authenticate a catalog from an earlier run

`component-catalog.py` treats downloaded catalogs and Sigstore bundles as
untrusted. It invokes the consumer's pinned Cosign executable with its pinned
trust root, exact pilot workflow/main identity, GitHub issuer, repository,
dispatch event and original source commit. Cosign performs certificate, SCT,
transparency-log and signature verification; none of those checks are disabled.
The flags and their enforcement were checked against the pinned upstream
[certificate options](https://github.com/sigstore/cosign/blob/v3.1.3/cmd/cosign/cli/options/certificate.go)
and [bundle verification policy](https://github.com/sigstore/cosign/blob/v3.1.3/pkg/cosign/verify.go).
Verification uses private snapshots of the catalog, bundle and root. Duplicate
JSON keys, unknown fields, mixed producers, ambiguous entries, noncanonical
encoding, wrong executable/root hashes and wrong registry digests fail closed.

Capture current expected inputs independently before selecting a reference:

```sh
python3 scripts/component-catalog.py select \
  --catalog /output/prior/catalog.json \
  --bundle /output/prior/catalog.sigstore.json \
  --cosign /output/cosign-tool/cosign \
  --expected-inputs /output/expected-inputs.json --role toolchain-install \
  > /output/selection.json
```

`authenticated-reference` returns an entry with `reference`, `receipt_sha256`
and the full original `receipt`, suitable for the existing registry fetch
interface. A correctly authenticated catalog without an exact component/role/
input match returns `missing`; the planner must schedule its producer. Signature
or structural failures return an error, never a cache miss or an unverified
fallback. The original source commit may differ from the consumer's checkout;
the independently captured material identity must match exactly.

Authentication establishes the source of a receipt digest. Consumers still
verify actual OCI bytes and metadata with `component-registry.py`, and use the
appropriate domain verifier for any qualification artifact. Neither a catalog
entry nor the exported `authentication.json` observation establishes a passed
qualification or substitutes for verifying the signature again. New candidates
still require their own final integration and actual native ARM execution.

The pilot catalogs its two x86_64 build artifacts with the original schema 1
policy. Main CI uses schema 2, limited to raw toolchain installation/test-context
receipts and an exact `produce-toolchain.yml@refs/heads/main` signer, with either
the push or workflow_dispatch event. Unknown workflows/events, qualification
roles, mixed producers and schema-policy confusion fail closed. Fulcio derives
the certificate identity from the actual reusable signing workflow's
[`job_workflow_ref`](https://github.com/sigstore/fulcio/blob/main/docs/oidc.md).
Both formats retain exact repository, issuer, ref and original source commit
verification. The interface and workflow wiring have local regression coverage;
a real GitHub signature and cross-run consumption have not yet been exercised.

## Consume prior catalog components in the pilot

Dispatch `component-pilot.yml` on main with `mode: reuse` to consume the current
input indexes. Set its optional `catalog-reference` to a retained immutable
catalog digest to recover that specific component set. `mode: build` remains the
default; a recovery reference is rejected in build mode. Reuse only has package
read permission and does not produce, sign or publish replacement components.
The final gate validates the exact selected jobs, including the required skips
for the other mode.

The CI entry point is `component-pilot.py consume-catalog --cosign <pinned-tool>`
with the existing `--builder`, `--oras`, `--output` and optional `--docker-config`
arguments. Add `--catalog-reference <digest-only-reference>` for recovery. Like
the original pilot commands, it requires an exact clean main dispatch checkout;
local Docker callers can use the domain resolution and qualification commands
without inventing GitHub provenance.

`component_resolution.toolchain` independently captures the current canonical
toolchain inputs and observes the build environment, authenticates a matching
catalog, fetches its exact OCI reference, and verifies receipt metadata and image
bytes before returning `verified-build-component`. It recaptures the inputs and
environment before sealing the resolution record. Original producer, receipt,
catalog digest and authentication evidence are retained. This entry point resolves
toolchain installation/test-context build artifacts; it does not accept a
qualification role or infer passed tests from a generic receipt.

An absent discovery index returns `build-required` with an input-specific reason
and no usable subject/context. The read-only pilot reports that reason and fails
before consumer gates; main CI separately schedules missing producers. An
explicit recovery reference, invalid signature, failed transfer or invalid
artifact never becomes a request to silently replace the original component.

Both same-run and catalog consumers share the existing fresh toolchain/runtime
and GCC smoke qualification path, followed by the cp39 x86_64 cross build. The
new qualification producer belongs to this run; input components keep their
original producer. Diagnostics preserve the original catalog, bundle, manifest,
inputs and receipts with the new reports, excluding large OCI layouts. This
pilot does not yet assert a complete Python row or final candidate qualification.

## Bind toolchain components into an incremental CI stage

The existing CI build CLI can opt into the same authenticated reader. Run it
inside the Docker tooling environment, with its pinned builder, ORAS and Cosign
available and the registry configuration mounted at the normal Docker location:

```sh
python3 scripts/ci-build.py run toolchain-x86_64 \
  --directory /output/ci-diagnostics/toolchain-x86_64 \
  --component-builder "$COMPONENT_BUILDER" \
  --component-oras /output/oras-tool/oras \
  --component-cosign /output/cosign-tool/cosign \
  --component-directory /output/component-data/toolchain-x86_64
```

All four component options are required together. This incremental interface
rejects `--cold` and `--write-cache`; existing qualification workflows retain
their current execution path. The component data directory must be separate
from uploaded diagnostics, including its parent/child paths.

The binder identifies the canonical installation and GCC test-context edges for
each architecture in the selected Bake graph. It resolves each needed artifact
once and replaces every matching named context only after verification. A missing
index leaves its original producer reachable and records that producer explicitly
in `components/binding.json`; authentication and artifact failures stop the stage.
With `--require-components`, missing producers also stop the stage before Bake
execution. Main CI enables this strict mode after centralized preparation.
Existing selected roots and their gates remain required. Small original catalog,
receipt and input records go into diagnostics; OCI layouts stay outside them.
Component acquisition is included in the stage's recorded elapsed time.

`verify-main-builds.yml` enables this interface under the trusted main
push/dispatch wrapper. The ordinary PR/fork/non-main caller keeps contents:read
and disables component reading. Shared quick checks use `verify-quick.yml` without
registry permissions, including candidate and pilot preflight. The main wrapper
derives needed roles from the canonical graph and calls one producer per selected
architecture. A producer authenticates existing catalog availability without
downloading the large OCI payload; actual byte checks remain mandatory in the
consumer. Only absent indexes trigger build/export/publication. A same-run schema 2
handoff declares one architecture and only its newly produced roles; the upstream
job provides an independent SHA256 and immutable Actions artifact ID. Signing and
catalog storage must finish before selected consumer gates start. Current PRs
receive no new registry credentials. A local Docker execution verified the bound
toolchain stage and absence of source GCC dependencies using independently trusted
local receipts; that execution did not exercise GitHub catalog authentication or
claim fresh qualification from ordinary cache hits.

## Prepare and consume raw Python components in main CI

The shared clean-Rocky runtime roots use the existing authenticated
`rpm/sysroot-<arch>` projection. They no longer copy the full release document or
maintenance RPM plan. `assemble-python-runtime.py` accepts the same
`--release-component`, `--release-component-name` and independent
`--release-component-sha256` tuple as the RPM materializer; default invocation
retains full-release validation. Partial tuples and mixed release/component modes
fail before runtime mutation. The complete bundle is still verified before
selecting the seven runtime RPMs; installation and inventory checks are unchanged.

Overlay evidence schema 2 has an `input_binding` instead of `release_sha256`.
Runtime and final qualification readers authenticate the expected RPM component
from the row/target policy, or derive it from the complete release in legacy mode.
They continue checking base image, target, lock/transaction, selected RPM bytes
and actual runtime inventory. Schema 1 keeps its exact full-release contract and
cannot enter a component-only qualification. The target report chain uses the
row/target policy below; row aggregation uses a separately authenticated row root.
Local graph and regression checks are separate from actual new runtime
qualification, which remains pending.

The main plan also captures raw Python edges from the actual selected Bake graph.
Each selected row needs build Python plus its reached target installation and
build-audit contexts. SDK selection may resolve to individual rows; it does not
automatically request the full matrix. `ci-python.py execution` validates this
plan and `ci-python.py check` rejects missing, cancelled, failed, unexpectedly
executed, or altered matrix results.

`produce-python.yml` calls `ci-python.py ensure` once per needed row, currently
with at most two rows in parallel. It verifies the prepared toolchains, then
resolves build Python and the target parts in dependency order. Only an absent
authenticated input index allows production and publication. Existing components
retain their original receipts and producer. Unlike the toolchain availability
probe, this producer downloads and verifies dependencies because downstream
Python input identities must bind their actual artifact digests.

New raw parts cross the same-run boundary in a strict Python handoff with an
independent SHA256 and immutable artifact ID. Catalog schema 3 permits only
raw Python roles signed by the exact `produce-python.yml@refs/heads/main`
push/dispatch identity. The old pilot and toolchain signing policies remain
separate. Producer/store jobs have package write permission; only the sign job
has OIDC permission. Consumer jobs have contents/package read permission.

Both raw producer workflows support same-run partial signing/storage retries.
The successful producer supplies its original invocation alongside the handoff
digest and artifact ID. `component-catalog.py from-handoff --producer-invocation`
accepts that earlier attempt only in the main toolchain/Python modes, with the
same trusted run and source commit; omission keeps the exact-current-attempt
contract, and the legacy pilot remains unchanged. Catalogs retain the original
producer and receipt bytes. A successful signer supplies the raw catalog/bundle
SHA256 values and its own invocation; storage checks both exact files before
registry credentials, then repeats the existing pinned signature verification.
The required order is producer ≤ signer ≤ current storage attempt. Empty or
altered predecessor outputs fail before artifact download, and explicit artifact
IDs select the original successful artifacts. The metadata helper does not
authenticate signatures or trust a downloaded authentication report.

This recovery requires a successful predecessor and retained artifact; a failure
before the producer/sign job successfully uploads its handoff cannot be recovered
through these outputs. Cross-run recovery and live GitHub signing/storage retry
acceptance remain pending. No raw component is promoted to qualification by retry.

Python component preparation shares the inputs/toolchain prerequisites with
GCC and vcpkg, which can continue independently. Python row consumers and the
SDK wait for the component matrix, then use `--python-components` together with
`--require-components` and the four component options above. Each raw part is
verified against current inputs before all its corresponding contexts are
replaced. The resolver captures producer inputs against the original canonical
graph, even after verified toolchains have been bound for consumers. Missing
dependencies cannot become accepted component subjects.

This path removes source compilation from consumers but keeps their existing
row and SDK gates. It does not reuse a formal row qualification receipt yet,
and ordinary cached gates are not new qualification executions. A local Docker
cp39 consumer verified seven prior local components and completed with no
GCC/CPython source compiler in its material closure. Its registry discovery and
transport were explicit local fixtures, so GitHub signing, remote publication
and cross-run acceptance still require the rollout pilot.

## Python row qualification policy inputs

The component renderer now also emits a qualification policy for each Python
row, a qualification input component for each row/target pair, and one row
aggregate that binds its two targets. The legacy all-row qualification
components remain byte-for-byte compatible. Static compile, runtime, target final
and row aggregation now consume these inputs. Legacy report formats keep their
exact complete-release identities; this migration does not authorize CI report reuse.

`python_qualification_policy.py` reads a target component and its row policy
using one independently trusted target-component digest. It needs only
`release_component.py`, `python_row_contract.py`, and those two projection
files. `from_release` independently derives the expected input policy using
the complete release renderer; a consumer must not take that expectation from
an untrusted report. The returned policy names its own component and contains
the exact Python source/signature policy, target/sysroot, five ABI identities,
clean runtime image and overlay binding, row implementation contract, explicit
ARM QEMU identity, and cp314 zstd component identities.

Product metadata and other Python rows are absent from the target policy. An
architecture-specific ABI/sysroot change affects that architecture and the row
aggregate; QEMU affects ARM; zstd affects cp314. The existing RPM projections
still share base-image manifest inputs, so those changes can affect both
architectures. Component configuration identity is only part of qualification
identity: actual artifacts, copied test/validator code, execution environment,
and authenticated execution evidence remain required by the receipt interface.
The policy input binding cannot claim a complete `release_sha256`.

The static qualifier accepts either the legacy `--release` and two qualification
pins, or `--qualification-components`, its independently trusted target pin,
and `--source-manifest`. Component mode authenticates the source and build-policy
projections through the target/row-policy dependencies before checking the full
prepared source manifest. The Docker stage copies just those four projections
and its ABI inputs; it does not copy the full release, its schema, the renderer,
or the source-to-release binding module. Legacy support is imported only for
the explicit legacy CLI mode.

Compile schema 5, runtime schema 4 and target final schema 5 use the same scoped
`input_binding`. The runtime/final CLI accepts exactly one of `--release` or
`--qualification-components`; component mode additionally requires the independent
`--qualification-component-sha256` pin. Both runtime tiers validate the compile
binding before execution. The finalizer checks all nested bindings and serialized
report bytes along with the existing source, sysroot/transaction, ABI, ELF, guard,
SDK tree, extension, provider, probe and private zstd evidence. ARM continues to
require the exact pinned explicit QEMU executor; x86_64 uses native chroot.

The runtime Docker stage inherits the locked host tool root and explicitly copies
its twelve support scripts. It receives the qualification projections and ABI
files from the static stage, validates row/version/adapter against the trusted
root pin, and does not copy the complete release, schema, renderer or source
bridge. The finalizer can produce the scoped report without loading those files.

Legacy `--release` invocation keeps runtime schema 3 and final schema 4. A legacy
final report can embed compile schema 4 or 5, but both runtime reports must remain
schema 3 with exact full-release identities. A scoped final report requires compile
schema 5, runtime schema 4 and overlay schema 2 throughout; mixed chains are rejected.
The full-release consumer independently derives each scoped policy before accepting
a new target report. Legacy row schema 2 retains the complete-release binding and
independently computed all-row qualification pair. New row schema 3 uses the row
contract below; the assembled SDK still binds its complete release. This does not
rebind old evidence or establish cross-machine qualification reuse.

Docker contract tests cover the producer/consumer boundaries and a cropped runtime
stage with no release renderer; Rocky platform-python compatibility is checked
separately. Bake material captures show that changing the product version leaves
all twelve target runtime qualification inputs unchanged, changing cp39 source
affects only its two targets, and changing x86_64 ABI or QEMU executor affects only
the corresponding six targets. These are input-scope checks, not new qualification
executions or measured CI speedups. Actual new qualification runs and formal CI
reuse acceptance remain in progress.


### Component-bound row aggregation

`python_row_policy.py` authenticates `python/<row>-qualification` using the
producer's independent `--qualification-component-sha256`. Its two exact target
dependencies authenticate their policies and agree on row implementation, source,
source/build-policy references and support status. The source preparer's existing
reader authenticates the shared raw source/build-policy pair and constructs the
exact expected source manifest. No source/patch parser is duplicated, and no
partial release object is created. A row consumes six projection documents.

`cpython-row-assemble` inherits the locked host build-tool root and explicitly
copies fourteen Python files. It receives target policy projections and ABI files
from both qualified targets, plus the pinned row root. It does not inherit the
legacy full-release host. The prepared source must be schema 2, each target final
report must be schema 5, and the existing installed-file, ELF, ABI, build-Python,
zstd and report validations still run. New row schema 3 replaces `release_sha256`
and the all-row qualification pair with `crossforge-python-row-input-binding`.
The binding is configuration identity; actual artifacts and execution provenance
remain bound by the separate formal qualification receipt.

Bare `--release` invocation preserves row schema 2 and its original behavior.
Formal row receipt inspection passes `--release` and `--row-manifest` together:
the supplied manifest chooses only the format to verify, never the trusted
digest. For schema 3, that inspector derives the expected row policy independently
from the complete release, checks the prepared source, both target reports and
actual installed files, then compares the complete recomputed manifest. SDK
append derives its expectation from the row's six authenticated component
projections. Both paths retain their additional byte-for-byte output comparison.
Final SDK integration independently checks the scoped row metadata/binding before
executing its host Python checks; its own report still binds the full release.

Local Docker fixtures cover six-row production/consumption and rejection of
changed source, policy, report, files, extra fields and mixed legacy claims. The
cropped row-stage import/input test has no renderer or source-to-release bridge.
Target execution bodies and ELF tools in row aggregation fixtures remain mocked;
the full target-validator regressions are separate, and none of these fixtures
constitutes a new qualification run. Material captures over normalized snapshots
show product-version changes no longer invalidate any row, cp39 source affects
only cp39 and private zstd affects only cp314. Shared target ABI/QEMU changes
still affect every row containing that target. Remote CI production/reuse
acceptance, physical execution-environment policy and complete replay acceptance
remain open.

### Qualified row CI handoff and lookup

`ci-python-row.py` and the reusable `produce-python-row.yml` provide the producer
boundary for a complete qualified row. Main now calls this workflow for each
selected independent Python row, at a maximum of two concurrent rows. The
candidate SDK publication path remains separate. The producer requires the
existing trusted main caller and exact clean source, acquires both toolchain installations and all five
raw Python parts through authenticated catalogs, and reject a missing raw part.
This job has no implicit GCC/CPython source-build fallback.

`python_row_resolution.resolve` rebinds all seven subjects and captures the
current qualification inputs, including the observed build worker, physical
host and inspection tools. It then looks up the signed catalog, fetches the
digest-bound OCI artifact and invokes `python_qualification.verify_local` to
recheck execution vertices, reports and actual installed bytes. Only an absent
input index requests fresh qualification. Authentication, transfer or row
verification failures remain errors; a missing fixed recovery catalog cannot
fall back to another producer. A successful lookup preserves the original
producer and has mode `verified-prior-execution`.

On a genuine miss, the existing formal row producer freshly runs the static,
runtime and row gates, verifies the sealed artifact, and returns a qualification
receipt. Both fresh and reused rows then pass `python_row_install.execute`, which
reverifies all seven raw subjects and the sealed qualified artifact before
substituting the row into its canonical `python-<row>-dev` graph. The base remains
`sdk-toolchains-dev`; both independent append RUNs must execute freshly, and the
installed row manifest must match the qualified artifact byte for byte. Its
material graph ends at two toolchain digests and one qualified row, with no
GCC/CPython source compiler. Failed installation prevents a new publication,
signed handoff or successful CI result. These independent gates do not replace
the cumulative SDK append/final gates.

Both independent and cumulative append stages authenticate only the selected
row's six policy projections. Rendered arguments pin its source, build policy
and complete row qualification root. The original source-manifest comparison,
duplicate-row rejection, installed-file/ABI checks and byte-for-byte row
manifest comparison remain. Full release validation stays in quick/host and
final SDK boundaries. A cp39-only source or patch change therefore does not
invalidate the five unrelated independent append roots through `release.json`.

The CI adapter checks the planned input identity, receipt, source and environment
before publication. Reused rows generate no new handoff or signature.
New `python_row_handoff.py` binds one exact row, original run/attempt, physical
execution identity, qualified artifact digest and the complete metadata set.

Catalog schema 4 permits only complete Python row qualification receipts from
the exact `produce-python-row.yml@refs/heads/main` signer with the allowed main
event and original source SHA. Existing raw toolchain and Python signer policies
remain restricted to their original roles. `from-handoff --python-row-ci` cannot
consume a raw or pilot handoff. The separate producer, signer and registry writer
jobs retain the same exact artifact-ID/digest retry checks: a later successful
signing/storage attempt preserves the original qualification producer.

Failure diagnostics retain the available input record, BuildKit progress and
qualification reports; installed payloads and OCI blobs are excluded. Diagnostic
source paths reject symlinks. Main waits for the selected row workflow, including
its separate signer/store jobs when a new row is produced. Required status checks
the exact row matrix and rejects missing, cancelled or failed selected work.
Actual GitHub signing/cross-run acceptance and the physical environment policy
remain separate from local regression and Docker installation checks.

### Acquire SDK inputs from signed catalogs

`component-artifact.py acquire-python-sdk` resolves the full `python-dev` or
`sdk-complete-dev` input set without a hand-written local component manifest.
It takes the existing checked `--graph`, observed qualification `--execution`,
`--source` and `--builder`, plus pinned `--oras`/`--cosign` executables, a new
`--component-directory` for OCI data and a separate new `--output` for diagnostics.
The complete canonical SDK chain is checked before registry acquisition. Both
toolchain installations are acquired once and shared by all six rows; every raw
Python part is resolved through the existing dependency-aware reader, then each
complete row is checked through the qualified-row catalog reader.

On success, `components.json` has the same format accepted by the existing local
SDK binder. Missing raw artifacts or row qualifications return exit status 1,
explicit `required_builds` and `required_rows`, and no consumable complete component
file. Independent rows can still be inspected to provide a complete diagnostic
picture. A signature, transport or qualification-verification error aborts rather
than becoming a miss. Acquisition checks the physical environment before and
afterward and retains the original catalog, receipt and producer records. OCI
data cannot overlap the diagnostic directory, including through parent symlinks.

`component-artifact.py execute-python-sdk-catalog` accepts the same arguments and
continues only when acquisition is complete. It invokes the existing SDK executor,
which independently rechecks all local receipts and installed bytes, binds the
eight final artifacts, and freshly runs SDK append/final integration. It keeps
acquisition evidence under `output/acquisition/` and integration evidence under
`output/integration/`; only completed integration writes the outer `result.json`.
Neither command publishes artifacts or implicitly rebuilds missing dependencies.
The existing `bind-python-sdk` and `execute-python-sdk` commands retain their local
manifest interface and all prior checks.

These commands connect the catalog reader to full SDK integration. Main's SDK
job now uses `scripts/ci-sdk.py` to call acquisition and then run missing row
qualifications on the same worker before the existing SDK executor. That CI-only
controller requires the exact trusted GitHub caller and clean source; local
Docker operation continues through the explicit component commands above.
It preserves strict environment matching, shares one builder between at most two
independent row interpreter processes, and stops on missing raw inputs or any
authentication, transport or verification failure. Fresh local rows are not
signed or published. Candidate prequalification shares this job, while candidate
image publication retains its existing raw-component route.

Selected main toolchain, GCC smoke/full and vcpkg jobs use the explicit
qualification replay path. These jobs have no authenticated qualification
catalog, so they re-execute their canonical test stages from verified raw
components and require fresh BuildKit RUN events. They retain the same selected
roots and read-only permissions. Replay observations bind the physical worker
and execution interval; they are not signed reusable qualification receipts.
The PR source-build route continues to exercise the original source graph.
This main-job fallback is separate from candidate publication and does not
establish reusable signed qualification reports.

Candidate publication also uses an explicit fresh-execution fallback. Its graph
directly depends on GCC smoke/full and the complete vcpkg contract/Tier 1–3 chain,
as well as both toolchains, all six Python rows, packaging and final SDK checks.
The candidate consumer verifies thirty-four raw inputs: both toolchain installs,
both GCC test contexts, and thirty Python parts. It forces the declared
qualification stages while allowing caches for material preparation; GCC and
CPython source compilation remain outside the component graph.

`candidate-components.py build` validates the prepared component graph and
physical environment before and after building, retains raw BuildKit execution,
and rejects cached, failed, missing or stale qualification RUNs. Shared RUN
digests retain every checked owning target and count as one physical execution.
The successful execution record binds the emitted candidate digest, source
binding, component selection, inputs and original producer. It does not mint a
reusable component qualification receipt.

SDK publication checkpoint schema 3 carries the execution record, input and
qualification plans, and raw execution alongside the original image metadata.
Sealing and restoration verify this payload before downstream consumer/native
ARM work. Readers retain exact legacy schema 1/2 support; new SDK checkpoints
require schema 3 evidence. This implementation still needs actual candidate
execution and recovery acceptance on trusted main. Local synthetic execution
fixtures establish the failure boundaries, not a published candidate's result.

After a fresh row is sealed and its receipt matches the planned inputs and
producer, the controller removes that row's `payload/` and `extracted/` staging
directories. Diagnostic copies must have succeeded first. It retains the OCI
layout, receipt and input records for the SDK executor's independent acceptance;
qualification failure, mismatched inputs or failed diagnostic preservation keep
the staging trees. This removes local duplicate installations only. It does not
delete registry artifacts or implement reference-aware catalog retention.

When all six rows are acquired from authenticated catalogs, main's SDK acquisition
also saves the complete `32 + 6` recovery document before final integration.
`execution/sdk-recovery-reference.json` holds its independent digest and points to
`execution/acquisition/component-recovery.json`; the diagnostic artifact summary
reports this separately from the raw-only selection. Final integration rechecks
the saved complete selection, current source/root/materials and full physical
execution identity before reporting success. If integration fails, the prior
checkpoint remains available to the existing explicit SDK catalog recovery
interface. Missing or locally qualified unsigned rows do not create a complete
checkpoint or a complete-recovery summary.

The SDK diagnostic artifact still saves the original thirty-two raw selections and
their independent SHA256 before row qualification/final integration. Its summary
provides the artifact ID and document location. This supports the original raw
recovery interface; complete recovery of this CI job's fresh local row results
is not implemented. Selected independent Python jobs now publish qualified rows
through the separate producer workflow; strict environment differences can still
cause the SDK worker to qualify a row again.
Local fixtures use a real parsed Bake graph and the existing SDK input capture,
but explicitly mock registry/domain verification and execution boundaries. Graph
checks confirm the eight-artifact boundary and absence of GCC/CPython source
compilers; actual cross-run retrieval and SDK execution remain acceptance gates.

## GCC qualification policy inputs

GCC smoke/full Docker gates use `run-gcc-testsuite.py --components` with the
existing independently pinned `toolchain/gcc-testsuite-qualification` projection.
The policy reader authenticates its GCC source dependency and, for ARM, its
QEMU executor constraint through the ARM toolchain qualification dependency.
Plan and baseline files still pass the same schema, matrix, canonical digest
and exact result checks. The report component identity and candidate verifier
remain compatible; frozen baselines and upstream test commands are unchanged.

The qualification path no longer reads the whole `release.json` or its schema.
The old `--release` CLI remains supported, and the explicit observation branch
continues to use it. A scoped policy is not a partial release manifest, and
full release validation remains a separate CI/candidate requirement.

This first change removes unrelated release fields from GCC gate inputs. The
existing GCC qualification projection still groups smoke/full and both target
policies; their further separation remains work to do. Socketless graph checks
and policy/CLI checks in Rocky Python 3.6 are not new GCC qualification runs.

## Durable catalog storage and discovery

`catalog_registry.py` stores the exact catalog and signature bundle together as
an OCI artifact in the same internal component repository. It uses the pinned
ORAS [native file packing](https://oras.land/docs/commands/oras_push/) and
[OCI-layout copying](https://oras.land/docs/commands/oras_cp/) without a Docker
build or layer recompression. The envelope has an empty OCI config, a fixed
artifact type, and exactly two named JSON blobs. Unknown fields, extra blobs,
unsafe names, remote URLs and unexpected types/sizes are rejected; downloaded
blobs must match their descriptor digests and sizes. Fetching uses fixed local
filenames and digest-addressed blob requests, without extracting registry-supplied
archive paths.

Run these commands in the Docker tooling environment with the checkout, pinned
tools and desired output directory mounted:

```sh
python3 scripts/component-catalog.py publish \
  --catalog /output/signed/catalog.json \
  --bundle /output/signed/catalog.sigstore.json \
  --cosign /output/cosign-tool/cosign --oras /output/oras-tool/oras \
  --registry-config /tooling/config.json --output /output/catalog-store
```

Publishing first snapshots and authenticates the signed bytes. It retains the
envelope under `catalog-<full manifest SHA256>` and returns its digest-only
`reference`. Only after that succeeds does it update `input-<SHA256>` hints keyed
by component, role and complete input identity. The manifest creation annotation
uses the original producer time so retrying with the same catalog/bundle bytes
preserves the envelope digest. The catalog-store CI job has packages:write but
no OIDC permission; it independently verifies the signer job's bundle before
publishing. The final workflow gate requires durable storage to succeed.

```sh
python3 scripts/component-catalog.py lookup \
  --expected-inputs /output/current-inputs.json --role toolchain-install \
  --cosign /output/cosign-tool/cosign --oras /output/oras-tool/oras \
  --registry-config /tooling/config.json --output /output/catalog-download \
  > /output/selection.json
```

An input tag is an untrusted hint. Lookup reads its manifest once, fixes that
manifest digest, fetches and checks both blobs, then runs the signature and exact
input selection checks described above. A valid catalog under the wrong input
index is rejected. Only the pinned ORAS manifest-not-found response is a
`missing` result; authentication, network, malformed response, digest and signature
failures remain errors. The successful selection includes the original receipt
and `catalog.reference`, which should be retained with the consumer's evidence.

For recovery, add `--catalog-reference "$ORIGINAL_CATALOG_REFERENCE"` to the
lookup command. It bypasses mutable input tags and requires that exact envelope
digest and the same expected inputs. Absence of an explicitly requested recovery
digest is an error. Updating a hint cannot change or replace that recovery input.

Catalogs, bundles and component artifacts retain their full-digest registry tags;
these commands do not delete or expire them. The seven-day Actions files are
diagnostics and handoff conveniences, not the durable copies. No garbage
collector has been introduced. Any future cleanup must protect every catalog,
receipt and component referenced by candidates/releases before removing orphaned
artifacts. Registry capacity measurements and reference-aware cleanup remain
rollout work.

Local Docker tests exercised actual ORAS packing, registry transfers, moving an
input hint while recovering its original digest, and identical-digest repacking.
Those transport fixtures were unsigned. Actual pinned Cosign rejected them at
both public lookup and publish boundaries; no valid GitHub signature is claimed
by the transport experiment.
