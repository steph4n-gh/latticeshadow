# LatticeShadow user manual

*A field guide to saving a thought, finding it again, and deciding what the app may watch.*

**Edition:** version 0.2.0 daily-use alpha.

**Audience:** people trying the macOS client from source.
**Status:** development prototype. The public releases are source archives. An
Apple Silicon app has been built for internal validation, but there is no signed,
notarized public binary to install yet.

This manual follows the implemented commands. It explains the ordinary path
first and puts the more speculative machinery near the back. A tool that
remembers everything would be a problem; a tool that remembers exactly what you
asked it to remember is a more reasonable starting point.

## Contents

1. [What you are installing](#1-what-you-are-installing)
2. [First session: save and find one note](#2-first-session-save-and-find-one-note)
3. [Everyday recall and projects](#3-everyday-recall-and-projects)
4. [Background capture and consent](#4-background-capture-and-consent)
5. [The menu bar and Recall panel](#5-the-menu-bar-and-recall-panel)
6. [Back up and restore](#6-back-up-and-restore)
7. [Let an assistant read a chosen slice](#7-let-an-assistant-read-a-chosen-slice)
8. [Settings, status, and local files](#8-settings-status-and-local-files)
9. [Stop, remove, or start over](#9-stop-remove-or-start-over)
10. [Troubleshooting](#10-troubleshooting)
11. [Command reference](#11-command-reference)
12. [Glossary and limits](#12-glossary-and-limits)

## 1. What you are installing

LatticeShadow has two packages. `latticeshadow-db` is an independently
installable SQLite-backed Python library. `latticeshadow-cli` is the macOS
client: manual memory commands, an opt-in capture daemon, a menu bar interface,
and a local MCP server. This manual is about the **macOS client**. The
[DB guide](../packages/db/README.md) covers library use on other systems.

For the supported source setup, use a Mac with Python 3.12 or newer, Xcode
Command Line Tools, and network access for dependencies and the first retrieval
model download. From a checkout:

```sh
git clone https://github.com/steph4n-gh/latticeshadow.git
cd latticeshadow
make setup
source .venv/bin/activate
shadow --help
```

`make setup` installs both packages in an editable virtual environment and
builds the native component. It **does not** start the daemon, enable capture,
or modify your shell startup file. The `shadow` command above is available while
that environment is active. Re-activate it in a new shell, or use the absolute
path to `.venv/bin/shadow`.

> **First-run boundary:** installation is quiet. Your first manual save creates
> the vault; background capture still waits for your explicit source choices.

The first real save or search downloads the pinned Static Retrieval MRL model
from Hugging Face. Embeddings then run locally. Ordinary remember/search has no
cloud language-model provider by default. If the model is unavailable, the
client reports an error instead of silently switching to hash vectors.

The internal `.app` candidate is arm64 and ad-hoc signed for testing. It has
passed selected disposable-guest checks, but upgrade approval, stock
Gatekeeper, signing, and notarization are still open release gates. This manual
does not offer it as a public installer. Build details and exact evidence are in
[App packaging](../packages/cli/packaging/README.md).

If you have a working 0.1 vault, **do not replace its app with this unsigned
candidate**. A wrapped key may require Keychain approval after an app's signing
identity changes, and the 0.1-to-0.2 upgrade path has not passed that check.
Keep the original app, vault, `.key` file, and Keychain item intact. Deleting a
key to clear an access prompt can make the vault unreadable.

## 2. First session: save and find one note

Start with synthetic or harmless text. You can do all of this with the daemon
stopped:

```sh
shadow remember note "Restart the widget queue after checking its log" --project ops
shadow timeline --project ops
shadow timeline --query "widget queue restart" --project ops
shadow status
```

`remember` prints a generated event ID such as `note_...`; keep it if you want
to edit or delete that particular event. `timeline` without `--query` lists
recent events. With `--query`, it ranks matches within the selected scope.
`shadow status` may say `Daemon: STOPPED`. That is normal: manual saves are not
background capture wearing a disguise.

To remove the test note, replace `YOUR_ID` with the printed ID:

```sh
shadow forget --id YOUR_ID
```

Review the selected ID and type `FORGET` when prompted. `--yes` skips the prompt
for scripts; it deserves the usual respect for a delete switch. Forgetting
removes selected events from the live canonical store and attempts to clean
optional mirrored indexes. Read any cleanup warning before assuming all live
copies are gone. Backups, original source files, synced copies, and text already
sent to another application are separate.

## 3. Everyday recall and projects

### Save with useful provenance

`shadow remember TYPE TEXT` accepts these event types: `clipboard`, `terminal`,
`note`, `ambient`, `file`, `url`, `app`, `repair`, `sync`, and `model_call`. For
ordinary manual use, `note`, `terminal`, `file`, and `url` are usually enough.
The type describes the event; the **source** is a separate label used in
filters and sharing grants. Without `--source`, source equals type.

```sh
shadow remember terminal "Run the regression suite before the release" --project ops
shadow remember url "Widget queue runbook" \
  --metadata '{"url":"https://example.org/runbook"}' --project ops
shadow remember note "A decision with a real date" \
  --timestamp '2026-09-22T14:00:00-04:00' --project ops
```

`--timestamp` must include a timezone. If omitted, the event's occurrence time
is inferred from capture time. `--metadata` takes a JSON object. Use `--id`
only when you have a deliberate stable identifier; avoid secrets in IDs, project
names, and source labels. They are metadata, not encrypted document text.

### Find, narrow, and explain

```sh
shadow now
shadow timeline --project ops --source terminal --limit 20
shadow timeline --unassigned --since '2026-09-01T00:00:00Z'
shadow timeline --query "regression suite" --project ops --json
shadow why "what did I do before release?"
shadow search "regression suite"
```

`now` shows recent local context without calling an LLM. `timeline` is the main
scoped view: filter by `--project` or `--unassigned`, `--source`, and a
timezone-aware `--since`/`--until` interval. `--since` includes its boundary;
`--until` excludes it. Use `--json` for structured output. A recent (non-query)
page may print a cursor; repeat the same filters with `--cursor CURSOR` for the
next page. Query-ranked pages do not accept cursors. `search` is a shorter,
unscoped search command. `why` shows matches alongside recent context; it is an
explanation aid, not proof that a result answers your question.

Search scores are ranking signals, **not** probabilities or answer confidence.
An authored synthetic fixture reached the project's top-five recall target,
but that does not promise every paraphrase will work on your notes. If a query
misses, inspect a recent timeline, narrow the scope, or try words that appear
in the saved text. The model is helpful; it is not clairvoyant.

> **Remembering is selective:** a project and source are exact filters. If a
> result seems to vanish, check the scope before assuming the vault forgot it.

### Assign or clear a project

Captured events start Unassigned unless you assign them. Manual events may get
a project at save time. Existing events can be moved without changing their ID:

```sh
shadow assign-project --id YOUR_ID --project ops
shadow assign-project --id YOUR_ID --unassigned
```

Repeat `--id` to update several events. A project is an exact label and a useful
sharing boundary, not a folder or an automatic classifier. Check the result
with `shadow timeline --project ops` or `--unassigned`.

### Open, summarize, and delete carefully

`shadow open-context "widget runbook" --dry-run` prints a supported URL or
absolute local file target from the best matching event. Omit `--dry-run` to
open the web URL or reveal the local file. It does not execute a command from a
memory. `shadow summarize --query "widget"` summarizes matched events;
without a configured provider it returns an extractive local summary. If you
opt into a remote LLM provider, summarization and other LLM commands may send
processed context to that provider. `--no-foundation` skips an optional
Foundation Models bridge, but does not disable a separately configured LLM.

For a bounded selection before deletion:

```sh
shadow forget --query "widget queue" --project ops --limit 5
shadow forget --source terminal --since '2026-09-01T00:00:00Z' --limit 10
```

The command prints the IDs it selected and asks for `FORGET`. Prefer `--id`
when you know exactly which record should go.

## 4. Background capture and consent

Background capture is optional. The daemon can watch future clipboard text and
new entries written to Zsh history. Both sources default **off**, and you must
record an explicit on/off choice for each before `shadow enable` will start.
Capture can include very personal text. A password manager's concealment marker
and LatticeShadow's filters are useful, but neither recognizes every secret.

Prepare the launch agent, choose sources, then start it:

```sh
shadow install
shadow consent wizard
shadow consent status
shadow enable
shadow status
```

The wizard covers clipboard and terminal history. You can choose directly,
including the valid choice to capture nothing:

```sh
shadow consent set clipboard off
shadow consent set terminal_history off
shadow enable
```

`shadow install` prepares key material and a user launch agent **without**
starting capture, including at the next login. Re-running it stops a running
daemon. `shadow enable` loads the retrieval model, checks the recorded choices
and code-integrity baseline, then starts the agent. After a source-code upgrade,
review the change and run `shadow install` again before enabling. An old source
setting without a recorded choice must be confirmed again.

Capture is prospective. The terminal watcher starts at the current end of the
history file and depends on your shell writing to `HISTFILE` (often
`~/.zsh_history`). It does not promise a complete record of earlier commands.
Clipboard capture reads copied text when enabled; it is not a universal secret
filter. It takes a baseline at startup or after resuming: text already on the
clipboard is not a new capture, while later changes may be. Neither source
needs to be enabled for manual memory.

Pause keeps source choices and persists across a restart; disable stops the
service across logins:

```sh
shadow pause
shadow status
shadow resume
shadow disable
```

When paused or off, new terminal history should not be backfilled into capture
later. `shadow resume` requires the necessary choices. If you change a source
choice while the daemon is running, check `shadow status`; for a predictable
restart, disable, change consent, and enable again.

To skip a whole source or a case-insensitive literal before capture, set JSON
arrays:

```sh
shadow config set inputs.excluded_sources '["terminal"]'
shadow config set inputs.excluded_literals '["example secret prefix"]'
shadow config set retention.days 30
```

Each exclusion list allows at most 100 nonempty strings of at most 256 UTF-8
bytes. These rules affect future capture, not records already saved. Age
retention checks at daemon startup and about hourly, using occurrence time (or
insertion time for older events without one). `retention.days 0` turns it off.
Retention removes live records and optional hot-index copies, not old backups,
original clipboard/history material, or text already shared elsewhere.

Optional ambient context, iCloud packet sync, mesh, mobile API, and hot index
are separate consent or configuration choices and remain experimental. Review
[Capabilities and limits](CAPABILITIES.md) before enabling them. Do not point a
peer listener at an untrusted network.

## 5. The menu bar and Recall panel

On a logged-in Mac, `shadow gui` starts the AppKit menu bar client. The menu has
**Open Recall…**, capture status, Pause/Resume, and a keyboard-shortcut choice.
The shortcut defaults to Option–Space; Control–Option–Space,
Command–Option–Space, and Off are available. If macOS does not permit the
shortcut, use the menu item. The alpha's full logged-in UI and shortcut
permission behavior are still under validation.

With the Recall panel open:

1. Leave search empty for recent events, or type a query for ranked matches.
2. Narrow by **Project**, **Unassigned only**, **Source**, and **When** (Any
   time/Today/Last 7 days/Last 30 days).
3. Select a row to read its preview, source, project, occurrence/capture times,
   and local event reference.
4. **Copy** puts the full current event text on the clipboard; Return also
   copies. **Open link/file** supports web URLs and existing local files only.
5. **Assign project** edits the selected event's label. **Forget…** asks for
   confirmation and removes it from live views. Escape closes the panel.

The preview can shorten very long text; Copy uses the complete event. Before
copy/open/assign/forget, the panel rereads the selected event in the current
scope. If another operation removed or moved it, the panel asks you to refresh
instead of acting on a stale row. Pause/Resume here changes the same persistent
capture state as the CLI. The GUI is a view of your local vault, not a separate
copy of it.

The exact current labels are collected in the [GUI action reference](#gui-action-reference).

## 6. Back up and restore

A portable backup is a passphrase-encrypted snapshot of the **canonical** vault:

```sh
shadow backup export ~/Desktop/latticeshadow.lsb
shadow backup restore ~/Desktop/latticeshadow.lsb \
  --destination ~/Desktop/latticeshadow-restored
shadow backup inspect --destination ~/Desktop/latticeshadow-restored
```

The passphrase is prompted rather than placed in shell history. For a controlled
script, `--passphrase-fd N` reads one line from an already-open file descriptor.
Use a passphrase of at least eight characters and keep both it and the archive
safe. Export refuses to overwrite an existing archive. The current archive
plaintext cap is 256 MiB and 100,000 records/tombstones; export and restore can
temporarily need several times the plaintext size in memory.

The archive includes event IDs, text, provenance, tombstones, and source-model
identity. It excludes local keys, config, consent, logs, sync state, and derived
indexes. Restore creates a **new** owner-only directory and key, verifies the
new vault, and never merges with or replaces the live vault. Inspect reopens
that destination without activating capture. The printed activation directions
are for a fresh macOS profile with no existing LatticeShadow vault or Keychain
key. A wrong passphrase exits with an error and leaves no new destination;
an existing restored vault is left alone. Do not copy it over a populated
profile and hope the keys negotiate.

> **Recovery rule:** inspect the restored copy first. Activation is a deliberate
> move into an empty profile, not an overwrite button.

A restored destination may also be inspected through an explicit MCP
`--vault-dir` and its own grant, without making it the live vault. That is a
controlled read of a separate recovery copy, not an automatic restore.

## 7. Let an assistant read a chosen slice

The MCP server is local **stdio**, launched by a separately configured host. It
does not open a network listener. It is read-only and requires a grant with at
least one project choice and one source. Match the source you actually saved:

```sh
shadow remember note "Restart the widget queue" --project ops --source manual
shadow mcp grant create --project ops --source manual
shadow mcp grant list
shadow mcp grant preview GRANT_ID
shadow mcp serve --grant GRANT_ID
```

Replace `GRANT_ID` with the ID printed by create. In an MCP host configuration,
use the absolute path to `shadow` if the host does not inherit your shell's
`PATH`. For the internal app candidate, its bundled CLI wrapper or executable
with `--cli` is the equivalent entry point, but there is no public binary setup
path yet. Grant flags can be repeated for several projects or sources;
`--unassigned` includes Unassigned explicitly. `--since` is inclusive and
`--until` exclusive; both require timezone-aware values. `--limit` caps results
per request. Preview the eligible count and examples before connecting a host.

The tools are `latticeshadow.recall`, `latticeshadow.current_context`,
`latticeshadow.summarize`, and `latticeshadow.resolve`. They return a bounded,
redacted view and local `latticeshadow://event/...` citations. Resolution
rechecks the live event and grant. Ranking scores are not answer confidence.
Current context means recent eligible memory, not a live screen read.
Summaries here are extractive. The full host configuration, protocol details,
and independent client evidence are in [MCP sharing](MCP.md).

```sh
shadow mcp grant revoke GRANT_ID
```

Revocation stops subsequent server reads; it cannot recall text already sent to
an assistant or written to its transcript. The server runs with your user
privileges, so the grant is policy inside this server, not a filesystem sandbox
against a separate local process. Pattern redaction can miss unusual secrets.
Use narrow scopes and synthetic data while evaluating this alpha.

## 8. Settings, status, and local files

`shadow status` reports the daemon, capture choices, database size/count,
retention, exclusions, and log path. `shadow consent status` shows each
surface's choice and whether it still needs consent. `shadow config show` prints
all settings; `shadow config get KEY` reads one value. For example:

```sh
shadow config get memory.provider
shadow config get retention.days
shadow config set retention.days 0
shadow privacy-report
shadow doctor
```

The default language-model provider is `none`. `shadow config set
memory.provider ...` can opt into a local or remote provider, depending on its
value; LLM commands may then send context to that provider. `shadow config
set-key PROVIDER API_KEY` takes the key as a command-line argument, which can
appear in shell history or process listings. Avoid entering a real credential
that way on a shared machine. Configuring remote providers and experimental
sync/listener switches deserves a separate review of their data boundary.

Live client data normally lives in `~/.latticeshadow`:

| File or area | Purpose |
| --- | --- |
| `shadow.sqlite` and SQLite sidecars | Canonical local event store. |
| `.key` and macOS Keychain item | Master-key material; keep both private. Key wrapping can fall back to a raw local key file if platform protection fails. |
| `config.toml` | Settings, capture choices, and pause state. |
| `shadowd.log`, `launchd.out`, `launchd.err` | Operational logs; review for paths and details before sharing. |
| Grant store, indexes, and snapshots | Local sharing policy and derived or recovery state. Treat as private. |

Document text in the privacy-enabled collection is encrypted. Event IDs and
SQLite metadata, including type, timestamps, and caller-supplied metadata, are
not protected the same way. Vectors are rotated for similarity search; their
geometry remains available, so rotation is **not** opaque vector encryption.
The local model downloads from Hugging Face on first use. iCloud packet sync,
when separately enabled, keeps the live vault, keys, and logs local while
writing encrypted packets to iCloud.

## 9. Stop, remove, or start over

These actions have different effects:

| Action | Effect |
| --- | --- |
| `shadow pause` | Stop capture while keeping the daemon and source choices; survives restart. |
| `shadow disable` | Stop the launch agent and keep it off across logins; memories remain. |
| `shadow forget --id ID` | Delete selected live events after confirmation; check any index-cleanup warning. |
| `shadow remove` | Stop the agent, remove its plist and optional marked shell hook, then ask whether to delete `~/.latticeshadow`. Default **No** preserves data and key. |
| `shadow shred` | Destructive crypto-shred path for the local vault. It reports Keychain entry removal separately; a locked Keychain can leave that entry behind. Read the command and keep a recoverable backup only if that is your intent. |

For source installation, `shadow remove` does not delete the Git checkout or
`.venv`; remove those separately if you want the development files gone. Do not
manually discard a key while keeping its encrypted vault. Answering Yes to the
`remove` data prompt deletes the local data directory and attempts to remove the
Keychain entry; it does not erase external archives, source history files, or
text already shared with another program.

## 10. Troubleshooting

| Symptom | First check | Next step |
| --- | --- | --- |
| `shadow: command not found` | Is `.venv` active in this shell? | Run `make setup`, then `source .venv/bin/activate`, or use `.venv/bin/shadow`. |
| First save or enable cannot load the model | Does the Mac have network access for the first pinned-model download? | Retry after connectivity returns. Do not mix hash and real-model vectors in one collection. |
| `Daemon: STOPPED` after a manual save | This is expected. | Only if you want capture: `shadow install`, make both source choices, then `shadow enable`. |
| Enable asks for capture choices | Check `shadow consent status`. | Run the wizard or set **both** clipboard and terminal history to on/off explicitly. |
| Enable says the daemon source changed | A checkout update changed its integrity baseline. | Review the update, run `shadow install`, then `shadow enable`. Reinstall stops a running agent. |
| Vault or key unavailable | Check an unlocked macOS login and any Keychain access prompt. | Keep `.key` and `shadow.sqlite` intact; use a verified backup and inspect the error before changing keys. An ad-hoc app upgrade may need access approval. |
| Search finds nothing | Inspect `shadow timeline` without a query and check project/source filters. | Try terms from the saved text; search does not guarantee every paraphrase. |
| Capture runs but terminal events are missing | Check `shadow consent status`, `HISTFILE`, and whether Zsh has written the command. | Normal startup does not replay old history. |
| Old vault reports an embedding-model mismatch | Stop capture and other writers. | Run `shadow rebuild-index --yes`; it preserves IDs/metadata and prints a private database backup path. |
| A deleted result still appears elsewhere | Read the `forget` cleanup output. | Check optional hot indexes, old backups, source files, and copies already shared with another app. |
| Older iCloud installation refuses to open | Legacy live files may remain in the iCloud LatticeShadow folder. | Stop capture and back up both folders; follow the [CLI migration steps](../packages/cli/README.md) without overwriting either copy. |

Logs live under `~/.latticeshadow`. They can include local paths and operational
details; inspect them locally before putting excerpts in a public issue. Use
`shadow doctor` for a broader health check and `shadow audit` to verify the
signed local audit log.

## 11. Command reference

Run `shadow COMMAND --help` for every flag. The tables give the purpose and
important choices; commands in the experimental table are **not** a suggested
first-run checklist.

### Everyday memory

| Command | Purpose and notable arguments |
| --- | --- |
| `remember TYPE TEXT` | Save an event. Optional `--project`, `--source`, `--timestamp`, JSON `--metadata`, `--id`. Prints its ID. |
| `timeline` | Recent events or ranked `--query`; `--project`/`--unassigned`, `--source`, timezone-aware `--since`/`--until`, `--limit`, `--json`. `--cursor` is for recent pages only. |
| `now` | Recent private context, `--limit` or `--json`; no LLM call. |
| `search QUERY` | Quick, unscoped search. Use `timeline --query` for explicit scope. |
| `assign-project --id ID --project NAME` | Relabel one or several IDs; `--unassigned` clears the label. |
| `why QUERY` | Show ranked matches and nearby recent context; `--limit` defaults to five. |
| `summarize` | Summarize recent or `--query` results; `--limit`, `--json`, `--no-foundation`. May use a configured LLM. |
| `open-context QUERY` | Open a supported URL or reveal an absolute file path from a match; `--dry-run` previews, `--limit` bounds inspection. |
| `forget` | Select by repeated `--id`, `--query`, or source/project/time filter; `--limit` bounds search selection. Prints IDs and asks for `FORGET`, unless `--yes`. |
| `paste QUERY` | Copy the top search result back to the clipboard; inspect results first if a wrong copy would matter. |
| `watch` | Stream new clipboard captures until Ctrl-C; capture must be active. |

### Capture, recovery, and sharing

| Command | Purpose and notable arguments |
| --- | --- |
| `install` | Prepare key and launch agent; keeps capture disabled until explicit enable. Re-running stops the old agent. |
| `consent status` / `wizard` / `set SURFACE on\|off` | Inspect, answer required clipboard/terminal choices, or opt into a separate surface. |
| `enable` / `disable` | Start the installed launch agent after consent, or stop it across logins. |
| `pause` / `resume` | Persistently suspend or resume selected capture sources without erasing choices. |
| `status` | Report observable daemon/capture state and local store status. |
| `config show` / `get KEY` / `set KEY VALUE` | Read or change dot-notation settings. Prefer `consent set` for source choices; `config set` routes known consent keys through the same policy. |
| `config set-key PROVIDER API_KEY` | Store an LLM key in Keychain; argument may be exposed in process listings or shell history. |
| `backup export ARCHIVE` | Create a new passphrase-encrypted canonical snapshot; optional `--passphrase-fd`. |
| `backup restore ARCHIVE --destination DIR` | Create and verify a new private vault; never overwrites an existing destination. |
| `backup inspect --destination DIR` | Reopen and count a restored vault without activating capture. |
| `mcp grant create` | Require `--project` or `--unassigned` and `--source`; optional repeatable scopes, time bounds, `--limit`, `--vault-dir`. |
| `mcp grant list/preview/revoke` | Inspect policies, preview current eligible records, or stop subsequent reads. Preview/revoke take a grant ID; each accepts `--vault-dir`. |
| `mcp serve --grant ID` | Local read-only stdio server; optional `--vault-dir` for an explicitly restored vault. |
| `rebuild-index` | Re-embed saved events after a model mismatch. Stop other writers; `--yes` skips its confirmation. |
| `remove` | Stop agent/remove plist and shell hook, then ask whether to delete local data. Default preserves it. |
| `shell enable/disable` | Add or remove the marked optional Zsh widget source line; no key binding is installed. |
| `gui` | Launch the AppKit menu bar client in a logged-in session. |

### Diagnostics and experimental commands

| Command | What it does / maturity |
| --- | --- |
| `doctor` | Local diagnostic health check. |
| `privacy-report` | Inspect local privacy posture; `--json` available. This is a report, not a security certificate. |
| `privacy-test` | Synthetic leakage harness with count/dimension/query options and optional JSON output. |
| `audit` | Verify the signed local audit log; `--json` available. |
| `trust local/list/add/revoke` | Manage experimental peer identities and trust. |
| `repair list/status` | Review or change status of human-approved repair proposals. |
| `native intents` | Print the optional native companion's App Intents command contract. |
| `bench moonshot` | Synthetic retrieval benchmark with selected engines; not a user-data performance guarantee. |
| `sleep` | Experimental REM-style consolidation. May involve a configured LLM. |
| `shred` | Destructive local crypto-shred path; Keychain cleanup is reported separately. |
| `calibrate` | Experimental alignment/drift check (`--check`). |
| `compile` / `recall QUERY` | Experimental holographic daily index and query; distinct from ordinary `timeline` recall. |
| `unswap QUERY` | Experimental state restoration from remembered context. |
| `compose` / `fix` / `evolve` | Experimental command suggestion, traceback repair, and review/apply of pending code mutations. Review any proposed command or diff yourself. |
| `time-travel` | Experimental environment snapshots (`--list`) and rollback (`--rollback HASH`); potentially changes files. |
| `get-ghost-paste` / `get-loop-fix` / `ghost-paste QUERY` | Experimental ambient/speculative helpers. |
| `ask QUESTION` / `recap` / `context` | LLM-powered memory features; data may reach a configured provider. |
| `pot generate/verify` | Experimental signed proof-file commands; not a zk-SNARK guarantee. |

### GUI action reference

| Current label | Action / closest CLI command |
| --- | --- |
| `Open Recall…` | Open the Recall panel; `shadow gui` starts the menu bar client. |
| `Keyboard shortcut` | Choose `Off`, `Option–Space`, `Control–Option–Space`, or `Command–Option–Space`. |
| `Pause capture` / `Resume capture` | Change the persistent capture state; `shadow pause` / `shadow resume`. |
| `Project` | Filter by an exact project label; an empty field includes all projects. |
| `Unassigned only` | Limit panel results to events without a project; `shadow timeline --unassigned`. |
| `Source` | Filter by an exact source label; an empty field includes all sources. |
| `When` | Choose Any time, Today, Last 7 days, or Last 30 days. |
| `Copy` | Copy the selected event's complete text to the clipboard. |
| `Open link/file` | Open a supported web link or reveal an existing local file; `shadow open-context`. |
| `Assign project` | Set a project or leave it blank for Unassigned; `shadow assign-project`. |
| `Forget…` | Confirm deletion of the selected live event; `shadow forget --id ID`. |

## 12. Glossary and limits

| Term | Meaning |
| --- | --- |
| **Event** | One saved unit of text plus an ID, type, source, time, and optional project/metadata. |
| **Canonical vault** | The active SQLite-backed record store used for normal memory views. Derived indexes can mirror it. |
| **Source** | Exact label describing where an event came from; used by filters and MCP grants. Defaults to the event type for manual saves. |
| **Project** | Optional exact label. Unassigned is a real scope choice, not a wildcard. |
| **Occurrence time / capture time** | When the event happened / when LatticeShadow recorded it. Occurrence may be inferred. |
| **Grant** | Local policy selecting projects, sources, time bounds, and a result cap for one MCP server. |
| **Citation** | A `latticeshadow://event/...` local reference resolved against the current vault and grant, not a web link. |
| **Retention** | Optional age-based deletion from live indexes, not erasure of every external copy. |
| **Ad-hoc signed app** | Internal test bundle with a local code signature; it is not Developer ID signed or notarized for public distribution. |

LatticeShadow is meant to make *chosen* work context recoverable with clear
source and deletion behavior. It is not a password manager, exhaustive activity
logger, security boundary against other local processes, or guarantee that every
secret is detected. A local model does not make bad input safe, and rotated
vectors are not opaque encryption. Simulated P2P proofs are not zk-SNARKs.
Source setup and ordinary CLI flows work today; the full desktop, upgrade,
long-run, and public-binary validation is still in progress. The
[capability guide](CAPABILITIES.md) and [alpha evidence ledger](ALPHA_PLAN.md)
track what has actually been tested.

### For maintainers

Keep this manual's [command reference](#11-command-reference) aligned with the
`argparse` definitions in
[`shadow_cli.py`](../packages/cli/latticeshadow/shadow_cli.py) and its
[GUI action reference](#gui-action-reference) aligned with the labels in
[`menu.py`](../packages/cli/latticeshadow/menu.py). Update the manual in the
same change that renames a command, option, or visible action. The tutorial's
`--source manual` is deliberate: `remember note` otherwise defaults to source
`note`, which would not match the example MCP grant.

The offline [print-ready HTML edition](USER_MANUAL.html) is generated from this
Markdown by [`render_user_manual.sh`](../scripts/render_user_manual.sh) using
Pandoc. Keep the Markdown as the source of truth; regenerate the HTML after
editing it. The cover mark is inline SVG, and the manual includes no simulated
product screenshots.
