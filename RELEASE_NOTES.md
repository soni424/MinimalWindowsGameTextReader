# Game Text Reader 1.2.9-dev

Development build for testing before a stable release.

## Markdown narration and Reader seeking

- Ordered Markdown and plain-text steps now speak their numbers as cardinal words (for example, “one: Observation”); headings, quantities, and decimals keep their existing reading.
- Double-click a spoken word in Edit or Preview to continue from that occurrence. Preview sends only a current-document word index to the app, and old seek revisions cannot leave a second Reader stream active.
- Long passages with a selected SAPI voice begin from a short opening segment while later segments are prepared during playback. Playback stays tied to one logical Reader request, including its highlights, controls, and structural pauses. The app displays a preparing status until audio starts.
- Prominent actions across Reader, OCR corrections, Voice & shortcuts, and dialogs now have theme-aware line icons beside their text. The native Windows title bar and existing shortcuts are unchanged.

## Previous development build: 1.2.8-dev

## Markdown Reader preview

- Added Edit and Preview tabs for Last captured text. Recognizable Markdown paste opens Preview; OCR captures and ordinary text remain in Edit.
- Render headings, lists, tasks, tables, footnotes, equations, code blocks, and safe expandable sections in the current Light or Dark appearance. Remote images require a per-document click to load; external links open in the system browser.
- Play and Read Again speak rendered prose with source-aware sentence navigation and highlighting, omitting Markdown syntax, URLs, code, and equations. Copy continues to return the exact editable source.
- Preview uses the Microsoft Edge WebView2 Runtime. If WebView2 is absent or cannot start, Edit and audio reading remain available. No user Markdown content or personal configuration is included in the package.

## Previous development build: 1.2.6-dev

## Reader playback speed

- Added a remembered **0.5×–2.0×** speed slider and **1.0×** reset beside Last captured text playback controls.
- Reader Play and Read Again change speed on the current Windows media stream without restarting speech. Paused position, sentence navigation, and word highlighting stay intact.
- Captures, Auto-Read, and voice previews retain their existing speed. Unsupported playback rates revert to the last working value.

## Previous development build: 1.2.5-dev

## Reader playback controls

- Added Previous sentence, Play/Pause, and Next sentence beside Last captured text. Play starts the passage when idle; Read Again still restarts it.
- Sentence boundaries follow spoken punctuation, headings, and bullet pauses. Wrapped OCR lines remain continuous.
- Pausing keeps the current stream and highlight, including during sentence jumps. Playback timeouts account for time spent paused.
- Controls target only the reading tied to the displayed text. Voices without usable word timing can be paused but cannot skip sentences.

## Previous development build: 1.2.4-dev

## Guarded Instant Auto-Read

- Added a remembered **Normal / Fast / Instant** speed selector. Existing configurations still default to Normal.
- Instant samples and settles at roughly 60 ms and can speak the first usable OCR result. Tiny/noisy and near-duplicate results still require confirmation.
- Typewriter growth cancels only the partial Auto-Read request, suppresses intermediate fragments, then restarts the completed line after two matches and roughly 250 ms unchanged.
- Targeted cancellation leaves manual, queued, and overlapping speech alone. Empty reads, duplicates, stale work, lost focus, and repeated errors retain their existing safeguards.

## Previous development build: 1.2.3-dev

- Added Fast Auto-Read with shorter stable-text checks and two-result confirmation.
- Bounded Auto-Read to one pending OCR request while preserving manual-capture priority.

## Previous development build: 1.2.2-dev

- Added foreground-bound Auto-Read for the selected fixed capture profile, with Reader, tray, and optional global-shortcut controls.
- Auto-Read starts off each session, pauses on Alt-Tab, and replaces the prior automatic voice when confirmed dialogue changes.

## Previous development build: 1.2.1-dev

## Switchable text extraction

- Added **Standard / Enhanced** in the Reader tab. The choice persists and applies to both fixed-box and snippet captures; older settings default to Standard.
- Enhanced keeps the existing OCR result as its baseline and tries up to two size-bounded image preparations only for weak-looking results. It keeps the baseline when an alternative is uncertain.
- Recognized text continues through the same custom replacements, contextual corrections, display, and speech path. The optional debug log records the selected variant, pass count, and OCR time.
- New captures can skip obsolete optional passes, retaining responsive rapid reading. No PowerToys dependency or clipboard integration is added.
- Results will vary by game font, display, and Windows OCR language pack. This is not PowerToys' exact capture implementation.

## Previous development build: 1.2.0-dev

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
