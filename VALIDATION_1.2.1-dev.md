# Enhanced OCR development-build validation

- Platform: Windows 11, Python 3.14.6, PyInstaller 6.22.2.
- Packaged version/build: `1.2.1-dev` / `20260920T141600Z-3688126-dirty`. The `dirty` suffix records the uncommitted feature changes in this development build.
- Full `python -m unittest discover -q` run: **146 tests, 145 passed, 1 failed** (38.430 seconds). The sole failure was the existing `test_global_hotkey_registration`: Windows registered `Ctrl+Alt+F10`, but its synthetic `keybd_event` input did not dispatch the callback in this desktop session. It also failed when rerun alone. No shortcut implementation was changed for this feature.
- The new tests cover Standard defaults/persistence, Reader selection, both capture paths, custom replacements, weak/clear/blank/colored images, bounded dimensions, uncertain alternatives, optional-pass failure, and stale capture cancellation. Focused feature tests pass.
- Live Windows OCR, after warm-up, on a generated clear sample: Standard 3.5 ms median, Enhanced 3.4 ms median, one pass in both, same text. On generated faint 13-pixel text: Standard 0.6 ms median and no text; Enhanced 13.4 ms median, three passes, and `An Angel was fighting!`. These five-sample medians are machine- and image-specific, not guarantees.
- The unsigned packaged executable launched with an isolated AppData configuration set to Enhanced. Its version metadata matches the embedded build identifier. No personal configuration or debug log is included in the release folder or ZIP.

PowerToys is present on the machine, but no retained game screenshot/ground-truth pair was available for a meaningful side-by-side comparison. Enhanced mode uses the existing Windows OCR engine with adaptive image preparation; it is not an integration with PowerToys. A real game capture may still be worse or unchanged, so compare both modes on your own game text before relying on Enhanced.
