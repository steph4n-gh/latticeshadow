# Packaged macOS app: focused-row keyboard candidate

This report covers the unsigned Apple Silicon app built from exact source
commit `14aa7a3f79f8aa8fde5c60c79f49355803ffd687`. Its ZIP is 425,566,601
bytes, SHA-256
`9012f9f4a60993d6c8f85170c99f8cd75c052fed8f35186c933ff1c415fc76b6`.
The checksum matched in the build guest, transfer directory, and disposable
install guest. It is an internal test artifact, not a public binary.

`verify_app.py` passed on the build guest: version 0.2.0, arm64 launcher,
offline bundled-model and native-resource smoke, ad-hoc signature, and 307
Mach-O dependency links. The unpacked app contained 1,098,433,587 file bytes.
The pinned model revision was `f60985c706f192d45d218078e49e5a8b6f15283a`;
the model weight SHA-256 was
`164fc63ee9f9267be7378fcbd7df99d09788a2f45244c92aa99ae5a574925716`.

## Disposable install and synthetic recall seed

The guest was cloned from a macOS 26.6.2 CI base image. Homebrew and Xcode
Command Line Tools were moved out of their normal locations before installing
the exact ZIP under `/Applications`. Host clipboard sharing was disabled.
With network model access disabled, the bundled CLI saved two synthetic notes
under an explicit project. An offline project-scoped `timeline --query
checklist --json` returned both exact IDs. No capture source was enabled.

## Interactive keyboard check

In a logged-in session of that same stripped guest, the menu opened **Recall**
and showed both synthetic notes. Clicking **Copy** for the amber note put its
exact text on the guest pasteboard. The blue result row was then visibly
focused and highlighted, with its blue-note preview displayed. Pressing
**Return** changed the pasteboard from amber to the exact blue-note text.
With the amber row visibly focused and highlighted, **keypad Enter** changed
the pasteboard back to the exact amber-note text. These observations cover
the selected-row keyboard action and basic Recall/Copy path for this ZIP; they
do not measure accessibility on other keyboard layouts or macOS versions.

## Scope

The predecessor `20669ff` ZIP passed separate packaged 10,000-event query,
portable export/restore, daemon/consent/reboot, and broader GUI checks. Those
results are **predecessor-artifact evidence**, not exact measurements of this
ZIP. Full performance, recovery, and long-soak checks were not rerun on this
candidate.
This guest began as a CI image with Gatekeeper assessment disabled; it does not
establish stock Gatekeeper first launch. Developer ID signing and notarization
remain pending before any public binary release.
