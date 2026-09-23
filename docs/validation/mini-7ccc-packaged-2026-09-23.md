# Packaged query and recovery on superseded candidate `7ccc29c`

These tests ran on the exact ad-hoc signed, internal 0.2.0 app archive built
from source commit `7ccc29c1c62828e5c3a0f41bdb2a9d9ff89eb776`. The ZIP is
425,566,640 bytes with SHA-256
`651b313ff911d9da67c09fcfdc83581161cab53f701e49f418d7a7e1240a31c0`.
Its installed launcher SHA-256 is
`8203a8ee54d7e95f257e9bb5a083828e5a2710c44c6d9425836ded2d5f0e3e92`.
Later runtime fixes superseded this candidate; its R2 and M5 results are
reproducible prior-artifact evidence, not final gate clearance. The repaired
artifact requires its own checks.

Disposable 8 GiB Apple Silicon virtual guests on the Mac mini ran macOS
26.6.2. Each was cloned from the untouched lab base. Homebrew and Xcode
Command Line Tools were moved aside before the archive was installed. All
vaults, notes and queries were synthetic. This CI-derived guest is useful
standalone-app evidence, but is not a stock Gatekeeper or signed/notarized
release test.

## R2: packaged 10,000-event query

The guest copied a prebuilt, encrypted 10,000-event synthetic vault and kept
its private key at mode `0600`. The installed app created a grant restricted to
the synthetic project and manual source, previewed all 10,000 events, and
served scoped recall over MCP stdio. After 10 warmups, the driver timed 100
full request/response calls, including grant checks, production search,
redaction, canonical hydration and transport.

| Measure | Result |
| --- | ---: |
| Warm full-call p50 | 251.10 ms |
| Warm full-call p95 | 291.84 ms |
| First query, including cold model/index work | 5.31 s |
| MCP initialization | 0.20 s |
| Process-tree RSS after queries | 552.8 MiB |
| Open descriptors / threads | 11 / 11 |
| Synthetic vault and validation files on disk | 47.2 MiB |

The measured p95 passes the 500 ms R2 threshold on this guest. Cold cost is
reported separately. The earlier 40.11 ms Mini source-process p95 measures a
narrower in-process search operation and should not be read as app latency.
The sanitized raw report is ignored by Git at
`benchmark_results/mini-artifact-query-7ccc29c.json` (SHA-256
`d971f4214b0d1a2cd277622eb0ec79a6f7b497d00073c91728ae52217865e224`).
The copied validation script SHA-256 is
`a102971832a53b397d53e2ab7355a72840235f69f660f82ea8924dc53cf33c15`;
its report has a null Git commit because it ran outside a checkout. The app
and ZIP checksums identify the tested product bytes.

## M5: export, restore and reopen in separate fresh guests

A new source guest used this same app archive to save three authored synthetic
notes with explicit IDs, project labels, event times and metadata. It forgot
one and exported a 971-byte authenticated archive (SHA-256
`b7619a40aebb577bdcb3894dac3b302863999fc64d741ed509adb31c48fe86c5`).
A separately cloned, empty target guest installed the same app. It had no
LatticeShadow vault or key before restore. Only the archive and synthetic test
passphrase were used to restore; source timeline summaries were copied for
comparison, but the source vault and key were not transferred.

Restore created a new private key at mode `0600`. Separate `backup inspect`
processes reopened the restored destination. Activation in the empty profile
produced two retained event objects identical to the source across ID, text,
type, source, project, original/capture/last-access times and metadata. Source
and target timeline JSON files have the same SHA-256,
`76b6b476dba14b570f071f0c120db695d063ef1830f93532b1c29ab2f1993a9d`.
Both databases held the forgotten ID as one tombstone. Capture remained off.

The target's first activation migrated the restored flat key to a Keychain
keypair. Its notice went to stderr; `timeline --json` stdout parsed as pure
JSON. A wrong passphrase then exited nonzero without creating a destination or
entering py2app's Launch error path. The already restored vault still opened
and inspected both retained records afterward.

The full two-hour source candidate soak is a separate E1 gate. This guest
evidence does not assert that it passed. P2 signing, notarization and stock
Gatekeeper remain external release gates.
