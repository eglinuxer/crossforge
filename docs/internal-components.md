# Internal component handoff

The local toolchain pilot uses the canonical Docker/Bake build stages and exports
an OCI layout with an embedded component contract. It supports a single local
`docker-container` BuildKit node. Local qualification receipts and explicit prior
report verification are available. GitHub registry transport, trusted CI reuse,
and candidate consumption are not enabled by this interface yet.

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
