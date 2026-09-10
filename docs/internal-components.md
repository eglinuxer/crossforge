# Internal component handoff

The local toolchain pilot uses the canonical Docker/Bake build stages and exports
an OCI layout with an embedded component contract. It supports a single local
`docker-container` BuildKit node. Local qualification receipts and explicit prior
report verification are available. Digest-preserving registry transfer and a same-run CI pilot are implemented.
The GitHub pilot event, cross-run trusted reuse, and candidate consumption still
require validation or implementation.

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
digest verification before artifact use. Daily CI currently resolves its original
source dependencies, while the separate component pilot exercises artifact
consumption. See [the rollout observations](research/incremental-plan-2026-09-10.json)
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

Run the CLI inside the local Docker tooling environment with the Docker CLI,
pinned Buildx plugin, and access to the task's builder. Keep source mounted read
only. OCI paths must be visible at the same absolute paths to the producer and
consumer. Use new output directories so a failed or repeated invocation cannot
replace the original record.

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
invalidate reuse. The material model is not yet the
main/PR task selector. Keep generated files checked before planning, and do not
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

The pilot currently catalogs its two x86_64 build artifacts. The signed-catalog
interface and workflow wiring have local regression coverage; a real GitHub
signature and cross-run consumption have not yet been exercised. Missing-artifact
producer dispatch and production CI routing remain to be connected.

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
catalog digest and authentication evidence are retained. The module only resolves
toolchain installation/test-context build artifacts; it does not accept a
qualification role or infer passed tests from a generic receipt.

An absent discovery index returns `build-required` with an input-specific reason
and no usable subject/context. The read-only pilot reports that reason and fails
before consumer gates; production producer scheduling is still pending. An
explicit recovery reference, invalid signature, failed transfer or invalid
artifact never becomes a request to silently replace the original component.

Both same-run and catalog consumers share the existing fresh toolchain/runtime
and GCC smoke qualification path, followed by the cp39 x86_64 cross build. The
new qualification producer belongs to this run; input components keep their
original producer. Diagnostics preserve the original catalog, bundle, manifest,
inputs and receipts with the new reports, excluding large OCI layouts. This
pilot does not yet assert a complete Python row or final candidate qualification.

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
