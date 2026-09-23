# Packaged macOS app: final alpha candidate

This report covers the unsigned Apple Silicon app built from source commit
`d1c3e3bb7ac2c0b0c272bc9ebddb8ad963c10b27`. The ZIP is **425,565,684
bytes**, SHA-256
`9299111d69af2d1fa0ee573d44679af0b861002aafe1fa21705976fffa4c66d8`.
It is an internal test artifact, not a public download.

`verify_app.py` passed on the build guest: version 0.2.0, arm64 launcher,
offline bundled-model encode and native-resource smoke check, valid ad-hoc
signature, and 307 Mach-O dependency links. The unpacked app contained
1,098,432,419 file bytes. Its pinned embedding model revision was
`f60985c706f192d45d218078e49e5a8b6f15283a`; the model weight SHA-256 was
`164fc63ee9f9267be7378fcbd7df99d09788a2f45244c92aa99ae5a574925716`.
The archive checksum matched on the build host and in the install guest.

## Installed-guest checks

The guest ran macOS 26.6.2 and was cloned from a CI base image. Homebrew and
Xcode Command Line Tools were moved out of their normal locations **before**
the exact ZIP was installed under `/Applications`. The app ran with a minimal
environment and network model access disabled. All events and clipboard values
were authored synthetic test data; host clipboard sharing was off.

| Check | Observed result |
| --- | --- |
| First launch | The offline bundle check loaded imports, native resources and the model. |
| Manual recall | `shadow remember note` returned an event ID. A project-scoped offline `shadow timeline --query` returned that same ID and exact text. |
| Explicit daemon setup | `shadow install` wrote a LaunchAgent using `Contents/MacOS/LatticeShadow --daemon` and did not start capture. Clipboard was explicitly allowed; terminal history was explicitly turned off. `shadow enable` reported a running daemon. |
| Clipboard boundary | A value already present before enable did not increase the vault count. A later synthetic copy increased it from one to two. |
| Pause and reboot | `shadow pause` set `paused = true`. A normal guest `shutdown -r now` preserved the configuration and an adjacent canary byte-for-byte; after login, `shadow status` reported **RUNNING** and **PAUSED** with the source choices intact. |
| Removal | `shadow disable` stopped the daemon. `shadow remove` with the default No answer removed the LaunchAgent plist and preserved the key file byte-for-byte and both vault records. |
| Locked Keychain | In a separate app-only upgrade of a guest with a populated 0.2 vault and nonraw 196-byte key file, a locked login Keychain caused a clear CLI failure in under one second. The key checksum and vault count were unchanged. The CLI did not hang or replace the key. |

The first two immediate host-side `tart stop`/restart trials returned
`paused = false` after `shadow pause`. A later normal in-guest reboot preserved
`paused = true`, the exact config SHA-256, and an adjacent canary SHA-256. The
host-side stop is an abrupt VM lifecycle action; this report treats the normal
guest reboot as the persistence check and retains the contradictory abrupt-stop
observation for follow-up. The product's config writer fsyncs the file and
directory, but this run did not isolate the VM storage behavior further.

## What remains open

- Headless SSH after the app migrated a fresh key to Keychain-wrapped form could
  not decrypt it without an interactive login approval. The CLI failed quickly
  and left the key and vault intact; the already-running daemon captured the
  later synthetic copy. A logged-in approval and subsequent CLI/GUI recall of
  that same key still need direct validation.
- The earlier 0.1.0-to-0.2.0 upgrade attempt preserved an old key but did not
  prove successful decryption from a populated old vault. A fresh attempt to
  create a 0.1.0 vault in a headless stripped guest failed to create a usable
  Keychain keypair. Successful legacy upgrade remains unproven.
- The final styled Recall panel was not visually inspected in this headless
  guest, and no screenshot was retained. Earlier GUI results cover an older
  artifact only.
- The guest started from a CI image with Gatekeeper assessment disabled. It
  does not establish a stock minimal-macOS or stock Gatekeeper result. The app
  has no Developer ID signature or notarization; public binary distribution
  remains gated on signing, notarization and first-launch testing.

These checks establish the exact archive's offline manual recall, explicit
daemon lifecycle, clipboard consent boundary, normal reboot pause persistence,
and fail-closed locked-Keychain behavior. They do not establish a public-release
binary or a fully tested legacy upgrade.
