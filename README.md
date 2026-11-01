# dtop 💽

A **btop-inspired** terminal disk & SSD storage monitor with real-time I/O throughput, non-blocking space exploration, and native theme compatibility.

Written in **100% pure Python 3** using only standard library modules (`curses`, `threading`). Zero external pip dependencies required.

---

## ✨ Features

- **btop Aesthetics**: Beautiful rounded Unicode boxes, dual Used/Free visual gauges, and smooth color gradients.
- **Native btop Theme Support**: Automatically detects and loads your active `btop` theme from `~/.config/btop/btop.conf` or custom `.theme` files.
- **Smart Mount Filtering**: Intelligently deduplicates Btrfs subvolumes, loop devices, and virtual/tmpfs pseudo-filesystems (toggleable on the fly).
- **Real-time I/O & Throughput**: Displays live read/write speeds, IOPS, discard/TRIM activity, and sparkline activity graphs.
- **Interactive Directory Space Explorer**: Non-blocking background thread scanner for interactive folder drill-down without freezing the TUI.
- **Snapshot Mode (`--once` / `-1`)**: Instantly prints a styled storage overview directly to your terminal without launching the fullscreen curses interface.

---

## 🚀 Installation & Quick Start

### Direct Execution
Make `dtop.py` executable and link it to your local path:

```bash
chmod +x dtop.py
mkdir -p ~/.local/bin
ln -sf "$(pwd)/dtop.py" ~/.local/bin/dtop
```

Ensure `~/.local/bin` is in your `$PATH`.

### Clone & Run

```bash
git clone https://github.com/jmm701/dtop.git
cd dtop
./dtop.py
```

---

## ⌨️ Keybindings

| Key | Action |
|---|---|
| `Tab` | Switch active focus between **Disks** and **Directory Explorer** |
| `↑` / `k` | Navigate selection up |
| `↓` / `j` | Navigate selection down |
| `→` / `Enter` / `l` | Drill down into selected directory (or inspect selected mount) |
| `←` / `Backspace` / `h` | Go up to parent directory |
| `a` | Toggle all mounts (include virtual/tmpfs and duplicate subvolumes) |
| `s` | Scan the selected mount in the directory analyzer |
| `r` | Refresh disk metrics and force directory rescan |
| `+` / `=` | Decrease refresh interval (faster updates) |
| `-` / `_` | Increase refresh interval (slower updates) |
| `q` / `Esc` | Quit `dtop` |

---

## 🛠️ Command-Line Options

```text
usage: dtop.py [-h] [-a] [-1] [-t THEME] [path]

dtop - A btop-style terminal SSD & disk storage monitor.

positional arguments:
  path               Initial directory path to analyze in the space explorer (default: ~)

options:
  -h, --help         show this help message and exit
  -a, --all          Include all virtual/tmpfs and duplicate subvolume mounts
  -1, --once         Print a btop-style snapshot to the terminal and exit
  -t, --theme THEME  Path or name of custom btop .theme file (defaults to active btop theme)
```

### Examples

Launch analyzer on root filesystem:
```bash
dtop /
```

Print a one-time snapshot:
```bash
dtop --once
```

Load a specific theme:
```bash
dtop -t gruvbox_dark
```

---

## 📄 License

MIT License. Free and open source software.
