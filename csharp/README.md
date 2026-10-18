# Game Text Reader — C# development port

Version **2.0.0-dev** is a complete .NET 10/WPF application. The portable release contains its .NET runtime and has no Python dependency. Microsoft Edge WebView2 Runtime is still needed for Markdown Preview. Edit and speech remain available if Preview cannot initialize.

Preview now also reports missing or incomplete local assets instead of showing a blank pane. Re-extract the complete ZIP if that error appears. Auto-Read discards results that finish after Alt-Tab and keeps the dialogue eligible when you return to the game. A failed tray startup shows the main window so the app remains accessible.

## Speech stability and interface refresh

SAPI now uses native, validated word events instead of the managed `System.Speech` progress dispatcher that could terminate the app while preparing pasted text. Long passages still play as one continuous stream. The most recent Reader audio is cached (up to 16 MiB); changing voice, base rate, volume, text, or pauses invalidates it, while changing the Reader playback multiplier does not.

Word highlighting follows the actual Windows playback position in Edit and Preview, including after pause, seeking, speed changes, and tab/theme changes. Native positions are mapped through the exact synthesis input; stale updates are discarded and pending UI updates are coalesced. Decorative supplementary emoji are omitted from synthesis only because they corrupt native word offsets with the tested voices. Editable/copyable text, source spans, accented letters and ordinary Unicode prose are preserved.

The header now has one sun/moon button: sun switches to Light, moon switches to Dark. A previous System preference resolves to the effective appearance and is saved as an explicit Dark/Light choice. The native Windows title bar, dropdown faces/selections, and other control states follow the selected palette. Switching application tabs no longer inserts a moving action strip; capture actions stay in Reader, tray, and shortcuts.

Voice and Global shortcuts stretch across two columns on wide windows, with startup/import/tray utilities beneath. Below 1,100 logical pixels they stack with scrolling when necessary. Shortcut fields and Record/Clear buttons have consistent heights. Read Again has a clearer replay icon; icon resources are cached instead of rebuilt for every spoken word.

The Python application remains available as the rollback release while this development build is validated on real games and additional Windows installations. Exit older running copies before opening another version, because they use the same shortcuts and settings.

Extract the complete ZIP to a reasonably short path, such as `C:\Games\GameTextReader`. Very deeply nested folders can exceed native WPF/WebView library path limits even though the application is long-path aware. The build uses a short `artifacts/release/<timestamp>/GameTextReader` output path for this reason.

## Build and test

Install the .NET 10 SDK, then run from this folder:

```powershell
dotnet restore GameTextReader.slnx --locked-mode
dotnet test GameTextReader.slnx -c Release
./build.ps1
```

The build script also finds a user-local SDK at `%USERPROFILE%\.dotnet\dotnet.exe`. It tests, publishes a self-contained `win-x64` folder, freezes the UTC timestamp and source revision in `build_info.json`, checks that no personal configuration or Python files were included, and creates a ZIP under `artifacts`.

For an isolated development launch:

```powershell
dotnet run --project GameTextReader.App -- --settings C:\Path\To\Test\config.json --no-integrations
```

`--no-integrations` disables tray, global shortcut registration and startup registration changes. Use an explicit test settings path while developing. `--startup` starts hidden only when a tray icon can be created; otherwise it shows the window.

## Code organization

- `GameTextReader.Core` owns compatible JSON settings, monitor-relative profile geometry, correction and review records, dialogue acceptance, Markdown rendering and source-mapped speech documents.
- `GameTextReader.Windows` owns screenshots, Windows OCR, foreground binding, Auto-Read work, native hotkeys/startup and the dedicated speech thread. Native SAPI event queues and WinRT speech cues produce source-mapped timing for a single in-memory audio stream played by Windows MediaPlayer. Idle synthesizers and one bounded Reader audio cache are reused; no `System.Speech` synthesis dispatcher is shipped.
- `GameTextReader.App` owns the WPF workspace, editable source, safe WebView2 preview, overlays, dialogs, and tray actions. Native progress is marshalled to its dispatcher. Every playback callback checks the request and source snapshot.
- `GameTextReader.Tests` contains captured Python reference results plus domain, native muted audio, actual hotkey/overlay, and WPF/WebView2 tests. An isolated child-process probe exercises the actual paste/Play path with the preferred voice, ordinary text, XML characters, emoji and the full science document. The supplied Markdown sample is test data and is never executed.

## Settings and rollback

Settings continue to use `%LOCALAPPDATA%\GameTextReader\config.json` and the Python-compatible keys. Before the first C# write, an existing file is copied to `config.before-csharp.json` (with a numeric suffix if needed). Import validates first and keeps a separate backup. An unreadable settings file is not overwritten with defaults; import a valid backup to recover it.

Profiles, regions, replacements, protected terms, theme, voice, rates, shortcuts, window placement and startup preference persist. Reader content and Auto-Read activity remain temporary. No configuration is shipped in the release. Existing startup registration is repaired only by normal integrated launches, never by isolated development tests.

## Compatibility and known constraints

Windows 11 x64 is the primary target. Windows 10 LTSC/Enterprise support follows .NET 10's supported operating systems; other Windows 10 editions are best-effort. WebView2 and Windows OCR language packs are system components, and third-party voices may not provide word timing.

First synthesis of a long passage can still take several seconds with a Natural SAPI voice. The UI shows Preparing speech and keeps the complete passage in one stream for smooth playback. Repeating a passage can use the bounded audio cache. Hardware output processing may still cause Realtek/DTS bursts; the existing README's Windows Sonic guidance still applies.

Snippet selection opens overlay windows and can minimize exclusive-fullscreen games. Use borderless fullscreen for visual selection, or Read Fixed Box in exclusive fullscreen. The port does not change game display modes.

This is an unsigned development ZIP. It may receive a Windows SmartScreen prompt. It is not a final compatibility-certified replacement for the Python release until the reported validation limitations are closed. See [VALIDATION.md](VALIDATION.md) for measured timings and the remaining hardware checks.

Use [MANUAL_TEST_CHECKLIST.md](MANUAL_TEST_CHECKLIST.md) for the remaining game, mixed-DPI, Windows 10, and safe listening checks.
