# Minimal Windows Game Text Reader

A lightweight Windows 10/11 desktop reader for game subtitles, dialogue, and other on-screen text. It recognises screen text with the built-in Windows OCR engine and reads it aloud with installed Windows speech voices.

The application is a .NET 10/WPF program in [`csharp`](csharp/README.md). The portable release includes its own .NET runtime; no Python or separate .NET installation is required.

[Download the latest Windows release](https://github.com/soni424/MinimalWindowsGameTextReader/releases/latest)

## Highlights

- Reads a reusable fixed screen region or a one-time snippet selection.
- Optionally watches the fixed box and reads stable new dialogue automatically while its game is in the foreground (Normal, Fast or Instant).
- Reads text you type or paste, including Markdown with a styled Preview.
- Highlights the spoken word and row, with play/pause, sentence steps, word seeking and a 0.5×–2.0× Reader speed.
- Switches between Standard OCR and adaptive Enhanced OCR for difficult screen text.
- Conservatively corrects common game-font OCR mistakes, with custom replacements, protected game terms and a review list.
- Configurable global shortcuts, tray icon, optional start at sign-in, multiple capture profiles and Dark/Light appearance.

## Install

1. Download the Windows ZIP from the [Releases page](https://github.com/soni424/MinimalWindowsGameTextReader/releases).
2. Extract the complete ZIP to a short path, such as `C:\Games\GameTextReader`. Do not run it from inside the archive.
3. Run `GameTextReader.exe`.

Windows may show a SmartScreen prompt because the application is not digitally signed. Markdown Preview needs the [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/); Edit and speech work without it.

Close any older copy of the reader before opening another, because they share shortcuts and settings.

## Build from source

Install the .NET 10 SDK, then from the `csharp` folder:

```powershell
./build.ps1
```

The script restores locked dependencies, runs the tests, and creates a portable folder and ZIP under `csharp/artifacts`. See [csharp/README.md](csharp/README.md) for development commands, architecture and known constraints.

## Repository layout

- `csharp/` — application source, tests and build script.
- `assets/` — application icon, action icons and Markdown Preview (KaTeX) files used by the build.
- `test_ocr_passages.json` — OCR passages used by the correction tests.
- `RELEASE_NOTES.md` — changes in each build; copied into the release ZIP.

## Settings

Settings are stored in `%LOCALAPPDATA%\GameTextReader\config.json`, outside the application folder, so replacing or deleting a build keeps them. The first C# save keeps a `config.before-csharp.json` backup.

The earlier Python version (1.2.10-dev and before) is no longer in this folder. Its source remains in the Git history, and it reads the same settings file.
