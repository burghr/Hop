# Hop 🐇

A keyboard-driven address bar for macOS Finder. Click the bunny in your menu
bar, type a path, hit Enter — Finder jumps there. Tab-completes paths,
remembers recent destinations, and lets you star any folder as a favorite.

## Why

Finder doesn't have an address bar. `Cmd-Shift-G` opens "Go to folder…" but
it's modal, slow, and forgets what you typed. Hop is always one click away,
pre-fills the current Finder window's path, and tab-completes like a shell.

## Install & run

```sh
./run.sh
```

First run creates a `.venv` and installs dependencies (`rumps`, `pyobjc`).
Subsequent runs just launch the app. It lives in the menu bar with no Dock
icon.

### Start at login

The easiest way: right-click 🐇 → **Settings…** → toggle **Start Hop at login**.
This installs a LaunchAgent at `~/Library/LaunchAgents/com.burghr.hop.plist`
that runs `./run.sh` at login. Stdout/stderr go to `/tmp/hop.out.log` and
`/tmp/hop.err.log`.

You can also run `./install.sh` and `./uninstall.sh` from the project
folder to do the same thing from the command line.

## How to use

**Left-click** 🐇 in the menu bar to open the panel. The text field is
pre-filled with the path of the frontmost Finder window (or `~` if no
Finder window is open).

**Right-click** 🐇 for **Settings…** and **Quit Hop**.

The panel always shows your **Favorites** and **History** sections beneath
the text field, so frequent folders are one click away. Tab-completions
appear above them when you're actively typing.

### Keys

| Key            | Action                                                       |
| -------------- | ------------------------------------------------------------ |
| `Enter`        | Navigate Finder to the typed path                            |
| `Tab`          | Tab-complete: fill in the common prefix, show matches below  |
| `Tab` (again)  | Accept the first / highlighted completion, then re-complete  |
| `→` (at end)   | Same as Tab — accept the selected/first row                  |
| `↑` / `↓`      | Move the keyboard selection through all rows (completions, favorites, history) |
| `Esc`          | Close the panel                                              |
| `Cmd-Q`        | Quit Hop                                                     |

When you select a favorite or history row with `↑/↓` and press `Tab`, `→`,
or `Enter`, Finder navigates to it and the panel closes.

### Mouse

- **Hover** any row to highlight it
- **Click the path** to navigate Finder and close the panel
- **Click the star** (☆ / ★) to add or remove that path from favorites
- **Click outside** the panel to dismiss it

### Favorites & History

- Every time you navigate to a folder (via Enter, click, or accept), it's
  added to your history (most recent first, up to 50 stored, 12 shown).
- Click the empty star next to any row to favorite it. Click a filled star
  to unfavorite. Favorites appear at the top, before history, and aren't
  duplicated in the history section.
- Data is stored at `~/Library/Application Support/Hop/data.json`. Safe to
  edit or delete by hand if you want to clear it.

### Tab-completion behavior

It works like shell tab-completion:

1. Type a partial path, e.g. `~/Doc`
2. Press `Tab` → expands to the common prefix (`~/Documents/`)
3. Press `Tab` again → if there are multiple subdirectories, they appear as
   a clickable list. The first one is the default; press `Tab` or `→` to
   accept it and dive deeper, or `↑/↓` to pick a different one.
4. Keep typing to narrow the list — typing clears the completion list, so
   you can refine your search at any time.

`~` is expanded to your home directory. Only directories are listed (not
files), because the goal is to navigate Finder.

## Settings

Right-click 🐇 → **Settings…** to configure:

- **Menu bar icon** — Bunny 🐇 or Folder 📁.
- **Max history entries** — how many history rows to show (0–12). Stored
  history isn't trimmed; this just changes the display cap.
- **Global hotkey** — click **Record**, then press the combo you want
  (must include at least one modifier: ⌘/⇧/⌥/⌃). Press the hotkey from
  anywhere to toggle Hop. Uses Carbon's `RegisterEventHotKey`, so the
  combo is consumed system-wide — pick something not already bound. Press
  **Esc** while recording to cancel.
- **Start Hop at login** — toggles the LaunchAgent.

Settings are saved to `~/Library/Application Support/Hop/data.json`.

## Quitting

Right-click 🐇 → **Quit Hop**, or press `Cmd-Q` while the panel is open.

## Implementation notes

Hop uses a custom borderless `NSPanel` instead of `NSMenu` for the dropdown.
`NSMenu`'s tracking loop captures all keyboard events once items become
visible, which makes Tab and arrow keys impossible to intercept from an
embedded text field. A panel sidesteps the problem entirely — it's a normal
window with normal event flow.
