#!/usr/bin/env python3
"""
dtop - A btop-inspired terminal disk & SSD storage monitor.

Features:
- Btop-style rounded Unicode boxes, color gradients, and dual Used/Free gauges
- Focused monitoring: Linux VM virtual disk and host Computer SSD
- Real-time disk I/O read/write throughput, IOPS, TRIM/discard stats & sparklines
- Snapshot mode (`--once`) for instant terminal output without entering fullscreen TUI
"""

from __future__ import annotations

import argparse
import curses
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

__version__ = "1.0.0"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_bytes(num_bytes: float, precision: int = 1) -> str:
    """Format byte count into compact human-readable string (GiB/MiB/KiB/TiB)."""
    if num_bytes < 0:
        num_bytes = 0
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    val = float(num_bytes)
    for unit in units:
        if abs(val) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(val)} B"
            return f"{val:.{precision}f} {unit}"
        val /= 1024.0
    return f"{val:.{precision}f} PiB"


def fmt_rate(bytes_per_sec: float) -> str:
    """Format transfer speed into compact human-readable string."""
    if bytes_per_sec < 1024:
        return f"{int(bytes_per_sec)} B/s"
    return f"{fmt_bytes(bytes_per_sec, 1)}/s"


BLOCKS = " ▏▎▍▌▋▊▉█"
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def make_bar(pct: float, width: int) -> str:
    """Build a smooth fractional horizontal block bar."""
    if width <= 0:
        return ""
    pct = max(0.0, min(100.0, pct))
    fill = (pct / 100.0) * width
    full_blocks = int(fill)
    rem = fill - full_blocks
    part_idx = int(rem * 8)
    bar = "█" * full_blocks
    if full_blocks < width:
        if part_idx > 0:
            bar += BLOCKS[part_idx]
            bar += "░" * (width - full_blocks - 1)
        else:
            bar += "░" * (width - full_blocks)
    return bar[:width]


def make_sparkline(values: List[float], width: int, max_val: Optional[float] = None) -> str:
    """Render a sparkline string of `width` characters from recent values."""
    if width <= 0:
        return ""
    if not values:
        return "▁" * width
    tail = list(values[-width:])
    if len(tail) < width:
        tail = [0.0] * (width - len(tail)) + tail
    peak = max_val if (max_val is not None and max_val > 0) else max(tail)
    if peak <= 0:
        return "▁" * width
    out = []
    for v in tail:
        ratio = max(0.0, min(1.0, v / peak))
        idx = min(len(SPARK_CHARS) - 1, int(ratio * (len(SPARK_CHARS) - 1) + 0.49))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


# ---------------------------------------------------------------------------
# Data models & collectors
# ---------------------------------------------------------------------------

VIRTUAL_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "pstore",
    "bpf", "autofs", "debugfs", "mqueue", "configfs", "fusectl", "hugetlbfs",
    "tracefs", "binfmt_misc", "securityfs", "efivarfs", "fuse.gvfsd-fuse",
    "fuse.portal", "nsfs", "ramfs", "squashfs", "overlay",
}


@dataclass
class MountDisk:
    device: str
    mountpoint: str
    fstype: str
    opts: str
    total: int
    used: int
    free: int
    avail: int
    used_pct: float
    free_pct: float
    inodes_total: int
    inodes_used: int
    inodes_pct: float
    is_ssd: bool
    is_virtual: bool
    is_subvol_dup: bool
    subvol: str = ""
    submounts: List[str] = field(default_factory=list)
    block_dev: str = ""

    @property
    def display_name(self) -> str:
        if self.mountpoint == "/":
            return "Linux VM (/)"
        elif self.mountpoint == "/mnt/mac" or self.fstype in ("9p", "virtiofs", "vboxsf"):
            return f"Computer SSD ({self.mountpoint})"
        return self.mountpoint


@dataclass
class DiskIOState:
    name: str
    reads_completed: int
    sectors_read: int
    read_time_ms: int
    writes_completed: int
    sectors_written: int
    write_time_ms: int
    io_in_progress: int
    io_time_ms: int
    discards_completed: int
    sectors_discarded: int
    timestamp: float


@dataclass
class DiskIOMetrics:
    name: str
    read_bps: float = 0.0
    write_bps: float = 0.0
    read_iops: float = 0.0
    write_iops: float = 0.0
    util_pct: float = 0.0
    total_read_bytes: int = 0
    total_write_bytes: int = 0
    total_discard_bytes: int = 0
    read_history: deque = field(default_factory=lambda: deque([0.0] * 60, maxlen=120))
    write_history: deque = field(default_factory=lambda: deque([0.0] * 60, maxlen=120))
    peak_read_bps: float = 1024.0 * 1024.0
    peak_write_bps: float = 1024.0 * 1024.0


@dataclass
class SwapInfo:
    device: str
    total: int
    used: int
    free: int
    used_pct: float


def _detect_ssd(dev_path: str, fstype: str, opts: str) -> Tuple[bool, str]:
    """Detect whether a mount corresponds to an SSD/NVMe/virtio/flash device."""
    block_name = ""
    if dev_path.startswith("/dev/"):
        base = os.path.basename(dev_path)
        block_name = base
        # Resolve parent block device (e.g. nvme0n1p2 -> nvme0n1, vda2 -> vda, sda1 -> sda)
        parent = base
        if base.startswith("nvme") and "p" in base:
            parent = base.rsplit("p", 1)[0]
        elif base.startswith("mmcblk") and "p" in base:
            parent = base.rsplit("p", 1)[0]
        else:
            parent = base.rstrip("0123456789") or base

        rot_path = f"/sys/block/{parent}/queue/rotational"
        try:
            with open(rot_path, "r", encoding="utf-8") as f:
                if f.read().strip() == "0":
                    return True, base
        except OSError:
            pass

        # Btrfs discard=async or ssd option or virtio/nvme/Host-backed virtual disk
        if "discard" in opts or "ssd" in opts or base.startswith(("nvme", "vd")):
            return True, base
        return False, base

    if fstype in ("9p", "virtiofs", "fuse.sshfs", "apfs"):
        return True, dev_path
    return False, dev_path


def _extract_subvol(opts: str) -> str:
    for part in opts.split(","):
        if part.startswith("subvol="):
            return part.split("=", 1)[1]
    return ""


def get_mounts(include_all: bool = False) -> List[MountDisk]:
    """Read /proc/mounts and os.statvfs to build a list of storage mounts."""
    raw_entries: List[Tuple[str, str, str, str]] = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                dev, mnt, fstype, opts = parts[0], parts[1], parts[2], parts[3]
                # Unescape octal sequences like \040 for spaces
                mnt = mnt.replace("\\040", " ")
                raw_entries.append((dev, mnt, fstype, opts))
    except OSError:
        return []

    # Sort so root `/` and shorter mountpoints come first for deduplication
    raw_entries.sort(key=lambda x: (0 if x[1] == "/" else 1, len(x[1]), x[1]))

    seen_devices: Dict[str, MountDisk] = {}
    results: List[MountDisk] = []

    for dev, mnt, fstype, opts in raw_entries:
        # Never show /dev or /dev/* mountpoints as storage disks
        if mnt == "/dev" or mnt.startswith("/dev/"):
            continue

        is_virt = (
            fstype in VIRTUAL_FSTYPES
            or fstype == "tmpfs"
            or mnt.startswith(("/proc", "/sys", "/run/credentials", "/run/user"))
        )
        is_boot = mnt == "/boot" or mnt.startswith(("/boot/", "/efi"))
        if not include_all and (is_virt or is_boot):
            continue

        try:
            st = os.statvfs(mnt)
        except OSError:
            continue

        total = st.f_blocks * st.f_frsize
        if total <= 0:
            continue

        avail = st.f_bavail * st.f_frsize
        free = st.f_bfree * st.f_frsize
        used = max(0, total - free)
        # Match df / btop calculation where Used + Free = Total user-usable or actual blocks
        used_pct = (used / total) * 100.0 if total > 0 else 0.0
        free_pct = 100.0 - used_pct

        inodes_total = st.f_files
        inodes_free = st.f_ffree
        inodes_used = max(0, inodes_total - inodes_free)
        inodes_pct = (inodes_used / inodes_total * 100.0) if inodes_total > 0 else 0.0

        is_ssd, block_dev = _detect_ssd(dev, fstype, opts)
        subvol = _extract_subvol(opts)

        is_dup = False
        if dev.startswith("/dev/") and dev in seen_devices:
            is_dup = True
            seen_devices[dev].submounts.append(f"{mnt} ({subvol})" if subvol else mnt)
            if not include_all:
                continue

        disk = MountDisk(
            device=dev,
            mountpoint=mnt,
            fstype=fstype,
            opts=opts,
            total=total,
            used=used,
            free=avail,  # Available to user
            avail=avail,
            used_pct=used_pct,
            free_pct=free_pct,
            inodes_total=inodes_total,
            inodes_used=inodes_used,
            inodes_pct=inodes_pct,
            is_ssd=is_ssd,
            is_virtual=is_virt,
            is_subvol_dup=is_dup,
            subvol=subvol,
            block_dev=block_dev,
        )
        if dev.startswith("/dev/") and dev not in seen_devices:
            seen_devices[dev] = disk
        results.append(disk)

    return results


def get_swap_info() -> List[SwapInfo]:
    """Read /proc/swaps for swap/zram usage."""
    swaps: List[SwapInfo] = []
    try:
        with open("/proc/swaps", "r", encoding="utf-8") as f:
            lines = f.readlines()[1:]
        for line in lines:
            parts = line.split()
            if len(parts) >= 4:
                dev = parts[0]
                total = int(parts[2]) * 1024
                used = int(parts[3]) * 1024
                free = max(0, total - used)
                pct = (used / total * 100.0) if total > 0 else 0.0
                swaps.append(SwapInfo(device=dev, total=total, used=used, free=free, used_pct=pct))
    except OSError:
        pass
    return swaps


def read_diskstats() -> Dict[str, DiskIOState]:
    """Read /proc/diskstats for block devices."""
    now = time.monotonic()
    stats: Dict[str, DiskIOState] = {}
    try:
        with open("/proc/diskstats", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                name = parts[2]
                if name.startswith("loop") or name.startswith("ram"):
                    continue
                reads_completed = int(parts[3])
                sectors_read = int(parts[5])
                read_time_ms = int(parts[6])
                writes_completed = int(parts[7])
                sectors_written = int(parts[9])
                write_time_ms = int(parts[10])
                io_in_progress = int(parts[11])
                io_time_ms = int(parts[12])
                discards_completed = int(parts[14]) if len(parts) >= 18 else 0
                sectors_discarded = int(parts[16]) if len(parts) >= 18 else 0
                stats[name] = DiskIOState(
                    name=name,
                    reads_completed=reads_completed,
                    sectors_read=sectors_read,
                    read_time_ms=read_time_ms,
                    writes_completed=writes_completed,
                    sectors_written=sectors_written,
                    write_time_ms=write_time_ms,
                    io_in_progress=io_in_progress,
                    io_time_ms=io_time_ms,
                    discards_completed=discards_completed,
                    sectors_discarded=sectors_discarded,
                    timestamp=now,
                )
    except OSError:
        pass
    return stats


class IOTracker:
    """Tracks delta rates and history for block devices in /proc/diskstats."""

    def __init__(self) -> None:
        self.prev: Dict[str, DiskIOState] = read_diskstats()
        self.metrics: Dict[str, DiskIOMetrics] = {}
        for name, st in self.prev.items():
            self.metrics[name] = DiskIOMetrics(
                name=name,
                total_read_bytes=st.sectors_read * 512,
                total_write_bytes=st.sectors_written * 512,
                total_discard_bytes=st.sectors_discarded * 512,
            )

    def update(self) -> Dict[str, DiskIOMetrics]:
        curr = read_diskstats()
        for name, st in curr.items():
            m = self.metrics.get(name)
            if m is None:
                m = DiskIOMetrics(name=name)
                self.metrics[name] = m
            p = self.prev.get(name)
            if p is not None:
                dt = max(1e-3, st.timestamp - p.timestamp)
                d_read_bytes = max(0, (st.sectors_read - p.sectors_read) * 512)
                d_write_bytes = max(0, (st.sectors_written - p.sectors_written) * 512)
                d_reads = max(0, st.reads_completed - p.reads_completed)
                d_writes = max(0, st.writes_completed - p.writes_completed)
                d_io_ms = max(0, st.io_time_ms - p.io_time_ms)

                m.read_bps = d_read_bytes / dt
                m.write_bps = d_write_bytes / dt
                m.read_iops = d_reads / dt
                m.write_iops = d_writes / dt
                m.util_pct = min(100.0, (d_io_ms / (dt * 1000.0)) * 100.0)
                m.read_history.append(m.read_bps)
                m.write_history.append(m.write_bps)
                m.peak_read_bps = max(m.peak_read_bps, m.read_bps, 64 * 1024.0)
                m.peak_write_bps = max(m.peak_write_bps, m.write_bps, 64 * 1024.0)

            m.total_read_bytes = st.sectors_read * 512
            m.total_write_bytes = st.sectors_written * 512
            m.total_discard_bytes = st.sectors_discarded * 512

        self.prev = curr
        return self.metrics





# ---------------------------------------------------------------------------
# Btop Theme Loader & Color Resolver
# ---------------------------------------------------------------------------

@dataclass
class BtopTheme:
    main_bg: str = ""
    main_fg: str = "#14b9b5"
    title: str = "#14B9B5"
    hi_fg: str = "#ff7f41"
    selected_bg: str = "#c8e967"
    selected_fg: str = "#000000"
    inactive_fg: str = "#c53253"
    graph_text: str = "#fd3e6a"
    proc_misc: str = "#7cd699"
    cpu_box: str = "#e20342"
    mem_box: str = "#7cd699"
    net_box: str = "#9147a8"
    proc_box: str = "#c8e967"
    div_line: str = "#a60234"
    used_start: str = "#7cd699"
    used_mid: str = "#ff7f41"
    used_end: str = "#e20342"
    free_start: str = "#14b9b5"
    free_mid: str = "#7cd699"
    free_end: str = "#c8e967"
    download_start: str = "#14b9b5"
    upload_start: str = "#ff7f41"
    rounded_corners: bool = True


def hex_to_rgb(hex_str: str) -> Optional[Tuple[int, int, int]]:
    """Convert hex string (#RRGGBB) to (r, g, b) 0..255."""
    s = hex_str.strip().lstrip("#")
    if len(s) == 6:
        try:
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        except ValueError:
            return None
    return None


def rgb_to_256(r: int, g: int, b: int) -> int:
    """Map RGB (0-255) to the closest standard xterm-256 color index."""
    if abs(r - g) < 10 and abs(g - b) < 10:
        # Grayscale ramp 232..255
        gray = 232 + int((r / 255.0) * 23.99)
        return max(232, min(255, gray))
    v = [0, 95, 135, 175, 215, 255]
    def closest_idx(val: int) -> int:
        return min(range(6), key=lambda i: abs(v[i] - val))
    ri, gi, bi = closest_idx(r), closest_idx(g), closest_idx(b)
    return 16 + 36 * ri + 6 * gi + bi


def hex_to_256(hex_str: str, default_idx: int) -> int:
    rgb = hex_to_rgb(hex_str)
    if rgb is None:
        return default_idx
    return rgb_to_256(*rgb)


def hex_to_ansi_fg(hex_str: str, fallback_256: int) -> str:
    """Format 24-bit ANSI truecolor foreground escape sequence."""
    rgb = hex_to_rgb(hex_str)
    if rgb:
        return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    return f"\033[38;5;{fallback_256}m"


def hex_to_ansi_bg(hex_str: str, fallback_256: int) -> str:
    """Format 24-bit ANSI truecolor background escape sequence."""
    rgb = hex_to_rgb(hex_str)
    if rgb:
        return f"\033[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    return f"\033[48;5;{fallback_256}m"


def find_active_btop_theme_path() -> Optional[str]:
    """Find the path to the currently active btop theme file."""
    # Check ~/.config/btop/btop.conf for configured theme
    conf_path = os.path.expanduser("~/.config/btop/btop.conf")
    theme_name = ""
    if os.path.isfile(conf_path):
        try:
            with open(conf_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("color_theme"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            theme_name = parts[1].strip().strip('"').strip("'")
                            break
        except OSError:
            pass

    candidates: List[str] = []
    if theme_name and theme_name not in ("Default", "TTY"):
        candidates.extend([
            os.path.expanduser(f"~/.config/btop/themes/{theme_name}.theme"),
            os.path.expanduser(f"~/.config/btop/themes/{theme_name}"),
            f"/usr/share/btop/themes/{theme_name}.theme",
            f"/usr/share/btop/themes/{theme_name}",
        ])

    candidates.extend([
        os.path.expanduser("~/.config/btop/themes/current.theme"),
        os.path.expanduser("~/.local/state/omarchy/current/theme/btop.theme"),
        os.path.expanduser("~/.config/omarchy/current/theme/btop.theme"),
    ])

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_btop_theme(theme_path: Optional[str] = None) -> BtopTheme:
    """Parse btop .theme file and return structured BtopTheme."""
    theme = BtopTheme()
    path = theme_path or find_active_btop_theme_path()
    if not path or not os.path.isfile(path):
        return theme

    raw: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("theme[") and "]" in line and "=" in line:
                    key = line[6:line.index("]")].strip()
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    raw[key] = val
                elif line.startswith("rounded_corners"):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
                    theme.rounded_corners = (val in ("true", "1", "yes"))
    except OSError:
        return theme

    if raw.get("main_bg"): theme.main_bg = raw["main_bg"]
    if raw.get("main_fg"): theme.main_fg = raw["main_fg"]
    if raw.get("title"): theme.title = raw["title"]
    if raw.get("hi_fg"): theme.hi_fg = raw["hi_fg"]
    if raw.get("selected_bg"): theme.selected_bg = raw["selected_bg"]
    if raw.get("selected_fg"): theme.selected_fg = raw["selected_fg"]
    if raw.get("inactive_fg"): theme.inactive_fg = raw["inactive_fg"]
    if raw.get("graph_text"): theme.graph_text = raw["graph_text"]
    if raw.get("proc_misc"): theme.proc_misc = raw["proc_misc"]
    if raw.get("cpu_box"): theme.cpu_box = raw["cpu_box"]
    if raw.get("mem_box"): theme.mem_box = raw["mem_box"]
    if raw.get("net_box"): theme.net_box = raw["net_box"]
    if raw.get("proc_box"): theme.proc_box = raw["proc_box"]
    if raw.get("div_line"): theme.div_line = raw["div_line"]
    if raw.get("used_start"): theme.used_start = raw["used_start"]
    if raw.get("used_mid"): theme.used_mid = raw["used_mid"]
    if raw.get("used_end"): theme.used_end = raw["used_end"]
    if raw.get("free_start"): theme.free_start = raw["free_start"]
    if raw.get("free_mid"): theme.free_mid = raw["free_mid"]
    if raw.get("free_end"): theme.free_end = raw["free_end"]
    if raw.get("download_start"): theme.download_start = raw["download_start"]
    if raw.get("upload_start"): theme.upload_start = raw["upload_start"]
    return theme


# ---------------------------------------------------------------------------
# Curses Btop-Style UI Renderer
# ---------------------------------------------------------------------------

C_DEFAULT = 1
C_BORDER = 2
C_TITLE = 3
C_KEY = 4
C_USED_LOW = 5      # Used meter start (<60%)
C_USED_MID = 6      # Used meter mid (60-85%)
C_USED_HIGH = 7     # Used meter high (>85%)
C_FREE = 8          # Free space meter
C_DIM = 9           # Inactive/dim text
C_SELECTED = 10     # Highlighted row
C_IO_READ = 11      # Read / Download
C_IO_WRITE = 12     # Write / Upload
C_BADGE = 13        # SSD badge / proc_misc
C_ACTIVE_BOX = 14   # Active box border


def init_colors(theme: Optional[BtopTheme] = None) -> None:
    t = theme or load_btop_theme()
    curses.start_color()
    curses.use_default_colors()

    if curses.COLORS >= 256:
        c_def = hex_to_256(t.main_fg, 252)
        c_border = hex_to_256(t.mem_box or t.div_line, 240)
        c_title = hex_to_256(t.title, 255)
        c_key = hex_to_256(t.hi_fg, 214)
        c_used_low = hex_to_256(t.used_start, 114)
        c_used_mid = hex_to_256(t.used_mid, 209)
        c_used_high = hex_to_256(t.used_end, 161)
        c_free = hex_to_256(t.free_start or t.main_fg, 37)
        c_dim = hex_to_256(t.inactive_fg, 167)
        c_sel_bg = hex_to_256(t.selected_bg, 185)
        c_sel_fg = hex_to_256(t.selected_fg, 16)
        c_io_read = hex_to_256(t.download_start or t.free_start, 37)
        c_io_write = hex_to_256(t.upload_start or t.hi_fg, 209)
        c_badge = hex_to_256(t.proc_misc or t.title, 114)
        c_act_box = hex_to_256(t.proc_box or t.title, 185)

        curses.init_pair(C_DEFAULT, c_def, -1)
        curses.init_pair(C_BORDER, c_border, -1)
        curses.init_pair(C_TITLE, c_title, -1)
        curses.init_pair(C_KEY, c_key, -1)
        curses.init_pair(C_USED_LOW, c_used_low, -1)
        curses.init_pair(C_USED_MID, c_used_mid, -1)
        curses.init_pair(C_USED_HIGH, c_used_high, -1)
        curses.init_pair(C_FREE, c_free, -1)
        curses.init_pair(C_DIM, c_dim, -1)
        curses.init_pair(C_SELECTED, c_sel_fg, c_sel_bg)
        curses.init_pair(C_IO_READ, c_io_read, -1)
        curses.init_pair(C_IO_WRITE, c_io_write, -1)
        curses.init_pair(C_BADGE, c_badge, -1)
        curses.init_pair(C_ACTIVE_BOX, c_act_box, -1)
    else:
        curses.init_pair(C_DEFAULT, curses.COLOR_WHITE, -1)
        curses.init_pair(C_BORDER, curses.COLOR_BLUE, -1)
        curses.init_pair(C_TITLE, curses.COLOR_WHITE, -1)
        curses.init_pair(C_KEY, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_USED_LOW, curses.COLOR_GREEN, -1)
        curses.init_pair(C_USED_MID, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_USED_HIGH, curses.COLOR_RED, -1)
        curses.init_pair(C_FREE, curses.COLOR_CYAN, -1)
        curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
        curses.init_pair(C_SELECTED, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(C_IO_READ, curses.COLOR_GREEN, -1)
        curses.init_pair(C_IO_WRITE, curses.COLOR_MAGENTA, -1)
        curses.init_pair(C_BADGE, curses.COLOR_CYAN, -1)
        curses.init_pair(C_ACTIVE_BOX, curses.COLOR_CYAN, -1)


def color_for_pct(pct: float) -> int:
    if pct >= 85.0:
        return curses.color_pair(C_USED_HIGH)
    if pct >= 60.0:
        return curses.color_pair(C_USED_MID)
    return curses.color_pair(C_USED_LOW)


def safe_addstr(stdscr: "curses._CursesWindow", y: int, x: int, text: str, attr: int = 0) -> None:
    """Safely draw string clipped to window boundaries."""
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    if x < 0:
        text = text[-x:]
        x = 0
    max_len = w - x
    if y == h - 1:
        max_len -= 1
    if max_len <= 0:
        return
    try:
        stdscr.addstr(y, x, text[:max_len], attr)
    except curses.error:
        pass


def draw_btop_box(
    stdscr: "curses._CursesWindow",
    y: int,
    x: int,
    h: int,
    w: int,
    title: str = "",
    right_label: str = "",
    active: bool = False,
    rounded: bool = True,
) -> None:
    """Draw a box in btop style (`╭─┤ title ├─╮` or `┌─┤ title ├─┐`)."""
    if h < 2 or w < 4:
        return
    b_attr = curses.color_pair(C_ACTIVE_BOX if active else C_BORDER)
    t_attr = curses.color_pair(C_TITLE) | curses.A_BOLD

    tl, tr = ("╭", "╮") if rounded else ("┌", "┐")
    bl, br = ("╰", "╯") if rounded else ("└", "┘")

    top = tl + "─" * (w - 2) + tr
    bot = bl + "─" * (w - 2) + br
    safe_addstr(stdscr, y, x, top, b_attr)
    for row in range(y + 1, y + h - 1):
        safe_addstr(stdscr, row, x, "│", b_attr)
        safe_addstr(stdscr, row, x + w - 1, "│", b_attr)
    safe_addstr(stdscr, y + h - 1, x, bot, b_attr)

    if title and w > len(title) + 6:
        safe_addstr(stdscr, y, x + 2, "┤ ", b_attr)
        safe_addstr(stdscr, y, x + 4, title, t_attr)
        safe_addstr(stdscr, y, x + 4 + len(title), " ├", b_attr)

    if right_label and w > len(title) + len(right_label) + 14:
        rx = x + w - len(right_label) - 6
        safe_addstr(stdscr, y, rx, "┤ ", b_attr)
        safe_addstr(stdscr, y, rx + 2, right_label, curses.color_pair(C_KEY))
        safe_addstr(stdscr, y, rx + 2 + len(right_label), " ├", b_attr)


class DtopApp:
    def __init__(self, theme: Optional[BtopTheme] = None) -> None:
        self.theme = theme or load_btop_theme()
        self.include_all_mounts = False
        self.selected_disk_idx = 0
        self.refresh_interval = 1.0
        self.io_tracker = IOTracker()
        self.mounts = get_mounts(self.include_all_mounts)
        self.swaps = get_swap_info()
        self.last_update = time.monotonic()

    def refresh_metrics(self) -> None:
        self.mounts = get_mounts(self.include_all_mounts)
        self.swaps = get_swap_info()
        self.io_tracker.update()
        if self.selected_disk_idx >= len(self.mounts):
            self.selected_disk_idx = max(0, len(self.mounts) - 1)
        self.last_update = time.monotonic()

    def run(self, stdscr: "curses._CursesWindow") -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        init_colors(self.theme)

        while True:
            now = time.monotonic()
            if now - self.last_update >= self.refresh_interval:
                self.refresh_metrics()

            stdscr.erase()
            self.draw(stdscr)
            stdscr.refresh()

            # Wait up to 100ms for input
            curses.napms(80)
            ch = stdscr.getch()
            if ch == -1:
                continue

            if ch in (ord("q"), ord("Q"), 27):  # q or ESC
                break
            elif ch in (ord("a"), ord("A")):
                self.include_all_mounts = not self.include_all_mounts
                self.refresh_metrics()
            elif ch in (ord("r"), ord("R")):
                self.refresh_metrics()
            elif ch in (ord("+"), ord("=")):
                self.refresh_interval = max(0.25, self.refresh_interval - 0.25)
            elif ch in (ord("-"), ord("_")):
                self.refresh_interval = min(5.0, self.refresh_interval + 0.25)
            elif ch in (curses.KEY_UP, ord("k")):
                self.selected_disk_idx = max(0, self.selected_disk_idx - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.selected_disk_idx = min(max(0, len(self.mounts) - 1), self.selected_disk_idx + 1)

    def draw(self, stdscr: "curses._CursesWindow") -> None:
        h, w = stdscr.getmaxyx()
        if h < 10 or w < 50:
            safe_addstr(stdscr, 0, 0, f"Terminal too small ({w}x{h}). Resize to at least 50x10.", curses.A_BOLD)
            return

        body_h = h - 1  # leaves 1 row for bottom footer

        # Split layout if terminal is wide (w >= 100), stacked if narrower
        if w >= 100:
            left_w = int(w * 0.55)
            right_w = w - left_w
            self.draw_disks_box(stdscr, 0, 0, body_h, left_w)
            self.draw_io_box(stdscr, 0, left_w, body_h, right_w)
        else:
            # Stacked: Disks on top, IO Activity below
            disks_h = min(body_h - 6, max(7, len(self.mounts) * 4 + 4))
            io_h = body_h - disks_h
            self.draw_disks_box(stdscr, 0, 0, disks_h, w)
            self.draw_io_box(stdscr, disks_h, 0, io_h, w)

        # Bottom shortcut bar
        self.draw_footer(stdscr, h - 1, w)

    def draw_disks_box(self, stdscr: "curses._CursesWindow", y: int, x: int, h: int, w: int) -> None:
        clock_str = time.strftime("%H:%M:%S")
        mode_lbl = "all [a]" if self.include_all_mounts else "physical [a]"
        draw_btop_box(
            stdscr, y, x, h, w,
            title="dtop ── disks",
            right_label=f"{mode_lbl} │ ↻ {self.refresh_interval:.1f}s │ {clock_str}",
            active=True,
            rounded=self.theme.rounded_corners,
        )

        inner_w = w - 4
        row_y = y + 1
        max_y = y + h - 1

        if not self.mounts:
            safe_addstr(stdscr, row_y, x + 2, "No storage mounts found.", curses.color_pair(C_DIM))
            return

        for idx, m in enumerate(self.mounts):
            if row_y >= max_y:
                break
            is_sel = (idx == self.selected_disk_idx)

            # Line 1: Mountpoint, Device, FSType, SSD tag, Total Size
            ssd_tag = "SSD" if m.is_ssd else ("TMP" if m.is_virtual else "HDD")
            dev_short = os.path.basename(m.device) if m.device.startswith("/dev/") else m.device
            header_left = f"▸ {m.display_name}" if is_sel else f"  {m.display_name}"
            meta = f"[{dev_short} · {m.fstype} · {ssd_tag}]"
            tot_lbl = f"Total: {fmt_bytes(m.total)}"

            hdr_attr = curses.color_pair(C_SELECTED) | curses.A_BOLD if is_sel else (curses.color_pair(C_TITLE) | curses.A_BOLD)
            safe_addstr(stdscr, row_y, x + 2, header_left, hdr_attr)
            safe_addstr(stdscr, row_y, x + 3 + len(header_left), meta, curses.color_pair(C_BADGE))
            safe_addstr(stdscr, row_y, x + w - 2 - len(tot_lbl), tot_lbl, curses.color_pair(C_TITLE) | curses.A_BOLD)
            row_y += 1
            if row_y >= max_y:
                break

            # Line 2: Used Bar (btop style)
            used_val_str = f"{fmt_bytes(m.used):>9} ({m.used_pct:4.1f}%)"
            bar_w = max(8, inner_w - 8 - len(used_val_str))
            u_bar = make_bar(m.used_pct, bar_w)
            safe_addstr(stdscr, row_y, x + 4, "Used ", curses.color_pair(C_DIM))
            safe_addstr(stdscr, row_y, x + 9, u_bar, color_for_pct(m.used_pct))
            safe_addstr(stdscr, row_y, x + 10 + bar_w, used_val_str, curses.color_pair(C_TITLE))
            row_y += 1
            if row_y >= max_y:
                break

            # Line 3: Free Bar (btop style)
            free_val_str = f"{fmt_bytes(m.free):>9} ({m.free_pct:4.1f}%)"
            f_bar = make_bar(m.free_pct, bar_w)
            safe_addstr(stdscr, row_y, x + 4, "Free ", curses.color_pair(C_DIM))
            safe_addstr(stdscr, row_y, x + 9, f_bar, curses.color_pair(C_FREE))
            safe_addstr(stdscr, row_y, x + 10 + bar_w, free_val_str, curses.color_pair(C_FREE) | curses.A_BOLD)
            row_y += 1

            # Optional Line 4: Subvolumes / mount options / inode info if space allows
            if row_y < max_y:
                details = []
                if m.subvol:
                    details.append(f"subvol={m.subvol}")
                if m.submounts:
                    details.append(f"shared with: {', '.join(m.submounts[:3])}")
                if "compress=" in m.opts:
                    for opt in m.opts.split(","):
                        if opt.startswith("compress="):
                            details.append(opt)
                if m.inodes_total > 0:
                    details.append(f"inodes: {m.inodes_pct:.1f}%")
                if details:
                    det_str = "    └─ " + " · ".join(details)
                    safe_addstr(stdscr, row_y, x + 2, det_str[:inner_w], curses.color_pair(C_DIM))
                    row_y += 1

            # Divider between disks if room permits
            if row_y < max_y and idx < len(self.mounts) - 1:
                safe_addstr(stdscr, row_y, x + 2, "─" * inner_w, curses.color_pair(C_BORDER))
                row_y += 1

        # Show Swap at bottom of disks box if space allows
        if self.swaps and row_y < max_y:
            safe_addstr(stdscr, row_y, x + 2, "─" * inner_w, curses.color_pair(C_BORDER))
            row_y += 1
            if row_y < max_y:
                sw = self.swaps[0]
                sw_tag = f"[Swap {os.path.basename(sw.device)}]"
                safe_addstr(stdscr, row_y, x + 2, sw_tag, curses.color_pair(C_BADGE))
                sw_info = f"Used: {fmt_bytes(sw.used)} / {fmt_bytes(sw.total)} ({sw.used_pct:.1f}%)   │   Free: {fmt_bytes(sw.free)}"
                safe_addstr(stdscr, row_y, x + 3 + len(sw_tag), sw_info, curses.color_pair(C_TITLE))

    def draw_io_box(self, stdscr: "curses._CursesWindow", y: int, x: int, h: int, w: int) -> None:
        # Show I/O for selected disk's block device or primary block device (e.g. vda2 / vda)
        sel_mount = self.mounts[self.selected_disk_idx] if self.mounts else None
        dev_name = sel_mount.block_dev if (sel_mount and sel_mount.block_dev in self.io_tracker.metrics) else ""
        if not dev_name:
            # Fallback to first main block device
            for candidate in ("vda2", "vda", "nvme0n1", "sda"):
                if candidate in self.io_tracker.metrics:
                    dev_name = candidate
                    break
            if not dev_name and self.io_tracker.metrics:
                dev_name = next(iter(self.io_tracker.metrics.keys()))

        m = self.io_tracker.metrics.get(dev_name)
        draw_btop_box(
            stdscr, y, x, h, w,
            title=f"io activity ── {dev_name or 'disk'}",
            right_label=f"util {m.util_pct:.1f}%" if m else "",
            rounded=self.theme.rounded_corners,
        )
        if not m:
            safe_addstr(stdscr, y + 1, x + 2, "No block device I/O stats available.", curses.color_pair(C_DIM))
            return

        inner_w = w - 4
        spark_w = max(10, inner_w - 38)

        # Read row + sparkline
        if y + 1 < y + h - 1:
            r_spark = make_sparkline(list(m.read_history), spark_w, m.peak_read_bps)
            safe_addstr(stdscr, y + 1, x + 2, "▲ Read ", curses.color_pair(C_IO_READ) | curses.A_BOLD)
            safe_addstr(stdscr, y + 1, x + 10, r_spark, curses.color_pair(C_IO_READ))
            safe_addstr(
                stdscr, y + 1, x + 11 + spark_w,
                f"{fmt_rate(m.read_bps):>11} ({m.read_iops:5.0f} IOPS)",
                curses.color_pair(C_TITLE),
            )

        # Write row + sparkline
        if y + 2 < y + h - 1:
            w_spark = make_sparkline(list(m.write_history), spark_w, m.peak_write_bps)
            safe_addstr(stdscr, y + 2, x + 2, "▼ Write", curses.color_pair(C_IO_WRITE) | curses.A_BOLD)
            safe_addstr(stdscr, y + 2, x + 10, w_spark, curses.color_pair(C_IO_WRITE))
            safe_addstr(
                stdscr, y + 2, x + 11 + spark_w,
                f"{fmt_rate(m.write_bps):>11} ({m.write_iops:5.0f} IOPS)",
                curses.color_pair(C_TITLE),
            )

        # Totals since boot + TRIM/Discard
        if y + 4 < y + h - 1:
            safe_addstr(stdscr, y + 3, x + 2, "─" * inner_w, curses.color_pair(C_BORDER))
            totals_str = (
                f"Total Read: {fmt_bytes(m.total_read_bytes)}   │   "
                f"Total Written: {fmt_bytes(m.total_write_bytes)}   │   "
                f"SSD TRIM/Discard: {fmt_bytes(m.total_discard_bytes)}"
            )
            safe_addstr(stdscr, y + 4, x + 2, totals_str[:inner_w], curses.color_pair(C_DIM))

        # Show other block devices summary if height permits
        row = y + 5
        for other_name, om in self.io_tracker.metrics.items():
            if other_name == dev_name or row >= y + h - 1:
                continue
            line = (
                f"dev {other_name:<6}  R: {fmt_rate(om.read_bps):>10}  "
                f"W: {fmt_rate(om.write_bps):>10}  "
                f"Tot R/W: {fmt_bytes(om.total_read_bytes)} / {fmt_bytes(om.total_write_bytes)}"
            )
            safe_addstr(stdscr, row, x + 2, line[:inner_w], curses.color_pair(C_DIM))
            row += 1

    def draw_footer(self, stdscr: "curses._CursesWindow", y: int, w: int) -> None:
        keys = [
            ("↑/↓", "Select Disk"),
            ("r", "Refresh"),
            ("a", "All Mounts"),
            ("+/-", "Speed"),
            ("q", "Quit"),
        ]
        cx = 1
        for k, desc in keys:
            if cx + len(k) + len(desc) + 5 >= w:
                break
            safe_addstr(stdscr, y, cx, k, curses.color_pair(C_KEY) | curses.A_BOLD)
            cx += len(k)
            safe_addstr(stdscr, y, cx, f":{desc}  ", curses.color_pair(C_DIM))
            cx += len(desc) + 3


# ---------------------------------------------------------------------------
# Non-Interactive Btop-Style ANSI Snapshot (`--once`)
# ---------------------------------------------------------------------------

def render_snapshot(
    include_all: bool = False,
    theme: Optional[BtopTheme] = None,
) -> None:
    """Print a colored btop-style storage summary directly to stdout."""
    t = theme or load_btop_theme()

    RST = "\033[0m"
    BOLD = "\033[1m"
    DIM = hex_to_ansi_fg(t.inactive_fg, 243)
    BORDER = hex_to_ansi_fg(t.mem_box or t.div_line, 240)
    TITLE = hex_to_ansi_fg(t.title, 255)
    KEY = hex_to_ansi_fg(t.hi_fg, 214)
    FREE = hex_to_ansi_fg(t.free_start or t.main_fg, 75)
    BADGE = hex_to_ansi_fg(t.proc_misc or t.title, 117)
    MAIN = hex_to_ansi_fg(t.main_fg, 252)

    def ansi_pct_color(pct: float) -> str:
        if pct >= 85.0:
            return hex_to_ansi_fg(t.used_end, 203)
        if pct >= 60.0:
            return hex_to_ansi_fg(t.used_mid, 220)
        return hex_to_ansi_fg(t.used_start, 84)

    tl, tr = ("╭", "╮") if t.rounded_corners else ("┌", "┐")
    bl, br = ("╰", "╯") if t.rounded_corners else ("└", "┘")
    ml, mr = ("├", "┤")

    mounts = get_mounts(include_all=include_all)
    swaps = get_swap_info()
    width = 78
    inner = width - 4

    def pad_row(visible_len: int, content: str) -> str:
        pad = max(0, (width - 4) - visible_len)
        return f"{BORDER}│{RST} {content}{' ' * pad} {BORDER}│{RST}"

    hdr_title = "dtop ── SSD & Disk Storage Summary"
    top_dashes = width - len(hdr_title) - 7
    print(f"{BORDER}{tl}─┤ {RST}{TITLE}{BOLD}{hdr_title}{RST}{BORDER} ├{'─' * top_dashes}{tr}{RST}")

    for idx, m in enumerate(mounts):
        ssd_tag = "SSD" if m.is_ssd else ("TMP" if m.is_virtual else "DISK")
        dev_short = os.path.basename(m.device) if m.device.startswith("/dev/") else m.device
        disp_name = m.display_name
        left_plain = f"{disp_name:<25} [{dev_short} · {m.fstype} · {ssd_tag}]"
        right_plain = f"Total: {fmt_bytes(m.total)}"
        gap = max(1, inner - len(left_plain) - len(right_plain))
        row_str = (
            f"{TITLE}{BOLD}{disp_name:<25}{RST} {BADGE}[{dev_short} · {m.fstype} · {ssd_tag}]{RST}"
            f"{' ' * gap}Total: {MAIN}{BOLD}{fmt_bytes(m.total)}{RST}"
        )
        print(pad_row(len(left_plain) + gap + len(right_plain), row_str))

        bar_w = 42
        u_bar = make_bar(m.used_pct, bar_w)
        f_bar = make_bar(m.free_pct, bar_w)
        u_col = ansi_pct_color(m.used_pct)
        u_plain = f"  Used {u_bar} {fmt_bytes(m.used):>10} ({m.used_pct:5.1f}%)"
        f_plain = f"  Free {f_bar} {fmt_bytes(m.free):>10} ({m.free_pct:5.1f}%)"
        print(pad_row(
            len(u_plain),
            f"  {DIM}Used{RST} {u_col}{u_bar}{RST} {MAIN}{BOLD}{fmt_bytes(m.used):>10}{RST} ({m.used_pct:5.1f}%)"
        ))
        print(pad_row(
            len(f_plain),
            f"  {DIM}Free{RST} {FREE}{f_bar}{RST} {FREE}{BOLD}{fmt_bytes(m.free):>10}{RST} ({m.free_pct:5.1f}%)"
        ))
        if m.submounts:
            sub_str = f"  └─ shared subvolumes: {', '.join(m.submounts)}"
            if len(sub_str) > inner:
                sub_str = sub_str[: inner - 1] + "…"
            print(pad_row(len(sub_str), f"{DIM}{sub_str}{RST}"))
        if idx < len(mounts) - 1:
            print(f"{BORDER}│ {'─' * inner} │{RST}")

    if swaps:
        sw = swaps[0]
        print(f"{BORDER}│ {'─' * inner} │{RST}")
        sw_tag = f"[Swap {os.path.basename(sw.device)}]"
        sw_plain = f"{sw_tag} Used: {fmt_bytes(sw.used)} / {fmt_bytes(sw.total)} ({sw.used_pct:.1f}%)  │  Free: {fmt_bytes(sw.free)}"
        sw_colored = (
            f"{BADGE}{sw_tag}{RST} Used: {MAIN}{BOLD}{fmt_bytes(sw.used)}{RST} / {fmt_bytes(sw.total)} ({sw.used_pct:.1f}%)"
            f"  │  Free: {FREE}{fmt_bytes(sw.free)}{RST}"
        )
        print(pad_row(len(sw_plain), sw_colored))

    print(f"{BORDER}{bl}{'─' * (width - 2)}{br}{RST}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="dtop - A btop-style terminal SSD & disk storage monitor."
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"dtop {__version__}",
    )
    parser.add_argument(
        "-a", "--all",
        action="store_true",
        help="Include all virtual/tmpfs and duplicate subvolume mounts",
    )
    parser.add_argument(
        "-1", "--once",
        action="store_true",
        help="Print a btop-style snapshot to the terminal and exit",
    )
    parser.add_argument(
        "-t", "--theme",
        default=None,
        help="Path or name of custom btop .theme file (defaults to active btop theme)",
    )
    args = parser.parse_args()

    theme = load_btop_theme(args.theme)

    if args.once or not sys.stdout.isatty():
        render_snapshot(include_all=args.all, theme=theme)
        return 0

    app = DtopApp(theme=theme)
    app.include_all_mounts = args.all
    app.refresh_metrics()
    try:
        curses.wrapper(app.run)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
