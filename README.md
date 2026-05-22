# Hop 🐇

A keyboard-driven address bar for macOS Finder. Click the bunny in your menu
bar, type a path, hit Enter — Finder jumps there.

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

## How to use

Click 🐇 in the menu bar to open the panel. The text field is pre-filled
with the path of the frontmost Finder window (or `~` if no Finder window is
open).

### Keys

| Key            | Action                                                       |
| -------------- | ------------------------------------------------------------ |
| `Enter`        | Navigate Finder to the typed path                            |
| `Tab`          | Tab-complete: fill in the common prefix, show matches below  |
| `Tab` (again)  | Accept the first / highlighted completion, then re-complete  |
| `→` (at end)   | Same as Tab when completions are showing                     |
| `↑` / `↓`      | Move the keyboard selection through the completion list      |
| `Esc`          | Close the panel                                              |
| `Cmd-Q`        | Quit Hop                                                     |

### Mouse

- **Hover** a completion to highlight it (Tab will then accept that one)
- **Click** a completion to navigate Finder there immediately
- **Click outside** the panel to dismiss it

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

## Quitting

`Cmd-Q` while the panel is open, or kill the process from Activity Monitor.

## Implementation notes

Hop uses a custom borderless `NSPanel` instead of `NSMenu` for the dropdown.
`NSMenu`'s tracking loop captures all keyboard events once items become
visible, which makes Tab and arrow keys impossible to intercept from an
embedded text field. A panel sidesteps the problem entirely — it's a normal
window with normal event flow.
