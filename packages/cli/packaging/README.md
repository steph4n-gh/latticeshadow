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
display. At that stage, full offline save/search, a visible UI journey,
upgrade/removal, stock Gatekeeper and final signed-artifact checks remained
pending.

## Integrated 0.2.0 candidate lab result

The app built from source commit `0af86431d4f6d5b83d5b799e858cee11853bef97`
passed `verify_app.py`: arm64 launcher, offline bundled-model encode and native
resource smoke check, code-signature check, and 307 Mach-O library-link checks.
The ZIP is 425,566,350 bytes with SHA-256
`94e2daf036f54a650cd6a357688b04f9fb723cf2b8f05c56f9bae3a9b05b2ad8`.
It is an ad-hoc signed internal test artifact, not a public release binary.

In a disposable macOS 26.6.2 guest cloned from a CI base, Homebrew and Xcode
Command Line Tools were removed before installing that exact ZIP under
`/Applications`. With an empty application profile, the app's offline bundle
check passed. `shadow remember note` saved a synthetic event while network
model access was disabled; an offline project-scoped `shadow timeline --query`
returned its exact ID and text, and the same query in an unrelated project
returned no events. No capture source was enabled by installation.

`shadow install` left the service stopped and wrote a launchd command of
`Contents/MacOS/LatticeShadow --daemon`. After explicit off choices for
clipboard and terminal history, `shadow enable` reported running with no
capture sources; `launchctl list` showed a live PID and exit status 0.
`shadow disable` stopped it. `shadow remove` with the default No answer removed
the launchd plist but preserved the vault key and configuration byte-for-byte,
and the saved event remained searchable afterward.

A separate guest with version 0.1.0 installed first preserved the key and
configuration byte-for-byte when its app bundle was replaced with 0.2.0. The
first attempt to use that old wrapped key then waited inside macOS Keychain
private-key decryption, consistent with an access-approval prompt after the
ad-hoc app signature changed. The logged-in UI approval and a post-approval
read remain to be checked. The stripped CI-base guest also cannot prove an
out-of-box minimal macOS install or stock Gatekeeper behavior. The build guest
had no Developer ID Application signing identity, and no notarization credential
environment variables were present. Signed/notarized first-launch validation
remains pending.

## Prior alpha candidate

The later 0.2.0 candidate from source commit
`d1c3e3bb7ac2c0b0c272bc9ebddb8ad963c10b27` produced a 425,565,684-byte
ZIP with SHA-256
`9299111d69af2d1fa0ee573d44679af0b861002aafe1fa21705976fffa4c66d8`.
The app verifier passed its offline model/resource smoke check, ad-hoc
signature and 307 Mach-O link checks. In a stripped CI-base guest, offline
manual save/search, explicit launchd enable/disable/remove, clipboard
preexisting-value exclusion and later-copy capture passed. A normal guest
reboot preserved consent and pause state; default removal preserved the key
and vault. A locked Keychain caused a prompt CLI failure without changing an
existing nonraw key file or populated vault.

Headless access to a Keychain-wrapped key remains unavailable without an
interactive login approval. The final styled GUI journey, successful
0.1.0-to-0.2.0 decryption, stock Gatekeeper first launch, Developer ID signing,
and notarization are still pending. See the
[exact-artifact validation report](../../../docs/validation/packaged-app-2026-09-23.md)
for the observations, including the abrupt Tart-stop pause discrepancy.

## Earlier packaged regression candidate

The later candidate from source commit
`7ccc29c1c62828e5c3a0f41bdb2a9d9ff89eb776` produced a 425,566,640-byte
ZIP with SHA-256
`651b313ff911d9da67c09fcfdc83581161cab53f701e49f418d7a7e1240a31c0`.
The app verifier passed for version 0.2.0, the arm64 launcher, offline bundled
model and native resources, ad-hoc signature, and 307 Mach-O library links.
The exact ZIP checksum matched in a stripped install guest.

Offline manual save/search returned an authored note. Using a retained
synthetic portable-backup fixture, a wrong-passphrase restore exited nonzero
in under one second, reported authentication failure, created no destination,
and showed no py2app launch error. A correct restore and inspection reopened
two retained events and one deletion tombstone. In a separate fresh target,
the full restore/activation script completed and its first `timeline --json`
output parsed as JSON containing both retained IDs.

That fresh target's key remained in the legacy raw format because its headless
session could not create a software Keychain keypair. The specific
first-use **key migration notice** was therefore not triggered in this run,
although the first-use JSON output was clean. The later `b603442` candidate
superseded this archive for the full packaged performance and recovery repeat.
Developer ID signing, notarization,
stock Gatekeeper behavior, interactive Keychain approval, and the final styled
GUI journey remain outside this packaged regression check.

## Current packaged validation candidate

The app rebuilt from source commit
`b603442dfb471cf22f9a561f1b034a6582f830d9` produced a 425,567,521-byte
ZIP with SHA-256
`6bcf2edec4a128323bfb425885ca445efbd9208346bd22bd1aa352df1d70e35e`.
The exact checksum matched in the build and stripped install guests.
`verify_app.py` passed for version 0.2.0, arm64 launcher, offline bundled-model
and native-resource smoke, ad-hoc signature, and 307 Mach-O dependency links.

In a dedicated disposable guest with only synthetic data, both a missing vault
and an existing vault opened with a wrong key made `timeline --json` exit
nonzero with **zero stdout bytes**; guidance appeared on stderr. The same
guest's vault was actually crypto-shredded. Its synthetic Keychain entry was
successfully removed, and the CLI accurately reported that result. The branch
where Keychain deletion fails was covered by a focused unit test, but could
not be induced in that guest without changing the tested artifact or OS state.

In a separate fresh stripped target, a wrong-passphrase restore of the retained
synthetic backup exited promptly, created no destination, and showed no py2app
launch error. Correct restore, inspection, and activation reopened two retained
events and one deletion tombstone; the first `timeline --json` output parsed
cleanly with both retained IDs. The guest did not create a software Keychain
keypair, so this run did not trigger a key-migration notice. In two further
fresh stripped guests, the exact archive passed a complete export/restore flow:
the retained canonical event JSON matched byte-for-byte after activation and
the deletion tombstone persisted. A wrong passphrase left no destination and
did not interfere with a later correct restore. The first `timeline --json`
after activation remained valid JSON, with the key-migration notice on stderr.
The same archive handled 100 warmed full MCP queries against 10,000 synthetic
events at p95 300.57 ms; the cold first call took 5.36 s. See the
[final package lab report](../../../docs/validation/mini-b603-packaged-2026-09-23.md)
for the workload and limits.

The exact app also passed a [logged-in packaged Recall journey](../../../docs/validation/desktop-gui.md)
and [explicit daemon, reboot, and removal checks](../../../docs/validation/legacy-upgrade-and-lifecycle-2026-09-23.md)
with synthetic data. A populated 0.1.0 upgrade remained unreadable even after
one-time Keychain approval; reinstalling the ad-hoc 0.1.0 bundle also failed.
The 0.2.0 app preserved the key and vault on failure. Successful legacy upgrade,
cross-app shortcut behavior, stock Gatekeeper first launch, Developer ID signing,
and notarization remain open checks before a public binary release.
