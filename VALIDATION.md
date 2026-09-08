# 1.2.0-dev validation

Validated on Windows 11 with Python 3.14.6 and PyInstaller 6.22.2.

- Final build: `20260908T043328Z-fe62f57-dirty` (uncommitted development working tree).
- Full suite: `python -m unittest discover -v` — **133 tests passed**, 72.658 seconds.
- All 12 supplied OCR passages have regression expectations distinguishing automatic edits from review-only alternatives.
- Native Windows and SAPI speech tests verified muted forward/backward seeking, stream reuse, and repeated voice discovery. Mock tests cover unavailable metadata, suffix fallback, stale callbacks, rapid seeking, Unicode offsets, and overlap/queue cancellation.
- UI tests cover context actions, duplicate rules, Cancel, review acceptance/dismissal/staleness, scrollbar states, highlighting, themes, and shortcut recording/rollback.
- Packaged UI testing recorded/applied Alt+X and Ctrl+Shift+T and confirmed native dispatch opened the snippet overlay. Alt+X survived restart and the removed Alt+S no longer opened an overlay. The final build retained Ctrl+Shift+T across restart and its debug log confirmed dispatch and overlay creation at 12:34:58 local time on September 8, 2026.
- Packaged testing used an isolated LOCALAPPDATA directory; personal configuration and Windows startup registration were not changed by the test harness.
- Mixed-passage warmed correction benchmarks (median of three): approximately 45–50 ms for 500 words and 186–204 ms for 2,000 words across Conservative/Balanced/Strong. Results are machine-specific, not a latency guarantee.
- Executable FileVersion/ProductVersion and packaged build metadata agree with the on-screen header.

## Warnings and remaining limits

- No test failures. One non-failing Tk `persist_now` timer callback warning occurred during window-placement test teardown.
- Packaging reported optional winocr data-collection warnings and a pystray GTK SyntaxWarning; the Windows package built and launched successfully.
- The snippet overlay is an override-redirect window that the UI automation tool could not independently target for cancellation. Registration, native dispatch, and overlay creation were verified; the test process was then stopped. Automated overlay/component tests passed, but this is not a claim that every monitor arrangement or game-specific key interception was manually tested.
- OCR review alternatives remain uncertain and require user judgment. Voice metadata availability controls native seek/highlight support.
- This unsigned development package has not been published to GitHub.
