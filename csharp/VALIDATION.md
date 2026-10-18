# C# port validation

The port remains **2.0.0-dev**, not a parity-certified final release. Keep the Python release for rollback.

## Latest speech-stability and interface build

- Version `2.0.0-dev`, frozen build **`20261016T013340Z-97b0f6d-dirty`**; executable file version `2.0.0.0`. The source revision is unchanged because no commit was requested.
- Full suite: **140 passed, 0 failed, 0 skipped**, confirmed again by the final build (`TestResults/csharp-release.trx`, with the preceding run in `csharp-stability.trx`). The earlier baseline was 123; the investigation also confirmed the 114 non-audio cases before implementation.
- The isolated preferred-voice paste/Play regression reproduced a fatal `System.Speech` negative-word-length exception with the supplied science document. The same test now passes all eight ordinary/Markdown/XML/emoji/full-document cycles using native SAPI event validation. It never writes normal AppData settings or plays physical audio.
- Added invalid-length/offset/time/stream rejection, exact native word mapping beyond inserted pauses, leading emoji/source preservation, cache invalidation, and Windows/SAPI transport checks. Supplementary decorative symbols are omitted from synthesis input only; the authoritative text and source offsets remain intact.
- Ten WPF/WebView2 checks cover native initial caption-theme setters, contrast, selected voice names, stable tabs, wide/narrow settings, compact shortcut rows, latest-word coalescing, stale events, restoration after theme changes, rapid paste during Preview startup, and plain-text progress after switching to Preview.
- On this run, the preferred Aria voice prepared the 6,874-character science document in approximately **9.7 s**, and its cached repeat in **74 ms**. Short probe passages prepared in about **0.21–0.88 s**, with cached repeats around **0.06–0.07 s**. A separate 12-paragraph Aria case prepared in **6.4 s**, repeat **125 ms**. These are measured preparation/readiness times, not guarantees for other voices or machines.
- Dark/Light/narrow UI screenshots were inspected. Physical mixed-DPI desktops, subjective listening and Windows 10 remain manual checks below; these are not claimed complete.
- Final packaged smoke: **passed** (`artifacts/smoke-20261016T013340Z/report.json`). It verified the bundled runtime, eight voices, hidden startup/tray, custom shortcut and overlay, real capture/OCR, safe Markdown, single-player seeking, Clear/type, responsive workspace, and two enumerated monitors.
- The first smoke attempt exposed a verification-harness race: Escape closes all monitor overlays, so subsequent snapshot entries have no presentation source. The harness now rechecks each source and exits nonzero on failure; the final packaged run passed. Normal snippet behavior was not changed.
- A separate muted **packaged Aria** child-process probe passed three reads of the full 6,874-character science document (`artifacts/probe-20261016T013340Z/report.json`): **10,618 ms** first readiness, **72/73 ms** cached repeats. Observer sampling recorded a **313.1 MiB** peak main-process working set and **1,266** handles; these include WPF/Preview initialization, are not whole-system/WebView-child totals, and do not establish a before/after memory improvement.
- ZIP inspection: **740 entries**, no personal `config.json`, Python files/runtime, debug symbols, or `System.Speech.dll`. The loaded CLR came from the package directory.
- Executable: `artifacts/release/20261016T013340Z/GameTextReader/GameTextReader.exe`.
- ZIP: `artifacts/GameTextReader-2.0.0-dev-20261016T013340Z-97b0f6d-dirty-win-x64.zip`.

## Previous continuation build

- Build: `20261015T112502Z-97b0f6d-dirty` (version `2.0.0-dev`).
- Baseline before continuation: 118 passed. Final release suite: **123 passed, 0 failed, 0 skipped** (`TestResults/csharp-release.trx`).
- Test-first regressions reproduced late Auto-Read publication after Alt-Tab and failed local Preview navigation being treated as success. Both fixes pass their regression tests.
- Added WPF failure-path checks for missing Preview page/scripts, an unavailable WebView2 Runtime, Clear/type/paste/muted playback without Preview, and a sign-in launch becoming visible when tray creation fails. Native availability is injected at the system boundary; no installed runtime or startup registry value was changed.
- Packaged smoke test: **passed** (`artifacts/smoke-20261015T112502Z/report.json`), including the package-local runtime, native shortcut/overlay, real screen capture/OCR, safe Preview, single-player seeking, Clear/editing, and responsive layout.
- ZIP inspection: 741 entries, with no personal settings, Python source/runtime, or debug symbols before this validation-note refresh. Only documentation was refreshed afterward; application binaries are unchanged.
- Executable: `artifacts/release/20261015T112502Z/GameTextReader/GameTextReader.exe`.
- ZIP: `artifacts/GameTextReader-2.0.0-dev-20261015T112502Z-97b0f6d-dirty-win-x64.zip`.
- The remaining physical/game checks are listed in `MANUAL_TEST_CHECKLIST.md`; they have not been marked complete.

## Previous completed development build

- Build: `20261015T073301Z-97b0f6d-dirty`.
- Release suite: **118 passed, 0 failed, 0 skipped** (`TestResults/csharp-release.trx`).
- Packaged smoke test: **passed** (`artifacts/smoke-20261015T073301Z/report.json`). It verified the package-local runtime, eight installed voices, hidden tray startup, custom shortcut/snippet overlay, actual screen capture/OCR, safe Markdown rendering, single-player Preview seeking, Clear/typing, and responsive layout.
- ZIP inspection: 740 entries; no personal `config.json`, Python source/runtime, or debug symbols. Native executable file version is `2.0.0.0`; product version is `2.0.0-dev` with source revision.
- Executable: `artifacts/release/20261015T073301Z/GameTextReader/GameTextReader.exe`.
- ZIP: `artifacts/GameTextReader-2.0.0-dev-20261015T073301Z-97b0f6d-dirty-win-x64.zip`.
- ZIP SHA-256: `C42D72B69D8BA2C84CC3602ADE41F018AB262B32072FD7E54F740D6FD701FBD7`.

An initial deeply nested package path exposed a native WPF path-length failure. The release output now uses a shorter directory; users should likewise avoid deeply nested extraction paths. The packaged screen-capture test also exposed .NET rejecting combined `CopyPixelOperation` flags; capture now uses checked native `BitBlt` with the original raster flags and deterministic GDI cleanup.

## Automated checks on the development Windows 11 machine

- Python baseline recorded before port changes: 228 tests passed. Existing Python UI/test edits were preserved.
- C# domain fixtures compare 16 speech documents (including UTF-16 source spans and structural pauses) and 51 correction cases against captured Python results. The documented Unicode-bullet behavior intentionally fixes a Python Markdown-only omission.
- Native tests exercise Windows OCR in Standard/Enhanced modes, Windows and SAPI synthesis at zero volume, playback timing, pause, live rate, repeated seeking, suffix fallback, queue/overlap cancellation, continuous long speech and cache reuse.
- WPF/WebView2 tests exercise source-mapped Markdown, blocked remote images, Dark/Light switching, narrow layout, stale speech callbacks, and typing/pasting after Clear. UI tests explicitly suppress production startup and use temporary settings.
- Native keyboard injection registers a custom shortcut, dispatches it into an actual snippet overlay, cancels that overlay and confirms the old key can be registered after reassignment.
- Startup registration tests use a fake store; they do not change the current account's Run key. Package smoke tests require a separate settings path and exercise hidden startup/tray and the packaged shortcut path.
- Settings tests cover compatible round trips, backups, invalid-field salvage, malformed-file protection and prevention of stale saves after import.

## Measurements, not speed guarantees

A warmed synthetic 500-word correction pass took approximately 34–39 ms. Clear synthetic OCR retained its original image in both extraction modes. A short muted passage prepared in roughly 0.1–0.3 seconds on the tested built-in voices.

The saved Microsoft Aria Natural SAPI voice is much slower on a long first synthesis: C# measurements varied around 4–10 seconds, versus roughly 4.5–4.9 seconds in Python with the same passage, voice, base rate and volume. A C# cached repeat took about 0.12 seconds. Engine cold-start/load variability is substantial; the port does not promise faster synthesis. Long Reader passages deliberately use one stream to avoid handoff stutter.

## Still requiring human/hardware validation

- Persona 3 Reload and other real games, exclusive-fullscreen and borderless behavior.
- Physical mixed-DPI behavior, display reconnects, and different GPU/output-driver combinations. Synthetic geometry includes negative coordinates and scaling; the latest package smoke enumerated two monitors and exercised the multi-window selector, but does not certify every scaling/placement combination.
- Windows 10 LTSC/Enterprise and a clean Windows installation without a separately installed .NET runtime. The package contains its own runtime and its smoke report checks that the loaded runtime came from the package.
- Missing WebView2 on a clean real machine and unusual third-party voices/audio-device failures. Simulated missing-runtime/asset failures now verify Edit and muted playback recovery, but do not replace deployment checks or judge perceived pronunciation/smoothness.
- Subjective audible listening checks and accessibility review. Automated speech checks are muted for hearing safety.

The final test result and packaged smoke report are recorded under `TestResults` and `artifacts`, respectively; generated outputs are not tracked in Git.
