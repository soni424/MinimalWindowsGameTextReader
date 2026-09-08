# Game Text Reader 1.2.0-dev

Development build for testing before a stable release.

## Reader navigation

- Double-click a word while reading to jump forward or backward to that occurrence. Seeking stops other voices and pending readings without changing the saved speech mode.
- Native seeking reuses the synthesized audio. Voices without usable seek metadata fall back to reading the remaining passage.
- Right-click for clipboard actions, **Replace Word…**, and **Add to Replacement Rules…**. Replace Word saves a rule and updates only the selected occurrence; Add to Replacement Rules leaves the current text untouched.
- Fixed source mapping for repeated numbered lists and Unicode speech offsets. Highlighting follows the newest playback revision.
- Scrollbars retain their appearance with empty, short, and overflowing text, including hover, pressed, and disabled states.

## OCR corrections

- Added bounded offline contextual candidate scoring, character-confusion costs, phrase evidence, and passage-local name evidence.
- Preserved unfamiliar names such as Xion and Yohan instead of normalizing them to dictionary words.
- Added **Corrections → Needs review** for uncertain alternatives. Apply changes one occurrence and preserves the raw OCR/change history; Dismiss leaves its text unchanged.
- Custom replacement rules still run when automatic correction is disabled. Manually typed text remains uncorrected unless explicitly edited.
- Avoided expensive word-splitting checks for already-valid dictionary words.

## Shortcuts and reliability

- Fixed released-modifier tracking and clearly separated recorded shortcut changes from applied shortcuts.
- Added registration rollback, dispatch/overlay error reporting, and optional shortcut debug logging.
- Added persistent version/build information and an About dialog with application and settings locations.
- Fixed speech runtime/apartment lifetime handling discovered during repeated native voice discovery and seeking tests.

## Updating

Extract the complete package into a new folder and run `GameTextReader.exe`. Settings remain in `%LOCALAPPDATA%\GameTextReader\config.json`; releases contain no personal configuration. Use **Import settings from older app folder…** for an older copy that still stored configuration beside the executable.

## Limitations

- This is an unsigned Windows development build, not a published stable release.
- Offline correction cannot reliably recover fully obscured wording. Ambiguous alternatives require review; protected terms and custom rules remain the best way to specify unusual game terminology.
- Native seeking and word highlighting depend on voice metadata. The fallback does not invent word timings.
- Windows or another application may reserve a shortcut. Check the applied status and optional debug log if registration or snippet selection fails.
- Keep the existing Windows Sonic/audio-enhancement troubleshooting guidance in the README in mind for device-specific audio bursts.
