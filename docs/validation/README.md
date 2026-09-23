# Daily-use alpha validation

Measured local runs: [retrieval relevance](recall-local.md),
[15-minute driver smoke](local-smoke-2026-09-23.md), and
[10,000-event local precheck](local-performance-2026-09-23.md). Each report
states which candidate and gate it covers; the latter two are preparatory
checks, not the final Mini or installed-app result.

These checks use disposable, authored data. They never read the default vault,
clipboard, shell history, Keychain, or personal credentials. The JSON summary is
safe to review: it contains counts, timings, revision, system information, and
check names. It omits stored text, local paths, and keys. If you retain a data
directory, treat the vault and progress log as local test artifacts anyway.

From the repository root, after `make setup`:

```sh
python packages/cli/scripts/validate_lifecycle.py lifecycle --report benchmark_results/lifecycle.json
python packages/cli/scripts/validate_lifecycle.py performance --events 10000 --warmup 10 --queries 100 --data-dir benchmark_results/performance-vault --report benchmark_results/performance.json
python packages/cli/scripts/validate_lifecycle.py artifact-query --app /path/to/LatticeShadow.app --artifact /path/to/tested-candidate.zip --vault-dir benchmark_results/performance-vault --expected-events 10000 --warmup 10 --queries 100 --report benchmark_results/artifact-query.json
python packages/cli/scripts/validate_lifecycle.py soak-smoke --data-dir /path/to/empty/disposable-dir --report benchmark_results/soak-smoke.json
python packages/cli/scripts/validate_lifecycle.py soak-candidate --data-dir /path/to/empty/disposable-dir --report benchmark_results/soak-candidate.json
```

The default smoke runs for **15 minutes**; the candidate profile runs for
**2 hours**. Both run a seeded, bounded synthetic capture/query/delete/reopen
cycle at up to two operations per second, keep at most 256 live events, and fsync a
resource sample every 30 seconds to `soak-progress.jsonl`. They fail if file
descriptors or threads grow by more than eight, or child processes by more than
two, relative to the first sample. The progress log stays in the supplied data
directory. Without `--data-dir`, all generated data is deleted on exit. These
limits catch runaway process resources; they do not by themselves prove a
latency, memory, or user-workload guarantee.

`performance` uses the pinned real embedding model and an encrypted vault. It
repeats 75 authored scenario records into 10,000 events, then records model
initialization, vault open, ingestion, first query with ephemeral-index build,
warm embedding, ranker, and full scoped search latency separately. The report
includes p50/p95, process RSS, descriptors, threads, children, disk size, model
revision, hardware model/RAM, operating system, dependency versions, Git SHA,
and validation-script SHA-256. The driver rejects a checkout or script that
changes before the run finishes. A hub/cache model start may
include a network download, which is **not separately timed**; a bundle run can
identify its local model source. The small local test count is only a driver
smoke; the product's 10,000-event target requires a quiet, exclusive reference
machine and the complete parameters above. Do not compare a tiny fixture to the
500 ms acceptance gate. `benchmark_results/` is ignored and should hold raw
reports rather than Git commits.

The retained `performance` vault has a private `.key` file and `shadow.sqlite`
so `artifact-query` can open the same synthetic 10,000-event collection through
the packaged app's `--cli mcp` launcher. Its sanitized process environment has
no Python path, developer checkout, or real HOME. It creates an explicit grant,
checks the eligible event count, then measures MCP startup, first query and warm
total query latency. This includes grant checks, stdio transport and redaction;
it is a separate artifact measurement from source-process ranking. The optional
`--artifact` SHA-256 ties the report to the exact tested ZIP/DMG. Both modes
should run on a quiet reference machine under the exclusive lab lease.

## Fault coverage

`lifecycle` checks an already-open reader against a separate writer and delete,
two writers released together, an exit before canonical insert, an exit after
canonical commit but before the derived manifest, an exit after canonical
delete but before that manifest, a missing vector sidecar, interruption during
rebuild and restore, a wrong vault key, wrong passphrase, tampered archive,
fresh-destination restore/reopen, and an injected `ENOSPC` error before commit.
It also uses the installed CLI to create and preview a synthetic grant, starts
an MCP stdio server, recalls and resolves a redacted citation, forgets the event,
and confirms that citation is unavailable both immediately and after an MCP
server restart. It does not automate the AppKit panel or constitute an
independent MCP SDK/assistant-host check.
It checks that a committed ID survives and a deleted ID remains unavailable
after restart. The `ENOSPC` case is an injected store-boundary error, **not** a
full-disk VM test. Process interruption leaves private staging files until
manual cleanup; the harness removes its synthetic staging files after checking
that the source remains readable. It does not prove that killing a process at
every possible machine instruction is safe.

Cross-surface desktop/MCP journeys, real artifact installation, stock-guest
Gatekeeper behavior, and the 15-minute/2-hour elapsed runs need their own
candidate reports. A passing script here does not mark those gates passed.
