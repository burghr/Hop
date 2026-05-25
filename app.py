#!/usr/bin/env python3
"""Hop — a keyboard-driven address bar for macOS Finder.

Clicking 🐇 in the menu bar drops down a floating panel pre-filled with the
current Finder window's path. Enter navigates, Escape dismisses, Tab
completes. Multiple Tab matches appear as a clickable list, followed by
your starred favorites and recent history. Click the star next to any path
to add or remove it from favorites.

Right-click the menu bar icon for Settings (icon, max history, global
hotkey, start at login) and Quit.

A custom NSPanel is used instead of NSMenu because NSMenu's tracking loop
captures keyboard events once items are visible, preventing the embedded
text field from ever seeing Tab/arrow keys.
"""

import ctypes
import glob
import json
import os
import plistlib
import re
import subprocess
from ctypes import CFUNCTYPE, POINTER, Structure, byref, c_int, c_uint, c_void_p
from pathlib import Path

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSEvent,
    NSEventMaskKeyDown,
    NSEventMaskLeftMouseDown,
    NSEventMaskLeftMouseUp,
    NSEventMaskRightMouseDown,
    NSEventMaskRightMouseUp,
    NSEventModifierFlagCommand,
    NSEventModifierFlagControl,
    NSEventModifierFlagOption,
    NSEventModifierFlagShift,
    NSFont,
    NSImage,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSPanel,
    NSPopUpButton,
    NSRectFill,
    NSStatusBar,
    NSStatusWindowLevel,
    NSStepper,
    NSTextField,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindow,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskTitled,
)
from PyObjCTools import AppHelper


# ── Storage ───────────────────────────────────────────────────────────────────

DATA_DIR = Path.home() / 'Library' / 'Application Support' / 'Hop'
DATA_FILE = DATA_DIR / 'data.json'

ICON_BUNNY = '🐇'
ICON_FOLDER = '📁'
ICON_BUNNY_WHITE = 'sf:hare.fill'
ICON_BUNNY_OUTLINE = 'sf:hare'

ICONS = [
    (ICON_BUNNY,         f'{ICON_BUNNY}  Bunny'),
    (ICON_FOLDER,        f'{ICON_FOLDER}  Folder'),
    (ICON_BUNNY_WHITE,   '🐰  White Bunny'),
    (ICON_BUNNY_OUTLINE, '○  Outline Bunny'),
]

DEFAULT_MAX_HISTORY = 12

DEFAULT_SETTINGS = {
    'icon': ICON_BUNNY,
    'max_history': DEFAULT_MAX_HISTORY,
    'hotkey': None,  # {'modifiers': int, 'keycode': int, 'display': str} or None
}


def load_data():
    default = {
        'favorites': [],
        'history': [],
        'settings': dict(DEFAULT_SETTINGS),
    }
    try:
        with open(DATA_FILE) as f:
            d = json.load(f)
    except Exception:
        return default
    result = {
        'favorites': list(d.get('favorites', [])),
        'history': list(d.get('history', [])),
    }
    s = dict(DEFAULT_SETTINGS)
    s.update(d.get('settings') or {})
    result['settings'] = s
    return result


def save_data(data):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ── Login agent (Start at login) ──────────────────────────────────────────────

LAUNCH_AGENT_LABEL = 'com.burghr.hop'
LAUNCH_AGENT_PATH = (
    Path.home() / 'Library' / 'LaunchAgents' / f'{LAUNCH_AGENT_LABEL}.plist'
)
PROJECT_DIR = Path(__file__).resolve().parent


def login_enabled() -> bool:
    return LAUNCH_AGENT_PATH.exists()


def set_login_enabled(enabled: bool) -> None:
    if enabled:
        plist = {
            'Label': LAUNCH_AGENT_LABEL,
            'ProgramArguments': [str(PROJECT_DIR / 'run.sh')],
            'WorkingDirectory': str(PROJECT_DIR),
            'RunAtLoad': True,
            'StandardOutPath': '/tmp/hop.out.log',
            'StandardErrorPath': '/tmp/hop.err.log',
        }
        LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LAUNCH_AGENT_PATH, 'wb') as f:
            plistlib.dump(plist, f)
        subprocess.run(
            ['launchctl', 'unload', str(LAUNCH_AGENT_PATH)],
            capture_output=True,
        )
        subprocess.run(
            ['launchctl', 'load', str(LAUNCH_AGENT_PATH)],
            capture_output=True,
        )
    else:
        if LAUNCH_AGENT_PATH.exists():
            subprocess.run(
                ['launchctl', 'unload', str(LAUNCH_AGENT_PATH)],
                capture_output=True,
            )
            LAUNCH_AGENT_PATH.unlink()


# ── Global hotkey (Carbon RegisterEventHotKey via ctypes) ────────────────────
#
# Carbon hotkeys are intercepted system-wide AND don't require Input Monitoring
# permission, unlike NSEvent.addGlobalMonitor. The cost is a small ctypes
# binding layer to Carbon.framework.

_carbon = ctypes.cdll.LoadLibrary(
    '/System/Library/Frameworks/Carbon.framework/Carbon'
)


def _fourcc(s: str) -> int:
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


_kEventClassKeyboard = _fourcc('keyb')
_kEventHotKeyPressed = 5

_CARBON_CMD = 256
_CARBON_SHIFT = 512
_CARBON_OPT = 2048
_CARBON_CTRL = 4096


class _EventTypeSpec(Structure):
    _fields_ = [('eventClass', c_uint), ('eventKind', c_uint)]


class _EventHotKeyID(Structure):
    _fields_ = [('signature', c_uint), ('id', c_uint)]


_HotKeyHandlerProc = CFUNCTYPE(c_int, c_void_p, c_void_p, c_void_p)

_carbon.GetApplicationEventTarget.restype = c_void_p
_carbon.GetApplicationEventTarget.argtypes = []

_carbon.InstallEventHandler.argtypes = [
    c_void_p, _HotKeyHandlerProc, c_uint,
    POINTER(_EventTypeSpec), c_void_p, POINTER(c_void_p),
]
_carbon.InstallEventHandler.restype = c_int

_carbon.RegisterEventHotKey.argtypes = [
    c_uint, c_uint, _EventHotKeyID, c_void_p, c_uint, POINTER(c_void_p),
]
_carbon.RegisterEventHotKey.restype = c_int

_carbon.UnregisterEventHotKey.argtypes = [c_void_p]
_carbon.UnregisterEventHotKey.restype = c_int


def _ns_to_carbon_mods(ns_mods: int) -> int:
    cmods = 0
    if ns_mods & NSEventModifierFlagCommand:
        cmods |= _CARBON_CMD
    if ns_mods & NSEventModifierFlagShift:
        cmods |= _CARBON_SHIFT
    if ns_mods & NSEventModifierFlagOption:
        cmods |= _CARBON_OPT
    if ns_mods & NSEventModifierFlagControl:
        cmods |= _CARBON_CTRL
    return cmods


class HotkeyManager:
    """Registers a single global hotkey via Carbon and fires a Python callback."""

    def __init__(self):
        self._hotkey_ref = None
        self._handler_installed = False
        self._callback = None
        # Keep strong reference to the C callback so it isn't GC'd.
        self._handler_proc = _HotKeyHandlerProc(self._handler)

    def _handler(self, call_ref, event, user_data):
        if self._callback:
            try:
                self._callback()
            except Exception:
                pass
        return 0  # noErr

    def _ensure_handler_installed(self):
        if self._handler_installed:
            return
        spec = _EventTypeSpec(_kEventClassKeyboard, _kEventHotKeyPressed)
        out_ref = c_void_p()
        target = _carbon.GetApplicationEventTarget()
        result = _carbon.InstallEventHandler(
            target, self._handler_proc, 1, byref(spec), None, byref(out_ref)
        )
        if result == 0:
            self._handler_installed = True

    def register(self, ns_modifiers: int, keycode: int, callback) -> bool:
        """Register the hotkey. Returns True on success."""
        self.unregister()
        carbon_mods = _ns_to_carbon_mods(ns_modifiers)
        if carbon_mods == 0:
            return False
        self._callback = callback
        self._ensure_handler_installed()
        hkid = _EventHotKeyID(_fourcc('hop1'), 1)
        out_ref = c_void_p()
        result = _carbon.RegisterEventHotKey(
            keycode, carbon_mods, hkid,
            _carbon.GetApplicationEventTarget(), 0, byref(out_ref),
        )
        if result == 0:
            self._hotkey_ref = out_ref
            return True
        return False

    def unregister(self):
        if self._hotkey_ref is not None:
            _carbon.UnregisterEventHotKey(self._hotkey_ref)
            self._hotkey_ref = None
        self._callback = None


def format_hotkey(hk) -> str:
    if not hk:
        return 'None'
    mods = hk.get('modifiers', 0)
    parts = ''
    if mods & NSEventModifierFlagControl:
        parts += '⌃'
    if mods & NSEventModifierFlagOption:
        parts += '⌥'
    if mods & NSEventModifierFlagShift:
        parts += '⇧'
    if mods & NSEventModifierFlagCommand:
        parts += '⌘'
    key = hk.get('display', '') or ''
    return parts + key.upper()


# ── AppleScript helpers ───────────────────────────────────────────────────────

def get_finder_path() -> str:
    r = subprocess.run(
        ["osascript", "-e", """
            tell application "Finder"
                if (count of Finder windows) > 0 then
                    POSIX path of (target of front Finder window as alias)
                else
                    return ""
                end if
            end tell
        """],
        capture_output=True, text=True, timeout=5,
    )
    return r.stdout.strip()


_URL_SCHEME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://')


def is_url(text: str) -> bool:
    return bool(_URL_SCHEME_RE.match(text))


def open_url(url: str) -> None:
    """Hand a URL (smb://, afp://, ftp://, etc.) to macOS to mount and open."""
    subprocess.run(['open', url], timeout=10)


def navigate_finder(path: str) -> None:
    escaped = path.replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f"""
            tell application "Finder"
                set dest to POSIX file "{escaped}" as alias
                if (count of Finder windows) > 0 then
                    set target of front Finder window to dest
                else
                    open dest
                end if
                activate
            end tell
        """],
        timeout=5,
    )


def tab_complete(text: str):
    """Return (common_prefix, [all_dir_matches_with_trailing_slash])."""
    expanded = os.path.expanduser(text)
    try:
        matches = sorted(m for m in glob.glob(expanded + '*') if os.path.isdir(m))
    except Exception:
        return text, []
    if not matches:
        return text, []
    display = [m.rstrip('/') + '/' for m in matches]
    if len(matches) == 1:
        return display[0], display
    return os.path.commonprefix(matches), display


# ── Layout constants ──────────────────────────────────────────────────────────

PANEL_W = 360
TF_H = 24
ERR_H = 18
ROW_H = 22
HEADER_H = 18
PADDING = 8
SECTION_GAP = 6
TOP_GAP = 4
MAX_COMPLETIONS = 10
MAX_FAVORITES = 8
MAX_FAVORITES_STORED = 24
MAX_HISTORY = 12  # used as a hard upper bound on what we'll ever display
MAX_HISTORY_STORED = 50


# ── Section header view ───────────────────────────────────────────────────────

class _SectionHeader(NSView):

    def initWithFrame_(self, frame):
        self = objc.super(_SectionHeader, self).initWithFrame_(frame)
        if self is None:
            return None
        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, 0, frame.size.width - 16, frame.size.height)
        )
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.systemFontOfSize_(10))
        label.setTextColor_(NSColor.secondaryLabelColor())
        self.addSubview_(label)
        self._label = label
        return self

    def setText_(self, text):
        self._label.setStringValue_(text.upper())


# ── Row view (used for completions, favorites, and history) ──────────────────

class _CompletionRow(NSView):

    def initWithFrame_(self, frame):
        self = objc.super(_CompletionRow, self).initWithFrame_(frame)
        if self is None:
            return None

        star = NSButton.alloc().initWithFrame_(
            NSMakeRect(2, 1, 22, frame.size.height - 2)
        )
        star.setBordered_(False)
        star.setTitle_('☆')
        star.setFont_(NSFont.systemFontOfSize_(14))
        star.setTarget_(self)
        star.setAction_(b'starClicked:')
        star.setFocusRingType_(1)  # NSFocusRingTypeNone
        self.addSubview_(star)
        self._star = star

        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(26, 1, frame.size.width - 30, frame.size.height - 2)
        )
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.systemFontOfSize_(13))
        cell = label.cell()
        cell.setLineBreakMode_(6)  # NSLineBreakByTruncatingHead
        cell.setTruncatesLastVisibleLine_(True)
        self.addSubview_(label)
        self._label = label

        self._path = None
        self._selected = False
        self._tf_del = None

        opts = (
            NSTrackingMouseEnteredAndExited
            | NSTrackingActiveAlways
            | NSTrackingInVisibleRect
        )
        ta = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), opts, self, None
        )
        self.addTrackingArea_(ta)
        return self

    def setPath_(self, path):
        self._path = path
        self._label.setStringValue_(path or "")

    def setFavorite_(self, is_fav):
        self._star.setTitle_('★' if is_fav else '☆')

    def setSelected_(self, sel):
        if self._selected == sel:
            return
        self._selected = sel
        self._label.setTextColor_(
            NSColor.alternateSelectedControlTextColor() if sel
            else NSColor.controlTextColor()
        )
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        if self._selected:
            NSColor.alternateSelectedControlColor().set()
            NSRectFill(self.bounds())

    def mouseEntered_(self, event):
        if self._tf_del and self._path:
            self._tf_del._hover_select_row(self)

    def mouseUp_(self, event):
        if self._tf_del and self._path:
            self._tf_del._navigate_to(self._path)

    def starClicked_(self, sender):
        if self._tf_del and self._path:
            self._tf_del._toggle_favorite(self._path)


# ── Panel ─────────────────────────────────────────────────────────────────────

class _Panel(NSPanel):

    def canBecomeKeyWindow(self):
        return True

    def keyDown_(self, event):
        if event.keyCode() == 53:  # Esc
            self.orderOut_(None)
            return
        mods = event.modifierFlags() & NSEventModifierFlagCommand
        if mods and event.charactersIgnoringModifiers() == 'q':
            NSApp.terminate_(None)
            return
        objc.super(_Panel, self).keyDown_(event)


# ── Text field delegate ───────────────────────────────────────────────────────

class _TFDelegate(NSObject):

    def init(self):
        self = objc.super(_TFDelegate, self).init()
        if self is None:
            return None
        self._tf = None
        self._err_label = None
        self._panel = None
        self._fav_header = None
        self._hist_header = None
        self._completion_rows = []
        self._favorite_rows = []
        self._history_rows = []
        self._n_comp = 0
        self._n_fav = 0
        self._n_hist = 0
        self._selectable = []
        self._sel = -1
        self._base_text = ""
        self._programmatic = False
        self._data = {'favorites': [], 'history': [], 'settings': dict(DEFAULT_SETTINGS)}
        return self

    # ── Public ──────────────────────────────────────────────────────────────

    def _load_and_render(self):
        self._n_comp = 0  # drop any in-progress completions from a previous open
        self._data = load_data()
        self._render_sections()

    # ── Command selectors ───────────────────────────────────────────────────

    @objc.typedSelector(b'B@:@@:')
    def control_textView_doCommandBySelector_(self, control, textView, cmd):
        sel = cmd.decode() if isinstance(cmd, bytes) else str(cmd)
        if sel == 'cancelOperation:':
            self._clear_completions()
            self._panel.orderOut_(None)
            return True
        if sel == 'insertNewline:':
            self._do_navigate()
            return True
        if sel == 'insertTab:':
            if self._n_comp > 0 or self._sel >= 0:
                self._accept_or_navigate()
            else:
                self._do_tab_complete()
            return True
        if sel == 'moveRight:':
            rng = textView.selectedRange()
            at_end = rng.location + rng.length >= len(self._tf.stringValue())
            if at_end and (self._n_comp > 0 or self._sel >= 0):
                self._accept_or_navigate()
                return True
        if sel == 'moveDown:' and self._selectable:
            self._move_sel(+1)
            return True
        if sel == 'moveUp:' and self._selectable:
            self._move_sel(-1)
            return True
        return False

    def controlTextDidChange_(self, notification):
        if not self._programmatic:
            self._base_text = self._tf.stringValue()
            self._clear_completions()

    # ── Navigation ──────────────────────────────────────────────────────────

    def _do_navigate(self):
        raw = self._tf.stringValue()
        if is_url(raw):
            self._err_label.setHidden_(True)
            self._navigate_to(raw)
            return
        try:
            path = Path(raw).expanduser().resolve()
        except Exception:
            self._show_error(f'Bad path: {raw}')
            return
        if not path.exists():
            self._show_error(f'Path does not exist: {raw}')
            return
        if not path.is_dir():
            self._show_error(f'Not a directory: {raw}')
            return
        self._err_label.setHidden_(True)
        self._navigate_to(str(path))

    def _show_error(self, msg):
        self._err_label.setStringValue_(msg)
        self._err_label.setHidden_(False)
        self._relayout()

    def _navigate_to(self, path):
        if is_url(path):
            norm = path.rstrip('/')  # URLs: drop trailing slash for dedup
            self._record_history(norm)
            open_url(norm)
        else:
            norm = path.rstrip('/') + '/'
            self._record_history(norm)
            navigate_finder(path)
        self._panel.orderOut_(None)

    def _record_history(self, path):
        h = self._data['history']
        if path in h:
            h.remove(path)
        h.insert(0, path)
        del h[MAX_HISTORY_STORED:]
        save_data(self._data)

    # ── Tab complete / accept ───────────────────────────────────────────────

    def _set_value(self, text):
        self._programmatic = True
        self._tf.setStringValue_(text)
        self._programmatic = False
        win = self._tf.window()
        if win:
            win.makeFirstResponder_(self._tf)
        editor = self._tf.currentEditor()
        if editor:
            editor.moveToEndOfDocument_(None)

    def _do_tab_complete(self):
        current = self._tf.stringValue()
        common, matches = tab_complete(current)
        if not matches:
            return
        if common != current:
            self._set_value(common)
        self._base_text = common
        if len(matches) > 1:
            self._show_completions(matches)

    def _accept_or_navigate(self):
        if self._sel < 0:
            if self._n_comp > 0:
                self._accept_completion(self._completion_rows[0]._path)
            return
        row = self._selectable[self._sel]
        if row in self._completion_rows[:self._n_comp]:
            self._accept_completion(row._path)
        else:
            self._navigate_to(row._path)

    def _accept_completion(self, path):
        if not path:
            return
        self._set_value(path)
        self._base_text = path
        self._n_comp = 0
        self._do_tab_complete()

    # ── Selection / hover ───────────────────────────────────────────────────

    def _move_sel(self, delta: int):
        n = len(self._selectable)
        if n == 0:
            return
        new = self._sel + delta
        new = max(-1, min(n - 1, new))
        if new == self._sel:
            return
        self._sel = new
        self._refresh_selection()
        text = self._base_text if new == -1 else (self._selectable[new]._path or "")
        self._set_value(text)

    def _hover_select_row(self, row):
        try:
            idx = self._selectable.index(row)
        except ValueError:
            return
        if idx == self._sel:
            return
        self._sel = idx
        self._refresh_selection()

    def _refresh_selection(self):
        for i, row in enumerate(self._selectable):
            row.setSelected_(i == self._sel)

    # ── Section rendering ───────────────────────────────────────────────────

    def _show_completions(self, paths):
        n = min(len(paths), MAX_COMPLETIONS)
        self._n_comp = n
        favset = set(self._data['favorites'])
        for i, row in enumerate(self._completion_rows):
            if i < n:
                row.setPath_(paths[i])
                row.setFavorite_(paths[i] in favset)
                row.setSelected_(False)
        self._render_sections()

    def _clear_completions(self):
        if self._n_comp == 0:
            if self._sel != -1:
                self._sel = -1
                self._refresh_selection()
            return
        self._n_comp = 0
        self._render_sections()

    def _max_history_setting(self) -> int:
        n = self._data['settings'].get('max_history', DEFAULT_MAX_HISTORY)
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = DEFAULT_MAX_HISTORY
        return max(0, min(MAX_HISTORY, n))

    def _render_sections(self):
        """Rebuild selectable list, populate fav/hist rows, then relayout."""
        self._selectable = []
        self._sel = -1

        favset = set(self._data['favorites'])

        for i in range(self._n_comp):
            self._selectable.append(self._completion_rows[i])

        favs = self._data['favorites'][:MAX_FAVORITES]
        self._n_fav = len(favs)
        for i, row in enumerate(self._favorite_rows):
            if i < self._n_fav:
                row.setPath_(favs[i])
                row.setFavorite_(True)
                row.setSelected_(False)
                self._selectable.append(row)

        max_hist = self._max_history_setting()
        hist = [p for p in self._data['history'] if p not in favset][:max_hist]
        self._n_hist = len(hist)
        for i, row in enumerate(self._history_rows):
            if i < self._n_hist:
                row.setPath_(hist[i])
                row.setFavorite_(False)
                row.setSelected_(False)
                self._selectable.append(row)

        self._relayout()

    def _toggle_favorite(self, path):
        favs = self._data['favorites']
        if path in favs:
            favs.remove(path)
        else:
            favs.insert(0, path)
            del favs[MAX_FAVORITES_STORED:]
        save_data(self._data)
        self._render_sections()

    # ── Layout ──────────────────────────────────────────────────────────────

    def _compute_height(self) -> int:
        h = PADDING + TF_H + PADDING
        if not self._err_label.isHidden():
            h += ERR_H + PADDING
        if self._n_comp > 0:
            h += self._n_comp * ROW_H
        if self._n_fav > 0:
            if self._n_comp > 0:
                h += SECTION_GAP
            h += HEADER_H + self._n_fav * ROW_H
        if self._n_hist > 0:
            if self._n_comp > 0 or self._n_fav > 0:
                h += SECTION_GAP
            h += HEADER_H + self._n_hist * ROW_H
        if self._n_comp > 0 or self._n_fav > 0 or self._n_hist > 0:
            h += PADDING
        return h

    def _layout_subviews(self, h: int):
        y = h - PADDING - TF_H
        self._tf.setFrame_(NSMakeRect(PADDING, y, PANEL_W - 2 * PADDING, TF_H))

        if not self._err_label.isHidden():
            y -= PADDING + ERR_H
            self._err_label.setFrame_(
                NSMakeRect(PADDING, y, PANEL_W - 2 * PADDING, ERR_H)
            )

        any_section = self._n_comp > 0 or self._n_fav > 0 or self._n_hist > 0
        if any_section:
            y -= PADDING

        for i, row in enumerate(self._completion_rows):
            if i < self._n_comp:
                y -= ROW_H
                row.setFrame_(NSMakeRect(0, y, PANEL_W, ROW_H))
                row.setHidden_(False)
            else:
                row.setHidden_(True)

        if self._n_fav > 0:
            if self._n_comp > 0:
                y -= SECTION_GAP
            y -= HEADER_H
            self._fav_header.setFrame_(NSMakeRect(0, y, PANEL_W, HEADER_H))
            self._fav_header.setText_('Favorites')
            self._fav_header.setHidden_(False)
        else:
            self._fav_header.setHidden_(True)
        for i, row in enumerate(self._favorite_rows):
            if i < self._n_fav:
                y -= ROW_H
                row.setFrame_(NSMakeRect(0, y, PANEL_W, ROW_H))
                row.setHidden_(False)
            else:
                row.setHidden_(True)

        if self._n_hist > 0:
            if self._n_comp > 0 or self._n_fav > 0:
                y -= SECTION_GAP
            y -= HEADER_H
            self._hist_header.setFrame_(NSMakeRect(0, y, PANEL_W, HEADER_H))
            self._hist_header.setText_('History')
            self._hist_header.setHidden_(False)
        else:
            self._hist_header.setHidden_(True)
        for i, row in enumerate(self._history_rows):
            if i < self._n_hist:
                y -= ROW_H
                row.setFrame_(NSMakeRect(0, y, PANEL_W, ROW_H))
                row.setHidden_(False)
            else:
                row.setHidden_(True)

    def _relayout(self):
        new_h = self._compute_height()
        frame = self._panel.frame()
        top_y = frame.origin.y + frame.size.height
        new_y = top_y - new_h
        self._panel.setFrame_display_(
            NSMakeRect(frame.origin.x, new_y, PANEL_W, new_h), True
        )
        self._layout_subviews(new_h)


# ── Settings controller / window ──────────────────────────────────────────────

SETTINGS_W = 420
SETTINGS_H = 240


class _SettingsController(NSObject):

    def init(self):
        self = objc.super(_SettingsController, self).init()
        if self is None:
            return None
        self._app = None  # _AppDelegate (set externally)
        self._window = None
        self._icon_popup = None
        self._hist_field = None
        self._hist_stepper = None
        self._hotkey_label = None
        self._record_btn = None
        self._login_checkbox = None
        self._recording = False
        self._record_monitor = None
        return self

    # ── Show ────────────────────────────────────────────────────────────────

    def show(self):
        if self._window is None:
            self._build_window()
        self._populate()
        self._window.center()
        self._window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    # ── Build ───────────────────────────────────────────────────────────────

    def _build_window(self):
        rect = NSMakeRect(0, 0, SETTINGS_W, SETTINGS_H)
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
        )
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        win.setTitle_('Hop Settings')
        win.setReleasedWhenClosed_(False)
        content = win.contentView()

        label_x = 20
        label_w = 140
        ctrl_x = 170
        row_h = 28
        # Layout from the top of the content view downward
        top = SETTINGS_H - 30

        def add_label(text, y, w=label_w):
            lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(label_x, y, w, 18))
            lbl.setStringValue_(text)
            lbl.setBezeled_(False)
            lbl.setDrawsBackground_(False)
            lbl.setEditable_(False)
            lbl.setSelectable_(False)
            content.addSubview_(lbl)
            return lbl

        # ── Row 1: Menu bar icon
        y = top
        add_label('Menu bar icon:', y + 4)
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(ctrl_x, y, 160, 26), False
        )
        for _, label in ICONS:
            popup.addItemWithTitle_(label)
        popup.setTarget_(self)
        popup.setAction_(b'iconChanged:')
        content.addSubview_(popup)
        self._icon_popup = popup

        # ── Row 2: Max history
        y -= row_h + 6
        add_label('Max history entries:', y + 4)
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(ctrl_x, y, 60, 22))
        field.setAlignment_(1)  # right
        field.setTarget_(self)
        field.setAction_(b'histFieldChanged:')
        content.addSubview_(field)
        self._hist_field = field
        stepper = NSStepper.alloc().initWithFrame_(NSMakeRect(ctrl_x + 64, y, 19, 22))
        stepper.setMinValue_(0)
        stepper.setMaxValue_(MAX_HISTORY)
        stepper.setIncrement_(1)
        stepper.setValueWraps_(False)
        stepper.setTarget_(self)
        stepper.setAction_(b'histStepperChanged:')
        content.addSubview_(stepper)
        self._hist_stepper = stepper

        # ── Row 3: Global hotkey
        y -= row_h + 6
        add_label('Global hotkey:', y + 4)
        hk_label = NSTextField.alloc().initWithFrame_(NSMakeRect(ctrl_x, y + 4, 80, 18))
        hk_label.setBezeled_(False)
        hk_label.setDrawsBackground_(False)
        hk_label.setEditable_(False)
        hk_label.setSelectable_(False)
        hk_label.setFont_(NSFont.systemFontOfSize_(14))
        content.addSubview_(hk_label)
        self._hotkey_label = hk_label
        record = NSButton.alloc().initWithFrame_(NSMakeRect(ctrl_x + 84, y, 80, 24))
        record.setBezelStyle_(1)
        record.setTitle_('Record')
        record.setTarget_(self)
        record.setAction_(b'recordHotkey:')
        content.addSubview_(record)
        self._record_btn = record
        clear = NSButton.alloc().initWithFrame_(NSMakeRect(ctrl_x + 168, y, 60, 24))
        clear.setBezelStyle_(1)
        clear.setTitle_('Clear')
        clear.setTarget_(self)
        clear.setAction_(b'clearHotkey:')
        content.addSubview_(clear)

        # ── Row 4: Start at login
        y -= row_h + 6
        cb = NSButton.alloc().initWithFrame_(NSMakeRect(label_x, y, 240, 22))
        cb.setButtonType_(3)  # NSSwitchButton
        cb.setTitle_('Start Hop at login')
        cb.setTarget_(self)
        cb.setAction_(b'loginChanged:')
        content.addSubview_(cb)
        self._login_checkbox = cb

        # ── Footer info
        info = NSTextField.alloc().initWithFrame_(
            NSMakeRect(label_x, 16, SETTINGS_W - 40, 30)
        )
        info.setStringValue_(
            'Tip: press Esc to cancel hotkey recording. The hotkey is '
            'consumed system-wide, so pick a combo not used elsewhere.'
        )
        info.setBezeled_(False)
        info.setDrawsBackground_(False)
        info.setEditable_(False)
        info.setSelectable_(False)
        info.setFont_(NSFont.systemFontOfSize_(10))
        info.setTextColor_(NSColor.secondaryLabelColor())
        info.cell().setWraps_(True)
        content.addSubview_(info)

        self._window = win

    # ── Populate from current settings ──────────────────────────────────────

    def _populate(self):
        s = self._app._tf_del._data['settings']
        icon = s.get('icon', ICON_BUNNY)
        icon_keys = [k for k, _ in ICONS]
        try:
            idx = icon_keys.index(icon)
        except ValueError:
            idx = 0
        self._icon_popup.selectItemAtIndex_(idx)
        n = int(s.get('max_history', DEFAULT_MAX_HISTORY) or 0)
        self._hist_field.setIntegerValue_(n)
        self._hist_stepper.setIntegerValue_(n)
        self._hotkey_label.setStringValue_(format_hotkey(s.get('hotkey')))
        self._login_checkbox.setState_(1 if login_enabled() else 0)

    # ── Actions ─────────────────────────────────────────────────────────────

    def iconChanged_(self, popup):
        idx = popup.indexOfSelectedItem()
        icon = ICONS[idx][0]
        s = self._app._tf_del._data['settings']
        s['icon'] = icon
        save_data(self._app._tf_del._data)
        self._app._apply_icon()

    def histFieldChanged_(self, field):
        n = field.integerValue()
        n = max(0, min(MAX_HISTORY, n))
        self._hist_field.setIntegerValue_(n)
        self._hist_stepper.setIntegerValue_(n)
        self._save_max_history(n)

    def histStepperChanged_(self, stepper):
        n = stepper.integerValue()
        self._hist_field.setIntegerValue_(n)
        self._save_max_history(n)

    def _save_max_history(self, n):
        self._app._tf_del._data['settings']['max_history'] = int(n)
        save_data(self._app._tf_del._data)

    def recordHotkey_(self, sender):
        if self._recording:
            return
        self._recording = True
        self._record_btn.setTitle_('Press…')
        self._record_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, self._on_record_key,
        )

    def _on_record_key(self, event):
        keycode = event.keyCode()
        if keycode == 53:  # Esc cancels recording
            self._stop_recording()
            return None
        mask = (
            NSEventModifierFlagCommand
            | NSEventModifierFlagShift
            | NSEventModifierFlagOption
            | NSEventModifierFlagControl
        )
        modifiers = int(event.modifierFlags() & mask)
        if modifiers == 0:
            return None  # require at least one modifier
        chars = event.charactersIgnoringModifiers() or ''
        hk = {
            'modifiers': modifiers,
            'keycode': int(keycode),
            'display': chars,
        }
        self._app._tf_del._data['settings']['hotkey'] = hk
        save_data(self._app._tf_del._data)
        self._hotkey_label.setStringValue_(format_hotkey(hk))
        self._stop_recording()
        self._app._apply_hotkey()
        return None  # swallow

    def _stop_recording(self):
        self._recording = False
        self._record_btn.setTitle_('Record')
        if self._record_monitor is not None:
            NSEvent.removeMonitor_(self._record_monitor)
            self._record_monitor = None

    def clearHotkey_(self, sender):
        self._app._tf_del._data['settings']['hotkey'] = None
        save_data(self._app._tf_del._data)
        self._hotkey_label.setStringValue_('None')
        self._app._apply_hotkey()

    def loginChanged_(self, sender):
        try:
            set_login_enabled(sender.state() == 1)
        except Exception:
            # Revert on failure
            sender.setState_(1 if login_enabled() else 0)


# ── App delegate / panel controller ───────────────────────────────────────────

NS_EVENT_TYPE_RIGHT_MOUSE_UP = 4


class _AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, notif):
        # Build the panel first so _tf_del exists. _build_panel initializes
        # _tf_del._data to defaults; we replace it with the loaded data below.
        self._build_panel()
        self._tf_del._data = load_data()

        self._status = NSStatusBar.systemStatusBar().statusItemWithLength_(-1)
        button = self._status.button()
        button.setTarget_(self)
        button.setAction_(b'statusClicked:')
        button.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
        self._status_button = button
        self._apply_icon()

        # Outside-click dismiss
        self._mouse_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown,
            self._on_outside_click,
        )

        # Hotkey
        self._hotkey = HotkeyManager()
        self._apply_hotkey()

        # Settings
        self._settings = None

    # ── Apply settings ──────────────────────────────────────────────────────

    def _apply_icon(self):
        icon = self._tf_del._data['settings'].get('icon', ICON_BUNNY)
        if icon.startswith('sf:'):
            symbol_name = icon[3:]
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol_name, None)
            if img:
                img.setTemplate_(True)
                self._status_button.setImage_(img)
                self._status_button.setTitle_('')
                return
            icon = ICON_BUNNY  # fallback if SF Symbol unavailable
        self._status_button.setImage_(None)
        self._status_button.setTitle_(icon)

    def _apply_hotkey(self):
        hk = self._tf_del._data['settings'].get('hotkey')
        if hk:
            self._hotkey.register(
                hk.get('modifiers', 0),
                hk.get('keycode', 0),
                self._toggle_via_hotkey,
            )
        else:
            self._hotkey.unregister()

    def _toggle_via_hotkey(self):
        if self._panel.isVisible():
            self._panel.orderOut_(None)
        else:
            self._show_panel()

    # ── Status item handlers ────────────────────────────────────────────────

    def statusClicked_(self, sender):
        event = NSApp.currentEvent()
        if event is not None:
            is_right = event.type() == NS_EVENT_TYPE_RIGHT_MOUSE_UP
            is_ctrl = bool(event.modifierFlags() & NSEventModifierFlagControl)
            if is_right or is_ctrl:
                self._show_context_menu(event)
                return
        if self._panel.isVisible():
            self._panel.orderOut_(None)
        else:
            self._show_panel()

    def _show_context_menu(self, event):
        menu = NSMenu.alloc().init()
        settings_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Settings…', b'showSettings:', ''
        )
        settings_item.setTarget_(self)
        menu.addItem_(settings_item)
        menu.addItem_(NSMenuItem.separatorItem())
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Hop Off (Quit)', b'quitApp:', 'q'
        )
        quit_item.setTarget_(self)
        menu.addItem_(quit_item)
        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self._status_button)

    def quitApp_(self, sender):
        NSApp.terminate_(None)

    def showSettings_(self, sender):
        if self._settings is None:
            self._settings = _SettingsController.alloc().init()
            self._settings._app = self
        self._settings.show()

    def _on_outside_click(self, event):
        if self._panel.isVisible():
            self._panel.orderOut_(None)

    # ── Panel show ──────────────────────────────────────────────────────────

    def _show_panel(self):
        # Position the panel with a minimal height; _load_and_render resizes.
        button = self._status_button
        btn_frame = button.window().convertRectToScreen_(button.frame())
        top_y = btn_frame.origin.y - TOP_GAP
        center_x = btn_frame.origin.x + btn_frame.size.width / 2
        x = center_x - PANEL_W / 2
        minimal_h = PADDING + TF_H + PADDING
        self._panel.setFrame_display_(
            NSMakeRect(x, top_y - minimal_h, PANEL_W, minimal_h), False
        )

        self._err_label.setHidden_(True)
        path = get_finder_path() or str(Path.home())
        if not path.endswith('/'):
            path += '/'
        self._tf.setStringValue_(path)
        self._tf_del._base_text = self._tf.stringValue()
        self._tf_del._load_and_render()

        self._panel.makeKeyAndOrderFront_(None)
        self._panel.makeFirstResponder_(self._tf)
        editor = self._panel.fieldEditor_forObject_(True, self._tf)
        editor.setSelectedRange_((len(path), 0))

    # ── Panel build ─────────────────────────────────────────────────────────

    def _build_panel(self):
        initial_h = PADDING + TF_H + PADDING
        rect = NSMakeRect(0, 0, PANEL_W, initial_h)
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = _Panel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        panel.setLevel_(NSStatusWindowLevel)
        panel.setHasShadow_(True)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHidesOnDeactivate_(False)

        content = NSVisualEffectView.alloc().initWithFrame_(rect)
        content.setMaterial_(5)  # NSVisualEffectMaterialMenu
        content.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        content.setState_(NSVisualEffectStateActive)
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(8.0)
        content.layer().setMasksToBounds_(True)
        panel.setContentView_(content)

        tf = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PADDING, PADDING, PANEL_W - 2 * PADDING, TF_H)
        )
        tf.setFont_(NSFont.systemFontOfSize_(13))
        tf.setPlaceholderString_('/path/to/folder')
        content.addSubview_(tf)

        err = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PADDING, 0, PANEL_W - 2 * PADDING, ERR_H)
        )
        err.setBezeled_(False)
        err.setDrawsBackground_(False)
        err.setEditable_(False)
        err.setSelectable_(False)
        err.setTextColor_(NSColor.systemRedColor())
        err.setFont_(NSFont.systemFontOfSize_(11))
        err.setHidden_(True)
        content.addSubview_(err)

        def _make_row():
            row = _CompletionRow.alloc().initWithFrame_(
                NSMakeRect(0, 0, PANEL_W, ROW_H)
            )
            row.setHidden_(True)
            content.addSubview_(row)
            return row

        completion_rows = [_make_row() for _ in range(MAX_COMPLETIONS)]
        favorite_rows = [_make_row() for _ in range(MAX_FAVORITES)]
        history_rows = [_make_row() for _ in range(MAX_HISTORY)]

        fav_header = _SectionHeader.alloc().initWithFrame_(
            NSMakeRect(0, 0, PANEL_W, HEADER_H)
        )
        fav_header.setHidden_(True)
        content.addSubview_(fav_header)

        hist_header = _SectionHeader.alloc().initWithFrame_(
            NSMakeRect(0, 0, PANEL_W, HEADER_H)
        )
        hist_header.setHidden_(True)
        content.addSubview_(hist_header)

        tf_del = _TFDelegate.alloc().init()
        tf_del._tf = tf
        tf_del._err_label = err
        tf_del._panel = panel
        tf_del._fav_header = fav_header
        tf_del._hist_header = hist_header
        tf_del._completion_rows = completion_rows
        tf_del._favorite_rows = favorite_rows
        tf_del._history_rows = history_rows
        tf.setDelegate_(tf_del)

        for row in completion_rows + favorite_rows + history_rows:
            row._tf_del = tf_del

        self._panel = panel
        self._tf = tf
        self._err_label = err
        self._tf_del = tf_del


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # accessory — no Dock icon
    delegate = _AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()
