"""Photo Culler - a small Windows-first photo selection application.

The app deliberately copies selected originals on export; it never moves,
renames, or edits the source photographs.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import math
import os
import queue
import shutil
import sys
import tkinter as tk
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread


def configure_bundled_tk_runtime() -> None:
    """Point the packaged app at its own complete Tcl/Tk runtime before tkinter imports."""
    if not getattr(sys, "frozen", False):
        return
    bundle = Path(sys._MEIPASS)
    os.environ["TCL_LIBRARY"] = str(bundle / "tcl" / "tcl8.6")
    os.environ["TK_LIBRARY"] = str(bundle / "tcl" / "tk8.6")
    # _tkinter.pyd loads Tcl/Tk DLLs during import; keep this handle alive.
    if hasattr(os, "add_dll_directory"):
        global _tk_dll_directory
        _tk_dll_directory = os.add_dll_directory(str(bundle / "bin"))


_tk_dll_directory: object | None = None
configure_bundled_tk_runtime()

from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk, UnidentifiedImageError

try:
    import rawpy
except ImportError:  # The release build bundles rawpy, but keep a clear fallback.
    rawpy = None


APP_NAME = "Photo Culler"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".dng"}
THUMB_WIDTH = 132
THUMB_HEIGHT = 88
THUMB_SLOT = 148
THUMB_CACHE_LIMIT = 110
THUMB_RENDER_OVERSCAN = 3
THUMB_RENDER_POLL_MS = 24
JPEG_EXTENSIONS = {".jpg", ".jpeg"}
SIDEBAR_WIDTH_DEFAULT = 238
SIDEBAR_WIDTH_MIN = 180
SIDEBAR_WIDTH_MAX = 420
# Keep roughly one extra screen around the visible image. During a drag this
# buffer can move without requesting a new crop/resample operation.
PREVIEW_OVERSCAN = 0.72
PREVIEW_OVERSCAN_MAX_PX = 560
PREVIEW_INTERACTIVE_DELAY_MS = 24
PREVIEW_QUALITY_DELAY_MS = 150


def enable_windows_high_dpi() -> None:
    """Opt out of Windows bitmap scaling so Tk is rendered sharply on HiDPI monitors."""
    if sys.platform != "win32":
        return
    try:
        # PER_MONITOR_AWARE_V2: sharp rendering even if the window moves screens.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


@dataclass(frozen=True)
class PhotoGroup:
    """One culling decision, optionally made of a DNG and its JPEG preview."""

    key: str
    primary: Path
    members: tuple[Path, ...]

    @property
    def paired_raw_jpeg(self) -> bool:
        return any(path.suffix.lower() == ".dng" for path in self.members) and any(
            path.suffix.lower() in JPEG_EXTENSIONS for path in self.members
        )


@dataclass(frozen=True)
class PreviewGeometry:
    """The source region and on-canvas position for one preview frame."""

    source_box: tuple[int, int, int, int]
    target_size: tuple[int, int]
    origin: tuple[float, float]
    downsample_factor: int


ThumbnailKey = tuple[str, int, int, int, int]


@dataclass
class ThumbnailCanvasItems:
    """Canvas item IDs and the live PhotoImage reference for one visible tile."""

    group_key: str
    rect_id: int
    image_id: int
    marker_id: int
    mode_id: int
    label_id: int
    error_id: int | None = None
    thumb_key: ThumbnailKey | None = None
    photo: ImageTk.PhotoImage | None = None


def build_photo_groups(paths: list[Path]) -> list[PhotoGroup]:
    """Hide DNG + JPEG pairs behind one culling item, without grouping unrelated files."""
    by_stem: dict[str, list[Path]] = {}
    for path in paths:
        by_stem.setdefault(path.stem.casefold(), []).append(path)

    result: list[PhotoGroup] = []
    for same_name_paths in by_stem.values():
        ordered = sorted(same_name_paths, key=lambda path: path.name.casefold())
        raws = [path for path in ordered if path.suffix.lower() == ".dng"]
        jpegs = [path for path in ordered if path.suffix.lower() in JPEG_EXTENSIONS]
        paired_members = tuple(raws + jpegs)
        if raws and jpegs:
            # JPEG is much faster to browse and represents the same capture.
            primary = jpegs[0]
            key = "pair|" + str(primary.parent.resolve()).casefold() + "|" + primary.stem.casefold()
            result.append(PhotoGroup(key=key, primary=primary, members=paired_members))
            paired_paths = set(paired_members)
            for path in ordered:
                if path not in paired_paths:
                    result.append(PhotoGroup(key=str(path.resolve()), primary=path, members=(path,)))
        else:
            for path in ordered:
                result.append(PhotoGroup(key=str(path.resolve()), primary=path, members=(path,)))
    return sorted(result, key=lambda item: item.primary.name.casefold())


class PhotoCuller(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self._configure_dpi_layout()
        self.configure(bg="#17191d")

        self.folder: Path | None = None
        self.sidebar_width = self._load_sidebar_width()
        self.all_items: list[PhotoGroup] = []
        self.index = 0
        self.kept: set[str] = set()
        self.pair_modes: dict[str, str] = {}
        self.show_kept_only = tk.BooleanVar(value=False)
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.preview_image_item: int | None = None
        self._preview_item_origin: tuple[float, float] | None = None
        self._preview_item_size: tuple[int, int] | None = None
        self.current_source_image: Image.Image | None = None
        self.current_source_path: Path | None = None
        self.zoom_scale = 1.0  # Screen pixels for each source pixel; 1.0 means 100%.
        self.fit_scale = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._drag_state: tuple[int, int, float, float] | None = None
        self._interactive_render_job: str | None = None
        self._quality_render_job: str | None = None
        self._preview_render_generation = 0
        self._preview_render_events: queue.Queue[tuple[int, str, Image.Image | None, PreviewGeometry | None, Exception | None]] = queue.Queue()
        self._preview_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="photo-culler-preview")
        self._preview_futures: set[Future[None]] = set()
        # Interactive frames use a small, per-current-photo image pyramid. The
        # final settled frame still comes from the original pixels and Lanczos.
        self._preview_levels: dict[tuple[str, int], Image.Image] = {}
        self._preview_levels_lock = Lock()
        self.thumbnail_cache: OrderedDict[ThumbnailKey, ImageTk.PhotoImage] = OrderedDict()
        self._thumbnail_items: dict[str, ThumbnailCanvasItems] = {}
        self._thumbnail_jobs: dict[ThumbnailKey, Future[None]] = {}
        self._thumbnail_events: queue.Queue[
            tuple[int, ThumbnailKey, Image.Image | None, Exception | None]
        ] = queue.Queue()
        self._thumbnail_generation = 0
        self._thumbnail_render_job: str | None = None
        self._thumbnail_center_pending = False
        self._thumbnail_errors: set[ThumbnailKey] = set()
        self._thumbnail_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="photo-culler-thumbnail")
        self._thumbnail_poll_job: str | None = None
        self._thumbnail_placeholder_photo = ImageTk.PhotoImage(
            Image.new("RGB", (self.thumb_width, self.thumb_height), "#202329")
        )
        # Full-resolution JPEGs live here after the background preloader reads them.
        # PhotoImage objects are deliberately not created off the Tk main thread.
        self.jpeg_cache: dict[str, Image.Image] = {}
        self._jpeg_cache_lock = Lock()
        self._preload_events: queue.Queue[tuple[int, int, int, bool]] = queue.Queue()
        self._preload_generation = 0
        self._preload_done = True
        self._resize_job: str | None = None

        self._make_style()
        self._build_ui()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._preview_poll_job = self.after(16, self._poll_preview_render_events)
        self._thumbnail_poll_job = self.after(THUMB_RENDER_POLL_MS, self._poll_thumbnail_events)
        self.after(250, self.open_folder)

    def _configure_dpi_layout(self) -> None:
        """Scale geometry and raster thumbnail dimensions to the monitor's real DPI."""
        monitor_dpi = self.winfo_fpixels("1i")
        self.ui_scale = max(1.0, monitor_dpi / 96.0)
        self.tk.call("tk", "scaling", monitor_dpi / 72.0)
        self.thumb_width = self._px(THUMB_WIDTH)
        self.thumb_height = self._px(THUMB_HEIGHT)
        self.thumb_slot = self._px(THUMB_SLOT)

        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = min(self._px(1280), int(screen_width * 0.94))
        height = min(self._px(820), int(screen_height * 0.88))
        self.geometry(f"{width}x{height}")
        self.minsize(min(self._px(880), screen_width), min(self._px(620), screen_height))

    def _px(self, logical_pixels: int) -> int:
        return max(1, round(logical_pixels * self.ui_scale))

    def _make_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#17191d")
        style.configure("Toolbar.TFrame", background="#202329")
        style.configure("App.TLabel", background="#17191d", foreground="#e7e9ed")
        style.configure("Muted.TLabel", background="#17191d", foreground="#a8adb7")
        style.configure("Header.TLabel", background="#202329", foreground="#e7e9ed", font=("Segoe UI", 10, "bold"))
        style.configure("Zoom.TLabel", background="#202329", foreground="#8bd7ff", font=("Segoe UI", 10, "bold"))
        style.configure("App.TButton", font=("Segoe UI", 10), padding=(11, 7))
        style.configure("Keep.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 7))
        style.configure("App.TCheckbutton", background="#202329", foreground="#e7e9ed", font=("Segoe UI", 10))
        style.map("App.TCheckbutton", background=[("active", "#202329")], foreground=[("active", "#ffffff")])

    def _build_ui(self) -> None:
        # Keep every clickable control in one right-hand column. A native
        # PanedWindow sash lets the photographer widen or narrow that column.
        self.layout_paned = tk.PanedWindow(
            self,
            orient="horizontal",
            bg="#17191d",
            borderwidth=0,
            opaqueresize=True,
            sashcursor="sb_h_double_arrow",
            sashpad=0,
            sashrelief="raised",
            sashwidth=self._px(6),
            showhandle=True,
        )
        self.layout_paned.pack(fill="both", expand=True, padx=16, pady=16)
        self.layout_paned.bind("<ButtonRelease-1>", self._sidebar_resize_released)

        control_panel = ttk.Frame(self, style="Toolbar.TFrame", padding=(12, 14))
        self.control_panel = control_panel

        ttk.Label(control_panel, text="操作", style="Header.TLabel").pack(fill="x", pady=(0, 12))

        def add_control_button(text: str, command: object, style: str = "App.TButton") -> ttk.Button:
            button = ttk.Button(control_panel, text=text, style=style, command=command)
            button.pack(fill="x", pady=(0, 8))
            return button

        add_control_button("打开照片文件夹  O", self.open_folder)
        add_control_button("保留 / 取消  Space", self.toggle_keep, "Keep.TButton")
        self.keep_mode_button = add_control_button("模式：单文件  F", self.cycle_keep_mode)
        add_control_button("全不保留", self.clear_all_kept)
        add_control_button("重置模式", self.reset_all_pair_modes)
        add_control_button("导出保留照片  E", self.export_kept)
        ttk.Separator(control_panel, orient="horizontal").pack(fill="x", pady=(2, 12))
        add_control_button("适合屏幕  Z", self.zoom_fit)
        add_control_button("100%  1", self.zoom_actual)
        self.zoom_label = ttk.Label(control_panel, text="适合屏幕", style="Zoom.TLabel", anchor="center")
        self.zoom_label.pack(fill="x", pady=(0, 12))
        ttk.Checkbutton(
            control_panel,
            text="只看保留",
            variable=self.show_kept_only,
            style="App.TCheckbutton",
            command=self.toggle_filter,
        ).pack(fill="x", pady=(0, 8))
        ttk.Label(
            control_panel,
            text="拖动左侧分隔线调整宽度",
            style="Muted.TLabel",
            anchor="center",
        ).pack(side="bottom", fill="x", pady=(12, 0))

        main_column = ttk.Frame(self, style="App.TFrame")
        self.layout_paned.add(main_column, stretch="always", minsize=self._px(600))
        self.layout_paned.add(
            control_panel,
            stretch="never",
            minsize=self._px(SIDEBAR_WIDTH_MIN),
            width=self._px(self.sidebar_width),
        )
        self.after_idle(self._restore_sidebar_width)

        toolbar = ttk.Frame(main_column, style="Toolbar.TFrame", padding=(16, 10))
        toolbar.pack(fill="x")
        self.folder_label = ttk.Label(toolbar, text="尚未打开文件夹", style="Header.TLabel")
        self.folder_label.pack(side="left")

        self.preview_frame = tk.Frame(main_column, bg="#111317", highlightthickness=0)
        self.preview_frame.pack(fill="both", expand=True, pady=(12, 8))
        self.preview_canvas = tk.Canvas(
            self.preview_frame,
            bg="#111317",
            highlightthickness=0,
            cursor="arrow",
        )
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_canvas.create_text(
            0,
            0,
            text="打开一个照片文件夹开始选片",
            fill="#bdc3cd",
            font=("Segoe UI", 16),
            tags="preview-message",
        )
        self.preview_canvas.bind("<Configure>", self._queue_preview_resize)
        self.preview_canvas.bind("<MouseWheel>", self._preview_mouse_wheel)
        self.preview_canvas.bind("<ButtonPress-1>", self._preview_drag_start)
        self.preview_canvas.bind("<B1-Motion>", self._preview_drag_motion)
        self.preview_canvas.bind("<ButtonRelease-1>", self._preview_drag_end)

        info = ttk.Frame(main_column, style="App.TFrame", padding=(0, 5))
        info.pack(fill="x")
        self.status_label = ttk.Label(info, text="", style="App.TLabel")
        self.status_label.pack(side="left")
        self.preload_label = ttk.Label(info, text="", style="Muted.TLabel")
        self.preload_label.pack(side="left", padx=(18, 0))
        self.help_label = ttk.Label(
            info,
            text="[ ] 切换 · Space 保留 · F 模式 · 滚轮缩放 · Z 适合/100% · + − 微调",
            style="Muted.TLabel",
        )
        self.help_label.pack(side="right")

        thumbs_container = tk.Frame(main_column, bg="#202329", height=self._px(132))
        thumbs_container.pack(fill="x", pady=(0, 0))
        thumbs_container.pack_propagate(False)
        self.thumb_canvas = tk.Canvas(thumbs_container, bg="#202329", highlightthickness=0, height=self._px(132))
        self.thumb_scrollbar = ttk.Scrollbar(thumbs_container, orient="horizontal", command=self.thumb_canvas.xview)
        self.thumb_canvas.configure(xscrollcommand=self.thumb_scrollbar.set)
        self.thumb_canvas.pack(fill="both", expand=True)
        self.thumb_scrollbar.pack(fill="x")
        self.thumb_canvas.bind("<Button-1>", self._thumbnail_clicked)
        self.thumb_canvas.bind("<MouseWheel>", self._scroll_thumbnails)
        self.thumb_canvas.bind("<Configure>", lambda _event: self._schedule_thumbnail_render())

    def _restore_sidebar_width(self) -> None:
        """Place the sash after the first layout pass, using the saved logical width."""
        if not hasattr(self, "layout_paned"):
            return
        total_width = self.layout_paned.winfo_width()
        if total_width <= 1:
            return
        min_main = self._px(600)
        min_sidebar = self._px(SIDEBAR_WIDTH_MIN)
        desired_sidebar = self._px(self.sidebar_width)
        sash = total_width - desired_sidebar
        sash = max(min_main, min(sash, total_width - min_sidebar))
        try:
            self.layout_paned.sashpos(0, sash)
        except tk.TclError:
            pass

    def _sidebar_resize_released(self, _event: tk.Event) -> None:
        """Persist the user-selected sidebar width after dragging its sash."""
        if not hasattr(self, "control_panel"):
            return
        width = self.control_panel.winfo_width()
        if width <= 1:
            return
        logical_width = round(width / max(self.ui_scale, 1.0))
        logical_width = max(SIDEBAR_WIDTH_MIN, min(SIDEBAR_WIDTH_MAX, logical_width))
        if logical_width != self.sidebar_width:
            self.sidebar_width = logical_width
            self._save_sidebar_width()

    def _bind_keys(self) -> None:
        self.bind_all("<bracketleft>", lambda _event: self.change_index(-1))
        self.bind_all("<bracketright>", lambda _event: self.change_index(1))
        self.bind_all("<space>", self._on_space)
        self.bind_all("<f>", self._on_mode_key)
        self.bind_all("<F>", self._on_mode_key)
        self.bind_all("<o>", lambda _event: self.open_folder())
        self.bind_all("<O>", lambda _event: self.open_folder())
        self.bind_all("<e>", lambda _event: self.export_kept())
        self.bind_all("<E>", lambda _event: self.export_kept())
        self.bind_all("<z>", lambda _event: self.toggle_zoom())
        self.bind_all("<Z>", lambda _event: self.toggle_zoom())
        self.bind_all("<Key-1>", lambda _event: self.zoom_actual())
        self.bind_all("<plus>", lambda _event: self.zoom_step(1))
        self.bind_all("<KP_Add>", lambda _event: self.zoom_step(1))
        self.bind_all("<minus>", lambda _event: self.zoom_step(-1))
        self.bind_all("<KP_Subtract>", lambda _event: self.zoom_step(-1))
        self.bind_all("<Control-Shift-x>", self._on_clear_all_shortcut)
        self.bind_all("<Control-Shift-m>", self._on_reset_modes_shortcut)

    def _on_space(self, _event: tk.Event) -> str:
        # ttk buttons already consume Space through their class binding. If the
        # global binding toggled as well, one keypress would keep then unkeep.
        widget = _event.widget
        widget_class = widget.winfo_class() if hasattr(widget, "winfo_class") else ""
        if widget_class in {"TButton", "TCheckbutton", "Button", "Checkbutton"}:
            return "break"
        self.toggle_keep()
        return "break"

    def _on_mode_key(self, _event: tk.Event) -> str:
        self.cycle_keep_mode()
        return "break"

    def _on_clear_all_shortcut(self, _event: tk.Event) -> str:
        self.clear_all_kept()
        return "break"

    def _on_reset_modes_shortcut(self, _event: tk.Event) -> str:
        self.reset_all_pair_modes()
        return "break"

    @property
    def visible_items(self) -> list[PhotoGroup]:
        if not self.show_kept_only.get():
            return self.all_items
        return [item for item in self.all_items if item.key in self.kept]

    @staticmethod
    def _pair_mode_label(mode: str) -> str:
        return {"both": "RAW+JPG", "raw": "仅 RAW", "jpg": "仅 JPG"}.get(mode, "RAW+JPG")

    def _pair_mode(self, item: PhotoGroup) -> str:
        mode = self.pair_modes.get(item.key, "both")
        return mode if mode in {"both", "raw", "jpg"} else "both"

    def _update_keep_mode_ui(self) -> None:
        item = self.current_item
        if item is not None and item.paired_raw_jpeg:
            self.keep_mode_button.configure(text=f"模式：{self._pair_mode_label(self._pair_mode(item))}  F")
            self.keep_mode_button.state(["!disabled"])
        else:
            self.keep_mode_button.configure(text="模式：单文件  F")
            self.keep_mode_button.state(["disabled"])

    @property
    def current_item(self) -> PhotoGroup | None:
        items = self.visible_items
        if not items:
            return None
        self.index = min(max(self.index, 0), len(items) - 1)
        return items[self.index]

    def open_folder(self) -> None:
        chosen = filedialog.askdirectory(title="选择包含照片的文件夹", initialdir=str(self.folder) if self.folder else None)
        if not chosen:
            return
        folder = Path(chosen)
        try:
            paths = sorted(
                (path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS),
                key=lambda path: path.name.casefold(),
            )
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法读取这个文件夹：\n{exc}")
            return

        self.folder = folder
        self.all_items = build_photo_groups(paths)
        self.index = 0
        self._reset_thumbnail_state()
        self._start_jpeg_preload([path for path in paths if path.suffix.lower() in JPEG_EXTENSIONS])
        saved, saved_pair_modes = self._load_selection()
        current_keys = {item.key for item in self.all_items}
        self.kept = saved.intersection(current_keys)
        pair_keys = {item.key for item in self.all_items if item.paired_raw_jpeg}
        self.pair_modes = {
            key: mode for key, mode in saved_pair_modes.items() if key in pair_keys and mode in {"both", "raw", "jpg"}
        }
        self.folder_label.configure(text=folder.name or str(folder))
        if not self.all_items:
            self.current_source_image = None
            self.current_source_path = None
            self._show_preview_message("这个文件夹中没有受支持的照片")
            self._set_status("支持 JPG、JPEG、PNG、TIFF、DNG")
            self._update_keep_mode_ui()
            self._render_thumbnails()
            return
        self._show_current(center=True)

    def change_index(self, direction: int) -> None:
        items = self.visible_items
        if not items:
            return
        new_index = self.index + direction
        if 0 <= new_index < len(items):
            self.index = new_index
            self._show_current(center=True)

    def toggle_keep(self) -> None:
        item = self.current_item
        if item is None:
            return
        key = item.key
        if key in self.kept:
            self.kept.remove(key)
        else:
            self.kept.add(key)
            if item.paired_raw_jpeg:
                # The first Space press always means keep both originals.
                self.pair_modes.setdefault(key, "both")
        self._save_selection()
        if self.show_kept_only.get() and key not in self.kept:
            items = self.visible_items
            if not items:
                self.index = 0
                self._show_preview_message("没有保留的照片")
                self._set_status("保留 0 张照片")
                self._render_thumbnails()
                return
            self.index = min(self.index, len(items) - 1)
        self._show_current(center=False, reset_zoom=False)

    def cycle_keep_mode(self) -> None:
        item = self.current_item
        if item is None or not item.paired_raw_jpeg:
            return
        # The order follows the culling workflow: keep both by default, then
        # choose the convenient JPEG-only option, then RAW-only.
        modes = ["both", "jpg", "raw"]
        current = self._pair_mode(item)
        self.pair_modes[item.key] = modes[(modes.index(current) + 1) % len(modes)]
        self._save_selection()
        self._update_keep_mode_ui()
        self._set_status(self._status_text(item))
        self._render_thumbnails(center=False)

    def clear_all_kept(self) -> None:
        if not self.kept:
            messagebox.showinfo(APP_NAME, "当前没有已保留的照片。")
            return
        answer = messagebox.askyesno(
            APP_NAME,
            f"确定要取消全部 {len(self.kept)} 个保留项目吗？\n\n各组的 RAW/JPG 模式不会改变。",
        )
        if not answer:
            return
        self.kept.clear()
        self._save_selection()
        if self.show_kept_only.get():
            self.show_kept_only.set(False)
        self.index = min(self.index, max(len(self.visible_items) - 1, 0))
        if self.visible_items:
            self._show_current(center=True, reset_zoom=False)
        else:
            self._show_preview_message("打开一个照片文件夹开始选片")
            self._update_keep_mode_ui()
            self._render_thumbnails()

    def reset_all_pair_modes(self) -> None:
        pair_items = [item for item in self.all_items if item.paired_raw_jpeg]
        if not pair_items:
            messagebox.showinfo(APP_NAME, "当前文件夹没有 RAW+JPG 绑定组。")
            return
        answer = messagebox.askyesno(
            APP_NAME,
            f"确定要将 {len(pair_items)} 个 RAW+JPG 绑定组的模式全部重置为 RAW+JPG 吗？\n\n各组的保留/不保留状态不会改变。",
        )
        if not answer:
            return
        self.pair_modes = {item.key: "both" for item in pair_items}
        self._save_selection()
        self._update_keep_mode_ui()
        self._set_status(self._status_text(self.current_item))
        self._render_thumbnails(center=False)

    def toggle_filter(self) -> None:
        active = self.current_item
        items = self.visible_items
        if active is not None and active in items:
            self.index = items.index(active)
        else:
            self.index = 0
        if not items:
            self._show_preview_message("没有保留的照片")
            self._set_status("保留 0 张照片")
            self._update_keep_mode_ui()
            self._render_thumbnails()
            return
        self._show_current(center=True)

    def _show_current(self, center: bool, reset_zoom: bool = True) -> None:
        item = self.current_item
        if item is None:
            return
        path = item.primary
        self._cancel_preview_jobs()
        self._set_status("正在载入：" + path.name)
        self.update_idletasks()
        try:
            if self.current_source_path != path or self.current_source_image is None:
                with self._preview_levels_lock:
                    self._preview_levels.clear()
                self.current_source_image = self._load_image(path, thumbnail=False)
                self.current_source_path = path
            # A fast screen-resolution frame appears first. The original pixels
            # are then resampled with Lanczos in the background once idle.
            self._render_preview(reset_zoom=reset_zoom, interactive=True)
            self._schedule_preview_render(interactive=False, quality_delay=90)
        except Exception as exc:  # Do not stop an entire culling session for one bad image.
            self.current_source_image = None
            self.current_source_path = None
            self._show_preview_message(f"无法显示\n{path.name}\n\n{exc}")
        self._set_status(self._status_text(item))
        self._update_keep_mode_ui()
        self._render_thumbnails(center=center)

    def _load_image(self, path: Path, thumbnail: bool) -> Image.Image:
        if path.suffix.lower() != ".dng":
            if path.suffix.lower() in JPEG_EXTENSIONS:
                key = str(path.resolve())
                with self._jpeg_cache_lock:
                    cached = self.jpeg_cache.get(key)
                if cached is not None:
                    return cached
                image = self._read_raster_image(path)
                # The foreground image may be requested before the background worker reaches it.
                with self._jpeg_cache_lock:
                    return self.jpeg_cache.setdefault(key, image)
            return self._read_raster_image(path)

        if rawpy is None:
            raise RuntimeError("DNG 支持组件未安装")
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    with Image.open(io.BytesIO(thumb.data)) as embedded:
                        return ImageOps.exif_transpose(embedded).convert("RGB").copy()
                return Image.fromarray(thumb.data).convert("RGB")
            except Exception:
                # Some DNG files carry no embedded thumbnail. Decode a smaller RAW preview.
                array = raw.postprocess(
                    use_camera_wb=True,
                    no_auto_bright=False,
                    # A screen preview has no benefit from decoding every RAW pixel.
                    half_size=True,
                    output_bps=8,
                )
                return Image.fromarray(array).convert("RGB")

    @staticmethod
    def _read_raster_image(path: Path) -> Image.Image:
        """Decode a normal image once and detach it from its file handle."""
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
            return image.convert("RGB").copy()

    @staticmethod
    def _fit_for_display(image: Image.Image, max_width: int, max_height: int) -> Image.Image:
        """Return a display-sized copy without modifying an image held in the JPEG memory cache."""
        scale = min(max_width / image.width, max_height / image.height, 1.0)
        if scale >= 1.0:
            return image
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        return image.resize(size, Image.Resampling.LANCZOS)

    def _show_preview_message(self, message: str) -> None:
        self._cancel_preview_jobs()
        self.preview_photo = None
        self.preview_image_item = None
        self._preview_item_origin = None
        self._preview_item_size = None
        self._drag_state = None
        self.preview_canvas.delete("all")
        width = max(self.preview_canvas.winfo_width(), 1)
        height = max(self.preview_canvas.winfo_height(), 1)
        self.preview_canvas.create_text(
            width / 2,
            height / 2,
            text=message,
            fill="#bdc3cd",
            font=("Segoe UI", 16),
            justify="center",
        )
        self.preview_canvas.configure(cursor="arrow")
        self.zoom_label.configure(text="—")

    def _render_preview(self, reset_zoom: bool, interactive: bool = False) -> None:
        """Render one immediate frame; deferred frames use the background worker."""
        image = self.current_source_image
        path = self.current_source_path
        if image is None or path is None:
            return
        geometry = self._preview_geometry(reset_zoom=reset_zoom, interactive=interactive)
        frame = self._build_preview_frame(image, path, geometry, interactive)
        self._apply_preview_frame(frame, geometry)

    def _preview_geometry(self, reset_zoom: bool, interactive: bool) -> PreviewGeometry:
        """Calculate an oversized source crop so normal drags need no rerender."""
        image = self.current_source_image
        if image is None:
            raise RuntimeError("没有可显示的照片")
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        previous_fit = self.fit_scale
        new_fit = min(canvas_width / image.width, canvas_height / image.height, 1.0)
        was_at_fit = abs(self.zoom_scale - previous_fit) < 0.0001
        self.fit_scale = new_fit
        if reset_zoom or was_at_fit:
            self.zoom_scale = new_fit
            self.pan_x = 0.0
            self.pan_y = 0.0
        else:
            self.zoom_scale = max(new_fit, min(4.0, self.zoom_scale))
        self._constrain_pan(canvas_width, canvas_height)

        display_width = image.width * self.zoom_scale
        display_height = image.height * self.zoom_scale
        left = canvas_width / 2 + self.pan_x - display_width / 2
        top = canvas_height / 2 + self.pan_y - display_height / 2
        overscan = min(max(canvas_width, canvas_height) * PREVIEW_OVERSCAN, PREVIEW_OVERSCAN_MAX_PX)
        source_left = max(0, math.floor((-overscan - left) / self.zoom_scale))
        source_top = max(0, math.floor((-overscan - top) / self.zoom_scale))
        source_right = min(image.width, math.ceil((canvas_width + overscan - left) / self.zoom_scale))
        source_bottom = min(image.height, math.ceil((canvas_height + overscan - top) / self.zoom_scale))
        if source_right <= source_left or source_bottom <= source_top:
            raise RuntimeError("无法显示这个缩放区域")

        factor = self._interactive_downsample_factor(image) if interactive else 1
        level_left = max(0, source_left // factor)
        level_top = max(0, source_top // factor)
        level_right = min(math.ceil(image.width / factor), math.ceil(source_right / factor))
        level_bottom = min(math.ceil(image.height / factor), math.ceil(source_bottom / factor))
        if level_right <= level_left or level_bottom <= level_top:
            raise RuntimeError("无法显示这个缩放区域")
        target_width = max(1, round((level_right - level_left) * self.zoom_scale * factor))
        target_height = max(1, round((level_bottom - level_top) * self.zoom_scale * factor))
        return PreviewGeometry(
            source_box=(level_left, level_top, level_right, level_bottom),
            target_size=(target_width, target_height),
            origin=(left + level_left * factor * self.zoom_scale, top + level_top * factor * self.zoom_scale),
            downsample_factor=factor,
        )

    def _interactive_downsample_factor(self, image: Image.Image) -> int:
        """Choose a pyramid level close to screen resolution for responsive input."""
        factor = 1
        max_factor = min(16, max(1, min(image.width, image.height)))
        while factor * 2 <= max_factor and self.zoom_scale * factor * 2 <= 1.0:
            factor *= 2
        return factor

    def _preview_source_for(self, image: Image.Image, path: Path, factor: int, interactive: bool) -> Image.Image:
        if factor == 1:
            return image
        key = (str(path.resolve()), factor)
        with self._preview_levels_lock:
            cached = self._preview_levels.get(key)
        if cached is not None:
            return cached
        size = (max(1, math.ceil(image.width / factor)), max(1, math.ceil(image.height / factor)))
        level = image.resize(size, Image.Resampling.BILINEAR if interactive else Image.Resampling.LANCZOS)
        with self._preview_levels_lock:
            return self._preview_levels.setdefault(key, level)

    def _build_preview_frame(self, image: Image.Image, path: Path, geometry: PreviewGeometry, interactive: bool) -> Image.Image:
        source = self._preview_source_for(image, path, geometry.downsample_factor, interactive)
        crop = source.crop(geometry.source_box)
        if crop.size != geometry.target_size:
            crop = crop.resize(
                geometry.target_size,
                Image.Resampling.BILINEAR if interactive else Image.Resampling.LANCZOS,
            )
        return crop

    def _apply_preview_frame(self, frame: Image.Image, geometry: PreviewGeometry) -> None:
        self.preview_photo = ImageTk.PhotoImage(frame)
        if self.preview_image_item is None:
            self.preview_canvas.delete("preview-message")
            self.preview_image_item = self.preview_canvas.create_image(
                round(geometry.origin[0]),
                round(geometry.origin[1]),
                image=self.preview_photo,
                anchor="nw",
                tags="preview-image",
            )
        else:
            self.preview_canvas.itemconfigure(self.preview_image_item, image=self.preview_photo)
            self.preview_canvas.coords(self.preview_image_item, round(geometry.origin[0]), round(geometry.origin[1]))
        self._preview_item_origin = geometry.origin
        self._preview_item_size = frame.size
        self._update_zoom_label()
        self.preview_canvas.configure(cursor="fleur" if self.zoom_scale > self.fit_scale + 0.0001 else "arrow")

    def _cancel_preview_jobs(self) -> None:
        if self._interactive_render_job is not None:
            self.after_cancel(self._interactive_render_job)
            self._interactive_render_job = None
        if self._quality_render_job is not None:
            self.after_cancel(self._quality_render_job)
            self._quality_render_job = None
        self._preview_render_generation += 1
        for future in self._preview_futures:
            future.cancel()
        self._preview_futures.clear()

    def _schedule_preview_render(self, interactive: bool = True, quality_delay: int = PREVIEW_QUALITY_DELAY_MS) -> None:
        """Coalesce input; only the newest viewport is allowed to reach the canvas."""
        self._preview_render_generation += 1
        for future in self._preview_futures:
            future.cancel()
        self._preview_futures = {future for future in self._preview_futures if not future.done()}
        if self._interactive_render_job is not None:
            self.after_cancel(self._interactive_render_job)
        if self._quality_render_job is not None:
            self.after_cancel(self._quality_render_job)
        self._interactive_render_job = None
        self._quality_render_job = None
        if interactive:
            self._interactive_render_job = self.after(PREVIEW_INTERACTIVE_DELAY_MS, self._render_interactive_frame)
        self._quality_render_job = self.after(quality_delay, self._render_quality_frame)

    def _render_interactive_frame(self) -> None:
        self._interactive_render_job = None
        self._request_preview_render(interactive=True)

    def _render_quality_frame(self) -> None:
        self._quality_render_job = None
        self._request_preview_render(interactive=False)

    def _request_preview_render(self, interactive: bool) -> None:
        """Crop and resample outside Tk's event loop; only Tk image creation stays on the UI thread."""
        image = self.current_source_image
        path = self.current_source_path
        if image is None or path is None:
            return
        self._preview_render_generation += 1
        generation = self._preview_render_generation
        for future in self._preview_futures:
            future.cancel()
        self._preview_futures = {future for future in self._preview_futures if not future.done()}
        try:
            geometry = self._preview_geometry(reset_zoom=False, interactive=interactive)
        except Exception:
            return
        future = self._preview_executor.submit(
            self._preview_render_worker,
            generation,
            str(path.resolve()),
            image,
            path,
            geometry,
            interactive,
        )
        self._preview_futures.add(future)

    def _preview_render_worker(
        self,
        generation: int,
        path_key: str,
        image: Image.Image,
        path: Path,
        geometry: PreviewGeometry,
        interactive: bool,
    ) -> None:
        try:
            frame = self._build_preview_frame(image, path, geometry, interactive)
            self._preview_render_events.put((generation, path_key, frame, geometry, None))
        except Exception as exc:
            self._preview_render_events.put((generation, path_key, None, None, exc))

    def _poll_preview_render_events(self) -> None:
        newest: tuple[int, str, Image.Image | None, PreviewGeometry | None, Exception | None] | None = None
        while True:
            try:
                event = self._preview_render_events.get_nowait()
            except queue.Empty:
                break
            if event[0] == self._preview_render_generation:
                newest = event
        if newest is not None:
            _, path_key, frame, geometry, _error = newest
            current_path = self.current_source_path
            if frame is not None and geometry is not None and current_path is not None and str(current_path.resolve()) == path_key:
                self._apply_preview_frame(frame, geometry)
        self._preview_futures = {future for future in self._preview_futures if not future.done()}
        if self.winfo_exists():
            self._preview_poll_job = self.after(16, self._poll_preview_render_events)

    def _constrain_pan(self, canvas_width: int, canvas_height: int) -> None:
        image = self.current_source_image
        if image is None:
            return
        display_width = image.width * self.zoom_scale
        display_height = image.height * self.zoom_scale
        max_x = max(0.0, (display_width - canvas_width) / 2)
        max_y = max(0.0, (display_height - canvas_height) / 2)
        self.pan_x = max(-max_x, min(max_x, self.pan_x))
        self.pan_y = max(-max_y, min(max_y, self.pan_y))

    def _update_zoom_label(self) -> None:
        if self.current_source_image is None:
            self.zoom_label.configure(text="—")
            return
        percent = round(self.zoom_scale * 100)
        if abs(self.zoom_scale - self.fit_scale) < 0.0001:
            self.zoom_label.configure(text=f"适合 {percent}%")
        else:
            self.zoom_label.configure(text=f"{percent}%")

    def _set_zoom(self, scale: float, anchor: tuple[float, float] | None = None) -> None:
        image = self.current_source_image
        if image is None:
            return
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        self._constrain_pan(canvas_width, canvas_height)
        old_scale = self.zoom_scale
        new_scale = max(self.fit_scale, min(4.0, scale))
        if abs(new_scale - old_scale) < 0.000001:
            return
        if anchor is not None:
            anchor_x, anchor_y = anchor
            old_left = canvas_width / 2 + self.pan_x - image.width * old_scale / 2
            old_top = canvas_height / 2 + self.pan_y - image.height * old_scale / 2
            source_x = max(0.0, min(float(image.width), (anchor_x - old_left) / old_scale))
            source_y = max(0.0, min(float(image.height), (anchor_y - old_top) / old_scale))
            self.pan_x = anchor_x - source_x * new_scale - canvas_width / 2 + image.width * new_scale / 2
            self.pan_y = anchor_y - source_y * new_scale - canvas_height / 2 + image.height * new_scale / 2
        self.zoom_scale = new_scale
        self._schedule_preview_render()

    def zoom_fit(self) -> None:
        if self.current_source_image is None:
            return
        self.zoom_scale = self.fit_scale
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._cancel_preview_jobs()
        self._render_preview(reset_zoom=False, interactive=True)
        self._schedule_preview_render(interactive=False, quality_delay=80)

    def zoom_actual(self) -> None:
        if self.current_source_image is None:
            return
        self.zoom_scale = max(self.fit_scale, 1.0)
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._cancel_preview_jobs()
        self._render_preview(reset_zoom=False, interactive=True)
        self._schedule_preview_render(interactive=False, quality_delay=80)

    def toggle_zoom(self) -> None:
        if self.current_source_image is None:
            return
        if abs(self.zoom_scale - self.fit_scale) < 0.0001:
            self.zoom_actual()
        else:
            self.zoom_fit()

    def zoom_step(self, direction: int) -> None:
        if self.current_source_image is None:
            return
        factor = 1.25 if direction > 0 else 1 / 1.25
        center = (self.preview_canvas.winfo_width() / 2, self.preview_canvas.winfo_height() / 2)
        self._set_zoom(self.zoom_scale * factor, center)

    def _preview_mouse_wheel(self, event: tk.Event) -> str:
        if self.current_source_image is None or event.delta == 0:
            return "break"
        steps = event.delta / 120
        self._set_zoom(self.zoom_scale * (1.25 ** steps), (event.x, event.y))
        return "break"

    def _preview_drag_start(self, event: tk.Event) -> None:
        if self.current_source_image is None or self.zoom_scale <= self.fit_scale + 0.0001:
            self._drag_state = None
            return
        self._drag_state = (event.x, event.y, self.pan_x, self.pan_y)

    def _preview_drag_motion(self, event: tk.Event) -> None:
        if self._drag_state is None:
            return
        start_x, start_y, start_pan_x, start_pan_y = self._drag_state
        previous_pan_x, previous_pan_y = self.pan_x, self.pan_y
        self.pan_x = start_pan_x + event.x - start_x
        self.pan_y = start_pan_y + event.y - start_y
        self._constrain_pan(max(self.preview_canvas.winfo_width(), 1), max(self.preview_canvas.winfo_height(), 1))
        self._move_preview_item(self.pan_x - previous_pan_x, self.pan_y - previous_pan_y)
        if self._preview_frame_needs_refresh():
            self._schedule_preview_render(interactive=True, quality_delay=180)
        else:
            self._schedule_preview_render(interactive=False, quality_delay=180)

    def _preview_drag_end(self, _event: tk.Event) -> None:
        self._drag_state = None
        self._schedule_preview_render(interactive=True, quality_delay=70)

    def _move_preview_item(self, dx: float, dy: float) -> None:
        if self.preview_image_item is None or (dx == 0 and dy == 0):
            return
        self.preview_canvas.move(self.preview_image_item, dx, dy)
        if self._preview_item_origin is not None:
            self._preview_item_origin = (self._preview_item_origin[0] + dx, self._preview_item_origin[1] + dy)

    def _preview_frame_needs_refresh(self) -> bool:
        if self.preview_image_item is None:
            return True
        bounds = self.preview_canvas.bbox(self.preview_image_item)
        if bounds is None:
            return True
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        safety = max(80, round(max(canvas_width, canvas_height) * 0.15))
        left, top, right, bottom = bounds
        return left > -safety or top > -safety or right < canvas_width + safety or bottom < canvas_height + safety

    def _start_jpeg_preload(self, jpeg_paths: list[Path]) -> None:
        """Decode all JPEGs in a worker so later navigation does not wait for disk I/O."""
        self._preload_generation += 1
        generation = self._preload_generation
        self._preload_done = not jpeg_paths
        with self._jpeg_cache_lock:
            self.jpeg_cache = {}

        if not jpeg_paths:
            self.preload_label.configure(text="")
            return
        self.preload_label.configure(text=f"正在预载 JPG：0 / {len(jpeg_paths)}")
        cache = self.jpeg_cache
        worker = Thread(
            target=self._preload_jpegs,
            args=(generation, jpeg_paths, cache),
            daemon=True,
            name="photo-culler-jpeg-preload",
        )
        worker.start()
        self.after(75, lambda: self._poll_preload_events(generation))

    def _preload_jpegs(self, generation: int, jpeg_paths: list[Path], cache: dict[str, Image.Image]) -> None:
        total = len(jpeg_paths)
        for number, path in enumerate(jpeg_paths, start=1):
            if generation != self._preload_generation:
                return
            try:
                image = self._read_raster_image(path)
                with self._jpeg_cache_lock:
                    if generation != self._preload_generation:
                        return
                    cache.setdefault(str(path.resolve()), image)
            except (OSError, UnidentifiedImageError):
                # A malformed JPEG remains available to the normal error display path.
                pass
            if number == 1 or number == total or number % 10 == 0:
                self._preload_events.put((generation, number, total, False))
        self._preload_events.put((generation, total, total, True))

    def _poll_preload_events(self, generation: int) -> None:
        if generation != self._preload_generation:
            return
        latest: tuple[int, int, int, bool] | None = None
        while True:
            try:
                event = self._preload_events.get_nowait()
            except queue.Empty:
                break
            if event[0] == generation:
                latest = event
        if latest is not None:
            _, completed, total, done = latest
            self._preload_done = done
            if done:
                self.preload_label.configure(text=f"JPG 已预载：{completed} 张")
            else:
                self.preload_label.configure(text=f"正在预载 JPG：{completed} / {total}")
        if not self._preload_done:
            self.after(75, lambda: self._poll_preload_events(generation))

    def _schedule_thumbnail_render(self, center: bool = False) -> None:
        """Coalesce scrollbar, resize, and worker-completion redraw requests."""
        self._thumbnail_center_pending = self._thumbnail_center_pending or center
        if self._thumbnail_render_job is None:
            self._thumbnail_render_job = self.after_idle(self._run_scheduled_thumbnail_render)

    def _run_scheduled_thumbnail_render(self) -> None:
        self._thumbnail_render_job = None
        center = self._thumbnail_center_pending
        self._thumbnail_center_pending = False
        self._render_thumbnails(center=center)

    def _thumbnail_cache_key(self, path: Path) -> ThumbnailKey:
        try:
            path_key = str(path.resolve())
        except OSError:
            path_key = str(path)
        try:
            stat = path.stat()
        except OSError:
            return path_key, 0, 0, self.thumb_width, self.thumb_height
        return path_key, stat.st_mtime_ns, stat.st_size, self.thumb_width, self.thumb_height

    def _render_thumbnails(self, center: bool = False) -> None:
        """Keep only a small visible/overscan set of Canvas items alive and reuse them."""
        items = self.visible_items
        if not items:
            self.thumb_canvas.configure(scrollregion=(0, 0, 1, self._px(120)))
            self._clear_thumbnail_canvas_items()
            return

        canvas_width = max(self.thumb_canvas.winfo_width(), self.thumb_slot * 5)
        total_width = len(items) * self.thumb_slot
        self.thumb_canvas.configure(scrollregion=(0, 0, total_width, self._px(120)))
        if center:
            left = max(0, self.index * self.thumb_slot + self.thumb_slot / 2 - canvas_width / 2)
            max_left = max(0, total_width - canvas_width)
            self.thumb_canvas.xview_moveto(min(left, max_left) / max(total_width, 1))

        view_left = self.thumb_canvas.canvasx(0)
        view_right = view_left + canvas_width
        first = max(0, int(view_left // self.thumb_slot) - THUMB_RENDER_OVERSCAN)
        last = min(len(items), int(math.ceil(view_right / self.thumb_slot)) + THUMB_RENDER_OVERSCAN)
        active_keys = {items[displayed_index].key for displayed_index in range(first, last)}

        for group_key in list(self._thumbnail_items):
            if group_key not in active_keys:
                self._delete_thumbnail_canvas_item(self._thumbnail_items.pop(group_key))

        for displayed_index in range(first, last):
            item = items[displayed_index]
            entry = self._thumbnail_items.get(item.key)
            if entry is None:
                entry = self._create_thumbnail_canvas_items(item.key)
                self._thumbnail_items[item.key] = entry
            self._position_thumbnail_canvas_items(entry, displayed_index)
            self._update_thumbnail_canvas_items(entry, item, displayed_index, self._thumbnail_cache_key(item.primary))

    def _create_thumbnail_canvas_items(self, group_key: str) -> ThumbnailCanvasItems:
        return ThumbnailCanvasItems(
            group_key=group_key,
            rect_id=self.thumb_canvas.create_rectangle(
                0,
                0,
                0,
                0,
                fill="#15171b",
                outline="#343944",
                width=1,
            ),
            image_id=self.thumb_canvas.create_image(0, 0, image=self._thumbnail_placeholder_photo),
            marker_id=self.thumb_canvas.create_text(
                0,
                0,
                text="",
                fill="#ffd35a",
                font=("Segoe UI Symbol", 12, "bold"),
                anchor="nw",
            ),
            mode_id=self.thumb_canvas.create_text(
                0,
                0,
                text="",
                fill="#8bd7ff",
                font=("Segoe UI", 7, "bold"),
                anchor="ne",
            ),
            label_id=self.thumb_canvas.create_text(
                0,
                0,
                text="",
                fill="#d9dde5",
                font=("Segoe UI", 8),
            ),
            photo=self._thumbnail_placeholder_photo,
        )

    def _position_thumbnail_canvas_items(self, entry: ThumbnailCanvasItems, displayed_index: int) -> None:
        x = displayed_index * self.thumb_slot + self.thumb_slot // 2
        self.thumb_canvas.coords(
            entry.rect_id,
            x - self.thumb_width // 2 - self._px(3),
            self._px(9),
            x + self.thumb_width // 2 + self._px(3),
            self._px(105),
        )
        self.thumb_canvas.coords(entry.image_id, x, self._px(57))
        self.thumb_canvas.coords(entry.marker_id, x - self._px(59), self._px(18))
        self.thumb_canvas.coords(entry.mode_id, x + self._px(59), self._px(18))
        self.thumb_canvas.coords(entry.label_id, x, self._px(115))
        if entry.error_id is not None:
            self.thumb_canvas.coords(entry.error_id, x, self._px(57))

    def _update_thumbnail_canvas_items(
        self,
        entry: ThumbnailCanvasItems,
        item: PhotoGroup,
        displayed_index: int,
        cache_key: ThumbnailKey,
    ) -> None:
        selected = displayed_index == self.index
        self.thumb_canvas.itemconfigure(
            entry.rect_id,
            outline="#4f9cff" if selected else "#343944",
            width=3 if selected else 1,
        )
        self.thumb_canvas.itemconfigure(entry.marker_id, text="★" if item.key in self.kept else "")
        mode_text = ""
        if item.paired_raw_jpeg:
            mode_text = self._pair_mode_label(self._pair_mode(item)) if item.key in self.kept else "未保留"
        self.thumb_canvas.itemconfigure(entry.mode_id, text=mode_text)
        label = item.primary.name
        if len(label) > 18:
            label = label[:16] + "…"
        self.thumb_canvas.itemconfigure(entry.label_id, text=label)

        photo = self._thumbnail_cache_get(cache_key)
        if photo is not None:
            if entry.photo is not photo:
                self.thumb_canvas.itemconfigure(entry.image_id, image=photo)
                entry.photo = photo
            self._remove_thumbnail_error(entry)
            entry.thumb_key = cache_key
            return

        if entry.photo is not self._thumbnail_placeholder_photo:
            self.thumb_canvas.itemconfigure(entry.image_id, image=self._thumbnail_placeholder_photo)
            entry.photo = self._thumbnail_placeholder_photo
        entry.thumb_key = cache_key
        if cache_key in self._thumbnail_errors:
            x = displayed_index * self.thumb_slot + self.thumb_slot // 2
            if entry.error_id is None:
                entry.error_id = self.thumb_canvas.create_text(
                    x,
                    self._px(57),
                    text="无法预览",
                    fill="#aab0ba",
                    font=("Segoe UI", 9),
                )
            else:
                self.thumb_canvas.itemconfigure(entry.error_id, text="无法预览")
                self.thumb_canvas.coords(entry.error_id, x, self._px(57))
        else:
            self._remove_thumbnail_error(entry)
            self._request_thumbnail_job(cache_key, item.primary)

    def _thumbnail_cache_get(self, cache_key: ThumbnailKey) -> ImageTk.PhotoImage | None:
        photo = self.thumbnail_cache.get(cache_key)
        if photo is not None:
            self.thumbnail_cache.move_to_end(cache_key)
        return photo

    def _thumbnail_cache_put(self, cache_key: ThumbnailKey, photo: ImageTk.PhotoImage) -> None:
        self.thumbnail_cache[cache_key] = photo
        self.thumbnail_cache.move_to_end(cache_key)
        while len(self.thumbnail_cache) > THUMB_CACHE_LIMIT:
            self.thumbnail_cache.popitem(last=False)

    def _request_thumbnail_job(self, cache_key: ThumbnailKey, path: Path) -> None:
        if cache_key in self.thumbnail_cache or cache_key in self._thumbnail_jobs or cache_key in self._thumbnail_errors:
            return
        future = self._thumbnail_executor.submit(
            self._thumbnail_worker,
            self._thumbnail_generation,
            cache_key,
            path,
        )
        self._thumbnail_jobs[cache_key] = future

    def _thumbnail_worker(self, generation: int, cache_key: ThumbnailKey, path: Path) -> None:
        try:
            image = self._load_image(path, thumbnail=True)
            image = self._fit_for_display(image, self.thumb_width, self.thumb_height)
            if image.width < self.thumb_width or image.height < self.thumb_height:
                background = Image.new("RGB", (self.thumb_width, self.thumb_height), "#202329")
                background.paste(image, ((self.thumb_width - image.width) // 2, (self.thumb_height - image.height) // 2))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            self._thumbnail_events.put((generation, cache_key, image, None))
        except Exception as exc:
            self._thumbnail_events.put((generation, cache_key, None, exc))

    def _poll_thumbnail_events(self) -> None:
        changed = False
        while True:
            try:
                generation, cache_key, image, _error = self._thumbnail_events.get_nowait()
            except queue.Empty:
                break
            if generation != self._thumbnail_generation:
                continue
            self._thumbnail_jobs.pop(cache_key, None)
            if image is None:
                self._thumbnail_errors.add(cache_key)
            else:
                try:
                    self._thumbnail_cache_put(cache_key, ImageTk.PhotoImage(image))
                    self._thumbnail_errors.discard(cache_key)
                except Exception:
                    self._thumbnail_errors.add(cache_key)
            changed = True
        if changed:
            self._schedule_thumbnail_render()
        if self.winfo_exists():
            self._thumbnail_poll_job = self.after(THUMB_RENDER_POLL_MS, self._poll_thumbnail_events)

    def _remove_thumbnail_error(self, entry: ThumbnailCanvasItems) -> None:
        if entry.error_id is not None:
            self.thumb_canvas.delete(entry.error_id)
            entry.error_id = None

    def _delete_thumbnail_canvas_item(self, entry: ThumbnailCanvasItems) -> None:
        self.thumb_canvas.delete(entry.rect_id, entry.image_id, entry.marker_id, entry.mode_id, entry.label_id)
        self._remove_thumbnail_error(entry)
        entry.photo = None

    def _clear_thumbnail_canvas_items(self) -> None:
        for entry in self._thumbnail_items.values():
            self._delete_thumbnail_canvas_item(entry)
        self._thumbnail_items.clear()

    def _cancel_thumbnail_jobs(self) -> None:
        if self._thumbnail_render_job is not None:
            try:
                self.after_cancel(self._thumbnail_render_job)
            except tk.TclError:
                pass
            self._thumbnail_render_job = None
        self._thumbnail_center_pending = False
        self._thumbnail_generation += 1
        for future in self._thumbnail_jobs.values():
            future.cancel()
        self._thumbnail_jobs.clear()

    def _reset_thumbnail_state(self) -> None:
        self._cancel_thumbnail_jobs()
        self.thumbnail_cache.clear()
        self._thumbnail_errors.clear()
        self._clear_thumbnail_canvas_items()

    def _thumbnail_clicked(self, event: tk.Event) -> None:
        items = self.visible_items
        if not items:
            return
        clicked = int(self.thumb_canvas.canvasx(event.x) // self.thumb_slot)
        if 0 <= clicked < len(items):
            self.index = clicked
            self._show_current(center=False)

    def _scroll_thumbnails(self, event: tk.Event) -> str:
        self.thumb_canvas.xview_scroll(int(-event.delta / 120) * 3, "units")
        self._schedule_thumbnail_render()
        return "break"

    def _queue_preview_resize(self, _event: tk.Event) -> None:
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(220, self._refresh_after_resize)

    def _refresh_after_resize(self) -> None:
        self._resize_job = None
        # Keep the current zoom state and avoid reopening a DNG while the layout is settling.
        if self.current_source_image is not None and self.preview_photo is not None:
            self._cancel_preview_jobs()
            self._render_preview(reset_zoom=False, interactive=True)
            self._schedule_preview_render(interactive=False, quality_delay=90)

    def export_kept(self) -> None:
        if not self.kept:
            messagebox.showinfo(APP_NAME, "还没有保留照片。按 Space 标记后再导出。")
            return
        destination = filedialog.askdirectory(title="选择导出保留照片的文件夹")
        if not destination:
            return
        destination_path = Path(destination)
        kept_items = [item for item in self.all_items if item.key in self.kept]
        sources = [path for item in kept_items for path in self._selected_members(item)]
        answer = messagebox.askyesno(
            APP_NAME,
            f"将导出 {len(kept_items)} 个保留项目（共 {len(sources)} 个原始文件）到：\n{destination_path}\n\nRAW+JPG 组按当前模式导出。原照片不会被移动或修改。继续吗？",
        )
        if not answer:
            return
        copied = 0
        failures: list[str] = []
        for source in sources:
            try:
                target = self._unique_destination(destination_path, source.name)
                shutil.copy2(source, target)
                copied += 1
            except OSError as exc:
                failures.append(f"{source.name}: {exc}")
            self._set_status(f"正在导出 {copied}/{len(sources)}…")
            self.update_idletasks()
        if failures:
            messagebox.showwarning(APP_NAME, f"已复制 {copied} 张；{len(failures)} 张未能复制。\n\n" + "\n".join(failures[:3]))
        else:
            messagebox.showinfo(APP_NAME, f"已复制 {copied} 张保留照片。")
        self._set_status(self._status_text(self.current_item))

    def _selected_members(self, item: PhotoGroup) -> tuple[Path, ...]:
        if not item.paired_raw_jpeg:
            return item.members
        mode = self._pair_mode(item)
        if mode == "raw":
            return tuple(path for path in item.members if path.suffix.lower() == ".dng")
        if mode == "jpg":
            return tuple(path for path in item.members if path.suffix.lower() in JPEG_EXTENSIONS)
        return item.members

    @staticmethod
    def _unique_destination(folder: Path, filename: str) -> Path:
        candidate = folder / filename
        if not candidate.exists():
            return candidate
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        number = 1
        while True:
            candidate = folder / f"{stem} ({number}){suffix}"
            if not candidate.exists():
                return candidate
            number += 1

    def _settings_file(self) -> Path:
        appdata = Path.home() / "AppData" / "Local" / "PhotoCuller"
        return appdata / "settings.json"

    def _load_sidebar_width(self) -> int:
        try:
            data = json.loads(self._settings_file().read_text(encoding="utf-8"))
            width = int(data.get("sidebar_width", SIDEBAR_WIDTH_DEFAULT))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            width = SIDEBAR_WIDTH_DEFAULT
        return max(SIDEBAR_WIDTH_MIN, min(SIDEBAR_WIDTH_MAX, width))

    def _save_sidebar_width(self) -> None:
        try:
            settings_file = self._settings_file()
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text(
                json.dumps({"sidebar_width": self.sidebar_width}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _selection_file(self) -> Path:
        assert self.folder is not None
        digest = hashlib.sha256(str(self.folder.resolve()).encode("utf-8")).hexdigest()[:20]
        appdata = Path.home() / "AppData" / "Local" / "PhotoCuller" / "selections"
        appdata.mkdir(parents=True, exist_ok=True)
        return appdata / f"{digest}.json"

    def _load_selection(self) -> tuple[set[str], dict[str, str]]:
        if self.folder is None:
            return set(), {}
        try:
            data = json.loads(self._selection_file().read_text(encoding="utf-8"))
            kept = {key for key in data.get("kept", []) if isinstance(key, str)}
            raw_pair_modes = data.get("pair_modes", {})
            if not isinstance(raw_pair_modes, dict):
                raw_pair_modes = {}
            pair_modes = {key: mode for key, mode in raw_pair_modes.items() if isinstance(key, str) and isinstance(mode, str)}
            return kept, pair_modes
        except (OSError, ValueError, json.JSONDecodeError):
            return set(), {}

    def _save_selection(self) -> None:
        if self.folder is None:
            return
        payload = {
            "folder": str(self.folder),
            "kept": sorted(self.kept),
            "pair_modes": dict(sorted(self.pair_modes.items())),
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            self._selection_file().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _status_text(self, item: PhotoGroup | None) -> str:
        if item is None:
            return f"保留 {len(self.kept)} 张照片"
        prefix = "★ 已保留" if item.key in self.kept else "未保留"
        paired = f"    绑定组：{self._pair_mode_label(self._pair_mode(item))}" if item.paired_raw_jpeg and item.key in self.kept else ("    RAW+JPG 绑定组" if item.paired_raw_jpeg else "")
        return f"{self.index + 1} / {len(self.visible_items)}    {prefix}    已保留 {len(self.kept)} 个项目{paired}    {item.primary.name}"

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _on_close(self) -> None:
        """Stop background preview jobs before Tk tears down its image runtime."""
        self._cancel_preview_jobs()
        self._cancel_thumbnail_jobs()
        if getattr(self, "_preview_poll_job", None) is not None:
            try:
                self.after_cancel(self._preview_poll_job)
            except tk.TclError:
                pass
        if getattr(self, "_thumbnail_poll_job", None) is not None:
            try:
                self.after_cancel(self._thumbnail_poll_job)
            except tk.TclError:
                pass
        self._preview_executor.shutdown(wait=False, cancel_futures=True)
        self._thumbnail_executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()


if __name__ == "__main__":
    try:
        if "--self-test" in sys.argv:
            # Used by the build verification command; no window is created.
            probe = tk.Tcl()
            probe.eval("package require Tk")
            print("Photo Culler runtime OK")
            raise SystemExit(0)
        enable_windows_high_dpi()
        PhotoCuller().mainloop()
    except Exception as error:
        # When distributed without a console, show an actionable error instead of silently exiting.
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, f"程序启动失败：\n{error}")
        root.destroy()
        raise
