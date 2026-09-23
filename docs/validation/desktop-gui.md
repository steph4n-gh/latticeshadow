# Desktop recall GUI validation

## Current `20669ff` packaged GUI smoke

The unsigned Apple Silicon app in `LatticeShadow-20669ff-arm64.zip` was
installed in a fresh, logged-in disposable macOS VM. The ZIP SHA-256 was
`6f5e00f2ca44ab85956b47169adb2dd29475e81450ca3db2951f2c319e1a09d6`.
Its bundled CLI saved one synthetic note (`gui-smoke-20669ff`) in a fresh
Keychain-wrapped vault. VM clipboard sharing with the host was disabled; no
capture source was selected.

| Journey | Observed result |
| --- | --- |
| Menu and Recall | The menu reported **Capture needs consent** and opened the styled Recall panel. The note appeared under recent events. |
| Search, filter, preview | Searching `widget` returned one result. Project filter `fieldguide` kept that result. Preview showed the exact synthetic text, note source, project, times, and event reference. |
| Copy | **Copy** reported success; the guest pasteboard contained exactly `The widget queue recovered after a synthetic deployment drill.` |
| Pause and consent | **Pause capture** changed to **Resume capture** while status remained **Capture needs consent**. Resume showed **Choose capture sources before resuming: clipboard, terminal_history** and kept capture paused. |
| Open link/file | A second synthetic event pointed to a file in the disposable guest. **Open link/file** brought Finder forward with that file selected. |
| Assign project | Assigning the note from `fieldguide` to `releasecheck` updated its row and preview. Filtering by `fieldguide` gave **No matches**; filtering by `releasecheck` returned the note. |
| Forget | **Forget…** warned that older backups and the original source might still contain the event. After confirming deletion of the synthetic note, the `releasecheck` filter showed **No matches** and an empty preview. Clearing it left only the file event. |
| Rapid search | Quickly replacing an absent query with `widget` ended on the latest `widget` query with both synthetic events. Rows and preview cleared while searching. This visual sequence does not prove every out-of-order completion race. |

Escape closed Recall. With Finder in front, Option-Space sent through Screen
Sharing instead triggered Finder Quick Look, as in the predecessor run. Remote
modifier forwarding was not established, so the cross-app global shortcut
remains unproven. A Return press on a selected result did not yield copy
feedback or pasteboard text in this remote run; the **Copy** button result above
is the verified copy path. No sanitized panel-only screenshot was retained.

The app was installed from the ZIP inside the disposable VM without a
quarantine attribute. Although Gatekeeper assessments were enabled there, this
does not validate the normal quarantined-download or notarization path. Daemon
capture was not enabled. The guest was stopped after the checks.

## Preceding packaged app in a disposable Mac VM

The unsigned Apple Silicon app in `LatticeShadow-b603442-arm64.zip` was
installed and exercised in a logged-in disposable macOS VM. The ZIP SHA-256
was `6bcf2edec4a128323bfb425885ca445efbd9208346bd22bd1aa352df1d70e35e`.
Its bundled CLI created one synthetic note and one synthetic local file event
in a new vault with a Keychain-wrapped master key. Clipboard sharing with the
VM host was disabled. Capture choices were not completed and no daemon was
started; the menu and panel reported **Capture needs consent**.

| Journey | Observed result |
| --- | --- |
| Menu, recent, search | The menu opened Recall. Both events appeared in recent results and in a `queue` search. The dark panel displayed readable results, actions, and preview. |
| Filters and preview | Source `file` returned only the file. Assigning the note to a new project updated its row and preview; filtering by that project returned only the note. Preview showed text, source, project, times, target where present, and event reference. |
| Copy and open | **Copy** put exactly the selected synthetic note text on the guest pasteboard. **Open link/file** for the synthetic file opened Finder with that file selected. |
| Forget | **Forget…** warned that backups and the original source might still contain the event. After confirming deletion of the disposable note, live results contained only the file. Filtering by the former note project showed **No matches** and an empty preview. |
| Rapid search | Quickly entering a no-result query followed by `queue` ended on the latest query's file result. Rows and preview cleared while searching. This visual sequence does not prove every out-of-order completion race. |
| Pause and resume | **Pause capture** changed to **Resume capture**. Resume showed a consent-required alert and kept capture paused because sources had not been chosen. |

With Finder in front, Option-Space sent through Screen Sharing triggered Finder
Quick Look rather than Recall. This is inconclusive for the global shortcut:
remote modifier forwarding was not established. A local cross-app keyboard
check is still needed. No sanitized panel-only screenshot file was retained;
the remote view included guest desktop and file paths outside the product UI.

## Styled panel on a logged-in local macOS session

The integrated styled AppKit panel was exercised from source containing
`d1c3e3b`, with a disposable encrypted vault holding two synthetic events and
the hash embedding model. This was a source-run GUI check, not an installed-app
or launchd test. The harness redirected configuration and clipboard writes to
isolated test storage. It supplied **Capture stopped** as the daemon status;
therefore that status text is visual evidence only, not proof of daemon state.

| Journey | Observed result |
| --- | --- |
| Search and filters | Searching `queue` returned both events. Project `Nimbus` narrowed to the note; source `file` plus that project returned **No matches** and cleared the preview. Removing the source filter restored the note. |
| Preview and copy | Selecting the note displayed its text, source, project, times, and event reference. **Copy** and Return after focusing the selected row wrote exactly its text to the isolated pasteboard. |
| Open | The note showed **No supported target**, explaining that it had no web link or local file. **Open link/file** was invoked for a synthetic local file, but this run did not obtain a visible success signal from the opened app. |
| Assign project | Assigning the note to `Field Guide` removed it from the `Nimbus` filter and updated the result row and preview when the filter was cleared. |
| Forget | **Forget…** opened a confirmation explaining that backups and the original source might retain the event. The confirmation was canceled in this local run; the earlier installed-app run above covers confirmed deletion. |
| Pause and keyboard | **Pause capture** changed to **Resume capture** and back using isolated configuration. Escape hid the panel, and Option-Space sent through the bound accessory app reopened it. This does not establish that the shortcut works with another app in the foreground. |

The dark panel rendered in the logged-in session with readable search, results,
preview, provenance, and action controls. An inline panel image was inspected,
but no sanitized image file was retained for documentation. Later packaged-app
checks of this styled revision appear above; cross-app shortcut behavior remains
open.

## Earlier installed-app validation

Tested the installed, unsigned 0.2.0 Apple Silicon app built from `0af8643` in
an isolated macOS VM through Screen Sharing. The artifact SHA-256 was
`94e2daf036f54a650cd6a357688b04f9fb723cf2b8f05c56f9bae3a9b05b2ad8`.
The only event was a disposable synthetic note. Capture was disabled and the
daemon had been removed after a separate lifecycle check. This is evidence for
that artifact, not for a later source revision or a signed release.

| Journey | Observed result |
| --- | --- |
| Menu to Recall | The menu reported **Capture stopped** and opened the Recall panel. Escape closed it. |
| Recent, search, preview | The note appeared in recent results. Searching a word from its text returned it. Preview showed the text, source, project, occurrence time, capture time, and stable event reference. |
| Filters | Source, project, Unassigned only, and Today each included or excluded the note as expected. Changing a filter cleared the previous row and preview while the next result loaded. |
| Copy | The button reported success; the guest clipboard contained exactly the note text. |
| Open link/file | For a plain note, an alert said **No supported target** and explained that it had no web link or local file. |
| Assign project | Assigning a new project updated both the result row and preview. Filtering by the former project returned no match; filtering by the new project returned the note. |
| Forget | A confirmation warned that older backups and the original source may still contain the event. After confirmation, the active search returned **No matches** and the preview remained empty. |
| Pause/Resume | The menu changed from **Pause capture** to **Resume capture** and back. It continued to report **Capture stopped**, consistent with the absent daemon. |

## Open checks from the earlier installed-app run

- The global Option-Space shortcut did not open Recall through Screen Sharing.
  Selecting Control-Option-Space changed the menu checkmark, but injecting that
  chord through the remote session also did not open it. The menu action worked.
  Global keyboard behavior needs a local interactive check; remote key forwarding
  or macOS shortcut reservation may have intercepted these attempts.
- A rapid stale-search race was not proven by this visual run. The panel cleared
  old rows on ordinary filter changes, and the forgotten event did not reappear
  in the active search. Generation checks have separate synthetic tests.
- No sanitized screenshot file was retained. The live remote view included VM
  and terminal details outside the panel; a controlled panel-only capture is
  still needed for documentation.
- A separate upgrade VM retained a 0.1.0 wrapped key and configuration, but had
  no vault database or event. The 0.2.0 Recall panel therefore reported **Vault
  unavailable** before any old-key decrypt could be observed. No Keychain
  approval prompt appeared, and successful upgrade decryption remains unproven.

## Key preservation incident in this artifact

An earlier headless `remember` attempt in the disposable fresh VM ran from an
SSH session that could not unwrap the existing key. The 0.2.0 artifact generated
a replacement `.key` file, then failed against the existing vault. The original
Keychain item still had its earlier modification time, and the logged-in GUI
continued to decrypt and search the synthetic note. No subsequent headless
writes were attempted. The original flat key file was not recovered from a
snapshot. This is a release blocker for the tested artifact; a fail-closed key
guard must be rebuilt and revalidated before release.
