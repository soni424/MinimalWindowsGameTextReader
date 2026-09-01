# Windows Game Text Reader

A lightweight Windows 10/11 desktop reader for game subtitles, dialogue, and other on-screen text. It uses the native Windows Media OCR API through `winocr`, then reads recognised text through Windows speech voices.

## Run

Use Python 3.10 or newer on Windows:

```powershell
python -m pip install -r requirements.txt
python main.py
```

The first run opens settings and creates a tray icon. Closing the settings window minimizes it normally. Choose **Hide settings to tray** from the tray menu only when you explicitly want to remove it from the taskbar; use **Quit** to exit fully.

## Use

1. Choose an installed Windows voice, speed, and volume.
2. In **Capture area**, create or select a game profile, select **Set capture area**, then drag inside the outlined area to move it or drag an edge/handle to resize it. Press **Enter** to save or **Esc** to cancel.
3. Press the Fixed Box hotkey (default `Alt+Z`) to OCR and read the selected profile's area.
4. Press the Quick Snippet hotkey (default `Alt+S`), drag over any text, and release. It reads that one selection without changing your Fixed Box.
5. Select **Read Again** to replay the last successful corrected result without another screenshot, OCR pass, or correction pass.

Capture profiles can be created, renamed, deleted, and switched without a profile limit imposed by the UI. Each region stores its Windows display identity, original display bounds/DPI, absolute coordinates, and monitor-relative coordinates. Resolution, scaling, and arrangement changes are remapped on the same display; if that display is disconnected, the app asks you to edit/select an area instead of moving it onto an unrelated screen.

## OCR correction

The **OCR corrections** tab controls an offline post-processing stage. The original Windows OCR result is retained, while the corrected result is displayed, copied, and spoken.

- **Conservative** (default) fixes only high-confidence context mistakes such as `I sow it` → `I saw it`, `In o second` → `In a second`, and common `I/l/1`, `S/5`, or `h/b` errors.
- **Balanced** also uses a bundled English frequency dictionary to repair likely one-character mistakes, joined words, letter/digit confusions, and safe punctuation spacing. Its dictionary loads in the background.
- **Strong** permits wider dictionary matches and is best used with protected terms.
- Custom replacement rules run first. Each rule can be enabled separately and can match whole words, exact case, or any case.
- Protected terms keep character names, locations, item names, and game-specific vocabulary unchanged.
- Select **Corrections** beside the latest result to compare raw and corrected text and see why every change was made.

Correction debug logging is optional. When enabled, a size-limited `ocr_debug.log` is written next to `config.json`; normal UI status stays concise.

## Interface and appearance

- Choose **System**, **Dark**, or **Light** from the Appearance menu. The preference is saved automatically.
- The **Reader** tab keeps the fixed box, latest OCR text, and copy controls together.
- The **OCR corrections** tab contains correction strength, replacements, and protected game terms.
- The **Voice & shortcuts** tab contains speech controls and global shortcut setup.
- **Stop audio** immediately interrupts the active utterance and clears older queued speech. It is also available from the tray menu.
- Speed and volume changes are saved automatically, including changes made with the sliders.
- Window size, position, and normal/maximized state are restored. Capture and speech operations never write transient minimized geometry over that preference.

Global shortcut reads minimize settings without hiding them to the tray. One persistent OCR worker replaces pending jobs, stops older speech immediately, and publishes only the newest result.

Choose **Record** beside Read Fixed Box, Select a Snippet, or the optional Read Again shortcut, then press any supported Windows combination such as `Ctrl+Shift+T`, `Alt+Q`, `Shift+F8`, or `Ctrl+Shift+Space`. Shortcuts can be cleared individually. Windows registration detects conflicts with this app and other programs before a setting is saved; `F12` is rejected because Windows reserves it.

Windows OCR, its event loop, SAPI voice tokens/player, and WinRT synthesizer/media player remain alive on their dedicated workers for repeated dialogue captures. When OCR debug logging is enabled, the same rotating log includes measured dispatch, capture, OCR, correction, and speech-start timings.

Settings persist in `config.json` next to the application.

## Verify

```powershell
python -m unittest discover -v
```

The test suite verifies correction, profile migration/CRUD/display mapping, window restoration and DPI layout, Read Again isolation, newest-job replacement, reusable OCR/TTS sessions, native Windows OCR, speech interruption/playback, shortcut parsing, actual callback dispatch, and OS-level shortcut conflicts.
