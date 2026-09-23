# Packaged query and portable recovery on `b603442`

These synthetic tests exercised the exact internal macOS 0.2.0 app archive
built from source `b603442dfb471cf22f9a561f1b034a6582f830d9`. The ZIP is
425,567,521 bytes with SHA-256
`6bcf2edec4a128323bfb425885ca445efbd9208346bd22bd1aa352df1d70e35e`.
The installed launcher SHA-256 is
`c95dc8fbb214d91f8342738b6f44134606242822a48ce6f14de52c986c21692d`.

Tests ran in disposable Apple Silicon virtual guests on the Mac mini (macOS
26.6.2, 8 GiB RAM). Every guest was cloned from the untouched lab base;
Homebrew and Command Line Tools were moved aside before installing the ZIP.
The base is CI-derived, so these results do not establish stock Gatekeeper or
Developer ID signing/notarization. No personal vault, clipboard, shell history
or credential was used.

## R2: 10,000-event packaged recall

A fresh guest copied a prebuilt encrypted synthetic vault with 10,000 events
and a private mode-`0600` key. The installed app created an MCP grant limited
to the synthetic project and manual source, previewed all 10,000 events, then
served recall over its packaged stdio launcher. The driver ran 10 warmups and
timed 100 complete request/response calls. These calls include grant checks,
production search, redaction, canonical hydration and transport.

| Measure | Result |
| --- | ---: |
| Warm full-call p50 | 253.18 ms |
| Warm full-call p95 | 300.57 ms |
| First query, including cold model/index work | 5.36 s |
| MCP initialization | 0.20 s |
| Process-tree RSS after queries | 562.5 MiB |
| Open descriptors / threads | 11 / 11 |
| Synthetic vault and validation files on disk | 47.2 MiB |

The full-call p95 passes the 500 ms R2 target on this guest. Cold cost is
separate. The earlier Mini source-process p95 of 40.11 ms measures a narrower
in-process operation and is not packaged-app latency. The sanitized raw report
is ignored by Git at `benchmark_results/mini-artifact-query-b603442.json`
(SHA-256 `37250c95099db8898011217f0439ae0e36e4fda2f122f20acb8cb19912eb49d8`).
The copied driver SHA-256 is
`a102971832a53b397d53e2ab7355a72840235f69f660f82ea8924dc53cf33c15`.
Its Git commit field is null because it ran outside a checkout; the app and
archive hashes identify the tested product bytes.

## M5: portable recovery across separate fresh guests

A new source guest installed this same archive, saved three authored synthetic
notes with explicit IDs, project assignments, event times and metadata,
deliberately forgot one, and exported a 971-byte authenticated archive
(SHA-256 `80f0d02edb14e92b9b20f2ecd73dce4c2468956ba5f503e5e63da8dc07ed87ed`).
A separately cloned target guest installed the same app and began with no
LatticeShadow vault or key. Restore used only the archive and synthetic test
passphrase. Source timeline summaries were copied for comparison; the source
vault and key were not transferred.

Restore created a new private key at mode `0600`. Separate `backup inspect`
processes reopened the destination. Activation in the empty profile produced
two retained event objects identical to the source across ID, text, type,
source, project, original/capture/last-access times and metadata. Source and
target timeline JSON files have identical SHA-256,
`fdf250f9a6ba7894baeabe340963fb56d635f18d494c807f48b926ee7737b4bc`.
Both databases contained the forgotten ID as one tombstone. Capture remained
off throughout.

The target's first activation migrated the restored flat key to a Keychain
keypair. Its notice went to stderr, and `timeline --json` stdout parsed as
pure JSON. A wrong passphrase exited nonzero, created no destination, and did
not enter py2app's Launch error path. The already restored destination still
opened and inspected both records afterward.

This clears the scoped packaged R2 and fresh-guest M5 exercises for `b603442`.
The full two-hour source soak and integrated desktop journey are separate E1
evidence. Public binary release still requires signing, notarization and stock
Gatekeeper validation.
