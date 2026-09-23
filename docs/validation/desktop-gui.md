# Desktop recall GUI validation

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
but no sanitized image file was retained for documentation. A packaged-app
check of this styled revision and cross-app shortcut behavior remain open.

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

## Open checks

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
