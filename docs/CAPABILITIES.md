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
| Manual memory on macOS | `shadow remember`, `shadow timeline`, `shadow search`, and `shadow forget` save, inspect, rank, and delete local events. | Recall quality has only a small labeled evaluation; search may miss a useful event or rank a bad one first. |
| Local retrieval model | The CLI pins a 128-dimensional Static Retrieval MRL model and records model identity for collections. | First use downloads the model; incompatible old vectors require an explicit rebuild. |
| Background capture | An opt-in daemon reads future clipboard text and Zsh history entries; `shadow status` and `shadow disable` expose and stop daemon state. Both sources default off and require explicit on/off choices before startup. | Password-manager concealment and secret filters are incomplete boundaries. Broader desktop validation is still needed. |
| DB library | `latticeshadow-db` works without the macOS client and provides SQLite document storage, retrieval, metadata filters, and delete operations. | Without a caller-provided embedding function it uses hash vectors, which are not semantic. Experimental indexes need workload-specific testing. |
| MCP | A stdio protocol handler exposes recall, recent context, summaries, privacy reports, repair proposals, and confirmed deletion. | Automated protocol tests exist; a live assistant-client connection and failure handling still need end-to-end validation. Redaction is best effort. |
| Menu bar and native companion | macOS UI and native scaffolding exist in the repository. | Packaging and real-world desktop validation are incomplete; there is no polished installable app release. |
| Releases | Version tags can publish source archives from tested `main` commits. | There are no installable wheels or packaged native companion yet. |

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
- The MCP server redacts known patterns before returning event text. Pattern
  matching can miss secrets; a connected assistant can receive anything the
  server does not catch. The CLI's optional LLM commands may send processed
  context to a configured provider.
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
