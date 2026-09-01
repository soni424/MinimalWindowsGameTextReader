"""Responsive, theme-aware settings UI for the Windows Game Text Reader."""

from __future__ import annotations

import threading
import tkinter as tk
from queue import Empty, SimpleQueue
from tkinter import scrolledtext, ttk
from typing import Callable

from appearance import ThemePalette, apply_windows_title_bar, resolve_theme
from config import ConfigStore
from hotkey_manager import HotkeyError, normalise_hotkey
from ocr_correction import CorrectionResult
from tts_engine import TtsEngine, Voice
from window_state import WindowStateController


def _theme_dialog_window(window: tk.Toplevel, palette: ThemePalette) -> None:
    """Theme a child window's body now and its native frame after mapping."""
    window.configure(bg=palette.window)

    def apply_frame(_event: object | None = None) -> None:
        apply_windows_title_bar(window, palette.dark)

    window.bind("<Map>", apply_frame, add="+")
    window.after_idle(apply_frame)


def _center_dialog(window: tk.Toplevel, parent: tk.Misc) -> None:
    window.update_idletasks()
    width = window.winfo_reqwidth()
    height = window.winfo_reqheight()
    try:
        parent.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
        window.geometry(f"+{x}+{y}")
    except tk.TclError:
        pass


def _show_modal(window: tk.Toplevel, parent: tk.Misc, focus: tk.Misc | None = None) -> None:
    """Reveal a fully styled modal only after its size and native handle exist."""
    try:
        previous_grab = parent.grab_current()
    except tk.TclError:
        previous_grab = None
    _center_dialog(window, parent)
    window.deiconify()
    window.update_idletasks()
    try:
        window.grab_set()
    except tk.TclError:
        pass
    if focus is not None:
        focus.focus_set()
    window.wait_window()
    if previous_grab is not None:
        try:
            if previous_grab.winfo_exists():
                previous_grab.grab_set()
        except tk.TclError:
            pass


class _ThemedTextPrompt:
    """Palette-aware replacement for ``simpledialog.askstring``."""

    def __init__(
        self,
        parent: tk.Misc,
        palette: ThemePalette,
        title: str,
        prompt: str,
        initial_value: str = "",
    ) -> None:
        self.parent = parent
        self.result: str | None = None
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title(title)
        self.window.transient(parent)
        self.window.resizable(False, False)
        _theme_dialog_window(self.window, palette)

        self.value = tk.StringVar(master=self.window, value=initial_value)
        body = ttk.Frame(self.window, style="Card.TFrame", padding=(22, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text=prompt, style="CardText.TLabel").grid(row=0, column=0, sticky="w")
        self.entry = ttk.Entry(body, textvariable=self.value, width=38)
        self.entry.grid(row=1, column=0, sticky="ew", pady=(8, 16))

        actions = ttk.Frame(body, style="CardInner.TFrame")
        actions.grid(row=2, column=0, sticky="e")
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self._cancel)
        self.cancel_button.pack(side="left")
        self.ok_button = ttk.Button(actions, text="OK", style="Primary.TButton", command=self._accept)
        self.ok_button.pack(side="left", padx=(8, 0))

        self.window.bind("<Return>", self._accept)
        self.window.bind("<Escape>", self._cancel)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)

    def _accept(self, _event: object | None = None) -> str:
        self.result = self.value.get()
        self.window.destroy()
        return "break"

    def _cancel(self, _event: object | None = None) -> str:
        self.result = None
        self.window.destroy()
        return "break"

    def show(self) -> str | None:
        self.entry.selection_range(0, "end")
        _show_modal(self.window, self.parent, self.entry)
        return self.result


class _ThemedConfirmDialog:
    """Small app-themed yes/no dialog for destructive confirmations."""

    def __init__(
        self,
        parent: tk.Misc,
        palette: ThemePalette,
        title: str,
        message: str,
        confirm_text: str = "OK",
    ) -> None:
        self.parent = parent
        self.result = False
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title(title)
        self.window.transient(parent)
        self.window.resizable(False, False)
        _theme_dialog_window(self.window, palette)

        body = ttk.Frame(self.window, style="Card.TFrame", padding=(22, 18))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=message, style="CardText.TLabel", wraplength=380, justify="left").pack(anchor="w")
        actions = ttk.Frame(body, style="CardInner.TFrame")
        actions.pack(anchor="e", pady=(18, 0))
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self._cancel)
        self.cancel_button.pack(side="left")
        self.confirm_button = ttk.Button(
            actions,
            text=confirm_text,
            style="Danger.TButton",
            command=self._confirm,
        )
        self.confirm_button.pack(side="left", padx=(8, 0))
        self.window.bind("<Return>", self._confirm)
        self.window.bind("<Escape>", self._cancel)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)

    def _confirm(self, _event: object | None = None) -> str:
        self.result = True
        self.window.destroy()
        return "break"

    def _cancel(self, _event: object | None = None) -> str:
        self.result = False
        self.window.destroy()
        return "break"

    def show(self) -> bool:
        _show_modal(self.window, self.parent, self.cancel_button)
        return self.result


class _ThemedAlertDialog:
    """App-themed replacement for the small native error message boxes."""

    def __init__(self, parent: tk.Misc, palette: ThemePalette, title: str, message: str) -> None:
        self.parent = parent
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title(title)
        self.window.transient(parent)
        self.window.resizable(False, False)
        _theme_dialog_window(self.window, palette)

        body = ttk.Frame(self.window, style="Card.TFrame", padding=(22, 18))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=message, style="CardText.TLabel", wraplength=420, justify="left").pack(anchor="w")
        self.ok_button = ttk.Button(body, text="OK", style="Primary.TButton", command=self.window.destroy)
        self.ok_button.pack(anchor="e", pady=(18, 0))
        self.window.bind("<Return>", lambda _event: self.window.destroy())
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

    def show(self) -> None:
        _show_modal(self.window, self.parent, self.ok_button)


class _ReplacementDialog:
    """Small modal editor for one literal OCR replacement rule."""

    def __init__(
        self,
        parent: tk.Misc,
        palette: ThemePalette,
        initial: dict[str, object] | None = None,
    ) -> None:
        initial = initial or {}
        self.palette = palette
        self.result: dict[str, object] | None = None
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title("Replacement rule")
        self.window.transient(parent)
        self.window.resizable(False, False)
        _theme_dialog_window(self.window, palette)
        self.original = tk.StringVar(value=str(initial.get("original", "")))
        self.replacement = tk.StringVar(value=str(initial.get("replacement", "")))
        self.enabled = tk.BooleanVar(value=initial.get("enabled", True) is True)
        self.case_sensitive = tk.BooleanVar(value=initial.get("case_sensitive", False) is True)
        self.whole_word = tk.BooleanVar(value=initial.get("whole_word", True) is True)

        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Text detected by OCR").grid(row=0, column=0, sticky="w", pady=5)
        original_entry = ttk.Entry(frame, textvariable=self.original, width=38)
        original_entry.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=5)
        ttk.Label(frame, text="Replace it with").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.replacement, width=38).grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=5)
        checks = ttk.Frame(frame)
        checks.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 4))
        ttk.Checkbutton(checks, text="Enabled", variable=self.enabled).pack(side="left")
        ttk.Checkbutton(checks, text="Match whole words", variable=self.whole_word).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(checks, text="Match exact case", variable=self.case_sensitive).pack(side="left", padx=(14, 0))
        actions = ttk.Frame(frame)
        actions.grid(row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(actions, text="Cancel", command=self.window.destroy).pack(side="left")
        ttk.Button(actions, text="Save rule", style="Primary.TButton", command=self._save).pack(side="left", padx=(8, 0))
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.bind("<Return>", lambda _event: self._save())
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        _show_modal(self.window, parent, original_entry)

    def _save(self) -> None:
        original = self.original.get().strip()
        if not original:
            _ThemedAlertDialog(
                self.window,
                self.palette,
                "Missing original text",
                "Enter the text OCR should match.",
            ).show()
            return
        self.result = {
            "original": original,
            "replacement": self.replacement.get(),
            "enabled": self.enabled.get(),
            "case_sensitive": self.case_sensitive.get(),
            "whole_word": self.whole_word.get(),
        }
        self.window.destroy()


class _ShortcutRecorderDialog:
    """Capture one keyboard chord instead of asking the user to type its name."""

    _MODIFIERS = {
        "Control_L": "Ctrl", "Control_R": "Ctrl",
        "Alt_L": "Alt", "Alt_R": "Alt",
        "Shift_L": "Shift", "Shift_R": "Shift",
        "Super_L": "Win", "Super_R": "Win", "Win_L": "Win", "Win_R": "Win",
    }
    _SPECIAL = {
        "space": "Space", "Return": "Enter", "Tab": "Tab", "Escape": "Esc",
        "Insert": "Insert", "Delete": "Delete", "Home": "Home", "End": "End",
        "Prior": "PageUp", "Next": "PageDown", "Up": "Up", "Down": "Down",
        "Left": "Left", "Right": "Right",
    }

    def __init__(self, parent: tk.Misc, palette: ThemePalette, title: str) -> None:
        self.result: str | None = None
        self._pressed_modifiers: set[str] = set()
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title(f"Record {title}")
        self.window.transient(parent)
        self.window.resizable(False, False)
        _theme_dialog_window(self.window, palette)
        frame = ttk.Frame(self.window, padding=(24, 22))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Press your shortcut now", font=("Segoe UI", 14, "bold")).pack()
        self.prompt = tk.StringVar(value="Hold modifiers, then press one key")
        ttk.Label(frame, textvariable=self.prompt).pack(pady=(8, 4))
        ttk.Label(frame, text="Examples: Ctrl + Shift + T, Alt + Q, Shift + F8", style="CardHint.TLabel").pack()
        ttk.Button(frame, text="Cancel", command=self.window.destroy).pack(pady=(18, 0))
        self.window.bind("<KeyPress>", self._key_pressed)
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        _show_modal(self.window, parent, self.window)

    def _key_pressed(self, event: tk.Event) -> str:
        keysym = str(event.keysym)
        modifier = self._MODIFIERS.get(keysym)
        if modifier:
            self._pressed_modifiers.add(modifier)
            self.prompt.set(" + ".join(sorted(self._pressed_modifiers)) + " + …")
            return "break"
        if keysym == "Escape" and not self._pressed_modifiers:
            self.window.destroy()
            return "break"

        modifiers = set(self._pressed_modifiers)
        state = int(getattr(event, "state", 0))
        if state & 0x0004:
            modifiers.add("Ctrl")
        if state & 0x0001:
            modifiers.add("Shift")
        if state & 0x20000:
            modifiers.add("Alt")
        if keysym in self._SPECIAL:
            trigger = self._SPECIAL[keysym]
        elif keysym.upper().startswith("F") and keysym[1:].isdigit():
            trigger = keysym.upper()
        elif len(keysym) == 1 and (keysym.isalnum() or keysym in "`;=,-./[]\\'"):
            trigger = keysym.upper() if keysym.isalnum() else keysym
        else:
            self.prompt.set(f"{keysym} is not supported. Try another key.")
            return "break"
        order = {"Ctrl": 0, "Alt": 1, "Shift": 2, "Win": 3}
        candidate = "+".join([*sorted(modifiers, key=order.get), trigger])
        try:
            self.result = normalise_hotkey(candidate)
        except HotkeyError as exc:
            self.prompt.set(str(exc))
            return "break"
        self.window.destroy()
        return "break"


class SettingsUI:
    """Own the desktop interface while application actions remain callbacks."""

    APPEARANCE_CHOICES = ("System", "Dark", "Light")
    CAPTURE_SPEECH_CHOICES = (
        "Replace current line",
        "Queue next line",
        "Allow overlapping lines",
    )

    def __init__(
        self,
        root: tk.Tk,
        config: ConfigStore,
        tts: TtsEngine,
        on_draw_box: Callable[[], None],
        on_read_box: Callable[[], None],
        on_quick_snippet: Callable[[], None],
        on_apply_hotkeys: Callable[[str, str, str], None],
        on_ocr_settings_changed: Callable[[], None] | None = None,
        on_shortcut_recording: Callable[[bool], None] | None = None,
        on_read_again: Callable[[], None] | None = None,
        on_clear_text: Callable[[], None] | None = None,
        on_profile_create: Callable[[str], None] | None = None,
        on_profile_rename: Callable[[str, str], None] | None = None,
        on_profile_delete: Callable[[str], None] | None = None,
        on_profile_select: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root
        self.config = config
        self.tts = tts
        self.on_draw_box = on_draw_box
        self.on_read_box = on_read_box
        self.on_quick_snippet = on_quick_snippet
        self.on_apply_hotkeys = on_apply_hotkeys
        self.on_ocr_settings_changed = on_ocr_settings_changed or (lambda: None)
        self.on_shortcut_recording = on_shortcut_recording or (lambda _active: None)
        self.on_read_again = on_read_again or (lambda: None)
        self.on_clear_text = on_clear_text or (lambda: None)
        self.on_profile_create = on_profile_create or (lambda _name: None)
        self.on_profile_rename = on_profile_rename or (lambda _profile_id, _name: None)
        self.on_profile_delete = on_profile_delete or (lambda _profile_id: None)
        self.on_profile_select = on_profile_select or (lambda _profile_id: None)
        self._voices: dict[str, Voice] = {}
        self._voice_labels: dict[str, str] = {}
        self._label_by_id: dict[str, str] = {}
        self._voice_results: SimpleQueue[list[Voice]] = SimpleQueue()
        self._voice_save_after: str | None = None
        settings = config.get()

        self.voice_value = tk.StringVar()
        self.rate_value = tk.IntVar(value=settings["rate"])
        self.volume_value = tk.IntVar(value=settings["volume"])
        speech_settings = settings.get("speech", {})
        capture_mode = speech_settings.get("capture_mode", "replace")
        self.capture_speech_value = tk.StringVar(
            value=(
                "Replace current line"
                if capture_mode == "replace"
                else (
                    "Allow overlapping lines"
                    if capture_mode == "overlap"
                    else "Queue next line"
                )
            )
        )
        self.capture_overlap_value = tk.IntVar(
            value=max(2, min(4, int(speech_settings.get("max_overlap", 2))))
        )
        self.fixed_hotkey = tk.StringVar(value=settings["hotkeys"]["fixed"])
        self.snippet_hotkey = tk.StringVar(value=settings["hotkeys"]["snippet"])
        self.read_again_hotkey = tk.StringVar(value=settings["hotkeys"].get("read_again", ""))
        self.theme_value = tk.StringVar(value=settings["theme"].title())
        self.profile_value = tk.StringVar()
        self._profile_id_by_name: dict[str, str] = {}
        self._profile_name_by_id: dict[str, str] = {}
        self.box_value = tk.StringVar()
        self.capture_meta = tk.StringVar(value="Nothing captured yet")
        self.voice_info = tk.StringVar(value="Discovering installed Windows voices…")
        self.status_value = tk.StringVar(value="Starting…")
        self.hotkey_status = tk.StringVar(value="Shortcuts starting")
        ocr_settings = settings["ocr"]
        self.ocr_enabled = tk.BooleanVar(value=ocr_settings["enabled"])
        self.ocr_strength = tk.StringVar(value=ocr_settings["strength"].title())
        self.ocr_debug = tk.BooleanVar(value=ocr_settings["debug_logging"])
        self._replacement_rules = list(ocr_settings["replacements"])
        self._protected_words = list(ocr_settings["protected_words"])
        self._last_result: CorrectionResult | None = None
        self._comboboxes: list[ttk.Combobox] = []
        self._palette = resolve_theme(settings["theme"])

        self._configure_window()
        self._configure_styles(self._palette)
        self._build()
        self._apply_tk_colours(self._palette)
        self.set_profiles(settings["capture_profiles"], settings["selected_profile_id"], settings.get("fixed_box"))

        # This callback is registered from Tk's main thread. The worker only
        # places plain Python data in a queue and never calls Tk itself.
        self.root.after(60, self._poll_voice_results)
        threading.Thread(target=self._load_voices, name="voice-enumeration", daemon=True).start()

    def _configure_window(self) -> None:
        self.root.title("Game Text Reader")
        self.root.minsize(780, 720)
        self.window_state = WindowStateController(self.root, self.config)
        self.root.option_add("*Font", "{Segoe UI} 10")
        self.root.configure(bg=self._palette.window)
        self.root.bind("<Map>", self._window_mapped, add="+")
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

    def _configure_styles(self, palette: ThemePalette) -> None:
        """Apply semantic colours to every ttk style in one place."""
        self._palette = palette
        style = self.style
        style.configure(".", background=palette.window, foreground=palette.text, font=("Segoe UI", 10))
        style.configure("App.TFrame", background=palette.window)
        style.configure("Surface.TFrame", background=palette.surface)
        style.configure("Card.TFrame", background=palette.card, bordercolor=palette.border, relief="solid", borderwidth=1)
        style.configure("CardInner.TFrame", background=palette.card, borderwidth=0)
        style.configure("Header.TFrame", background=palette.card)
        style.configure("HeaderTitle.TLabel", background=palette.card, foreground=palette.text, font=("Segoe UI", 21, "bold"))
        style.configure("HeaderSub.TLabel", background=palette.card, foreground=palette.muted, font=("Segoe UI", 10))
        style.configure("HeaderMeta.TLabel", background=palette.card, foreground=palette.muted, font=("Segoe UI", 9))
        style.configure("CardTitle.TLabel", background=palette.card, foreground=palette.text, font=("Segoe UI", 12, "bold"))
        style.configure("CardText.TLabel", background=palette.card, foreground=palette.text)
        style.configure("CardHint.TLabel", background=palette.card, foreground=palette.muted, font=("Segoe UI", 9))
        style.configure("HotkeysOn.TLabel", background=palette.success_soft, foreground=palette.success, font=("Segoe UI", 9, "bold"), padding=(10, 6))
        style.configure("HotkeysOff.TLabel", background=palette.danger_soft, foreground=palette.danger, font=("Segoe UI", 9, "bold"), padding=(10, 6))
        style.configure("Status.TFrame", background=palette.surface)
        style.configure("Status.TLabel", background=palette.surface, foreground=palette.muted, font=("Segoe UI", 9))
        style.configure("StatusError.TLabel", background=palette.surface, foreground=palette.danger, font=("Segoe UI", 9, "bold"))

        style.configure(
            "TButton",
            background=palette.button,
            foreground=palette.text,
            bordercolor=palette.border,
            lightcolor=palette.button,
            darkcolor=palette.button,
            padding=(12, 8),
            relief="flat",
        )
        style.map(
            "TButton",
            background=[("pressed", palette.card_alt), ("active", palette.button_hover), ("disabled", palette.card_alt)],
            foreground=[("disabled", palette.muted)],
        )
        style.configure(
            "Primary.TButton",
            background=palette.accent,
            foreground="#ffffff",
            bordercolor=palette.accent,
            lightcolor=palette.accent,
            darkcolor=palette.accent,
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("pressed", palette.accent_pressed), ("active", palette.accent_hover)],
            foreground=[("disabled", "#d7e2f2")],
        )
        style.configure(
            "Danger.TButton",
            background=palette.danger_soft,
            foreground=palette.danger,
            bordercolor=palette.danger,
            lightcolor=palette.danger_soft,
            darkcolor=palette.danger_soft,
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Danger.TButton",
            background=[("pressed", palette.danger), ("active", palette.danger)],
            foreground=[("pressed", "#ffffff"), ("active", "#ffffff")],
        )
        style.configure("Compact.TButton", padding=(8, 5), font=("Segoe UI", 9))

        for widget_style in ("TEntry", "TSpinbox", "TCombobox"):
            style.configure(
                widget_style,
                fieldbackground=palette.input,
                background=palette.input,
                foreground=palette.text,
                bordercolor=palette.border,
                lightcolor=palette.border,
                darkcolor=palette.border,
                arrowcolor=palette.text,
                insertcolor=palette.text,
                padding=6,
            )
            style.map(
                widget_style,
                fieldbackground=[("readonly", palette.input), ("disabled", palette.card_alt)],
                foreground=[("readonly", palette.text), ("disabled", palette.muted)],
                bordercolor=[("focus", palette.accent)],
            )

        # Clam leaves the arrow element on its light system colour while a
        # combobox is hovered or pressed unless those states are mapped
        # explicitly. Keep the field and arrow surfaces in the same palette
        # for every interaction state, including disabled controls.
        style.map(
            "TCombobox",
            background=[
                ("disabled", palette.card_alt),
                ("pressed", palette.button_hover),
                ("active", palette.button_hover),
                ("readonly", palette.input),
                ("focus", palette.input),
            ],
            fieldbackground=[
                ("disabled", palette.card_alt),
                ("readonly", palette.input),
                ("focus", palette.input),
            ],
            foreground=[
                ("disabled", palette.muted),
                ("readonly", palette.text),
                ("focus", palette.text),
            ],
            arrowcolor=[
                ("disabled", palette.muted),
                ("pressed", palette.text),
                ("active", palette.text),
                ("readonly", palette.text),
                ("focus", palette.text),
            ],
            bordercolor=[
                ("disabled", palette.border),
                ("focus", palette.accent),
                ("active", palette.accent),
            ],
            selectbackground=[("focus", palette.selection)],
            selectforeground=[("focus", palette.text)],
        )

        style.configure("TScale", background=palette.card, troughcolor=palette.card_alt, bordercolor=palette.border)
        style.configure("TCheckbutton", background=palette.card, foreground=palette.text)
        style.map("TCheckbutton", background=[("active", palette.card)], foreground=[("disabled", palette.muted)])
        style.configure(
            "Treeview",
            background=palette.input,
            fieldbackground=palette.input,
            foreground=palette.text,
            bordercolor=palette.border,
            rowheight=28,
        )
        style.map("Treeview", background=[("selected", palette.selection)], foreground=[("selected", palette.text)])
        style.configure("Treeview.Heading", background=palette.card_alt, foreground=palette.text, font=("Segoe UI", 9, "bold"))
        style.configure("TNotebook", background=palette.window, borderwidth=0, tabmargins=0)
        style.configure(
            "TNotebook.Tab",
            background=palette.card_alt,
            foreground=palette.muted,
            padding=(16, 9),
            font=("Segoe UI", 10, "bold"),
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", palette.card), ("active", palette.button_hover)],
            foreground=[("selected", palette.text), ("active", palette.text)],
        )
        style.configure(
            "Vertical.TScrollbar",
            background=palette.button,
            troughcolor=palette.input,
            bordercolor=palette.border,
            arrowcolor=palette.text,
            lightcolor=palette.button,
            darkcolor=palette.border,
        )
        style.configure(
            "TScrollbar",
            background=palette.button,
            troughcolor=palette.input,
            bordercolor=palette.border,
            arrowcolor=palette.text,
            lightcolor=palette.button,
            darkcolor=palette.border,
        )
        style.configure(
            "Horizontal.TScrollbar",
            background=palette.button,
            troughcolor=palette.input,
            bordercolor=palette.border,
            arrowcolor=palette.text,
            lightcolor=palette.button,
            darkcolor=palette.border,
        )

        # The open list is a classic Tk Listbox created by ttk's combobox
        # implementation, not another ttk widget. Style both the option
        # database (for newly-created popdowns) and existing popdowns (for a
        # live appearance switch).
        style.configure(
            "ComboboxPopdownFrame",
            background=palette.input,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            relief="solid",
            borderwidth=1,
        )
        listbox_options = {
            "background": palette.input,
            "foreground": palette.text,
            "selectBackground": palette.selection,
            "selectForeground": palette.text,
            "disabledForeground": palette.muted,
            "highlightBackground": palette.border,
            "highlightColor": palette.accent,
            "highlightThickness": 1,
            "borderWidth": 0,
            "selectBorderWidth": 0,
            "relief": "flat",
            "font": "{Segoe UI} 10",
        }
        for pattern in ("*ComboboxPopdown*Listbox", "*TCombobox*Listbox", "*Combobox*Listbox"):
            for option, value in listbox_options.items():
                self.root.option_add(f"{pattern}.{option}", value)
        self.root.option_add("*ComboboxPopdown.background", palette.input)
        self._refresh_combobox_popdowns()

    def _register_combobox(self, combo: ttk.Combobox) -> ttk.Combobox:
        """Attach a palette refresh to a ttk combobox's native Tk popdown."""
        if not hasattr(self, "_comboboxes"):
            self._comboboxes = []
        self._comboboxes.append(combo)
        combo.configure(postcommand=lambda combo=combo: self._prepare_combobox_popdown(combo))
        return combo

    def _refresh_combobox_popdowns(self) -> None:
        for combo in getattr(self, "_comboboxes", ()):
            self._style_combobox_popdown(combo)

    def _prepare_combobox_popdown(self, combo: ttk.Combobox) -> None:
        """Create and style a popdown before ttk posts it for the first time."""
        try:
            self.root.tk.call("ttk::combobox::PopdownWindow", combo._w)
        except (AttributeError, tk.TclError, TypeError, ValueError):
            return
        self._style_combobox_popdown(combo)

    def _style_combobox_popdown(self, combo: ttk.Combobox) -> None:
        """Apply the current palette to an already-created combobox list."""
        try:
            if not combo.winfo_exists():
                return
            popdown = f"{combo._w}.popdown"
            if not int(self.root.tk.call("winfo", "exists", popdown)):
                return
            self.root.tk.call(
                popdown,
                "configure",
                "-background",
                self._palette.input,
                "-highlightbackground",
                self._palette.border,
                "-highlightcolor",
                self._palette.accent,
                "-highlightthickness",
                1,
                "-borderwidth",
                0,
                "-relief",
                "flat",
            )
            for frame in self.root.tk.call("winfo", "children", popdown):
                for widget in self.root.tk.call("winfo", "children", frame):
                    if self.root.tk.call("winfo", "class", widget) != "Listbox":
                        continue
                    options = (
                        ("-background", self._palette.input),
                        ("-foreground", self._palette.text),
                        ("-selectbackground", self._palette.selection),
                        ("-selectforeground", self._palette.text),
                        ("-disabledforeground", self._palette.muted),
                        ("-highlightbackground", self._palette.border),
                        ("-highlightcolor", self._palette.accent),
                        ("-highlightthickness", 1),
                        ("-borderwidth", 0),
                        ("-selectborderwidth", 0),
                        ("-relief", "flat"),
                    )
                    for option, value in options:
                        self.root.tk.call(widget, "configure", option, value)
        except (AttributeError, tk.TclError, TypeError, ValueError):
            # A popdown can disappear between the existence check and the
            # configure call when the user closes it during a theme switch.
            return

    def _build(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(18, 16, 18, 12))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="Card.TFrame", padding=(20, 16))
        header.pack(fill="x", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        brand = ttk.Frame(header, style="Header.TFrame")
        brand.grid(row=0, column=0, rowspan=2, sticky="w")
        ttk.Label(brand, text="Game Text Reader", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(brand, text="Hear game dialogue and on-screen text instantly", style="HeaderSub.TLabel").pack(anchor="w", pady=(3, 0))

        appearance = ttk.Frame(header, style="Header.TFrame")
        appearance.grid(row=0, column=1, sticky="e")
        ttk.Label(appearance, text="Appearance", style="HeaderMeta.TLabel").pack(side="left", padx=(0, 7))
        self.theme_combo = self._register_combobox(
            ttk.Combobox(
                appearance,
                textvariable=self.theme_value,
                values=self.APPEARANCE_CHOICES,
                state="readonly",
                width=9,
            )
        )
        self.theme_combo.pack(side="left")
        self.theme_combo.bind("<<ComboboxSelected>>", self._theme_changed)
        self.hotkey_badge = ttk.Label(header, textvariable=self.hotkey_status, style="HotkeysOff.TLabel")
        self.hotkey_badge.grid(row=1, column=1, sticky="e", pady=(9, 0))

        quick = ttk.Frame(outer, style="Card.TFrame", padding=(16, 13))
        quick.pack(fill="x", pady=(0, 12))
        quick.columnconfigure(0, weight=1)
        quick_copy = ttk.Frame(quick, style="CardInner.TFrame")
        quick_copy.grid(row=0, column=0, sticky="w")
        ttk.Label(quick_copy, text="Quick actions", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(quick_copy, text="Reads stay in the background while you play.", style="CardHint.TLabel").pack(anchor="w", pady=(2, 0))
        quick_buttons = ttk.Frame(quick, style="CardInner.TFrame")
        quick_buttons.grid(row=0, column=1, sticky="e", padx=(12, 0))
        ttk.Button(quick_buttons, text="Read fixed box", style="Primary.TButton", command=self._read_now).pack(side="left")
        ttk.Button(quick_buttons, text="Select area", command=self._select_area).pack(side="left", padx=(7, 0))
        ttk.Button(quick_buttons, text="Stop audio", command=self.stop_speech).pack(side="left", padx=(7, 0))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        reader_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=(0, 10, 0, 0))
        settings_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=(0, 10, 0, 0))
        ocr_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=(0, 10, 0, 0))
        self.notebook.add(reader_tab, text="Reader")
        self.notebook.add(ocr_tab, text="OCR corrections")
        self.notebook.add(settings_tab, text="Voice & shortcuts")
        reader_tab.columnconfigure(0, weight=1)
        reader_tab.rowconfigure(1, weight=1)
        ocr_tab.columnconfigure(0, weight=1)
        ocr_tab.rowconfigure(1, weight=1)

        self._build_reader_tab(reader_tab)
        self._build_ocr_tab(ocr_tab)
        self._build_settings_tab(self._make_scrollable_tab(settings_tab))

        status_bar = ttk.Frame(outer, style="Status.TFrame", padding=(2, 10, 2, 0))
        status_bar.pack(fill="x")
        ttk.Label(status_bar, text="●", style="Status.TLabel").pack(side="left", padx=(0, 7))
        self.status_label = ttk.Label(status_bar, textvariable=self.status_value, style="Status.TLabel", anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)
        ttk.Label(status_bar, text="Close minimizes • tray keeps shortcuts ready", style="Status.TLabel").pack(side="right")

    def _make_scrollable_tab(self, parent: ttk.Frame) -> ttk.Frame:
        """Keep all controls reachable on high-DPI or short displays."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0, bg=self._palette.window)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        content = ttk.Frame(canvas, style="App.TFrame", padding=(0, 0, 6, 0))
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))
        self.settings_canvas = canvas
        return content

    def _build_reader_tab(self, parent: ttk.Frame) -> None:
        box_card = ttk.Frame(parent, style="Card.TFrame", padding=(16, 14))
        box_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        box_card.columnconfigure(0, weight=1)
        ttk.Label(box_card, text="Capture area", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        box_actions = ttk.Frame(box_card, style="CardInner.TFrame")
        box_actions.grid(row=0, column=1, sticky="e", padx=(12, 0))
        ttk.Button(box_actions, text="Set capture area", command=self.on_draw_box).pack(side="left")
        ttk.Button(box_actions, text="Read now", style="Primary.TButton", command=self._read_now).pack(side="left", padx=(7, 0))

        profile_row = ttk.Frame(box_card, style="CardInner.TFrame")
        profile_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        profile_row.columnconfigure(1, weight=1)
        ttk.Label(profile_row, text="Profile", style="CardText.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.profile_combo = self._register_combobox(
            ttk.Combobox(profile_row, textvariable=self.profile_value, state="readonly")
        )
        self.profile_combo.grid(row=0, column=1, sticky="ew")
        self.profile_combo.bind("<<ComboboxSelected>>", self._profile_selected)
        profile_actions = ttk.Frame(profile_row, style="CardInner.TFrame")
        profile_actions.grid(row=0, column=2, sticky="e", padx=(8, 0))
        ttk.Button(profile_actions, text="New", style="Compact.TButton", command=self.create_profile).pack(side="left")
        ttk.Button(profile_actions, text="Rename", style="Compact.TButton", command=self.rename_profile).pack(side="left", padx=(5, 0))
        ttk.Button(profile_actions, text="Delete", style="Compact.TButton", command=self.delete_profile).pack(side="left", padx=(5, 0))
        ttk.Label(box_card, textvariable=self.box_value, style="CardText.TLabel", font=("Segoe UI", 10, "bold")).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(box_card, text="The fixed shortcut uses the selected profile without bringing settings forward.", style="CardHint.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(3, 0))

        captured = ttk.Frame(parent, style="Card.TFrame", padding=(16, 14))
        captured.grid(row=1, column=0, sticky="nsew")
        captured.columnconfigure(0, weight=1)
        captured.rowconfigure(2, weight=1)
        ttk.Label(captured, text="Last captured text", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(captured, textvariable=self.capture_meta, style="CardHint.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 8))
        self.captured_text = scrolledtext.ScrolledText(
            captured,
            wrap="word",
            height=10,
            font=("Segoe UI", 11),
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=10,
            undo=False,
        )
        self.captured_text.grid(row=2, column=0, columnspan=2, sticky="nsew")
        capture_actions = ttk.Frame(captured, style="CardInner.TFrame")
        capture_actions.grid(row=0, column=1, rowspan=2, sticky="e")
        self.read_again_button = ttk.Button(capture_actions, text="Read Again", style="Primary.TButton", command=self.on_read_again, state="disabled")
        self.read_again_button.pack(side="left", padx=(0, 6))
        self.details_button = ttk.Button(capture_actions, text="Corrections", style="Compact.TButton", command=self.show_correction_details, state="disabled")
        self.details_button.pack(side="left")
        ttk.Button(capture_actions, text="Clear", style="Compact.TButton", command=self.clear_text).pack(side="left")
        ttk.Button(capture_actions, text="Copy", style="Compact.TButton", command=self.copy_text).pack(side="left", padx=(6, 0))

    def _build_ocr_tab(self, parent: ttk.Frame) -> None:
        automatic = ttk.Frame(parent, style="Card.TFrame", padding=(16, 14))
        automatic.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        automatic.columnconfigure(2, weight=1)
        ttk.Label(automatic, text="Automatic correction", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(
            automatic,
            text="Conservative keeps unusual game terms; Balanced also fixes likely spelling and joined words.",
            style="CardHint.TLabel",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 10))
        ttk.Checkbutton(automatic, text="Correct OCR text before reading", variable=self.ocr_enabled, command=self.save_ocr_settings).grid(row=2, column=0, sticky="w")
        ttk.Label(automatic, text="Strength", style="CardText.TLabel").grid(row=2, column=1, sticky="e", padx=(18, 7))
        strength = self._register_combobox(
            ttk.Combobox(
                automatic,
                textvariable=self.ocr_strength,
                values=("Conservative", "Balanced", "Strong"),
                state="readonly",
                width=14,
            )
        )
        strength.grid(row=2, column=2, sticky="w")
        strength.bind("<<ComboboxSelected>>", lambda _event: self.save_ocr_settings())
        ttk.Checkbutton(automatic, text="Write correction debug log", variable=self.ocr_debug, command=self.save_ocr_settings).grid(row=2, column=3, sticky="e", padx=(14, 0))

        lists = ttk.Frame(parent, style="App.TFrame")
        lists.grid(row=1, column=0, sticky="nsew")
        lists.columnconfigure(0, weight=3)
        lists.columnconfigure(1, weight=2)
        lists.rowconfigure(0, weight=1)

        replacements = ttk.Frame(lists, style="Card.TFrame", padding=(14, 12))
        replacements.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        replacements.columnconfigure(0, weight=1)
        replacements.rowconfigure(2, weight=1)
        ttk.Label(replacements, text="Custom replacements", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(replacements, text="Your rules run first and their results are protected.", style="CardHint.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 8))
        self.replacement_tree = ttk.Treeview(replacements, columns=("on", "from", "to", "match"), show="headings", height=9, selectmode="browse")
        self.replacement_tree.heading("on", text="On")
        self.replacement_tree.heading("from", text="OCR text")
        self.replacement_tree.heading("to", text="Replace with")
        self.replacement_tree.heading("match", text="Match")
        self.replacement_tree.column("on", width=38, stretch=False, anchor="center")
        self.replacement_tree.column("from", width=120)
        self.replacement_tree.column("to", width=120)
        self.replacement_tree.column("match", width=85, stretch=False)
        self.replacement_tree.grid(row=2, column=0, sticky="nsew")
        rule_actions = ttk.Frame(replacements, style="CardInner.TFrame")
        rule_actions.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        ttk.Button(rule_actions, text="Add", style="Compact.TButton", command=self.add_replacement).pack(side="left")
        ttk.Button(rule_actions, text="Edit", style="Compact.TButton", command=self.edit_replacement).pack(side="left", padx=(5, 0))
        ttk.Button(rule_actions, text="Enable / disable", style="Compact.TButton", command=self.toggle_replacement).pack(side="left", padx=(5, 0))
        ttk.Button(rule_actions, text="Delete", style="Compact.TButton", command=self.delete_replacement).pack(side="right")
        self.replacement_tree.bind("<Double-1>", lambda _event: self.edit_replacement())

        protected = ttk.Frame(lists, style="Card.TFrame", padding=(14, 12))
        protected.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        protected.columnconfigure(0, weight=1)
        protected.rowconfigure(2, weight=1)
        ttk.Label(protected, text="Protected game terms", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(protected, text="Names and lore words that should never be corrected.", style="CardHint.TLabel", wraplength=260).grid(row=1, column=0, sticky="w", pady=(2, 8))
        self.protected_list = tk.Listbox(protected, activestyle="none", selectmode="browse", relief="flat", borderwidth=0, font=("Segoe UI", 10))
        self.protected_list.grid(row=2, column=0, sticky="nsew")
        protected_actions = ttk.Frame(protected, style="CardInner.TFrame")
        protected_actions.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        ttk.Button(protected_actions, text="Add term", style="Compact.TButton", command=self.add_protected_word).pack(side="left")
        ttk.Button(protected_actions, text="Delete", style="Compact.TButton", command=self.delete_protected_word).pack(side="right")
        self._refresh_replacements()
        self._refresh_protected_words()

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        voice = ttk.Frame(parent, style="Card.TFrame", padding=(16, 14))
        voice.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        voice.columnconfigure(1, weight=1)
        ttk.Label(voice, text="Voice", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(voice, textvariable=self.voice_info, style="CardHint.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 10))
        ttk.Label(voice, text="Installed voice", style="CardText.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        self.voice_combo = self._register_combobox(
            ttk.Combobox(voice, textvariable=self.voice_value, state="readonly")
        )
        self.voice_combo.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(12, 0), pady=4)
        self.voice_combo.bind("<<ComboboxSelected>>", lambda _event: self._save_voice_settings())

        ttk.Label(voice, text="Speed", style="CardText.TLabel").grid(row=3, column=0, sticky="w", pady=(10, 4))
        rate_scale = ttk.Scale(voice, from_=-10, to=10, variable=self.rate_value, command=lambda _value: self._schedule_voice_save())
        rate_scale.grid(row=3, column=1, sticky="ew", padx=(12, 10), pady=(10, 4))
        rate_spin = ttk.Spinbox(voice, from_=-10, to=10, textvariable=self.rate_value, width=5)
        rate_spin.grid(row=3, column=2, sticky="e", pady=(10, 4))
        rate_spin.bind("<FocusOut>", lambda _event: self._save_voice_settings())
        rate_spin.bind("<Return>", lambda _event: self._save_voice_settings())
        ttk.Label(voice, text="−10 to 10", style="CardHint.TLabel").grid(row=3, column=3, sticky="e", padx=(8, 0), pady=(10, 4))

        ttk.Label(voice, text="Volume", style="CardText.TLabel").grid(row=4, column=0, sticky="w", pady=4)
        volume_scale = ttk.Scale(voice, from_=0, to=100, variable=self.volume_value, command=lambda _value: self._schedule_voice_save())
        volume_scale.grid(row=4, column=1, sticky="ew", padx=(12, 10), pady=4)
        volume_spin = ttk.Spinbox(voice, from_=0, to=100, textvariable=self.volume_value, width=5)
        volume_spin.grid(row=4, column=2, sticky="e", pady=4)
        volume_spin.bind("<FocusOut>", lambda _event: self._save_voice_settings())
        volume_spin.bind("<Return>", lambda _event: self._save_voice_settings())
        ttk.Label(voice, text="0 to 100", style="CardHint.TLabel").grid(row=4, column=3, sticky="e", padx=(8, 0), pady=4)
        ttk.Label(voice, text="New capture while speaking", style="CardText.TLabel").grid(row=5, column=0, sticky="w", pady=(10, 4))
        self.capture_speech_combo = self._register_combobox(
            ttk.Combobox(
                voice,
                textvariable=self.capture_speech_value,
                values=self.CAPTURE_SPEECH_CHOICES,
                state="readonly",
                width=24,
            )
        )
        self.capture_speech_combo.grid(row=5, column=1, columnspan=3, sticky="ew", padx=(12, 0), pady=(10, 4))
        self.capture_speech_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._capture_mode_changed(),
        )
        self.overlap_options = ttk.Frame(voice, style="CardInner.TFrame")
        self.overlap_options.columnconfigure(1, weight=1)
        ttk.Label(
            self.overlap_options,
            text="Maximum simultaneous readings",
            style="CardText.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.capture_overlap_spin = ttk.Spinbox(
            self.overlap_options,
            from_=2,
            to=4,
            increment=1,
            textvariable=self.capture_overlap_value,
            width=5,
            command=self._save_capture_speech_settings,
        )
        self.capture_overlap_spin.grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.capture_overlap_spin.bind(
            "<FocusOut>", lambda _event: self._save_capture_speech_settings()
        )
        self.capture_overlap_spin.bind(
            "<Return>", lambda _event: self._save_capture_speech_settings()
        )
        self.overlap_options.grid(
            row=6,
            column=1,
            columnspan=3,
            sticky="ew",
            padx=(12, 0),
            pady=(4, 0),
        )
        ttk.Label(
            voice,
            text="Replace is the clearest rapid mode; queue waits for the current line; overlap starts new voices immediately.",
            style="CardHint.TLabel",
            wraplength=620,
            justify="left",
        ).grid(row=7, column=1, columnspan=3, sticky="w", padx=(12, 0), pady=(0, 4))
        ttk.Button(voice, text="Test selected voice", command=self.test_voice).grid(row=8, column=1, columnspan=3, sticky="e", pady=(10, 0))
        self._set_overlap_options_visible(self._capture_mode() == "overlap")

        keys = ttk.Frame(parent, style="Card.TFrame", padding=(16, 14))
        keys.grid(row=1, column=0, sticky="ew")
        keys.columnconfigure(1, weight=1)
        ttk.Label(keys, text="Global shortcuts", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(keys, text="Select Record, then press the exact combination you want. Windows will report conflicts.", style="CardHint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 10))

        ttk.Label(keys, text="Read fixed box", style="CardText.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(keys, textvariable=self.fixed_hotkey, width=22, state="readonly").grid(row=2, column=1, sticky="ew", padx=(12, 8), pady=4)
        fixed_actions = ttk.Frame(keys, style="CardInner.TFrame")
        fixed_actions.grid(row=2, column=2, sticky="e")
        ttk.Button(fixed_actions, text="Record", style="Compact.TButton", command=lambda: self.record_shortcut(self.fixed_hotkey, "Read Fixed Box")).pack(side="left")
        ttk.Button(fixed_actions, text="Clear", style="Compact.TButton", command=lambda: self.fixed_hotkey.set("")).pack(side="left", padx=(5, 0))

        ttk.Label(keys, text="Select a snippet", style="CardText.TLabel").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(keys, textvariable=self.snippet_hotkey, width=22, state="readonly").grid(row=3, column=1, sticky="ew", padx=(12, 8), pady=4)
        snippet_actions = ttk.Frame(keys, style="CardInner.TFrame")
        snippet_actions.grid(row=3, column=2, sticky="e")
        ttk.Button(snippet_actions, text="Record", style="Compact.TButton", command=lambda: self.record_shortcut(self.snippet_hotkey, "Select a Snippet")).pack(side="left")
        ttk.Button(snippet_actions, text="Clear", style="Compact.TButton", command=lambda: self.snippet_hotkey.set("")).pack(side="left", padx=(5, 0))

        ttk.Label(keys, text="Read Again", style="CardText.TLabel").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(keys, textvariable=self.read_again_hotkey, width=22, state="readonly").grid(row=4, column=1, sticky="ew", padx=(12, 8), pady=4)
        again_actions = ttk.Frame(keys, style="CardInner.TFrame")
        again_actions.grid(row=4, column=2, sticky="e")
        ttk.Button(again_actions, text="Record", style="Compact.TButton", command=lambda: self.record_shortcut(self.read_again_hotkey, "Read Again")).pack(side="left")
        ttk.Button(again_actions, text="Clear", style="Compact.TButton", command=lambda: self.read_again_hotkey.set("")).pack(side="left", padx=(5, 0))
        self.apply_shortcuts_button = ttk.Button(keys, text="Apply shortcuts", style="Primary.TButton", command=self.apply_hotkeys)
        self.apply_shortcuts_button.grid(row=5, column=1, columnspan=2, sticky="e", pady=(10, 0))

    def _apply_tk_colours(self, palette: ThemePalette) -> None:
        self.root.configure(bg=palette.window)
        if hasattr(self, "captured_text"):
            self.captured_text.configure(
                bg=palette.input,
                fg=palette.text,
                insertbackground=palette.text,
                selectbackground=palette.selection,
                selectforeground=palette.text,
                highlightbackground=palette.border,
                highlightcolor=palette.accent,
                highlightthickness=1,
            )
        if hasattr(self, "protected_list"):
            self.protected_list.configure(
                bg=palette.input,
                fg=palette.text,
                selectbackground=palette.selection,
                selectforeground=palette.text,
                highlightbackground=palette.border,
                highlightcolor=palette.accent,
                highlightthickness=1,
            )
        if hasattr(self, "settings_canvas"):
            self.settings_canvas.configure(bg=palette.window)
        self.root.after_idle(lambda: apply_windows_title_bar(self.root, palette.dark))

    def _window_mapped(self, _event: object | None = None) -> None:
        """Reapply native-frame colors whenever Windows creates or remaps it."""
        apply_windows_title_bar(self.root, self._palette.dark)

    def save_ocr_settings(self) -> None:
        """Persist the visible correction controls and user dictionaries."""
        saved = self.config.update(
            ocr={
                "enabled": self.ocr_enabled.get(),
                "strength": self.ocr_strength.get().strip().lower(),
                "debug_logging": self.ocr_debug.get(),
                "replacements": self._replacement_rules,
                "protected_words": self._protected_words,
            }
        )["ocr"]
        self._replacement_rules = list(saved["replacements"])
        self._protected_words = list(saved["protected_words"])
        self.on_ocr_settings_changed()
        self.set_status("OCR correction settings saved.")

    def _selected_replacement_index(self) -> int | None:
        selected = self.replacement_tree.selection()
        if not selected:
            self.set_status("Select a replacement rule first.", error=True)
            return None
        try:
            return int(selected[0])
        except (TypeError, ValueError):
            return None

    def _refresh_replacements(self, select_index: int | None = None) -> None:
        if not hasattr(self, "replacement_tree"):
            return
        self.replacement_tree.delete(*self.replacement_tree.get_children())
        for index, rule in enumerate(self._replacement_rules):
            match = "Exact case" if rule.get("case_sensitive") else "Any case"
            match += ", word" if rule.get("whole_word", True) else ", anywhere"
            self.replacement_tree.insert(
                "",
                "end",
                iid=str(index),
                values=("✓" if rule.get("enabled", True) else "—", rule.get("original", ""), rule.get("replacement", ""), match),
            )
        if select_index is not None and str(select_index) in self.replacement_tree.get_children():
            self.replacement_tree.selection_set(str(select_index))

    def add_replacement(self) -> None:
        dialog = _ReplacementDialog(self.root, self._palette)
        if dialog.result is None:
            return
        self._replacement_rules.append(dialog.result)
        self.save_ocr_settings()
        self._refresh_replacements(len(self._replacement_rules) - 1)

    def edit_replacement(self) -> None:
        index = self._selected_replacement_index()
        if index is None:
            return
        dialog = _ReplacementDialog(self.root, self._palette, self._replacement_rules[index])
        if dialog.result is None:
            return
        self._replacement_rules[index] = dialog.result
        self.save_ocr_settings()
        self._refresh_replacements(index)

    def toggle_replacement(self) -> None:
        index = self._selected_replacement_index()
        if index is None:
            return
        updated = dict(self._replacement_rules[index])
        updated["enabled"] = not updated.get("enabled", True)
        self._replacement_rules[index] = updated
        self.save_ocr_settings()
        self._refresh_replacements(index)

    def delete_replacement(self) -> None:
        index = self._selected_replacement_index()
        if index is None:
            return
        del self._replacement_rules[index]
        self.save_ocr_settings()
        self._refresh_replacements(min(index, len(self._replacement_rules) - 1) if self._replacement_rules else None)

    def _refresh_protected_words(self) -> None:
        if not hasattr(self, "protected_list"):
            return
        self.protected_list.delete(0, "end")
        for term in self._protected_words:
            self.protected_list.insert("end", term)

    def add_protected_word(self) -> None:
        term = _ThemedTextPrompt(
            self.root,
            self._palette,
            "Protect a game term",
            "Name, phrase, item, or lore term:",
        ).show()
        if term is None or not term.strip():
            return
        if term.strip().casefold() in {item.casefold() for item in self._protected_words}:
            self.set_status("That protected term already exists.", error=True)
            return
        self._protected_words.append(term.strip())
        self.save_ocr_settings()
        self._refresh_protected_words()
        self.protected_list.selection_set("end")

    def delete_protected_word(self) -> None:
        selected = self.protected_list.curselection()
        if not selected:
            self.set_status("Select a protected term first.", error=True)
            return
        del self._protected_words[selected[0]]
        self.save_ocr_settings()
        self._refresh_protected_words()

    def _theme_changed(self, _event: object | None = None) -> None:
        preference = self.theme_value.get().strip().lower()
        self.config.update(theme=preference)
        self._palette = resolve_theme(preference)
        self._configure_styles(self._palette)
        self._apply_tk_colours(self._palette)
        self.set_status(f"{self.theme_value.get()} appearance applied.")

    def _load_voices(self) -> None:
        """Discover voices off-thread and publish only plain data to the UI queue."""
        try:
            voices = self.tts.list_voices()
        except Exception:
            voices = []
        self._voice_results.put(voices)

    def _poll_voice_results(self) -> None:
        try:
            voices = self._voice_results.get_nowait()
        except Empty:
            try:
                self.root.after(60, self._poll_voice_results)
            except tk.TclError:
                pass
            return
        self._set_voices(voices)

    def _set_voices(self, voices: list[Voice]) -> None:
        self._voices = {voice.identifier: voice for voice in voices}
        self._voice_labels = {}
        self._label_by_id = {}
        for voice in voices:
            label = voice.display_name
            suffix = 2
            while label in self._voice_labels:
                label = f"{voice.display_name} [{suffix}]"
                suffix += 1
            self._voice_labels[label] = voice.identifier
            self._label_by_id[voice.identifier] = label
        labels = list(self._voice_labels)
        self.voice_combo["values"] = labels
        saved_voice = self.config.get()["voice"]
        selected = self._voices.get(saved_voice)
        if selected:
            self.voice_value.set(self._label_by_id[selected.identifier])
        elif labels:
            self.voice_value.set(labels[0])
            self._save_voice_settings()
        if labels:
            self.voice_combo.configure(state="readonly")
            self.voice_info.set(f"{len(labels)} Windows voice{'s' if len(labels) != 1 else ''} available")
        else:
            self.voice_combo.configure(state="disabled")
            self.voice_info.set("No Windows voices found. Check Windows speech settings.")

    def speech_settings(self) -> tuple[str, int, int]:
        """Return current UI speech values as safe primitive types."""
        voice_id = self._voice_labels.get(self.voice_value.get())
        if voice_id is None:
            voice_id = self.config.get()["voice"]
        try:
            rate = max(-10, min(10, int(self.rate_value.get())))
        except (tk.TclError, ValueError):
            rate = 0
        try:
            volume = max(0, min(100, int(self.volume_value.get())))
        except (tk.TclError, ValueError):
            volume = 100
        return voice_id, rate, volume

    def _schedule_voice_save(self) -> None:
        """Debounce slider motion so values apply quickly without disk churn."""
        if self._voice_save_after is not None:
            try:
                self.root.after_cancel(self._voice_save_after)
            except tk.TclError:
                pass
        self._voice_save_after = self.root.after(160, self._save_voice_settings)

    def _save_voice_settings(self) -> None:
        self._voice_save_after = None
        voice_id, rate, volume = self.speech_settings()
        self.config.update(voice=voice_id, rate=rate, volume=volume)

    def _capture_mode(self) -> str:
        return {
            "Replace current line": "replace",
            "Queue next line": "queue",
            "Allow overlapping lines": "overlap",
        }.get(self.capture_speech_value.get(), "replace")

    def _set_overlap_options_visible(self, visible: bool) -> None:
        if not hasattr(self, "overlap_options"):
            return
        if visible:
            self.overlap_options.grid()
        else:
            self.overlap_options.grid_remove()

    def _capture_mode_changed(self) -> None:
        mode = self._capture_mode()
        self._set_overlap_options_visible(mode == "overlap")
        self._save_capture_speech_settings()

    def _save_capture_speech_settings(self) -> None:
        mode = self._capture_mode()
        try:
            max_overlap = max(2, min(4, int(self.capture_overlap_value.get())))
        except (tk.TclError, TypeError, ValueError):
            max_overlap = 2
            self.capture_overlap_value.set(max_overlap)
        self.config.update(
            speech={"capture_mode": mode, "max_overlap": max_overlap}
        )
        setter = getattr(self.tts, "set_capture_mode", None)
        if callable(setter):
            try:
                setter(mode, max_overlap)
            except Exception:
                pass
        if mode == "replace":
            message = "New captures will replace the current line."
        elif mode == "queue":
            message = "New captures will queue one next line."
        else:
            message = f"New captures may overlap (up to {max_overlap} voices)."
        self.set_status(message)

    def _read_now(self) -> None:
        self._save_voice_settings()
        self.on_read_box()

    def _select_area(self) -> None:
        self._save_voice_settings()
        self.on_quick_snippet()

    def test_voice(self) -> None:
        """Speak a short confirmation via the selected Windows voice."""
        self._save_voice_settings()
        voice, rate, volume = self.speech_settings()
        self.tts.stop()
        self.tts.speak("This is your selected Windows voice.", voice, rate, volume)
        self.set_status("Playing a voice sample…")

    def stop_speech(self) -> None:
        """Interrupt current speech and discard anything waiting behind it."""
        self.tts.stop()
        self.set_status("Speech stopped and the queue was cleared.")

    def apply_hotkeys(self) -> None:
        """Register both shortcuts after saving the current voice settings."""
        self._save_voice_settings()
        try:
            self.on_apply_hotkeys(self.fixed_hotkey.get(), self.snippet_hotkey.get(), self.read_again_hotkey.get())
        except Exception as exc:
            self.set_status(str(exc), error=True)
            _ThemedAlertDialog(self.root, self._palette, "Could not apply shortcuts", str(exc)).show()

    def record_shortcut(self, target: tk.StringVar, title: str) -> None:
        """Open the chord recorder and place the captured value in its field."""
        self.on_shortcut_recording(True)
        dialog: _ShortcutRecorderDialog | None = None
        try:
            dialog = _ShortcutRecorderDialog(self.root, self._palette, title)
        finally:
            self.on_shortcut_recording(False)
        if dialog is not None and dialog.result is not None:
            target.set(dialog.result)
            self.set_status(f"Recorded {dialog.result}. Select Apply shortcuts to activate it.")

    def set_hotkeys(self, fixed: str, snippet: str, read_again: str = "") -> None:
        """Replace shortcut fields with the canonical values accepted by Windows."""
        self.fixed_hotkey.set(fixed)
        self.snippet_hotkey.set(snippet)
        self.read_again_hotkey.set(read_again)

    def set_profiles(
        self,
        profiles: list[dict[str, object]],
        selected_profile_id: str,
        box: list[int] | None = None,
        unavailable_reason: str = "",
    ) -> None:
        """Refresh profile choices and the selected area's availability summary."""
        self._profile_id_by_name = {str(profile["name"]): str(profile["id"]) for profile in profiles}
        self._profile_name_by_id = {profile_id: name for name, profile_id in self._profile_id_by_name.items()}
        names = list(self._profile_id_by_name)
        self.profile_combo["values"] = names
        selected_name = self._profile_name_by_id.get(selected_profile_id, names[0] if names else "")
        self.profile_value.set(selected_name)
        if unavailable_reason:
            self.box_value.set(unavailable_reason)
        else:
            self.set_box(box)

    def _selected_profile_id(self) -> str:
        return self._profile_id_by_name.get(self.profile_value.get(), "")

    def _profile_selected(self, _event: object | None = None) -> None:
        profile_id = self._selected_profile_id()
        if profile_id:
            self.on_profile_select(profile_id)

    def create_profile(self) -> None:
        name = _ThemedTextPrompt(
            self.root,
            self._palette,
            "New capture profile",
            "Profile name:",
        ).show()
        if name is None:
            return
        try:
            self.on_profile_create(name)
        except Exception as exc:
            self.set_status(str(exc), error=True)
            _ThemedAlertDialog(self.root, self._palette, "Could not create profile", str(exc)).show()

    def rename_profile(self) -> None:
        profile_id = self._selected_profile_id()
        if not profile_id:
            return
        current = self.profile_value.get()
        name = _ThemedTextPrompt(
            self.root,
            self._palette,
            "Rename capture profile",
            "Profile name:",
            initial_value=current,
        ).show()
        if name is None:
            return
        try:
            self.on_profile_rename(profile_id, name)
        except Exception as exc:
            self.set_status(str(exc), error=True)
            _ThemedAlertDialog(self.root, self._palette, "Could not rename profile", str(exc)).show()

    def delete_profile(self) -> None:
        profile_id = self._selected_profile_id()
        if not profile_id:
            return
        name = self.profile_value.get()
        if not _ThemedConfirmDialog(
            self.root,
            self._palette,
            "Delete capture profile",
            f'Delete "{name}"?',
            confirm_text="Delete",
        ).show():
            return
        try:
            self.on_profile_delete(profile_id)
        except Exception as exc:
            self.set_status(str(exc), error=True)
            _ThemedAlertDialog(self.root, self._palette, "Could not delete profile", str(exc)).show()

    def set_box(self, box: list[int] | None) -> None:
        """Update the fixed-box summary shown in the Reader tab."""
        if not box:
            self.box_value.set("No fixed box saved")
            return
        left, top, right, bottom = box
        self.box_value.set(f"{right - left} × {bottom - top} px  •  position {left}, {top}")

    def set_last_text(self, text: str) -> None:
        """Compatibility helper for showing an uncorrected OCR result."""
        self.set_last_result(CorrectionResult(text, text, (), 0.0))

    def set_last_result(self, result: CorrectionResult) -> None:
        """Show corrected text while retaining raw OCR and the change trace."""
        self._last_result = result
        text = result.corrected_text
        clean = text.strip() if text else ""
        self.captured_text.delete("1.0", "end")
        self.captured_text.insert("1.0", clean or "No readable text was found in this area.")
        if clean:
            lines = len(clean.splitlines())
            changes = len(result.corrections)
            suffix = f" • {changes} correction{'s' if changes != 1 else ''}" if changes else " • unchanged"
            self.capture_meta.set(f"Latest result • {len(clean)} characters • {lines} line{'s' if lines != 1 else ''}{suffix}")
            self.set_read_again_enabled(True)
        else:
            self.capture_meta.set("Latest result • no readable text")
        if hasattr(self, "details_button"):
            self.details_button.configure(state="normal" if result.raw_text else "disabled")

    def show_correction_details(self) -> None:
        """Open a readable raw/corrected/change view for the most recent capture."""
        result = self._last_result
        if result is None or not result.raw_text:
            self.set_status("There is no OCR result to inspect.", error=True)
            return
        window = tk.Toplevel(self.root)
        window.title("OCR correction details")
        window.geometry("760x620")
        window.minsize(580, 440)
        window.transient(self.root)
        _theme_dialog_window(window, self._palette)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text=f"{len(result.corrections)} change{'s' if len(result.corrections) != 1 else ''} • {result.elapsed_ms:.1f} ms", style="CardHint.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        details = scrolledtext.ScrolledText(frame, wrap="word", font=("Segoe UI", 10), padx=12, pady=10)
        details.grid(row=1, column=0, sticky="nsew")
        changes = "\n".join(
            f"• {change.original!r} → {change.replacement!r}  ({change.reason}, {change.confidence:.0%})"
            for change in result.corrections
        ) or "• No changes were made."
        details.insert("1.0", f"RAW OCR\n{result.raw_text}\n\nCORRECTED\n{result.corrected_text}\n\nCHANGES\n{changes}")
        details.configure(state="disabled")
        palette = self._palette
        details.configure(
            bg=palette.input,
            fg=palette.text,
            selectbackground=palette.selection,
            selectforeground=palette.text,
            highlightbackground=palette.border,
            highlightcolor=palette.accent,
        )
        ttk.Button(frame, text="Close", command=window.destroy).grid(row=2, column=0, sticky="e", pady=(10, 0))

    def clear_text(self) -> None:
        self.on_clear_text()
        self._last_result = None
        self.captured_text.delete("1.0", "end")
        self.capture_meta.set("Nothing captured yet")
        if hasattr(self, "details_button"):
            self.details_button.configure(state="disabled")
        self.set_read_again_enabled(False)
        self.set_status("Captured text cleared.")

    def set_read_again_enabled(self, enabled: bool) -> None:
        if hasattr(self, "read_again_button"):
            self.read_again_button.configure(state="normal" if enabled else "disabled")

    def close(self) -> None:
        """Flush debounced settings before the native window is destroyed."""
        if hasattr(self, "window_state"):
            self.window_state.close()

    def copy_text(self) -> None:
        """Copy the currently displayed OCR text to the standard clipboard."""
        text = self.captured_text.get("1.0", "end-1c").strip()
        if not text:
            self.set_status("There is no captured text to copy.", error=True)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self.set_status("Captured text copied to the clipboard.")

    def set_status(self, message: str, error: bool = False) -> None:
        """Show an actionable status message with a visible error state."""
        self.status_value.set(message)
        if hasattr(self, "status_label"):
            self.status_label.configure(style="StatusError.TLabel" if error else "Status.TLabel")

    def set_hotkey_status(self, active: bool, fixed: str = "", snippet: str = "", read_again: str = "") -> None:
        """Update the shortcut health badge after registration changes."""
        if active:
            labels = "  /  ".join(item for item in (fixed, snippet, read_again) if item)
            self.hotkey_status.set(f"Shortcuts ready  •  {labels}")
            if hasattr(self, "hotkey_badge"):
                self.hotkey_badge.configure(style="HotkeysOn.TLabel")
            self.set_status("Global shortcuts are active.")
        else:
            self.hotkey_status.set("Shortcuts inactive")
            if hasattr(self, "hotkey_badge"):
                self.hotkey_badge.configure(style="HotkeysOff.TLabel")
