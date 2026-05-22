#!/usr/bin/env python3
"""Hop — a keyboard-driven address bar for macOS Finder.

Clicking 🐇 in the menu bar drops down a floating panel pre-filled with the
current Finder window's path. Enter navigates, Escape dismisses, Tab
completes. Multiple Tab matches appear as a clickable list; clicking or
pressing Tab/Right on a hovered/selected entry navigates Finder directly.

A custom NSPanel is used instead of NSMenu because NSMenu's tracking loop
captures keyboard events once items are visible, preventing the embedded
text field from ever seeing Tab/arrow keys.
"""

import glob
import os
import subprocess
from pathlib import Path

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSBackingStoreBuffered,
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
PADDING = 8
TOP_GAP = 4  # gap between status item and panel top edge
MAX_COMPLETIONS = 12


# ── Completion row view ───────────────────────────────────────────────────────

class _CompletionRow(NSView):

    def initWithFrame_(self, frame):
        self = objc.super(_CompletionRow, self).initWithFrame_(frame)
        if self is None:
            return None
        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, 1, frame.size.width - 16, frame.size.height - 2)
        )
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.systemFontOfSize_(13))
        self.addSubview_(label)
        self._label = label
        self._path = None
        self._selected = False
        self._panel = None
        self._tf_del = None
        self._index = -1
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
        if self._tf_del and self._path and self._index >= 0:
            self._tf_del._hover_select(self._index)

    def mouseUp_(self, event):
        if self._path:
            navigate_finder(self._path)
        if self._panel:
            self._panel.orderOut_(None)


# ── Panel ─────────────────────────────────────────────────────────────────────

class _Panel(NSPanel):

    def canBecomeKeyWindow(self):
        return True

    def keyDown_(self, event):
        # Escape dismisses the panel even if focus has drifted off the field.
        if event.keyCode() == 53:
            self.orderOut_(None)
            return
        # Cmd-Q quits, since there's no menu bar item for it anymore.
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
        self._pool = []        # list[_CompletionRow]
        self._n_visible = 0
        self._sel = -1
        self._base_text = ""
        self._programmatic = False
        return self

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
            if self._n_visible:
                self._accept_completion()
            else:
                self._do_tab_complete()
            return True
        if sel == 'moveRight:' and self._n_visible:
            rng = textView.selectedRange()
            if rng.location + rng.length >= len(self._tf.stringValue()):
                self._accept_completion()
                return True
        if sel == 'moveDown:' and self._n_visible:
            self._move_sel(+1)
            return True
        if sel == 'moveUp:' and self._n_visible:
            self._move_sel(-1)
            return True
        return False

    def controlTextDidChange_(self, notification):
        if not self._programmatic:
            self._clear_completions()

    # ── private ──────────────────────────────────────────────────────────────

    def _do_navigate(self):
        raw = self._tf.stringValue()
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            self._err_label.setStringValue_(f'Path does not exist: {raw}')
            self._err_label.setHidden_(False)
            self._relayout()
            return
        if not path.is_dir():
            self._err_label.setStringValue_(f'Not a directory: {raw}')
            self._err_label.setHidden_(False)
            self._relayout()
            return
        self._clear_completions()
        self._err_label.setHidden_(True)
        navigate_finder(str(path))
        self._panel.orderOut_(None)

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

    def _accept_completion(self):
        if self._sel >= 0:
            path = self._pool[self._sel]._path
        elif self._n_visible > 0:
            path = self._pool[0]._path
        else:
            return
        if not path:
            return
        self._set_value(path)
        self._base_text = path
        self._clear_completions()
        self._do_tab_complete()

    def _move_sel(self, delta: int):
        new = self._sel + delta
        new = max(-1, min(self._n_visible - 1, new))
        if new == self._sel:
            return
        self._sel = new
        self._refresh_titles()
        text = self._base_text if new == -1 else (self._pool[new]._path or "")
        self._set_value(text)

    def _hover_select(self, index: int):
        if index == self._sel or index < 0 or index >= self._n_visible:
            return
        self._sel = index
        self._refresh_titles()

    def _refresh_titles(self):
        for i, row in enumerate(self._pool):
            row.setSelected_(i == self._sel and i < self._n_visible)

    def _show_completions(self, paths):
        n = min(len(paths), MAX_COMPLETIONS)
        self._n_visible = n
        self._sel = -1
        for i, row in enumerate(self._pool):
            if i < n:
                row.setPath_(paths[i])
                row.setSelected_(False)
        self._relayout()

    def _clear_completions(self):
        had = self._n_visible > 0
        self._n_visible = 0
        self._sel = -1
        for row in self._pool:
            row.setSelected_(False)
        if had:
            self._relayout()

    def _compute_height(self) -> int:
        h = PADDING + TF_H + PADDING
        if not self._err_label.isHidden():
            h += ERR_H + PADDING
        if self._n_visible > 0:
            h += self._n_visible * ROW_H + PADDING
        return h

    def _layout_subviews(self, h: int):
        # Top-down layout (NSView origin = bottom-left).
        y = h - PADDING - TF_H
        self._tf.setFrame_(NSMakeRect(PADDING, y, PANEL_W - 2 * PADDING, TF_H))

        if not self._err_label.isHidden():
            y -= PADDING + ERR_H
            self._err_label.setFrame_(
                NSMakeRect(PADDING, y, PANEL_W - 2 * PADDING, ERR_H)
            )

        if self._n_visible > 0:
            y -= PADDING
            for i in range(MAX_COMPLETIONS):
                row = self._pool[i]
                if i < self._n_visible:
                    y -= ROW_H
                    row.setFrame_(
                        NSMakeRect(PADDING, y, PANEL_W - 2 * PADDING, ROW_H)
                    )
                    row.setHidden_(False)
                else:
                    row.setHidden_(True)
        else:
            for row in self._pool:
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

        # Dismiss panel when user clicks anywhere outside our app.
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
        # Reset state
        self._tf_del._n_visible = 0
        self._tf_del._sel = -1
        for row in self._tf_del._pool:
            row.setSelected_(False)
            row.setHidden_(True)
        self._err_label.setHidden_(True)
        self._tf.setStringValue_(get_finder_path() or str(Path.home()))

        # Position panel: top edge `TOP_GAP` below status item, centered on it.
        button = self._status.button()
        btn_frame = button.window().convertRectToScreen_(button.frame())
        top_y = btn_frame.origin.y - TOP_GAP
        center_x = btn_frame.origin.x + btn_frame.size.width / 2

        h = self._tf_del._compute_height()
        x = center_x - PANEL_W / 2
        y = top_y - h
        self._panel.setFrame_display_(NSMakeRect(x, y, PANEL_W, h), False)
        self._tf_del._layout_subviews(h)

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

        # Vibrant menu-style background with rounded corners.
        content = NSVisualEffectView.alloc().initWithFrame_(rect)
        # Material 5 = NSVisualEffectMaterialMenu (the constant isn't always
        # exported by pyobjc; use the raw value).
        content.setMaterial_(5)
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

        pool = []
        for i in range(MAX_COMPLETIONS):
            row = _CompletionRow.alloc().initWithFrame_(
                NSMakeRect(PADDING, 0, PANEL_W - 2 * PADDING, ROW_H)
            )
            row._panel = panel
            row._index = i
            row.setHidden_(True)
            content.addSubview_(row)
            pool.append(row)

        tf_del = _TFDelegate.alloc().init()
        tf_del._tf = tf
        tf_del._err_label = err
        tf_del._panel = panel
        tf_del._pool = pool
        tf.setDelegate_(tf_del)
        for row in pool:
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
