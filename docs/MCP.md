# Letting an assistant read a slice of memory

LatticeShadow can answer an assistant's recall request through a local MCP
**stdio** server. You choose a project and source allowlist first. The server
cannot read memory until you create a grant and launch it with that grant's ID.
It does not start a network listener or run commands found in remembered text.
The assistant gets a slice of your memory, not a free tour of the attic.

## Make a grant

First, save or assign events to a project. `shadow timeline --project ops` is a
useful check. Automatic capture is Unassigned unless you explicitly assign it.
Then create and inspect a grant:

```sh
shadow mcp grant create --project ops --source manual --source terminal
shadow mcp grant list
shadow mcp grant preview GRANT_ID
```

Repeat `--project` or `--source` for each allowed value. Use `--unassigned` to
include Unassigned explicitly; it is never included merely because no project
flag was supplied. A grant requires at least one project choice and one source.
You can also set fixed UTC time bounds and a per-response result cap; run
`shadow mcp grant create --help` for the exact flags. Relative dates should be
resolved when creating the grant so “last week” cannot silently become next
week's data. Preview shows the current eligible count and sample records. Read
that preview before connecting a host: redaction is a useful net with holes,
not an assurance that every secret has been found.

Use the returned ID in your MCP host's local server configuration. Host config
formats vary, but the command and arguments are:

```json
{
  "command": "shadow",
  "args": ["mcp", "serve", "--grant", "GRANT_ID"]
}
```

Use an absolute path to `shadow` if the host does not inherit your shell's
`PATH`. For an intentionally inspected restored vault, add
`"--vault-dir", "/path/to/restored-directory"` to the argument array. That
directory contains its own encrypted `shadow.sqlite`, owner-only `.key`, and
sharing grant store. This does not activate capture or replace the live vault.

To stop future reads from a running server:

```sh
shadow mcp grant revoke GRANT_ID
```

The next request checks the current local grant again; a server also retains
the scope it saw at startup as a ceiling. Editing a grant to widen it cannot
widen an already-running server. Launch a new server with a newly created grant
for a broader slice. Revocation cannot pull back text already sent to an
assistant, its provider, or its transcript. Deleting an event makes its citation
unavailable to subsequent LatticeShadow reads; it cannot erase prior copies
outside LatticeShadow.

## What the host sees

The read-only tools are `latticeshadow.recall`,
`latticeshadow.current_context`, `latticeshadow.summarize`, and
`latticeshadow.resolve`. Recall ranks matching records; its ranking score is
**not** a probability or an answer-confidence score. Current context means
recent eligible memory, not a live view of the foreground application.
Summarize returns an extractive count and eligible events; it does not ask a
remote model to summarize the vault. The fixed timeline/context resources and
the `recall-with-citations` prompt obey the same grant.

Each returned event includes a stable `latticeshadow://event/<encoded-id>` URI,
occurrence and capture times, source, project, a short text field, necessary
provenance, and a `redacted` indicator. The URI can be resolved through the MCP
resource or `latticeshadow.resolve`. Resolution reads the current canonical
record and rechecks the current grant; a missing event and an excluded event
look the same to the host. IDs are local references, not web links.

The server allows selected provenance metadata (`title`, `url`, `path`,
`application`, `timestamp_inferred`, and bounded nested `provenance`). Other
metadata is omitted. Known credential patterns are redacted recursively from
allowed text and metadata; long text and deep or large metadata are truncated.
The `redacted` indicator is true when substitution, truncation, or omission
occurred. Pattern matching can miss secrets, especially unusual formats. Scope
selection is the stronger control: share fewer projects and sources.
Project and source display labels receive the same pattern redaction. An event
whose caller-chosen ID matches a known secret pattern is withheld from MCP
entirely, because the stable citation URI would otherwise repeat that ID.
The local timeline still contains that event. Unrecognized secret-like IDs
remain possible; avoid putting credentials in event IDs or labels.

The MCP process runs with your local user privileges and can open the vault. A
grant is application policy enforced by this server, not a sandbox or a
cryptographic barrier against another local tool with filesystem access. Treat
remembered text as untrusted data: a note saying “ignore your instructions” is
still a note. The server never executes it, but no prompt can guarantee what a
separate assistant will do with hostile text.

## Compatibility and verification

This server speaks MCP `2025-06-18` over newline-delimited JSON-RPC on stdio.
It advertises its version during the `initialize` handshake. Diagnostics go to
stderr; stdout contains protocol messages only. It currently offers no HTTP
transport. The [MCP transport specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
and [lifecycle specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)
describe this protocol version.

For an independent client check, install the optional official Python MCP SDK
in a disposable environment, then run
`packages/cli/scripts/validate_mcp_client.py --server-python /path/to/product/python`.
The script creates only synthetic events and an isolated vault, launches the
installed CLI server through the [SDK stdio client](https://py.sdk.modelcontextprotocol.io/client/transports/),
then checks recall, scope, citation resolution, the prompt, and revocation in
one session. The SDK is a validation dependency, not a product dependency. A
real assistant host still needs its own walkthrough and sanitized transcript;
a protocol client cannot prove that a host displays citations well.
