# Capabilities and limits

LatticeShadow aims to make the fragments of a workday findable again: the note
you saved, the command you ran, the source you used, and enough context to pick
up where you left off. It is a local work-memory prototype, not an omniscient
diary. The latter would be alarming even if it worked.

## What can I rely on today?

“Implemented” below means the repository has a command or API and automated
coverage. It does not mean the feature has had broad use on real machines or a
security audit. Start with the [manual walkthrough](GETTING_STARTED.md).

| Area | What exists | Current limit |
| --- | --- | --- |
| Manual memory on macOS | `shadow remember`, scoped `shadow timeline`, `shadow search`, project assignment, and `shadow forget` save, inspect, rank, and delete local events. | Integrated real-model search reached [96% top-five recall](validation/recall-local.md) on an authored synthetic fixture; the 10,000-event hardware latency run is pending. Search returns suggestions, not answer confidence. |
| Local retrieval model | The CLI pins a 128-dimensional Static Retrieval MRL model and records model identity for collections. | First use downloads the model; incompatible old vectors require an explicit rebuild. |
| Background capture | An opt-in daemon reads future clipboard text and Zsh history entries. Both sources default off and require explicit choices. Pause persists; literal/source exclusions and optional age retention are implemented. | Concealment markers and exclusions are incomplete secret boundaries. Reboot and longer desktop validation are still underway. |
| Recovery | `shadow backup export`, `restore`, and `inspect` create an authenticated portable archive and a separately keyed recovery destination. | The 256 MiB plaintext cap can need several times that RAM. A fresh-guest restore and activation walkthrough remain to be validated; populated live vaults are never automatically replaced. |
| DB library | `latticeshadow-db` works without the macOS client and provides SQLite document storage, retrieval, metadata filters, and delete operations. | Without a caller-provided embedding function it uses hash vectors, which are not semantic. Experimental indexes need workload-specific testing. |
| MCP | Explicit local grants restrict read-only recall, recent context, extractive summaries, and live citation resolution. An independent SDK test and a synthetic Codex CLI host walkthrough passed. | Grant policy and pattern redaction do not stop a separate local process from reading files or guarantee that every secret is removed. |
| Menu bar and native companion | A recall panel with scope filters, preview, copy/open, project assignment, and confirmed forget is implemented. | Focused tests pass; logged-in guest interaction and shortcut/TCC behavior are still being checked. |
| Releases | Tags publish source archives. An unsigned integrated Apple Silicon app passed offline save/search and daemon lifecycle checks in a stripped disposable guest. | Upgrade Keychain approval, UI interaction, stock Gatekeeper, and signing/notarization remain to be validated before a public binary release. |

## Where does the information go?

- Manual saves and enabled capture write to a local SQLite-backed store. The
  default client path is `~/.latticeshadow/shadow.sqlite`; configuration, keys,
  and logs live nearby. The DB library uses the path you pass to `connect`.
- The CLI's default memory provider is `none`, so ordinary remember/search does
  not call a remote language model. Its retrieval model downloads from Hugging
  Face on first use and then runs locally. If you configure a cloud LLM or
  another sync/listener feature, those choices have additional data boundaries.
- The client encrypts stored document text in its privacy-enabled collection
  and rotates vectors for similarity search. Rotation preserves relationships
  between vectors; it is **not** opaque vector encryption. SQLite metadata such
  as event type, timestamps, and caller-supplied metadata is not covered by
  document-text encryption.
- The daemon log contains operational details and local paths. Master-key
  material is normally wrapped for Keychain storage, but the current code can
  fall back to a raw local key file if wrapping fails. Neither behavior should
  be hidden behind the word “private.”
- The MCP server requires an explicit project/source grant and redacts known
  patterns before returning allowed event text. Pattern matching can miss
  secrets; a connected assistant can receive anything the server does not
  catch. The [sharing guide](MCP.md) explains scope and revocation. The CLI's
  optional LLM commands may send processed context to a configured provider.
- iCloud, peer mesh, mobile API, and ambient app context are disabled by
  default. They are experimental and should be reviewed separately before
  enabling them. Do not expose a peer listener to an untrusted network.

These are implementation boundaries, not a threat-model review or a guarantee
that confidential material cannot escape. Use test data while evaluating the
project. Keep the database, backups, logs, and model cache out of Git.

## What is still research?

Peer networking, holographic recall, autonomous repair, model alignment,
quantization, and several retrieval indexes have code and experiments, but they
are not part of the recommended first-run workflow. Simulated P2P proofs are
not zk-SNARKs. Selected [benchmarks](benchmarks/README.md) describe their own
datasets and machines; they are not universal speed or privacy claims.

The near-term goal is deliberately less theatrical: measure real recall on
realistic notes and commands, make fresh installation and shutdown predictable,
prove deletion and recovery across active indexes, connect the MCP server to a
real client, and document the privacy cost of each feature. The
[improvement backlog](SPRINT.md) tracks that work. A memory tool should first
be good at remembering the right thing and forgetting when asked.
