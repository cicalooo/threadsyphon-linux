# threadsyphon-linux

**Watch 4chan threads. Save the media. Stay out of the way.**

A Linux-native port of [threadsyphon](https://github.com/cicalooo/threadsyphon-source) — multi-thread watcher with a clean GTK4 / libadwaita UI.

![GTK4](https://img.shields.io/badge/UI-GTK4%20%2B%20libadwaita-4a86cf)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)
![License](https://img.shields.io/badge/License-MIT-green)

---

## What it does

Paste a thread URL. Pick a folder (or accept the default). Hit **Add & watch**.

threadsyphon checks the thread on a schedule, downloads new images and videos, and keeps going until the thread dies or you pause it. Each thread has its own folder, interval, and worker.

Works as a desktop app or a headless CLI / systemd service.

---

## Install

### Arch Linux (recommended)

```bash
# dependencies
sudo pacman -S --needed python python-gobject gtk4 libadwaita libnotify

# clone
git clone https://github.com/cicalooo/threadsyphon-linux.git
cd threadsyphon-linux

# run from source
python main.py
```

Optional local package:

```bash
makepkg -si
threadsyphon
```

### Other distros

Install equivalent packages, then run from source the same way:

| Need | Debian/Ubuntu-ish | Fedora |
|------|-------------------|--------|
| Python GI | `python3-gi` | `python3-gobject` |
| GTK4 | `libgtk-4-1` / `gir1.2-gtk-4.0` | `gtk4` |
| libadwaita | `libadwaita-1-0` / `gir1.2-adw-1` | `libadwaita` |
| notifications | `gir1.2-notify-0.7` | `libnotify` |

```bash
python3 main.py
```

---

## Quick start

1. Launch the app: `python main.py` or `threadsyphon`
2. Paste a URL like `https://boards.4chan.org/g/thread/12345678`
3. Optionally set a save folder (default: `~/Downloads/threadsyphon/<board>/<thread>`)
4. Click **Add & watch**

That’s it. The list shows live status, file counts, and the next check time. Select a thread for details, log, and actions (pause, check now, open folder, gallery, remove).

---

## Features

**Watcher**
- Several threads at once, each with its own folder and schedule
- Resumes partial downloads when the CDN supports byte ranges
- Backs off politely on errors (with jitter)
- Survives app restarts (watch list is saved)

**Linux extras (v2)**
- Native GTK4 + libadwaita UI (follows system light/dark)
- MD5 checksum verification against the API
- Media filters: all / images only / video only
- Filename modes: original names, server names, or numbered
- Optional max file size + disk-space check before download
- Writes `gallery.html` and `thread.json` in each folder
- Desktop notifications when new files arrive or a thread finishes
- Optional clipboard watch (offer to add a copied thread URL)
- Tunable API and CDN rate gaps

**CLI & service**
```bash
threadsyphon              # GUI (default)
threadsyphon gui
threadsyphon add 'https://boards.4chan.org/g/thread/12345678'
threadsyphon list
threadsyphon watch        # headless — all auto-start threads
threadsyphon config       # print config path
```

Headless as a user service (after install):

```bash
systemctl --user enable --now threadsyphon.service
```

---


## Find threads & watchdog rules

### Find (manual)

In the app: **Find** (or `Ctrl+F`). Pick a board, type a filter, multi-select results → **Add selected**.

CLI:

```bash
threadsyphon find g 'title:linux min_images:5'
threadsyphon find wg 'body:wallpaper OR title:desktop'
```

### Filter syntax

| Example | Meaning |
|---------|---------|
| `linux nvidia` | both words in title or OP body |
| `title:"daily driver"` | phrase in subject |
| `body:arch` | word in OP comment |
| `id:12345678` | thread number |
| `min_images:10` | at least 10 images |
| `min_replies:50` | at least 50 replies |
| `sticky:false` | not sticky |
| `-title:meta` | exclude |
| `a OR b` | either side |

### Rules (watchdogs while the app is open)

**Rules** (`Ctrl+R`): add a board + query + scout interval (min 60s). While the window is open, enabled rules re-scan the catalog and auto-add new matches (deduped). Rules are saved in config; scouting stops when you quit (download workers keep their normal behavior).

```bash
threadsyphon rules add g 'min_images:20 title:general' --name 'fat generals' --interval 120
threadsyphon rules list
threadsyphon rules disable fat generals
```

## Where files live

| What | Path |
|------|------|
| Watch list & settings | `~/.config/threadsyphon/threads.json` |
| Default downloads | `~/Downloads/threadsyphon/<board>/<thread>/` |
| Per-folder manifest | `.threadsyphon.json` (inside the download folder) |
| Offline gallery | `gallery.html` + `thread.json` (same folder) |

No accounts. No telemetry. Only the HTTP requests needed to check a thread and fetch media.

---

## Preferences

Open **☰ → Preferences** for defaults that apply to new threads:

- Check interval
- Notifications / clipboard watch
- Default download root
- Filename mode & media filter
- Max file size, gallery writing, MD5 verify
- API gap / CDN gap (be nice to the servers)

---

## Tests

```bash
python -m unittest discover -s tests -v
```

Core tests use only local temp data and a local HTTP server — no network required.

---

## Project layout

```
threadsyphon-linux/
├── main.py                 # entry
├── threadsyphon/
│   ├── app.py              # GTK4 UI
│   ├── engine.py           # watchers + downloader
│   ├── models.py           # URLs + config models
│   ├── storage.py          # XDG settings
│   ├── gallery.py          # offline gallery
│   └── cli.py              # command line
├── data/                   # desktop file, icons, systemd unit
├── tests/
├── PKGBUILD                # Arch package recipe
└── LICENSE                 # MIT
```

---

## Related

- Upstream Windows app: [cicalooo/threadsyphon-source](https://github.com/cicalooo/threadsyphon-source)

## License

MIT — see [LICENSE](LICENSE).
