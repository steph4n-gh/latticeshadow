# Mini packaged query and portable recovery, 23 September 2026

This is synthetic evidence for the **superseded** `d1c3e3b` internal candidate.
Two CLI error paths found during recovery are being fixed, so these measurements
do not clear the final artifact or release gates. The original report is kept
outside Git at `benchmark_results/mini-artifact-query-d1c3e3b.json` (SHA-256
`214ae3fb42efa0a54665148e9511b834fd01243c91c8b7d64b82c79e549bbe36`).
It contains counts, timing and resource data, not remembered text or keys.

## Artifact and method

- Product source: `d1c3e3bb7ac2c0b0c272bc9ebddb8ad963c10b27`.
- App ZIP: 425,565,684 bytes; SHA-256
  `9299111d69af2d1fa0ee573d44679af0b861002aafe1fa21705976fffa4c66d8`.
  Installed launcher SHA-256:
  `f9954fd40b76a217cdbfa9bfc335e144892d2a3875eac7c4babc5e17d7fa5d64`.
- Validation driver SHA-256:
  `a102971832a53b397d53e2ab7355a72840235f69f660f82ea8924dc53cf33c15`.
  It was copied into the guest, so its Git `commit` field is null; the artifact
  checksum and driver checksum establish the tested bytes.
- All runs used disposable macOS 26.6.2 Apple Silicon virtual guests on the
  Mini, each with 8 GiB RAM. Guests were cloned from the untouched lab base.
  Homebrew and Command Line Tools were moved aside before the exact app ZIP
  was installed. No personal vault, clipboard, shell history or credentials
  were used. This CI-derived guest does not prove stock Gatekeeper or
  signed/notarized installation.

## 10,000-event packaged query

The prebuilt encrypted vault contained 10,000 authored synthetic events. The
installed app created a grant scoped to the synthetic project and manual
source, previewed all 10,000 records, then served recall through its packaged
MCP stdio launcher. The driver ran 10 warmups and timed 100 full RPCs, including
grant checks, search, redaction, canonical hydration and transport.

| Measure | Result |
| --- | ---: |
| Warm full query p50 | 250.60 ms |
| Warm full query p95 | 256.82 ms |
| First query, including model/index cold work | 4.66 s |
| MCP startup to initialize response | 0.19 s |
| Process-tree RSS after queries | 550.8 MiB |
| Open descriptors / threads | 11 / 11 |

The measured p95 is below the 500 ms R2 threshold for this artifact. The first
query is materially slower and is reported separately. The earlier 40.11 ms
Mini source-process result measures a narrower Python search call; the numbers
should not be interchanged. This app had no capture source enabled during the
query run.

## Portable recovery across fresh guests

A separate stripped source guest saved three synthetic notes with explicit IDs,
project labels, event times and metadata. It deliberately forgot one, then
exported a 971-byte authenticated archive (SHA-256
`01a4ce1608822ae5c2019052fcfd99d4a634062bcec3dfa739f6de77e65f7b28`).
Only the archive and test passphrase were used for the restore in a second
clean guest; source timeline summaries were copied for comparison, but the
source vault and key were not transferred. The target initially had no
LatticeShadow vault or key. Restore created a separate private key with mode
`0600`, and two independent `backup inspect` processes reopened the destination.
After deliberate activation in that empty profile, the two retained event
objects matched the source exactly across ID, text, type, source, project,
original and capture timestamps, last access, and metadata. Both databases
contained the forgotten ID as one tombstone. Capture remained disabled.

Two defects keep M5 pending for the final candidate:

1. The first activated `timeline --json` printed a key-migration notice before
   the JSON object. The underlying event objects matched after removing that
   notice, but the command's JSON output contract failed.
2. A wrong passphrase was rejected with an authentication error and created no
   destination. The packaged CLI then remained in py2app's Launch error path
   for more than 15 seconds and required termination. The existing restored
   destination still reopened and inspected both records afterward.

The candidate-specific two-hour source soak was still in progress when this
note was written. These packaged guest observations do not assert that soak
passed, nor do they cover the later repaired artifact.
