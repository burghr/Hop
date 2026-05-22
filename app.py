#!/usr/bin/env python3
"""Hop — a keyboard-driven address bar for macOS Finder.

Clicking 🐇 in the menu bar drops down a floating panel pre-filled with the
current Finder window's path. Enter navigates, Escape dismisses, Tab
completes. Multiple Tab matches appear as a clickable list, followed by
your starred favorites and recent history. Click the star next to any path
to add or remove it from favorites.

A custom NSPanel is used instead of NSMenu because NSMenu's tracking loop
captures keyboard events once items are visible, preventing the embedded
text field from ever seeing Tab/arrow keys.
"""

import glob
import json
import os
import subprocess
from pathlib import Path

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSEvent,
    NSEventMaskLeftMouseDown,
    NSEventMaskRightMouseDown,
    NSEventModifierFlagCommand,
    NSFont,
    NSMakeRect,
    NSObject,
    NSPanel,
    NSRectFill,
    NSStatusBar,
    NSStatusWindowLevel,
    NSTextField,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from PyObjCTools import AppHelper


# ── Storage ───────────────────────────────────────────────────────────────────

DATA_DIR = Path.home() / 'Library' / 'Application Support' / 'Hop'
DATA_FILE = DATA_DIR / 'data.json'


def load_data():
    try:
        with open(DATA_FILE) as f:
            d = json.load(f)
        return {
            'favorites': list(d.get('favorites', [])),
            'history': list(d.get('history', [])),
        }
    except Exception:
        return {'favorites': [], 'history': []}


def save_data(data):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


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
MAX_HISTORY = 12
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

        # Star toggle (left)
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

        # Path label (right of star)
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
        self._is_fav = False
        self._panel = None
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
        self._is_fav = bool(is_fav)
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
        self._data = {'favorites': [], 'history': []}
        return self

    # ── Public ──────────────────────────────────────────────────────────────

    def loadAndRender(self):
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
        # No selection? Default to first completion if there is one.
        if self._sel < 0:
            if self._n_comp > 0:
                self._accept_completion(self._completion_rows[0]._path)
            return
        row = self._selectable[self._sel]
        # If selected row is a tab completion, do the accept-and-recomplete dance.
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

    def _render_sections(self):
        """Rebuild selectable list, populate fav/hist rows, then relayout."""
        self._selectable = []
        self._sel = -1

        favset = set(self._data['favorites'])

        # Completions (already populated by _show_completions)
        for i in range(self._n_comp):
            self._selectable.append(self._completion_rows[i])

        # Favorites
        favs = self._data['favorites'][:MAX_FAVORITES]
        self._n_fav = len(favs)
        for i, row in enumerate(self._favorite_rows):
            if i < self._n_fav:
                row.setPath_(favs[i])
                row.setFavorite_(True)
                row.setSelected_(False)
                self._selectable.append(row)

        # History (excluding paths already shown as favorites)
        hist = [p for p in self._data['history'] if p not in favset][:MAX_HISTORY]
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
            del favs[MAX_FAVORITES * 3:]
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

        # Completions
        for i, row in enumerate(self._completion_rows):
            if i < self._n_comp:
                y -= ROW_H
                row.setFrame_(NSMakeRect(0, y, PANEL_W, ROW_H))
                row.setHidden_(False)
            else:
                row.setHidden_(True)

        # Favorites
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

        # History
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


# ── App delegate / panel controller ───────────────────────────────────────────

class _AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, notif):
        self._status = NSStatusBar.systemStatusBar().statusItemWithLength_(-1)
        button = self._status.button()
        button.setTitle_('🐇')
        button.setTarget_(self)
        button.setAction_(b'togglePanel:')

        self._build_panel()

        self._mouse_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown,
            self._on_outside_click,
        )

    def _on_outside_click(self, event):
        if self._panel.isVisible():
            self._panel.orderOut_(None)

    def togglePanel_(self, sender):
        if self._panel.isVisible():
            self._panel.orderOut_(None)
        else:
            self._show_panel()

    def _show_panel(self):
        # Position the panel first with a minimal height; _render_sections will
        # resize it, anchored to the top edge.
        button = self._status.button()
        btn_frame = button.window().convertRectToScreen_(button.frame())
        top_y = btn_frame.origin.y - TOP_GAP
        center_x = btn_frame.origin.x + btn_frame.size.width / 2
        x = center_x - PANEL_W / 2
        minimal_h = PADDING + TF_H + PADDING
        self._panel.setFrame_display_(
            NSMakeRect(x, top_y - minimal_h, PANEL_W, minimal_h), False
        )

        # Reset transient state and pre-fill text field.
        self._tf_del._n_comp = 0
        self._err_label.setHidden_(True)
        self._tf.setStringValue_(get_finder_path() or str(Path.home()))
        self._tf_del._base_text = self._tf.stringValue()

        # Load data and lay out (resizes panel, keeps top anchored).
        self._tf_del.loadAndRender()

        self._panel.makeKeyAndOrderFront_(None)
        self._panel.makeFirstResponder_(self._tf)
        self._tf.selectText_(None)

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
            row._panel = panel
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
