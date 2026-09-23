# Local 10,000-event performance precheck, 2026-09-23

This is a **development-machine harness precheck**, not the exclusive Mini
reference run for R2. It used evidence commit
`487a5d56643bc7f1d5787be121bd6958a131a9a9`, whose product-code ancestor
is `0af86431d4f6d5b83d5b799e858cee11853bef97`. The machine reported arm64,
Darwin 27.0.0, and Python 3.14.4. The source-process driver used the pinned
`static-retrieval-mrl-en-v1` model revision
`f60985c706f192d45d218078e49e5a8b6f15283a` in a local encrypted vault.

The corpus repeated 75 authored synthetic scenario records into 10,000 events.
The driver used 10 warm-up queries and 100 measured queries. These figures
exclude any separately timed model download; the run used the hub or local
cache path, and download time was not measured.

| Measurement | Observed |
| --- | ---: |
| Model initialization | 2.38 s |
| Vault open | 0.15 s |
| Event ingestion | 7.22 s |
| First query, including ephemeral index build | 0.55 s |
| Warm embedding p50 / p95 | 0.67 / 1.02 ms |
| Warm ranker p50 / p95 | 23.96 / 25.90 ms |
| Warm full scoped search p50 / p95 | 27.84 / 30.14 ms |
| Process RSS after run | 550.2 MiB |
| Vault disk footprint | 50.5 MiB |

The raw report remains ignored at `benchmark_results/performance-10k-local.json`.
This precheck shows the driver can complete at the required scale and is useful
for catching a gross regression. **R2 remains pending** until the same workload
runs on the quiet Mini and the tested app artifact has its own query report.
