# Mini guest source performance, 2026-09-23

The encrypted-vault backend completed the frozen 10,000-event synthetic
workload on the exclusive Mini lab. The source checkout was
`07d2321ecd46016e2279666fd1c164a86ce425d0` (product-code ancestor
`71d2ecf`; the extra commit changes only validation metadata). The validation
script SHA-256 was
`a102971832a53b397d53e2ab7355a72840235f69f660f82ea8924dc53cf33c15`.
The sanitized raw JSON report SHA-256 is
`57424270c222aec586d4fb983eec218661dcba6bf3d7f46986295c0ded12231a`;
the raw report remains ignored in `benchmark_results/`.

The run used one disposable macOS 26.6.2 guest (VirtualMac2,1, 8 GiB RAM,
arm64, Python 3.12.14) on the Mini host. No other guest was running. The pinned
`static-retrieval-mrl-en-v1` model revision was
`f60985c706f192d45d218078e49e5a8b6f15283a`, loaded from a local bundled
snapshot with offline mode enabled. The source process created 10,000 encrypted
events by repeating 75 authored scenario records, warmed up with 10 queries,
then measured 100 scoped queries. The first query includes building the
ephemeral retrieval index; warm total query time includes scope filtering,
ranking and canonical hydration.

| Measurement | Observed |
| --- | ---: |
| Model initialization | 4.81 s |
| Vault open | 0.30 s |
| Event ingestion | 7.29 s |
| First query with index build | 0.60 s |
| Warm embedding p50 / p95 | 0.54 / 2.42 ms |
| Warm ranker p50 / p95 | 28.79 / 31.78 ms |
| Warm full scoped search p50 / p95 | 31.84 / 40.11 ms |
| Process RSS after run | 518.5 MiB |
| Open file descriptors after run | 12 |
| Vault disk footprint | 50.5 MiB |

The source-process warm p95 is below the plan's 500 ms target. This is strong
backend evidence on the requested Mini hardware, but it is **not** a timed
interaction with the packaged desktop panel. The artifact-query profile and
fresh-guest recovery must run against the rebuilt final ZIP; their rows remain
pending. A local two-hour source-candidate soak is also running separately and
has not completed at the time of this note. The old 0af ZIP is superseded and
was not used to claim final artifact performance.
