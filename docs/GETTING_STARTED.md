# Getting started

You can try LatticeShadow without giving it your clipboard, shell history, or
background time. Save one harmless note, find it, and decide whether the rest is
useful to you. The daemon can wait; it is very patient.

LatticeShadow is a **development prototype**. The shortest supported path is
the macOS command line. For the project goals and the difference between
implemented commands and validated workflows, see [Capabilities and limits](CAPABILITIES.md).

## macOS: save your first memory

You need macOS, Python 3.12 or newer, the Xcode Command Line Tools, and network
access for installation and the first model download. In a checkout of this
repository, run:

```sh
make setup
source .venv/bin/activate
shadow remember terminal "Run the regression tests before release"
shadow timeline --source terminal
shadow search "release tests"
```

`make setup` installs both packages into `.venv` in editable mode and builds a
native library. It does **not** start capture, enable a launch agent, or edit your
shell startup file. `shadow remember` saves the text you give it and prints an
event ID such as `cmd_...`. The timeline shows that event with its type and
timestamp. Search ranks likely matches; it is useful, but not a promise to
understand every paraphrase.

On a fresh machine, the first manual save downloads the pinned
[Static Retrieval MRL model](https://huggingface.co/sentence-transformers/static-retrieval-mrl-en-v1)
from Hugging Face. Inference then runs locally. This first step may take longer
than subsequent commands. The CLI does not silently substitute hash vectors if
the model cannot load.

To delete your test event, copy the ID printed by `shadow remember`:

```sh
shadow forget --id 'cmd_...'
```

Replace `cmd_...` with the real ID. Review the selected IDs, then type `FORGET`
at the prompt. `shadow forget` deletes selected events from the main store and
attempts to remove any mirrored hot-index entries. If the latter fails, it
prints a warning; check it before assuming every copy is gone. The command does
not erase external backups or data an assistant has already received.

`shadow status` is also worth a look. Seeing `Daemon: STOPPED` after these
commands is expected: manual memory does not need a background process.

## macOS: choose background capture

The daemon can watch copied text and new entries in your Zsh history file. This
is more useful for recall and considerably more personal than a test note. Both
sources default to **off**. Before `shadow enable`, choose **on or off** for
each source. The daemon will not start until those choices are recorded.

```sh
shadow install
shadow consent wizard
shadow consent status
shadow enable
shadow status
```

Answer every wizard prompt deliberately. Its defaults reflect the current
configuration; both capture sources are off in a fresh setup. You can make the
two required choices without the full wizard. For example, to capture clipboard
text but leave shell history alone:

```sh
shadow consent set clipboard on
shadow consent set terminal_history off
shadow consent status
shadow enable
```

`shadow install` prepares a launch agent and key material; it does not start the
daemon. `shadow enable` loads the retrieval model and starts it. The daemon
captures future changes, not a guaranteed complete record of your past work.
The Zsh history watcher begins at the end of the current history file, and
history writes depend on your shell settings. Clipboard capture skips some
concealed content but is not a reliable secret detector.

On upgrade, existing source settings stay as configured, but an old setting
without a recorded choice must be confirmed before capture restarts. Check
`shadow consent status` and use the wizard or `shadow consent set` for each
pending source. Manual saves and searches do not require capture consent.

Stop capture with:

```sh
shadow disable
shadow status
```

If you want to change sources later, disable the daemon, run
`shadow consent set <surface> on|off`, check `shadow consent status`, then enable
it again. In particular, the terminal watcher is selected when the daemon
starts. Disabling the daemon stops capture; it does not delete saved memories.
Use `shadow forget --id ...` for selected events, or inspect `shadow remove`
before uninstalling. `shadow remove` asks separately whether to delete local
data.

## Linux or another non-macOS system: use the DB library

The DB package is independently installable. The macOS capture daemon, menu
bar, Keychain integration, and `shadow` CLI are outside this path.

```sh
make setup-db
source .venv/bin/activate
```

From the repository root, this small Python example creates an ignored
`demo.sqlite` file:

```python
from latticeshadow_db.latticedb import connect

db = connect(db_path="demo.sqlite")
db.add(documents=["Run the regression tests before release"], ids=["first-note"])
print(db.search("Run the regression tests before release", n_results=1).documents)
db.delete(["first-note"])
```

The library's default embedding function is deterministic hashing for tests
and basic storage, **not semantic search**. Pass your own `embedding_fn` and
matching `embedding_dim` for meaningful similarity search. This example also
uses the DB's default `privacy=False`; choose and review your storage policy
before adding sensitive data. See the [DB guide](../packages/db/README.md) for
its API and optional research features.

## Assistant access (experimental integration)

After you have saved an event, `shadow mcp serve` runs a local MCP server over
stdio. A client must be configured separately to launch that command from the
same installed environment. The server exposes redacted recall, recent context,
summaries, privacy reports, repair proposals, and explicit deletion by ID.
Redaction uses patterns and heuristics; it can miss secrets. Treat a connected
assistant as a recipient of the data you permit it to read.

The protocol handler has automated tests, but a real assistant-client setup is
still on the [improvement backlog](SPRINT.md). We do not yet give a copy-paste
client configuration that we have not verified end to end.

## If something goes wrong

| Symptom | What to check |
| --- | --- |
| `shadow: command not found` | Activate the repository environment with `source .venv/bin/activate`; run `make setup` if it does not exist. |
| First save or `shadow enable` cannot load the model | Check network access for the first Hugging Face download. Retry after connectivity returns. Avoid switching to hash embeddings in an existing collection. |
| `shadow status` says `STOPPED` | This is normal for manual use. To start capture, review sources above, then run `shadow install` and `shadow enable`. |
| `shadow enable` asks for capture choices | Run `shadow consent wizard`, or set both `clipboard` and `terminal_history` explicitly with `shadow consent set <source> on\|off`, then retry. |
| Search says “No matching memories found” | The store is empty or the query found no match. Check `shadow timeline` for saved events. |
| Search says “Search failed” | Search encountered an error. Read the error, then run `shadow doctor`; inspect the local daemon log if capture is involved. |
| Old data reports an embedding-model mismatch | Stop capture with `shadow disable`, then run `shadow rebuild-index --yes`. It re-embeds saved events and creates a private database backup. Stop other writers during the rebuild. |
| Capture is running but no terminal event appears | Check `shadow consent status`, your `HISTFILE` / `~/.zsh_history`, and whether your shell has written the command to that file. The watcher does not replay old history on a normal start. |

The local database, configuration, keys, and daemon log live under
`~/.latticeshadow`, including when encrypted iCloud packet sync is enabled.
Older iCloud live vaults require the manual migration described in the
[CLI guide](../packages/cli/README.md): stop capture, back up both folders,
copy old live files and sidecars into `~/.latticeshadow` without overwriting
local files, temporarily disable iCloud sync to verify local recall, then remove
old iCloud copies and re-enable sync. Leave `sync_packets` in iCloud so encrypted
packet sync continues. Logs can contain
local paths and other operational details. Read them locally; check their
contents before sharing them in a public issue.

For development checks, run `make test`, `make docs`, and on macOS
`make test-cli`. The [CLI guide](../packages/cli/README.md) covers more commands.
