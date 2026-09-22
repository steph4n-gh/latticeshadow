# LatticeShadow: Future Directions

LatticeShadow can save and retrieve local memory on macOS today. Clipboard and
terminal capture, a native UI, sync, and repair proposals have code paths, but
they have different levels of validation. This page lists possible directions,
not features promised for the next release. See the [CLI README](../README.md)
for current behavior and the [root guide](../../../README.md) for product goals.

## Near-Term Hardening

1. Make consent harder to misunderstand.
   `shadow consent wizard` and `shadow consent status` exist. Test them in the
   full install flow and make the state of every listener easy to see. Capture
   can contain secrets even when clipboard markers and regex filters are used.
   A surprised user is a very poor privacy feature.

2. Validate paired-device trust end to end.
   Ed25519 identities, a trust store, replay nonces, and revocation code exist.
   The full mesh protocol still needs adversarial review, interoperable pairing
   tests, and clear recovery behavior. Network advice must remain record-only.

3. Separate security claims from demonstrations.
   The DB has AES-GCM document encryption and rotated or hashed searchable
   vectors, depending on the collection. Key management has platform-specific
   fallbacks, and sensitivity filters are heuristic. The mesh's Groth16-shaped
   hashes are simulated proofs, not zk-SNARKs. Document and test each boundary.

4. Make installation and recovery routine.
   Provide repeatable installs, model downloads, key recovery instructions,
   backups, upgrades, and uninstall behavior. A working `make setup` is a start;
   it is not yet a packaged macOS release.

## Product Directions

1. Memory Timeline
   Build a dense local timeline that merges clipboard entries, terminal commands,
   active app context, URLs, files, and semantic clusters. The key product move is
   not "search my clipboard"; it is "show me what I was doing and why."

2. Intent Recovery
   The `fix`, loop detection, and compose experiments could become an intent
   recovery system. Surface a relevant prior command or docs snippet, explain
   why it matched, and let the user choose the next action.

3. Private Personal RAG
   The local vault could become a retrieval layer for assistants, with explicit
   access control and a visible record of what was shared. Remote model calls
   need a stronger boundary than regex redaction alone.

4. Native Recall UX
   The menu bar and Spotlight overlay can evolve into a fast command surface:
   recall, paste, pin, explain, forget, tag, summarize, and open source context.
   The UI should show provenance and privacy state for every result.

## Research Directions Within Reach

1. Real Private Mesh Search
   The LWE pathway explores asking another device about nearby memories. The
   next step is to define and test a threat model: encrypted query, bounded
   candidates, authenticated peers, measurable false-positive rates, and no
   plaintext response until the user asks. Current proof strings do not verify
   the answer.

2. Verifiable Local Actions
   The existing signed event journal could record important actions consistently:
   captures, deletes, sync imports, device pairings, model calls, and accepted
   repairs. Auditability requires complete event coverage and tamper tests.

3. Holographic Hot Memory
   The HRR index is a promising low-latency cache. The next experiment is to
   benchmark recall quality against normal vector search, then use it only where
   it wins: today's hot context, shell commands, and tiny on-device indexes.

4. Consent-Preserving Ambient Context
   Ambient context should become selective and inspectable. Capture window titles,
   URLs, or text only for allowlisted apps and only after the user can preview the
   exact fields. Build privacy budgets before building more sensors.

5. Human-Approved Autonomy
   Build on the existing repair proposal queue: explain a proposed patch, show
   the diff, run tests in isolation, and require explicit approval. A generated
   fix remains a suggestion until a person reviews it.

## Big Picture

The goal is a useful, inspectable memory tool that helps one person recover
context across their devices. Getting there depends on plain consent, good
retrieval, reliable recovery, and security claims that survive testing.
