# Daily-use alpha validation

These checks use disposable, authored data. They never read the default vault,
clipboard, shell history, Keychain, or personal credentials. The JSON summary is
safe to review: it contains counts, timings, revision, system information, and
check names. It omits stored text, local paths, and keys. If you retain a data
directory, treat the vault and progress log as local test artifacts anyway.

From the repository root, after `make setup`:

```sh
python packages/cli/scripts/validate_lifecycle.py lifecycle --report benchmark_results/lifecycle.json
python packages/cli/scripts/validate_lifecycle.py performance --events 10000 --warmup 10 --queries 100 --report benchmark_results/performance.json
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
revision, operating system, machine, and Git SHA. A hub/cache model start may
include a network download, which is **not separately timed**; a bundle run can
identify its local model source. The small local test count is only a driver
smoke; the product's 10,000-event target requires a quiet, exclusive reference
machine and the complete parameters above. Do not compare a tiny fixture to the
500 ms acceptance gate. `benchmark_results/` is ignored and should hold raw
reports rather than Git commits.

## Fault coverage

`lifecycle` checks an already-open reader against a separate writer and delete,
two writers released together, an exit before canonical insert, an exit after
canonical commit but before the derived manifest, an exit after canonical
delete but before that manifest, a missing vector sidecar, interruption during
rebuild and restore, a wrong vault key, wrong passphrase, tampered archive,
fresh-destination restore/reopen, and an injected `ENOSPC` error before commit.
It checks that a committed ID survives and a deleted ID remains unavailable
after restart. The `ENOSPC` case is an injected store-boundary error, **not** a
full-disk VM test. Process interruption leaves private staging files until
manual cleanup; the harness removes its synthetic staging files after checking
that the source remains readable. It does not prove that killing a process at
every possible machine instruction is safe.

Cross-surface desktop/MCP journeys, real artifact installation, stock-guest
Gatekeeper behavior, and the 15-minute/2-hour elapsed runs need their own
candidate reports. A passing script here does not mark those gates passed.
