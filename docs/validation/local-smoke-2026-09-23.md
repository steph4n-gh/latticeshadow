# Local 15-minute driver smoke, 2026-09-23

This was a **pre-fix harness run**, not evidence that the final app candidate
passes E1. It ran against evidence commit `6062a38f605df9b62458620a0a23ce354aacc544`
on a local arm64 macOS machine (Darwin 27.0.0, Python 3.14.4), with synthetic
hash-model data. The later product candidate and installed app require new runs.

Command profile: `soak-smoke` with its default 900-second duration, 0.5-second
operation interval, 256 live-event cap, and 30-second resource sampling.

| Observation | Measured result |
| --- | ---: |
| Workload elapsed | 900.5 seconds |
| Full process elapsed | 901.17 seconds |
| Capture / query / delete / reopen | 731 / 462 / 475 / 32 |
| Durable resource samples | 30 |
| Final deleted IDs rechecked after reopen | 256 |
| File descriptors, first → last | 5 → 5 |
| Threads, first → last | 1 → 1 |
| Child processes, first → last | 0 → 0 |
| RSS, first → last | 228.7 → 200.5 MiB |

The driver reported **passed**. The raw JSON summary and fsynced progress lines
remain in ignored `benchmark_results/` locally. This result tests the workload
driver and bounded resource behavior on that revision. It does not stand in
for the two-hour candidate soak, installed-app interactions, or the Mini's
10,000-event performance gate.
