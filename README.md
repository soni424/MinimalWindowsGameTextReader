# Minimal Windows Game Text Reader

A lightweight Windows 10/11 desktop reader for game subtitles, dialogue, and other on-screen text. It uses the native Windows Media OCR API through `winocr`, then reads recognised text through Windows speech voices.

[Download the latest Windows release](https://github.com/soni424/MinimalWindowsGameTextReader/releases/latest)

## Highlights

- Reads a reusable fixed screen region or a one-time snippet selection.
- Reads text you type or paste into the editable result box.
- Follows speech with a highlighted visible row and current word.
- Uses offline Windows OCR and installed Windows speech voices.
- Conservatively corrects common game-font OCR mistakes while protecting fictional terminology.
- Supports custom replacements, pronunciation previews, and protected game-specific words.
- Provides configurable system-wide keyboard shortcuts.
- Can start quietly in the system tray when you sign in to Windows.
- Supports rapid dialogue reading with replace, queue, or optional overlapping voices.
- Stores multiple capture profiles and remaps their regions after display changes.
- Includes System, Dark, and Light appearances throughout the main interface, dialogs, drop-down controls, and scrollbars.
- Runs quietly from the Windows system tray.

## Install the Windows release

1. Open the [Releases page](https://github.com/soni424/MinimalWindowsGameTextReader/releases).
2. Download the Windows ZIP attached to the latest release.
3. Extract the complete ZIP. Do not run the executable from inside the archive.
4. Open the extracted `GameTextReader` folder and run `GameTextReader.exe`.

Python is not required when using the packaged Windows release. Windows may display a SmartScreen warning because the application is not digitally signed.

## Run from source

Use Python 3.10 or newer on Windows:

```powershell
python -m pip install -r requirements.txt
python main.py
```

The first run opens settings and creates a tray icon. Closing the settings window minimizes it normally. Choose **Hide settings to tray** from the tray menu only when you explicitly want to remove it from the taskbar; use **Quit** to exit fully.

## What's new in v1.1.1

- Added a shared application icon for the window, taskbar, system tray, and packaged executable.
- Replaced light native profile prompts with dialogs that follow the current appearance.
- Fixed combo boxes and their open drop-down lists in Dark Mode, including arrow buttons, borders, scrollbars, focus, hover, pressed, disabled, and selected states.
- Appearance changes now update existing controls immediately without restarting the app.
- Added regression coverage for the updated interface behavior.

## Use

1. Choose an installed Windows voice, speed, and volume.
2. In **Capture area**, create or select a game profile, select **Set capture area**, then drag inside the outlined area to move it or drag an edge/handle to resize it. Press **Enter** to save or **Esc** to cancel.
3. Press the Fixed Box hotkey (default `Alt+Z`) to OCR and read the selected profile's area.
4. Press the Quick Snippet hotkey (default `Alt+S`), drag over any text, and release. It reads that one selection without changing your Fixed Box.
5. Select **Read Again** to replay the text currently visible in the result box without another screenshot or OCR pass. You can edit, replace, type, or paste text there first; manually entered text is spoken exactly as written, with only layout-aware pauses added.

Capture profiles can be created, renamed, deleted, and switched without a profile limit imposed by the UI. Each region stores its Windows display identity, original display bounds/DPI, absolute coordinates, and monitor-relative coordinates. Resolution, scaling, and arrangement changes are remapped on the same display; if that display is disconnected, the app asks you to edit/select an area instead of moving it onto an unrelated screen.

## OCR correction

The **OCR corrections** tab controls an offline post-processing stage. The original Windows OCR result is retained, while the corrected result is displayed, copied, and spoken.

- **Conservative** (default) fixes only high-confidence context mistakes such as `I sow it` → `I saw it`, `In o second` → `In a second`, and common `I/l/1`, `S/5`, or `h/b` errors.
- **Balanced** also uses a bundled English frequency dictionary to repair likely one-character mistakes, joined words, letter/digit confusions, and safe punctuation spacing. Its dictionary loads in the background.
- **Strong** permits wider dictionary matches and is best used with protected terms.
- Custom replacement rules run first. Each rule can be enabled separately and can match whole words, exact case, or any case.
- The **▶ Play** buttons beside **Text detected by OCR** and **Replace it with** preview either field using the selected voice, speed, and the same safe 60% preview-volume cap as **Test selected voice**. Previewing does not save or change the rule.
- Protected terms keep character names, locations, item names, and game-specific vocabulary unchanged.
- Select **Corrections** beside the latest result to compare raw and corrected text and see why every change was made.

Correction debug logging is optional. When enabled, a size-limited `ocr_debug.log` is written next to `config.json`; normal UI status stays concise.

## Interface and appearance

- Choose **System**, **Dark**, or **Light** from the Appearance menu. The preference is saved automatically.
- Profile prompts, confirmation dialogs, combo boxes, and open drop-down lists follow the active appearance. Newly opened dialogs also follow appearance changes made while the app is running.
- The **Reader** tab keeps the fixed box, latest OCR text, and copy controls together.
- The **OCR corrections** tab contains correction strength, replacements, and protected game terms.
- The **Voice & shortcuts** tab contains speech controls and global shortcut setup. On wide or maximized windows, the two cards use side-by-side columns; they stack automatically when the window is narrower.
- Enable **Launch when I sign in to Windows** in that tab to start quietly in the system tray with global shortcuts ready. It applies to the current Windows account and does not require administrator access.
- **Stop audio** immediately interrupts the active utterance and clears older queued speech. It is also available from the tray menu.
- During a capture or **Read Again**, the current visible row is shaded blue and the spoken word is highlighted in gold. Long text scrolls automatically. With overlapping voices, the newest reading owns the highlight.
- Speed and volume changes are saved automatically, including changes made with the sliders.
- **Test selected voice** uses a 60% preview cap to avoid an unexpected blast; normal reads still use the saved volume.
- OCR line layout is retained in displayed and copied text. For speech, ordinary wrapped lines remain continuous, while headings, visually separate blocks, and bullet items receive natural punctuation pauses; bullet symbols themselves are not spoken.
- SAPI voice previews and readings use verified PCM metadata before MediaPlayer playback, preventing format-related distortion.
- **New capture while speaking** defaults to replacing the current line immediately. Queue mode lets the current line finish first. **Allow overlapping lines** starts a new voice while the current one continues; the maximum number of simultaneous readings can be set from 2 to 4. Overlap is capped to keep audio resources bounded.
- Window size, position, and normal/maximized state are restored. Capture and speech operations never write transient minimized geometry over that preference.

Global shortcut reads minimize settings without hiding them to the tray. One persistent OCR worker replaces obsolete pending jobs and publishes only the newest result. Speech uses bounded playback sessions: replace mode keeps one newest line, queue mode keeps one next line, and overlap mode starts separate sessions up to the configured limit. SAPI voices are synthesized into memory before MediaPlayer playback, so rapid captures do not repeatedly purge a live SAPI audio device. Replacements use an isolated MediaPlayer channel and keep the previous stream alive briefly while Windows finishes its asynchronous handoff, preventing transition bursts.

Choose **Record** beside Read Fixed Box, Select a Snippet, or the optional Read Again shortcut, then press any supported Windows combination such as `Ctrl+Shift+T`, `Alt+Q`, `Shift+F8`, or `Ctrl+Shift+Space`. Shortcuts can be cleared individually. Windows registration detects conflicts with this app and other programs before a setting is saved; `F12` is rejected because Windows reserves it.

Windows OCR, its event loop, SAPI voice tokens/player, and WinRT synthesizer/media player remain alive on their dedicated workers for repeated dialogue captures. When OCR debug logging is enabled, the same rotating log includes measured dispatch, capture, OCR, correction, and speech-start timings.

Settings persist in `%LOCALAPPDATA%\GameTextReader\config.json`, independently of the extracted application folder. That keeps appearance, voices, shortcuts, profiles, capture areas, OCR rules, speech behavior, window placement, and startup preference across updates. The first updated launch automatically imports an older `config.json` when it remains beside the application. If the old copy is in a different folder, use **Voice & shortcuts → Settings and updates → Import settings from older app folder…** once, then restart the app. The old file is retained and an existing AppData configuration is backed up before a manual import.

Manually typed reading text is temporary and is not restored after restarting. Settings are local to the current Windows account and computer. Windows organization policies or disabling the app in Windows **Startup Apps** can override its launch-at-sign-in preference.

## Troubleshooting audio bursts

On some Realtek/DTS audio devices, rapidly starting another reading can produce a short `brrrt` or distorted burst even though the generated speech and a Stereo Mix/Audacity recording sound clean. This points to the Windows output-device processing path rather than damaged OCR or speech audio.

On Windows 11, open **Quick Settings → Sound output**, select the affected playback device, and change **Spatial sound** from **Off** to **Windows Sonic for Headphones**. Windows Sonic has been confirmed to remove these bursts on an affected Realtek device by using a more stable Windows audio-processing path. If the problem remains, also try disabling other DTS/Realtek audio enhancements or testing a different output device.

## Build the Windows app

Install the project requirements and PyInstaller in a virtual environment, then build from the repository folder:

```powershell
python -m pip install -r requirements.txt
python -m pip install pyinstaller
python -m PyInstaller GameTextReader.spec --noconfirm
```

The packaged application is created in `dist/GameTextReader`.
Release packages should not contain `config.json`; each user's settings are created in Windows AppData on first launch.

## Verify

```powershell
python -m unittest discover -v
```

The test suite verifies correction, profile migration/CRUD/display mapping, update-safe settings migration/import, window restoration and DPI layout, editable Read Again text, live word progress, themed scrollbars, pronunciation previews, Windows startup registration, newest-job replacement, reusable OCR/TTS sessions, speech replace/queue/overlap policies, native Windows OCR, speech interruption/playback, shortcut parsing, actual callback dispatch, and OS-level shortcut conflicts.

## App icon assets

The flat source mark is stored in `assets/app_icon.svg`. Runtime PNGs and the multi-size Windows ICO are generated from that source and shared by the main window, taskbar, system tray, and packaged executable.

To regenerate the exported icon assets on Windows with Microsoft Edge and Pillow installed:

```powershell
python tools/build_icon_assets.py
```
