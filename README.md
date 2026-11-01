# dtop 💽

A **btop-inspired** terminal disk & SSD storage monitor with real-time I/O throughput and native theme compatibility.

Focused directly on your core storage: **Linux VM disk** and **Computer SSD**, with zero bloat.

Written in **100% pure Python 3** using only standard library modules (`curses`). Zero external pip dependencies required.

---

## ✨ Features

- **btop Aesthetics**: Beautiful rounded Unicode boxes, dual Used/Free visual gauges, and smooth color gradients.
- **Native btop Theme Support**: Automatically detects and loads your active `btop` theme from `~/.config/btop/btop.conf` or custom `.theme` files.
- **Focused Storage View**: Displays your **Linux VM disk (`/`)** and **Computer SSD (`/mnt/mac`)**, keeping the interface clean and clutter-free.
- **Smart Filtering**: Automatically hides virtual filesystem trees (`/dev`, `/proc`, `/sys`) and boot partitions.
- **Real-time I/O & Throughput**: Displays live read/write speeds, IOPS, discard/TRIM activity, and sparkline activity graphs.
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
| `↑` / `k` | Select previous disk |
| `↓` / `j` | Select next disk |
| `r` | Refresh disk and I/O metrics |
| `a` | Toggle all mounts |
| `+` / `=` | Decrease refresh interval (faster updates) |
| `-` / `_` | Increase refresh interval (slower updates) |
| `q` / `Esc` | Quit `dtop` |

---

## 🛠️ Command-Line Options

```text
usage: dtop.py [-h] [-a] [-1] [-t THEME]

dtop - A btop-style terminal SSD & disk storage monitor.

options:
  -h, --help         show this help message and exit
  -a, --all          Include all virtual/tmpfs and duplicate subvolume mounts
  -1, --once         Print a btop-style snapshot to the terminal and exit
  -t, --theme THEME  Path or name of custom btop .theme file (defaults to active btop theme)
```

### Examples

Launch live interactive monitor:
```bash
dtop
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
