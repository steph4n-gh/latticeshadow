# Improvement sprint

The product goal is to recover useful work context reliably: what was copied,
which command was used, where a source came from, and what to do next.
The monorepo provides one development environment and test entry point;
the following product work is still to do.

## 1. Make recall meaningful

- Wire one real local embedding model into the client and both vault openers.
- Store the embedding model identity and dimension with each collection.
- Provide an explicit rebuild path for existing hash vectors.
- Evaluate recall on a small labeled corpus of realistic commands, notes, and URLs.

Acceptance: paraphrased queries retrieve the intended saved events; model changes
cannot silently mix incompatible vectors; results identify their source events.

Implemented: the CLI uses a pinned 128-dimensional local retrieval model, stores
model identity per collection, offers `shadow rebuild-index`, and checks eight
labeled paraphrase cases against both vaults. Broader recall evaluation remains useful.

## 2. Make installation and shutdown reliable

- Separate installing commands from opting into capture, shell widgets, and login startup.
- Preserve existing completion widgets and key bindings when shell integration is enabled.

Acceptance: a fresh install and an upgrade both work; disabling leaves no background
process; ordinary Tab completion works with and without the shell plugin.

Recent fixes: the source plugin now ships with the package; daemon
and CLI entry points are tested. Reinstall updates stale paths, launchctl errors are
reported, clean exits no longer trigger restart loops, and removal preserves keys
when data is kept. These are not a complete installer redesign.

Current installer keeps shell setup opt-in, and the optional plugin leaves existing
key bindings and completion widgets untouched. A disposable macOS Tahoe VM has
covered a fresh clone, setup, manual recall, explicit capture choices, synthetic
clipboard capture, deletion, and shutdown. An upgrade from the prior checkout
preserved source flags, required missing consent choices, refreshed the local
integrity baseline through reinstall, and kept enable/disable state across
reboots. The CI base image has Gatekeeper disabled, so signing, notarization,
native app packaging, and real desktop interaction remain to be validated.

## 3. Prove the core memory workflow

- Exercise capture, storage, restart, timeline, recall, paste, and forget end to end.
- Make provenance and timestamps consistent across clipboard and terminal events.
- Verify canonical/hot-index consistency during deletion, restart, and partial failures.
- Keep capture opt-in and make its current state visible.

Acceptance: a saved event survives restart, is retrievable, and disappears from every
active index when forgotten; tests use disposable data and mocked desktop inputs.

A disposable test now covers mocked clipboard capture, restart, timeline, recall,
paste, and forgetting from both canonical and hot indexes. Partial-failure recovery
and cross-source provenance still need further validation.

## 4. Make the assistant interface dependable

- Validate the stdio MCP server with a real client and its supported protocol version.
- Keep diagnostics off protocol stdout; test malformed inputs and disconnects.
- Make redaction and deletion behavior explicit and testable.
- Add a short configuration example after an end-to-end connection is verified.

Acceptance: an assistant can recall and cite saved context without unapproved
capture or exposing sensitive test values.

## 5. Establish representative performance and privacy evidence

- Benchmark the real embedding model and event corpus, including cold startup.
- Compare the simplest exact-search implementation before adding index complexity.
- Report latency distributions, recall, memory, and storage for the same workload.
- Separate document encryption guarantees from vector-geometry leakage.

Acceptance: reproducible reports support the actual shipped configuration. Existing
synthetic speedups against this project's baseline are not claims against other databases.

## 6. Reduce maintenance cost

- Keep the public DB interface small and the macOS client dependent on it.
- Isolate experiments from the default runtime path.
- Align Python support declarations with CI and add dependency constraints as needed.
- Make platform-specific wheels and the native companion build reproducibly.
- Replace outdated architecture claims and historical project-status documents.

Start with recall and installation. Avoid expanding peer sync or autonomous repair
until the basic memory workflow is reliable.
