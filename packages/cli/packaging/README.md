# macOS app build and validation

This directory builds an Apple Silicon LatticeShadow app from the CLI's Python
3.12 environment. The app contains the menu, CLI and daemon in one runtime.
The app is unsigned for internal validation; signing and notarization are a
separate release gate.

## Build

On an Apple Silicon Mac with Xcode Command Line Tools and Python 3.12:

```sh
make setup
.venv/bin/python -m pip install -e './packages/cli[build]'
.venv/bin/python packages/cli/packaging/setup_app.py py2app
python3 scripts/validation/verify_app.py dist/LatticeShadow.app > app-report.json
ditto -c -k --sequesterRsrc --keepParent dist/LatticeShadow.app LatticeShadow-arm64.zip
shasum -a 256 LatticeShadow-arm64.zip
```

The recipe compiles `liblwe.dylib` for arm64, downloads the exact pinned model
revision, resolves its cache symlinks into the app, and includes the model card
and Apache 2.0 license. It copies the PyTorch model files used by the current
embedding path. The bundle check loads that model in offline mode, computes an
embedding, checks the native library and shader, and exercises the CLI launcher.
The verifier also checks code signatures and build-machine library links. A
successful build-host check does not establish independent installation on a
Mac without development tools.

The pinned model is
[`sentence-transformers/static-retrieval-mrl-en-v1`](https://huggingface.co/sentence-transformers/static-retrieval-mrl-en-v1)
at revision `f60985c706f192d45d218078e49e5a8b6f15283a`. Its model card
declares Apache 2.0. The application version comes from
`packages/cli/pyproject.toml`.

## Explicit CLI installation

After moving the app to `/Applications/LatticeShadow.app`, the user can choose
to expose the bundled `shadow` command. These commands fail if a `shadow` link
already exists; inspect an existing command before replacing it.

```sh
mkdir -p "$HOME/.local/bin"
ln -s /Applications/LatticeShadow.app/Contents/Resources/bin/shadow "$HOME/.local/bin/shadow"
```

`~/.local/bin` must be on the user's `PATH` for a bare `shadow` command. The
wrapper resolves its own link to locate the app after an upgrade. Installing
the app or its CLI link does not enable capture.

## G0 feasibility result

A Python 3.12 arm64 py2app build on a disposable macOS guest produced a 406 MB
ZIP (`3578d53393c38bf472e4cb1f0dd53fb3410fdd90ff1c226018a6345a441b2fa8`).
The unpacked app contained 1,098,328,539 file bytes. The verifier passed the
offline model encode, CLI/wrapper and native-library smoke checks, signature
validation and 307 Mach-O link checks.

A separate disposable guest was cloned from a CI image, then had Homebrew and
Xcode Command Line Tools removed. The exact ZIP checksum matched after transfer;
the app installed in `/Applications`, its bundle and CLI checks passed with no
developer PATH, and Launch Services started a persistent menu process. This is
useful independence evidence, with precise provenance: the guest began as a CI
image, and its Gatekeeper assessments were disabled. The first offline `remember`
on that pre-integration app exposed a model-path bug; the memory-core fix needs
a rebuilt integrated artifact. Headless `screencapture` could not capture a
display. Full offline save/search, a visible UI journey, upgrade/removal, stock
Gatekeeper and final signed-artifact checks remain pending.
