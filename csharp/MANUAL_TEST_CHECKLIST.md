# C# port: remaining manual checks

These checks require hardware or a game session that automated tests cannot certify. Keep the Python release until they pass. Do not run both versions against the shared settings at once.

## Safe setup

1. Extract the complete development ZIP to a short path, for example `C:\Games\GameTextReader`.
2. For testing without changing your normal preferences, launch `GameTextReader.exe --settings C:\Games\GtrTest\config.json`. Leave Windows startup unchecked. This uses separate settings but still enables tray and global shortcuts; close other reader copies first.
3. Set application volume low before any audible test. Do not start at full headset volume. Increase it only after confirming normal output. Automated speech tests were muted.
4. Note the version/build from the header and the voice, base speed, Reader multiplier, OCR mode and Auto-Read speed used for any report.

## Persona 3 Reload / dialogue-heavy games

- [ ] In borderless fullscreen, create a profile with a tightly framed dialogue box. Select a snippet with a custom shortcut, not just the default. Cancel and retry; confirm game content remains visible.
- [ ] Read Fixed Box without opening an overlay in exclusive fullscreen. Snippet overlays may minimize an exclusive-fullscreen game; that known limitation is unchanged.
- [ ] Try Standard and Enhanced on the same small/faint text. Confirm names, punctuation and numbers are retained; inspect correction review for uncertain changes.
- [ ] Test Normal, Fast and Instant Auto-Read across complete dialogue, typewriter growth, quick consecutive advances, repeated dialogue and a blank/reappearing dialogue box.
- [ ] Alt-Tab during a difficult OCR pass. No newly completed background result should start reading; returning to the game should make the current dialogue eligible again. An already-playing line is not automatically stopped by Alt-Tab.
- [ ] Trigger a manual capture during Auto-Read; then stop Auto-Read and Stop Audio. Verify there is no late restart or duplicate voice.

## Reader and audio

- [ ] Paste long Markdown; try Edit/Preview repeatedly, Clear, type, paste, and Play. The source remains editable and Copy returns the original source.
- [ ] With both a Windows and a SAPI voice, compare short text and a long passage. First preparation of a Natural SAPI voice can take several seconds; repeat reads can use the cache.
- [ ] Listen for smooth continuous speech and structural/list-label pauses at a safe volume. Try pause/resume, backward/forward sentence steps, repeated word seeking in Edit/Preview, and live speed changes while playing and paused.
- [ ] Check that highlights follow the spoken word and the visible wrapped row. Record any timing drift or overlapping voice with the build and selected voice.
- [ ] Paste the supplied science document and ordinary text, then Play/Read Again repeatedly with the preferred Natural SAPI voice. No process termination, stale highlight, second Reader stream, or lost editing should occur. Decorative emoji remain visible but are omitted from narration.
- [ ] On a wide window, check that Voice and Global shortcuts use both columns and fit without unnecessary scrolling. Check narrow-window stacking and accessible scrolling at 125%/150% scaling.
- [ ] On initial Dark launch, confirm the native caption is dark. Toggle the sun/moon button, inspect open/closed dropdown states, and switch application tabs without their headers moving.

## Two monitors / mixed DPI

- [ ] Use different display scaling, such as 100% and 150%, including a monitor left of or above the primary display.
- [ ] Open a saved capture area on each monitor; check initial selection, centered panel, dimensions, handles, arrow-key movement and Cancel preserving the saved area.
- [ ] Draw a new area on the other monitor. Confirm the control panel follows it and the OCR box matches the physical text.
- [ ] Move/maximize/restore the main window across monitors. Check sidebar collapse, all action buttons, Preview sizing, selection/caret placement and themed scrolling.
- [ ] Disconnect/reconnect or rearrange a display. A missing profile monitor should give an actionable error, not silently capture another screen.

## Windows / deployment

- [ ] On a clean supported Windows installation without a separately installed .NET runtime, open the portable app. Check Edit and native OCR/speech; install only the required Windows OCR language pack if absent.
- [ ] On Windows without WebView2 Runtime, Preview should explain that it is unavailable while Edit, Clear, Paste and speech still work. Do not uninstall a runtime used by other applications just to perform this check.
- [ ] Repeat core checks on supported Windows 10 LTSC/Enterprise. Do not claim general Windows 10 certification from Windows 11 results.
- [ ] Close to minimize, reopen from the taskbar/tray, and Quit. No hidden process, active shortcut or speech should remain after Quit.
- [ ] Verify the first C# save keeps a `config.before-csharp*.json` backup and that the Python rollback build can still select the saved voice, profiles, rules and shortcuts.

Only enable sign-in startup if you explicitly want to test a real current-user registry change. That opt-in check should cover enable/disable, a moved executable path and a sign-in launch staying in the tray. Automated development tests use an isolated registration store instead.
