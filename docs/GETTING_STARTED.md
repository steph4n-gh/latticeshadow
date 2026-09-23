# Getting started

For the full guided reference, including the menu bar, backup recovery, and
every command family, see the [user manual](USER_MANUAL.md).

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

The wizard asks only about clipboard and terminal capture. Its defaults reflect
the current configuration; both are off in a fresh setup. Other services remain
off until you enable them individually with `shadow consent set`. You can make
the two required choices without the wizard. For example, to capture clipboard
text but leave shell history alone:

```sh
shadow consent set clipboard on
shadow consent set terminal_history off
shadow consent status
shadow enable
```

`shadow install` prepares a launch agent and key material; it does not start the
daemon, including at the next login. Re-running install stops an existing
daemon; run `shadow enable` again when ready. `shadow enable` loads the retrieval
model and starts it. The daemon
captures future changes, not a guaranteed complete record of your past work.
The Zsh history watcher begins at the end of the current history file, and
history writes depend on your shell settings. Clipboard capture skips some
concealed content but is not a reliable secret detector.

On upgrade, existing source settings stay as configured, but an old setting
without a recorded choice must be confirmed before capture restarts. Check
`shadow consent status` and use the wizard or `shadow consent set` for each
pending source. After updating the checkout, run `shadow install` again to
refresh the local daemon code baseline, then `shadow enable`. Reinstalling
stops the old daemon. Manual saves and searches do not require capture consent.

Pause capture without changing your source choices, including across a reboot:

```sh
shadow pause
shadow status
shadow resume
```

To exclude a whole source or a literal piece of text before the daemon saves it,
set a JSON array. Matches in text ignore case. These are literal strings, not
regular expressions, and they cannot retroactively delete an event:

```sh
shadow config set inputs.excluded_sources '["terminal"]'
shadow config set inputs.excluded_literals '["example secret prefix"]'
```

Each list accepts at most 100 nonempty entries of at most 256 UTF-8 bytes. An
empty list (`'[]'`) removes that exclusion. To remove events older than 30 days,
use `shadow config set retention.days 30`; `0` disables automatic retention.
The daemon checks once at startup and hourly, using the event's occurrence time
(or insertion time for legacy events without one). It deletes from the live
canonical and optional hot index. Older backups, original source files, and
data already shared with another application are separate copies.

The menu-bar app (`shadow gui`) has a recall panel with recent events when the
search field is empty. Select a result to see its source, time, project, ID, and
preview. You can copy, open a supported link or file, assign a project, or
confirm Forget. The menu lets you choose Option-Space, Control-Option-Space,
Command-Option-Space, or Off for the shortcut. Use **Open Recall…** in the menu
if a shortcut is unavailable. Desktop behavior is still being checked in a
logged-in test VM for this alpha.

Stop the daemon with:

```sh
shadow disable
shadow status
```

If you want to change sources later, disable the daemon, run
`shadow consent set <surface> on|off`, check `shadow consent status`, then enable
it again. In particular, the terminal watcher is selected when the daemon
starts. Disabling the daemon keeps it off across logins and reboots; it does not delete saved memories.
Use `shadow forget --id ...` for selected events, or inspect `shadow remove`
before uninstalling. `shadow remove` asks separately whether to delete local
data.

## Make a portable backup

Export a passphrase-encrypted snapshot of the canonical vault:

```sh
shadow backup export ~/Desktop/latticeshadow.lsb
shadow backup restore ~/Desktop/latticeshadow.lsb --destination ~/Desktop/latticeshadow-restored
shadow backup inspect --destination ~/Desktop/latticeshadow-restored
```

The passphrase is prompted, not placed on the command line. For a controlled
script, `--passphrase-fd N` reads one line from an already-open descriptor.
The archive includes event IDs, text, provenance, tombstones, and source-model
identity. It excludes local keys, settings, logs, consent, sync state, and
derived indexes. The current alpha rejects a plaintext payload above 256 MiB;
export and restore can temporarily need several times that much RAM.

Restore creates a **new** private directory and key, then verifies that it can
reopen the encrypted vault. It does not touch your current vault or Keychain,
enable capture, or connect an assistant. The destination's `.key` is a private
0600 file; keep that directory and archive safe. The command prints activation
steps for a fresh macOS profile without an existing LatticeShadow vault or
Keychain key. There is no automatic replacement or merge of a live vault.

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

## Assistant access (explicit grant)

Save a harmless note in a named project, then grant an assistant access to that
project and source:

```sh
shadow remember note "Restart the widget queue" --project ops --source manual
shadow mcp grant create --project ops --source manual
shadow mcp grant preview GRANT_ID
shadow mcp serve --grant GRANT_ID
```

Replace `GRANT_ID` with the ID printed by create. The last command is a local
stdio server for a separately configured MCP host; it does not start a network
listener. Its tools are read-only recall, recent context, extractive summaries,
and current citation resolution. No grant means no memory reads. Run
`shadow mcp grant revoke GRANT_ID` to stop later requests. Redaction remains
best effort, and already shared text cannot be recalled from an assistant.
See the [MCP sharing guide](MCP.md) for host arguments, limits, and the
independent client test and a synthetic Codex CLI host walkthrough.

## If something goes wrong

| Symptom | What to check |
| --- | --- |
| `shadow: command not found` | Activate the repository environment with `source .venv/bin/activate`; run `make setup` if it does not exist. |
| First save or `shadow enable` cannot load the model | Check network access for the first Hugging Face download. Retry after connectivity returns. Avoid switching to hash embeddings in an existing collection. |
| `shadow status` says `STOPPED` | This is normal for manual use. To start capture, review sources above, then run `shadow install` and `shadow enable`. |
| `shadow enable` asks for capture choices | Run `shadow consent wizard`, or set both `clipboard` and `terminal_history` explicitly with `shadow consent set <source> on\|off`, then retry. |
| `shadow enable` says the daemon source changed | Review the checkout update, run `shadow install` to refresh the local code baseline, then retry `shadow enable`. |
| An existing master key cannot be unlocked | Retry from an unlocked macOS login session and inspect any Keychain access prompt. LatticeShadow leaves the key file and vault in place; do not remove them while checking recovery. |
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
