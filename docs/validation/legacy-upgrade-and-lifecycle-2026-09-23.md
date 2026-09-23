# Legacy upgrade and final app lifecycle probe

This probe used disposable macOS guests and synthetic data. The 0.2.0 app was
the unsigned Apple Silicon ZIP built from
`b603442dfb471cf22f9a561f1b034a6582f830d9` (425,567,521 bytes;
SHA-256 `6bcf2edec4a128323bfb425885ca445efbd9208346bd22bd1aa352df1d70e35e`).
These are internal test artifacts, not public binaries. The guests were cloned
from a CI base image; Homebrew and Xcode Command Line Tools were moved aside.

## Populated 0.1.0 vault upgrade

In one guest, the exact 0.1.0 app created a Keychain-wrapped, 196-byte `.key`
and saved a synthetic record with the hash embedding model selected. The same
running 0.1.0 installation reopened that record by ID and text with
`timeline --json`; the SQLite vault contained one vector. The key and vault
were checkpointed inside that disposable guest before the app-only upgrade.

Replacing the app bundle with 0.2.0 preserved the key checksum and vault row.
Headless recall waited for Keychain access. In a logged-in GUI session, the OS
showed private-key export and access prompts. Four one-time approvals allowed
the prompts to finish, but `timeline --json` reported that the existing master
key could not be unlocked. The 0.2.0 app left the `.key` unchanged and the vault
row intact.

As a discriminator, the 0.1.0 app bundle was reinstalled over the same data.
It also failed to reopen the old record. Its older key handling generated a new
wrapped key and then reported that the vault was unrecoverable. The original
`.key` was immediately restored from the guest-only checkpoint; its SHA-256
again matched the pre-upgrade value, and the vault still held one vector. That
restoration cannot restore any Keychain private key the old code may have
replaced.

**Result:** successful decryption of a populated 0.1.0 vault after an app
upgrade remains **unverified**. Because even a reinstalled ad-hoc 0.1.0 bundle
could not reopen the key, this run does not isolate a 0.2.0 format regression.
The likely constraint is the unsigned app's changing identity and macOS
Keychain access control; the exact cause was not proven. No real user vault was
involved. A signed, stable-identity upgrade and interactive Keychain approval
need separate validation before claiming legacy upgrade support.

## Final 0.2.0 daemon lifecycle

A separate fresh guest installed the exact 0.2.0 ZIP. `shadow install` wrote
the LaunchAgent without starting capture. `shadow consent set clipboard on`
and `shadow consent set terminal_history off` made the source choices explicit.
`shadow enable` started the daemon. A synthetic copy made inside the guest
increased its vault entry count from zero to one. Host clipboard sharing was
disabled.

`shadow pause` showed **RUNNING / PAUSED**. A normal in-guest reboot preserved
the exact config file, an adjacent canary, and the wrapped `.key` byte for
byte. After reboot, an SSH-only session showed the LaunchAgent registered but
**STOPPED / PAUSED**; this run did not complete an interactive desktop login to
test automatic startup. Explicit `shadow enable` returned **RUNNING / PAUSED**.
`shadow disable` stopped the daemon. `shadow remove`, with its default No
answer to deleting data, removed the LaunchAgent while preserving the `.key`
checksum and the one vault row.

This confirms the final artifact's explicit daemon lifecycle and normal-reboot
pause persistence in a stripped CI guest. It does not establish automatic
LaunchAgent startup after a desktop login, stock Gatekeeper behavior, or
Developer ID signing and notarization.
