# LatticeShadow CLI

The macOS client for [LatticeShadow](../../README.md). It provides manual
memory commands, opt-in clipboard and terminal capture, a timeline, a menu-bar
interface, and a local stdio MCP server. It uses the independently installable
[`latticeshadow-db`](../db/README.md) package for storage and retrieval.

**Status:** development prototype. Start with manual saves. Automatic capture
handles personal clipboard and terminal content, so enable it only after you
understand what it records. The [improvement backlog](../../docs/SPRINT.md)
tracks remaining installation, recovery, and end-to-end validation work.

## Set up and try the commands

On macOS, use Python 3.12 or newer and the Xcode Command Line Tools. From the
repository root:

```sh
make setup
source .venv/bin/activate
shadow remember terminal "Check the release tests"
shadow search "release tests"
shadow timeline --source terminal
shadow status
```

Setup installs both packages in editable mode and builds the native LWE
component. It does not start a listener, enable capture, or change shell startup
files. The first save or search downloads the pinned local embedding model;
subsequent embedding runs locally. Your saved data and keys live under
`~/.latticeshadow` by default; keep backups private.

To opt into background capture:

```sh
shadow install
shadow consent wizard
shadow enable
shadow status
shadow disable
```

`shadow install` prepares the launch agent without starting capture.
The wizard shows each optional data source and network surface. Clipboard and
terminal capture are enabled by default in the configuration, so review those
choices before running `shadow enable`. That command loads the model and starts
the daemon; `shadow disable` stops it. Optional Zsh widgets require
`shadow shell enable`; that command does
not bind keys or replace Tab completion. You can choose a binding yourself,
for example `bindkey '^G' latticeshadow-ghost-paste`. Run
`shadow shell disable` to remove the plugin source line.

Older stores with hash vectors need an explicit rebuild after stopping capture:

```sh
shadow disable
shadow rebuild-index --yes
shadow enable
```

The rebuild preserves events, IDs, metadata, and timestamps and prints the path
to a database backup. Stop other writers while it runs.

## Assistant access and experimental features

`shadow mcp serve` starts a stdio server. Its tools cover recall, current
context, summaries, privacy reports, repair proposals, and explicit deletion.
It does not automatically connect to an assistant. Summaries may use a
configured model provider; check that configuration before sending private
memory to one. Peer mesh synchronization, homomorphic queries, and proof
handling are experimental; the P2P proofs are simulated and do not provide
zk-SNARK security. Do not expose a peer listener to untrusted networks.

## Where next?

- [Root guide](../../README.md): product goals, limitations, and the shortest path to a first memory.
- [Technical tome](docs/TOME.md): internals and experimental protocols.
- [Future directions](docs/FUTURE.md): proposed work, with current gaps called out.
- [Native companion](native/README.md): optional App Intents and Foundation Models bridge.
- [CLI tests](tests/): examples of supported command behavior.

Run `make test-cli` and `make docs` from the repository root after changing the
client. Hardware tests are opt-in via `make test-hardware`.
