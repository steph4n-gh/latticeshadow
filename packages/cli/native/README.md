# Native companion (optional)

This Swift package contains two pieces:

- `LatticeShadowIntents`, a library of App Intents that invokes installed
  `shadow ...` commands. Building the library does not install an app or register
  those intents with macOS; a host app must include it.
- `latticeshadow-foundation-bridge`, an executable that accepts a JSON summary
  request on stdin and prints summary text on stdout.

From the repository root, build with Xcode/Swift tooling on macOS:

```sh
cd packages/cli/native
swift build -c release
```

Point the CLI summarizer at the bridge in that shell:

```sh
export LATTICESHADOW_FOUNDATION_MODELS_CMD="$PWD/.build/release/latticeshadow-foundation-bridge"
```

The bridge uses Apple's Foundation Models only where the framework and local
model are available (macOS 26 or newer in the current code). Otherwise it
returns a short extractive summary. The CLI also has a separate configured LLM
fallback; check its provider before summarizing private events. See the
[CLI README](../README.md) for the recommended first run.
