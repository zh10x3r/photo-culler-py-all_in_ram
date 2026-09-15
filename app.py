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
from time import monotonic
from typing import Any, Callable


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

try:
    # The production window now uses the same Qt/OpenGL path as the validated
    # prototype.  Keep the import guarded so the legacy Tk helpers and their
    # filesystem tests remain importable in a minimal Python environment.
    os.environ.setdefault("QT_API", "pyside6")
    import numpy as np
    from OpenGL import GL
    from PySide6.QtCore import (
        QAbstractListModel,
        QEventLoop,
        QItemSelectionModel,
        QModelIndex,
        QPoint,
        QRect,
        QSize,
        Qt,
        QTimer,
        QSortFilterProxyModel,
    )
    from PySide6.QtGui import QAction, QColor, QFont, QImage, QKeySequence, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFrame,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QListView,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSizePolicy,
        QSplitter,
        QStyledItemDelegate,
        QStyle,
        QStackedLayout,
        QVBoxLayout,
        QWidget,
    )
    from vispy import app as vispy_app
    from vispy import scene

    QT_PHOTO_CULLER_AVAILABLE = True
    QT_PHOTO_CULLER_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised only without GUI deps.
    QT_PHOTO_CULLER_AVAILABLE = False
    QT_PHOTO_CULLER_IMPORT_ERROR = exc


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
SCAN_PROGRESS_MIN_INTERVAL = 0.08
PREVIEW_RENDER_DELAY_MS = 90
PREVIEW_RESAMPLING_FILTER = Image.Resampling.BICUBIC
PREVIEW_REDUCING_GAP = 2.0
PREVIEW_OVERSCAN_FRACTION = 0.18
PREVIEW_OVERSCAN_MAX_PX = 320
PREVIEW_DRAG_RENDER_DELAY_MS = 45
ZOOM_MAX_SCALE = 4.0
ZOOM_FACTOR_PER_STEP = 2 ** (1 / 6)
ZOOM_ANIMATION_INTERVAL_MS = 16
ZOOM_RENDER_INTERVAL_MS = 32
ZOOM_RESPONSE = 14.0
ZOOM_SETTLE_RELATIVE_EPSILON = 0.0015
GPU_PREVIEW_INTERPOLATIONS = ("cubic", "catrom", "linear", "nearest")
GPU_PREVIEW_DEFAULT_INTERPOLATION = "cubic"
GPU_PREVIEW_MAX_MAGNIFICATIONS = ("4", "8", "16", "32")
GPU_PREVIEW_DEFAULT_MAX_MAGNIFICATION = "16"
GPU_PREVIEW_DEFAULT_SMOOTH_ZOOM = True
GPU_PUMP_ACTIVE_INTERVAL_MS = 4
GPU_PUMP_IDLE_INTERVAL_MS = 16
GPU_PUMP_PROCESS_BUDGET_MS = 2
GPU_BACKGROUND_POLL_INTERVAL_MS = 64


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


def clamp_zoom_scale(scale: float, fit_scale: float) -> float:
    """Keep an interactive view between fit-to-window and 400%."""
    return max(fit_scale, min(ZOOM_MAX_SCALE, scale))


def zoom_target_after_wheel(target_scale: float, steps: float, fit_scale: float) -> float:
    """Accumulate wheel input without allowing one device event to jump wildly."""
    bounded_steps = max(-8.0, min(8.0, steps))
    return clamp_zoom_scale(target_scale * (ZOOM_FACTOR_PER_STEP**bounded_steps), fit_scale)


def advance_zoom_scale(current: float, target: float, elapsed: float) -> float:
    """Move toward the target with a frame-rate-independent exponential response."""
    elapsed = max(0.0, min(elapsed, 0.05))
    blend = 1.0 - math.exp(-ZOOM_RESPONSE * elapsed)
    return current + (target - current) * blend


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
class ScanResult:
    """Files discovered below a folder and any entries that could not be read."""

    paths: tuple[Path, ...]
    directories_scanned: int
    skipped_links: int
    errors: tuple[str, ...]
    cancelled: bool = False


def _entry_is_link_or_junction(entry: os.DirEntry[str]) -> bool:
    """Return whether a directory entry must not be followed during a scan."""
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
    except OSError:
        # A disappearing or inaccessible reparse point is safer to skip than
        # to follow blindly while the directory tree is changing.
        return True
    return False


def scan_photo_tree(
    root: Path,
    *,
    progress: Callable[[int, int, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ScanResult:
    """Iteratively discover supported images below *root*.

    Directory entries are inspected with ``os.scandir`` so metadata lookups
    can use the information returned by the operating system. Symbolic links
    and Windows junctions are deliberately not followed; this keeps a scan
    inside the chosen tree and prevents cycles. Errors are collected per
    directory/entry so one inaccessible branch does not discard the rest.
    """
    root = Path(root)
    pending = [root]
    paths: list[Path] = []
    errors: list[str] = []
    directories_scanned = 0
    skipped_links = 0
    last_report_at = 0.0
    last_report_state: tuple[int, int, int] | None = None

    def report(force: bool = False) -> None:
        nonlocal last_report_at, last_report_state
        if progress is None:
            return
        now = monotonic()
        state = (len(paths), directories_scanned, skipped_links + len(errors))
        if not force and now - last_report_at < SCAN_PROGRESS_MIN_INTERVAL and state == last_report_state:
            return
        last_report_at = now
        last_report_state = state
        progress(state[0], state[1], skipped_links, len(errors))

    while pending:
        if should_cancel is not None and should_cancel():
            report(force=True)
            return ScanResult(tuple(paths), directories_scanned, skipped_links, tuple(errors), cancelled=True)

        directory = pending.pop()
        directories_scanned += 1
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if should_cancel is not None and should_cancel():
                        report(force=True)
                        return ScanResult(tuple(paths), directories_scanned, skipped_links, tuple(errors), cancelled=True)
                    try:
                        if _entry_is_link_or_junction(entry):
                            skipped_links += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                            continue
                        if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.casefold() in SUPPORTED_EXTENSIONS:
                            paths.append(Path(entry.path))
                    except OSError as exc:
                        errors.append(f"{entry.path}: {exc}")
                    report()
        except OSError as exc:
            errors.append(f"{directory}: {exc}")
        report()

    report(force=True)
    return ScanResult(tuple(paths), directories_scanned, skipped_links, tuple(errors))


@dataclass(frozen=True)
class PreviewGeometry:
    """A source crop and its screen placement for one Bicubic preview frame."""

    source_box: tuple[int, int, int, int]
    target_size: tuple[int, int]
    origin: tuple[float, float]


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


def _resolved_path_string(path: Path) -> str:
    """Return the historical absolute path spelling used by selection records."""
    try:
        return str(path.resolve())
    except OSError:
        return os.path.abspath(str(path))


def _path_identity(path: Path) -> str:
    """Return a case-insensitive absolute path identity for grouping."""
    return os.path.normcase(_resolved_path_string(path))


def _relative_path_sort_key(path: Path, root: Path | None = None) -> tuple[int, tuple[str, ...]]:
    try:
        relative = path.relative_to(root) if root is not None else path
    except ValueError:
        relative = path
    parts = tuple(part.casefold() for part in relative.parts)
    # Keep files directly inside the selected folder before deeper folders,
    # then make every remaining position deterministic by relative path.
    return len(parts), parts


def _group_sort_key(item: PhotoGroup, root: Path | None = None) -> tuple[int, tuple[str, ...]]:
    return _relative_path_sort_key(item.primary, root)


def build_photo_groups(paths: list[Path], root: Path | None = None) -> list[PhotoGroup]:
    """Hide DNG + JPEG pairs behind one culling item, without cross-folder pairing."""
    by_stem: dict[tuple[str, str], list[Path]] = {}
    for path in paths:
        parent_key = _path_identity(path.parent)
        by_stem.setdefault((parent_key, path.stem.casefold()), []).append(path)

    result: list[PhotoGroup] = []
    for same_name_paths in by_stem.values():
        ordered = sorted(same_name_paths, key=lambda path: path.name.casefold())
        raws = [path for path in ordered if path.suffix.lower() == ".dng"]
        jpegs = [path for path in ordered if path.suffix.lower() in JPEG_EXTENSIONS]
        paired_members = tuple(raws + jpegs)
        if raws and jpegs:
            # JPEG is much faster to browse and represents the same capture.
            primary = jpegs[0]
            key = "pair|" + _resolved_path_string(primary.parent).casefold() + "|" + primary.stem.casefold()
            result.append(PhotoGroup(key=key, primary=primary, members=paired_members))
            paired_paths = set(paired_members)
            for path in ordered:
                if path not in paired_paths:
                    result.append(PhotoGroup(key=_resolved_path_string(path), primary=path, members=(path,)))
        else:
            for path in ordered:
                result.append(PhotoGroup(key=_resolved_path_string(path), primary=path, members=(path,)))
    return sorted(result, key=lambda item: _group_sort_key(item, root))


def normalize_gpu_preview_settings(raw: object) -> dict[str, object]:
    """Validate persisted GPU-preview settings without importing Qt/VisPy."""
    values = raw if isinstance(raw, dict) else {}
    interpolation = values.get("interpolation", GPU_PREVIEW_DEFAULT_INTERPOLATION)
    if interpolation not in GPU_PREVIEW_INTERPOLATIONS:
        interpolation = GPU_PREVIEW_DEFAULT_INTERPOLATION
    smooth_zoom = values.get("smooth_zoom", GPU_PREVIEW_DEFAULT_SMOOTH_ZOOM)
    if not isinstance(smooth_zoom, bool):
        smooth_zoom = GPU_PREVIEW_DEFAULT_SMOOTH_ZOOM
    max_magnification = str(values.get("max_magnification", GPU_PREVIEW_DEFAULT_MAX_MAGNIFICATION))
    if max_magnification not in GPU_PREVIEW_MAX_MAGNIFICATIONS:
        max_magnification = GPU_PREVIEW_DEFAULT_MAX_MAGNIFICATION
    return {
        "interpolation": interpolation,
        "smooth_zoom": smooth_zoom,
        "max_magnification": max_magnification,
    }


def _create_gpu_preview_window(controller: "_GpuPreviewController") -> object:
    """Create the legacy standalone diagnostic window used by ``--gpu-self-test``.

    The production gallery now uses :class:`QtPhotoCuller` directly.  This
    helper stays available for the historical diagnostic command and its
    regression tests.
    """
    os.environ.setdefault("QT_API", "pyside6")
    import numpy as np
    from OpenGL import GL
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMainWindow, QSizePolicy, QVBoxLayout, QWidget
    from vispy import app as vispy_app
    from vispy import scene

    owner = controller.owner

    def decode_gl_string(value: bytes | None) -> str:
        return (value or b"").decode("utf-8", "replace") or "unknown"

    def query_gpu_info(canvas: scene.SceneCanvas) -> dict[str, object]:
        canvas.set_current()
        return {
            "backend": canvas.app.backend_name,
            "vendor": decode_gl_string(GL.glGetString(GL.GL_VENDOR)),
            "renderer": decode_gl_string(GL.glGetString(GL.GL_RENDERER)),
            "opengl": decode_gl_string(GL.glGetString(GL.GL_VERSION)),
            "max_texture_size": int(GL.glGetIntegerv(GL.GL_MAX_TEXTURE_SIZE)),
        }

    class ZoomPlan:
        def __init__(self, fit_width: float, max_magnification: float) -> None:
            self.fit_width = fit_width
            self.max_magnification = max(1.0, float(max_magnification))

        def clamp_target_width(self, width: float) -> float:
            minimum = self.fit_width / self.max_magnification
            return min(self.fit_width, max(minimum, width))

        def wheel_target_width(self, current_width: float, pending_log_factor: float, wheel_delta: float) -> float:
            wheel_delta = max(-4.0, min(4.0, float(wheel_delta)))
            delta_log = -wheel_delta * math.log(2.0) / 5.0
            requested = current_width * math.exp(pending_log_factor + delta_log)
            return self.clamp_target_width(requested)

    class SmoothPanZoomCamera(scene.PanZoomCamera):
        """Pointer-anchored VisPy camera with an optional animated wheel."""

        def __init__(self, *, max_magnification: float = 16.0, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.max_magnification = max(1.0, float(max_magnification))
            self.smooth_enabled = True
            self._fit_width: float | None = None
            self._pending_log_factor = 0.0
            self._anchor: tuple[float, float] | None = None
            self._last_tick = monotonic()
            self._timer = vispy_app.Timer(interval=1 / 120, connect=self._tick, start=False)

        @property
        def magnification(self) -> float:
            if not self._fit_width or self.rect.width <= 0:
                return 1.0
            return self._fit_width / self.rect.width

        @property
        def animation_active(self) -> bool:
            """Whether the camera still has a wheel target to animate toward."""
            return self._timer.running or abs(self._pending_log_factor) >= 0.00035

        def remember_fit(self) -> None:
            self.stop_animation()
            self._fit_width = float(self.rect.width)

        def set_smooth_enabled(self, enabled: bool) -> None:
            self.smooth_enabled = bool(enabled)
            if not self.smooth_enabled:
                self.stop_animation()

        def set_max_magnification(self, value: float) -> None:
            self.max_magnification = max(1.0, float(value))
            if self._fit_width and self.rect.width > 0:
                minimum_width = self._fit_width / self.max_magnification
                if self.rect.width < minimum_width:
                    center = tuple(self.center[:2])
                    half_width = minimum_width / 2.0
                    half_height = self.rect.height * minimum_width / max(self.rect.width * 2.0, 1.0)
                    self.set_range(
                        x=(center[0] - half_width, center[0] + half_width),
                        y=(center[1] - half_height, center[1] + half_height),
                        margin=0,
                    )

        def stop_animation(self) -> None:
            self._pending_log_factor = 0.0
            self._anchor = None
            if self._timer.running:
                self._timer.stop()

        def smooth_zoom_factor(self, factor: float, center: tuple[float, float] | None = None) -> None:
            if factor <= 0 or not self._fit_width or self.rect.width <= 0:
                return
            if not self.smooth_enabled:
                super().zoom(factor, center or tuple(self.center[:2]))
                return
            requested = self.rect.width * math.exp(self._pending_log_factor) * factor
            target_width = ZoomPlan(self._fit_width, self.max_magnification).clamp_target_width(requested)
            self._pending_log_factor = math.log(target_width / self.rect.width)
            self._anchor = center or tuple(self.center[:2])
            self._last_tick = monotonic()
            if abs(self._pending_log_factor) > 1e-5 and not self._timer.running:
                self._timer.start()

        def _queue_wheel_zoom(self, wheel_delta: float, pos: Any) -> None:
            if not self._fit_width or self.rect.width <= 0:
                return
            try:
                mapped = self._scene_transform.imap(pos)
                anchor = (float(mapped[0]), float(mapped[1]))
            except Exception:
                anchor = tuple(self.center[:2])
            plan = ZoomPlan(self._fit_width, self.max_magnification)
            target_width = plan.wheel_target_width(self.rect.width, self._pending_log_factor, wheel_delta)
            if not self.smooth_enabled:
                super().zoom(target_width / self.rect.width, anchor)
                return
            self._pending_log_factor = math.log(target_width / self.rect.width)
            self._anchor = anchor
            self._last_tick = monotonic()
            if abs(self._pending_log_factor) > 1e-5 and not self._timer.running:
                self._timer.start()

        def _tick(self, _event: Any = None) -> None:
            if abs(self._pending_log_factor) < 0.00035 or self._anchor is None:
                if self._anchor is not None and self._pending_log_factor:
                    super().zoom(math.exp(self._pending_log_factor), self._anchor)
                self.stop_animation()
                return
            now = monotonic()
            dt = min(0.05, max(1 / 240, now - self._last_tick))
            self._last_tick = now
            fraction = 1.0 - math.exp(-dt / 0.065)
            step = self._pending_log_factor * fraction
            super().zoom(math.exp(step), self._anchor)
            self._pending_log_factor -= step

        def viewbox_mouse_event(self, event: Any) -> None:
            if event.handled or not self.interactive:
                return
            if event.type == "mouse_wheel":
                self._queue_wheel_zoom(float(event.delta[1]), event.pos)
                event.handled = True
                return
            if event.type == "mouse_press" and event.button in (1, 2):
                self.stop_animation()
            super().viewbox_mouse_event(event)

    class GpuPreviewWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle(f"{APP_NAME} - GPU 预览调试")
            self.resize(1280, 820)
            self.setMinimumSize(900, 600)
            self.current_path: Path | None = None
            self.image_size = (0, 0)
            self._gpu_initialized = False
            self.canvas = scene.SceneCanvas(keys=None, show=False, bgcolor="#0d1015", vsync=True)
            self.view = self.canvas.central_widget.add_view()
            self.camera = SmoothPanZoomCamera(
                aspect=1,
                max_magnification=owner._gpu_max_magnification(),
            )
            self.camera.flip = (False, True, False)
            self.view.camera = self.camera
            self.visual = scene.visuals.Image(
                None,
                interpolation=owner.gpu_preview_interpolation.get(),
                method="subdivide",
                parent=self.view.scene,
            )
            native = self.canvas.native
            native.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            native.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

            panel = QFrame()
            panel.setObjectName("gpuPanel")
            panel.setFixedWidth(290)
            panel.setStyleSheet(
                """
                QFrame#gpuPanel { background: #171b22; color: #e8eaed; }
                QLabel { color: #d5dae2; }
                """
            )
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(16, 18, 16, 18)
            panel_layout.setSpacing(10)
            title = QLabel("GPU 预览调试窗口")
            title.setStyleSheet("font-size: 18px; font-weight: 600; color: white;")
            panel_layout.addWidget(title)
            self.photo_label = QLabel("尚未同步照片")
            self.photo_label.setWordWrap(True)
            panel_layout.addWidget(self.photo_label)
            self.settings_label = QLabel()
            self.settings_label.setWordWrap(True)
            self.settings_label.setStyleSheet("color: #aeb6c2;")
            panel_layout.addWidget(self.settings_label)
            self.zoom_label = QLabel("缩放：适合屏幕")
            panel_layout.addWidget(self.zoom_label)
            self.gpu_label = QLabel("正在读取 GPU…")
            self.gpu_label.setWordWrap(True)
            self.gpu_label.setStyleSheet("color: #9fc4ff;")
            panel_layout.addWidget(self.gpu_label)
            help_label = QLabel(
                "滚轮：围绕光标平滑缩放\n"
                "左键拖动：平移\n"
                "[ / ]：上一组 / 下一组\n"
                "Space：保留 · F：模式\n"
                "Z：适合屏幕 · 1：100%\n"
                "+ / -：逐级缩放"
            )
            help_label.setWordWrap(True)
            help_label.setStyleSheet("color: #aeb6c2; line-height: 1.4;")
            panel_layout.addWidget(help_label)
            panel_layout.addStretch(1)
            note = QLabel("设置由 Photo Culler 右侧调试面板控制。")
            note.setWordWrap(True)
            note.setStyleSheet("color: #7f8997;")
            panel_layout.addWidget(note)

            shell = QWidget()
            shell_layout = QHBoxLayout(shell)
            shell_layout.setContentsMargins(0, 0, 0, 0)
            shell_layout.setSpacing(0)
            shell_layout.addWidget(native, 1)
            shell_layout.addWidget(panel)
            self.setCentralWidget(shell)

            self.gpu_info: dict[str, object] = {
                "backend": self.canvas.app.backend_name,
                "vendor": "pending",
                "renderer": "pending",
                "opengl": "pending",
                "max_texture_size": 0,
            }
            self.status_timer = QTimer(self)
            self.status_timer.timeout.connect(self._refresh_status)
            self.status_timer.start(100)
            self._refresh_status()

        def initialize_after_show(self) -> None:
            if self._gpu_initialized:
                return
            self._gpu_initialized = True
            try:
                self.gpu_info = query_gpu_info(self.canvas)
            except Exception as exc:
                self.gpu_info = {
                    "backend": self.canvas.app.backend_name,
                    "vendor": "unavailable",
                    "renderer": f"unavailable ({exc})",
                    "opengl": "unavailable",
                    "max_texture_size": 0,
                }
            self._refresh_status()

        def set_preview_image(self, image: Image.Image, path: Path) -> None:
            if not self._gpu_initialized:
                return
            self.canvas.set_current()
            rgb = image.convert("RGB")
            pixels = np.ascontiguousarray(np.asarray(rgb))
            max_texture = int(self.gpu_info.get("max_texture_size") or 0)
            if max_texture and max(pixels.shape[:2]) > max_texture:
                raise ValueError(
                    f"照片边长超过当前 GPU 单纹理限制 {max_texture}px；"
                    "当前调试后端暂不对超大照片分块。"
                )
            self.visual.set_data(pixels)
            # VisPy versions before the fix do not invalidate the lookup shape
            # when a new image has different dimensions.  Without this line a
            # first cubic frame can be blurred until interpolation is changed.
            self.visual._need_interpolation_update = True
            self.current_path = path
            self.image_size = (int(pixels.shape[1]), int(pixels.shape[0]))
            self.fit_image()
            self.canvas.update()

        def clear_preview_image(self) -> None:
            if self._gpu_initialized:
                self.canvas.set_current()
            self.camera.stop_animation()
            self.visual.set_data(None)
            self.visual._need_interpolation_update = True
            self.current_path = None
            self.image_size = (0, 0)
            self.canvas.update()

        def fit_image(self) -> None:
            width, height = self.image_size
            if not width or not height:
                return
            self.camera.stop_animation()
            self.camera.set_range(x=(0, width), y=(0, height), margin=0)
            self.camera.remember_fit()
            self.canvas.update()

        def actual_pixels(self) -> None:
            if not self.camera._fit_width:
                return
            physical_width = max(1.0, float(self.canvas.physical_size[0]))
            target_width = min(self.camera._fit_width, physical_width)
            factor = target_width / (self.camera.rect.width * math.exp(self.camera._pending_log_factor))
            self.camera.smooth_zoom_factor(factor)

        def set_interpolation(self, name: str) -> None:
            if name not in GPU_PREVIEW_INTERPOLATIONS:
                return
            if self._gpu_initialized:
                self.canvas.set_current()
            self.visual.interpolation = name
            self.visual._need_interpolation_update = True
            self.canvas.update()

        def set_smooth_zoom(self, enabled: bool) -> None:
            self.camera.set_smooth_enabled(enabled)
            self._refresh_status()

        def set_max_magnification(self, value: float) -> None:
            self.camera.set_max_magnification(value)
            self._refresh_status()

        def _refresh_status(self) -> None:
            width, height = self.image_size
            if self.current_path:
                self.photo_label.setText(f"{self.current_path.name}\n{width} × {height} px")
            self.settings_label.setText(
                f"插值：{self.visual.interpolation}\n"
                f"滚轮动画：{'开' if self.camera.smooth_enabled else '关'}\n"
                f"最大倍率：{self.camera.max_magnification:g}×"
            )
            self.zoom_label.setText(f"缩放：{self.camera.magnification:.2f}×（相对适屏）")
            info = self.gpu_info
            self.gpu_label.setText(
                f"GPU：{info['renderer']}\n"
                f"OpenGL：{info['opengl']}\n"
                f"单纹理上限：{info['max_texture_size']} px"
            )

        def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
            text = event.text()
            key = event.key()
            if text == "[" or key == Qt.Key.Key_BracketLeft:
                owner.change_index(-1)
            elif text == "]" or key == Qt.Key.Key_BracketRight:
                owner.change_index(1)
            elif key == Qt.Key.Key_Space:
                owner.toggle_keep()
            elif text in {"f", "F"} or key == Qt.Key.Key_F:
                owner.cycle_keep_mode()
            elif text in {"z", "Z"} or key == Qt.Key.Key_Z:
                self.fit_image()
            elif text == "1" or key == Qt.Key.Key_1:
                self.actual_pixels()
            elif text in {"+", "="} or key in {Qt.Key.Key_Plus, Qt.Key.Key_Equal}:
                self.camera.smooth_zoom_factor(1.25)
            elif text == "-" or key in {Qt.Key.Key_Minus, Qt.Key.Key_Underscore}:
                self.camera.smooth_zoom_factor(1 / 1.25)
            else:
                super().keyPressEvent(event)
                return
            event.accept()

        def closeEvent(self, event: Any) -> None:  # noqa: N802
            self.camera.stop_animation()
            self.status_timer.stop()
            self.canvas.close()
            controller._window_closed()
            event.accept()

    return GpuPreviewWindow()


class _GpuPreviewController:
    """Legacy bridge retained for the standalone GPU diagnostic window."""

    def __init__(self, owner: "PhotoCuller") -> None:
        self.owner = owner
        self.qt_app: object | None = None
        self.window: object | None = None
        self._qt_all_events: object | None = None
        self._pump_job: str | None = None
        self._closing = False

    @property
    def is_open(self) -> bool:
        return self.window is not None

    def open(self) -> None:
        os.environ.setdefault("QT_API", "pyside6")
        from PySide6.QtCore import QEventLoop
        from PySide6.QtWidgets import QApplication
        from vispy import app as vispy_app

        self.qt_app = QApplication.instance() or QApplication(["Photo Culler GPU preview"])
        self._qt_all_events = QEventLoop.ProcessEventsFlag.AllEvents
        vispy_app.use_app("pyside6")
        self.window = _create_gpu_preview_window(self)
        self.window.show()
        self.qt_app.processEvents()
        self.window.initialize_after_show()
        self._schedule_pump()

    def _schedule_pump(self) -> None:
        if self._pump_job is not None or self._closing:
            return
        try:
            interval = GPU_PUMP_ACTIVE_INTERVAL_MS if self.is_animating else GPU_PUMP_IDLE_INTERVAL_MS
            self._pump_job = self.owner.after(interval, self._pump)
        except tk.TclError:
            self._pump_job = None

    @property
    def is_animating(self) -> bool:
        camera = getattr(self.window, "camera", None)
        return bool(getattr(camera, "animation_active", False))

    def _pump(self) -> None:
        self._pump_job = None
        if self._closing or self.window is None or self.qt_app is None:
            return
        try:
            if self._qt_all_events is None:
                self.qt_app.processEvents()
            else:
                self.qt_app.processEvents(self._qt_all_events, GPU_PUMP_PROCESS_BUDGET_MS)
        except Exception as exc:
            self.owner._gpu_preview_error(exc)
            self.close()
            return
        self._schedule_pump()

    def set_image(self, image: Image.Image, path: Path) -> None:
        if self.window is None:
            return
        if getattr(self.window, "current_path", None) == path:
            return
        try:
            self.window.set_preview_image(image, path)
        except Exception as exc:
            self.owner._gpu_preview_error(exc)

    def clear_image(self) -> None:
        if self.window is None:
            return
        try:
            self.window.clear_preview_image()
        except Exception as exc:
            self.owner._gpu_preview_error(exc)

    def update_settings(self) -> None:
        if self.window is None:
            return
        try:
            self.window.set_interpolation(self.owner.gpu_preview_interpolation.get())
            self.window.set_smooth_zoom(bool(self.owner.gpu_preview_smooth_zoom.get()))
            self.window.set_max_magnification(self.owner._gpu_max_magnification())
        except Exception as exc:
            self.owner._gpu_preview_error(exc)

    def _cancel_pump(self) -> None:
        if self._pump_job is None:
            return
        try:
            self.owner.after_cancel(self._pump_job)
        except tk.TclError:
            pass
        self._pump_job = None

    def close(self) -> None:
        self._closing = True
        self._cancel_pump()
        window = self.window
        try:
            if window is not None:
                window.close()
            if self.qt_app is not None:
                self.qt_app.processEvents()
        except Exception:
            pass
        self.window = None
        self._closing = False

    def _window_closed(self) -> None:
        if self._closing:
            return
        self._cancel_pump()
        self.window = None
        self.owner._gpu_preview_window_closed()


def _run_gpu_preview_self_test() -> int:
    """Exercise the integrated optional window without opening the gallery."""
    try:
        os.environ.setdefault("QT_API", "pyside6")
        import numpy as np
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        from vispy import app as vispy_app

        qt_app = QApplication.instance() or QApplication(["Photo Culler GPU self-test"])
        vispy_app.use_app("pyside6")

        class Value:
            def __init__(self, value: object) -> None:
                self.value = value

            def get(self) -> object:
                return self.value

        class Owner:
            gpu_preview_interpolation = Value(GPU_PREVIEW_DEFAULT_INTERPOLATION)
            gpu_preview_smooth_zoom = Value(True)
            gpu_preview_max_zoom = Value(GPU_PREVIEW_DEFAULT_MAX_MAGNIFICATION)

            @staticmethod
            def _gpu_max_magnification() -> float:
                return 16.0

            @staticmethod
            def change_index(_direction: int) -> None:
                pass

            @staticmethod
            def toggle_keep() -> None:
                pass

            @staticmethod
            def cycle_keep_mode() -> None:
                pass

            @staticmethod
            def _gpu_preview_window_closed() -> None:
                pass

            @staticmethod
            def _gpu_preview_error(_error: Exception) -> None:
                pass

            def after(self, _delay: int, _callback: Callable[[], object]) -> str:
                return "self-test"

            @staticmethod
            def after_cancel(_job: str) -> None:
                pass

        class Controller:
            def __init__(self) -> None:
                self.owner = Owner()

            @staticmethod
            def _window_closed() -> None:
                pass

        controller = Controller()
        window = _create_gpu_preview_window(controller)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        qt_app.processEvents()
        window.initialize_after_show()
        qt_app.processEvents()

        height, width = 1200, 1800
        y, x = np.indices((height, width), dtype=np.uint32)
        pixels = np.empty((height, width, 3), dtype=np.uint8)
        pixels[..., 0] = ((x * 13 + y * 3) & 255).astype(np.uint8)
        pixels[..., 1] = ((x * 2 + y * 11) & 255).astype(np.uint8)
        pixels[..., 2] = (((x // 16) ^ (y // 16)) & 255).astype(np.uint8)
        window.set_preview_image(Image.fromarray(pixels, "RGB"), Path("gpu-self-test.jpg"))
        qt_app.processEvents()
        initial_width = float(window.camera.rect.width)
        window.camera._queue_wheel_zoom(1.0, (480.0, 320.0))
        for _ in range(180):
            window.camera._last_tick -= 1 / 120
            window.camera._tick()
        zoomed_width = float(window.camera.rect.width)
        frame = window.canvas.render(size=(960, 640), alpha=False)
        lookup_shape = None
        if window.visual._data_lookup_fn is not None:
            lookup_shape = list(window.visual._data_lookup_fn["shape"]._value)
        result = {
            "passed": (
                frame.shape == (640, 960, 3)
                and bool(frame.any())
                and 1.05 < initial_width / zoomed_width < 1.30
                and not window.camera._timer.running
                and lookup_shape == [1800, 1200]
            ),
            "gpu": window.gpu_info,
            "frame_shape": list(frame.shape),
            "frame_nonzero": bool(frame.any()),
            "zoom_ratio": round(initial_width / zoomed_width, 4),
            "interpolation_shape": lookup_shape,
            "fbo_regression_guard": "no pre-show canvas.render()",
        }
        window.close()
        qt_app.processEvents()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    except Exception as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


class PhotoCuller(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self._configure_dpi_layout()
        self.configure(bg="#17191d")

        self.folder: Path | None = None
        self._scan_folder: Path | None = None
        self._scan_notice = ""
        self._scan_generation = 0
        self._scan_future: Future[None] | None = None
        self._scan_events: queue.Queue[tuple[int, str, object]] = queue.Queue()
        self._scan_poll_job: str | None = None
        self._scan_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photo-culler-scan")
        self.sidebar_width = self._load_sidebar_width()
        gpu_settings = self._load_gpu_preview_settings()
        self.gpu_preview_interpolation = tk.StringVar(value=str(gpu_settings["interpolation"]))
        self.gpu_preview_smooth_zoom = tk.BooleanVar(value=bool(gpu_settings["smooth_zoom"]))
        self.gpu_preview_max_zoom = tk.StringVar(value=str(gpu_settings["max_magnification"]))
        self._gpu_preview_controller: _GpuPreviewController | None = None
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
        self.fit_scale = 1.0
        self.zoom_scale = 1.0
        self.zoom_target_scale = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._zoom_anchor: tuple[float, float, float, float] | None = None
        self._zoom_animation_job: str | None = None
        self._zoom_animation_last_time: float | None = None
        self._zoom_last_render_at = 0.0
        self._drag_state: tuple[int, int, float, float] | None = None
        self._preview_render_job: str | None = None
        self._preview_render_generation = 0
        self._preview_render_events: queue.Queue[tuple[int, str, Image.Image | None, PreviewGeometry | None, Exception | None]] = queue.Queue()
        self._preview_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photo-culler-preview")
        self._preview_futures: set[Future[None]] = set()
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
        self._scan_poll_job = self.after(60, self._poll_scan_events)
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
        style.configure("Gpu.TLabelframe", background="#202329", foreground="#e7e9ed")
        style.configure("Gpu.TLabelframe.Label", background="#202329", foreground="#e7e9ed")
        style.configure("Gpu.TLabel", background="#202329", foreground="#e7e9ed")

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

        gpu_frame = ttk.LabelFrame(control_panel, text="GPU 预览调试", style="Gpu.TLabelframe", padding=(8, 7))
        gpu_frame.pack(fill="x", pady=(0, 10))
        self.gpu_preview_button = ttk.Button(
            gpu_frame,
            text="打开 GPU 调试窗口",
            style="App.TButton",
            command=self.toggle_gpu_preview,
        )
        self.gpu_preview_button.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        ttk.Label(gpu_frame, text="插值", style="Gpu.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 5))
        self.gpu_preview_interpolation_combo = ttk.Combobox(
            gpu_frame,
            textvariable=self.gpu_preview_interpolation,
            values=GPU_PREVIEW_INTERPOLATIONS,
            state="readonly",
            width=10,
        )
        self.gpu_preview_interpolation_combo.grid(row=1, column=1, sticky="ew", pady=(0, 5))
        self.gpu_preview_interpolation_combo.bind("<<ComboboxSelected>>", self._on_gpu_preview_settings_changed)
        self.gpu_preview_smooth_check = ttk.Checkbutton(
            gpu_frame,
            text="平滑滚轮缩放",
            variable=self.gpu_preview_smooth_zoom,
            style="App.TCheckbutton",
            command=self._on_gpu_preview_settings_changed,
        )
        self.gpu_preview_smooth_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 5))
        ttk.Label(gpu_frame, text="最大倍率", style="Gpu.TLabel").grid(row=3, column=0, sticky="w")
        self.gpu_preview_max_zoom_combo = ttk.Combobox(
            gpu_frame,
            textvariable=self.gpu_preview_max_zoom,
            values=GPU_PREVIEW_MAX_MAGNIFICATIONS,
            state="readonly",
            width=10,
        )
        self.gpu_preview_max_zoom_combo.grid(row=3, column=1, sticky="ew")
        self.gpu_preview_max_zoom_combo.bind("<<ComboboxSelected>>", self._on_gpu_preview_settings_changed)
        gpu_frame.columnconfigure(1, weight=1)
        self.gpu_preview_status_label = ttk.Label(
            gpu_frame,
            text="未启动；需要 PySide6 + VisPy",
            style="Muted.TLabel",
            wraplength=self._px(190),
        )
        self.gpu_preview_status_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(7, 0))

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
            text="[ ] 切换 · Space 保留 · F 模式 · 滚轮缩放 · 拖动平移 · Z 适合/100%",
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
        self._begin_folder_scan(Path(chosen))

    def _begin_folder_scan(self, folder: Path) -> None:
        """Clear the current gallery and scan the selected tree in the background."""
        self._scan_generation += 1
        generation = self._scan_generation
        if self._scan_future is not None:
            self._scan_future.cancel()
            self._scan_future = None
        while True:
            try:
                self._scan_events.get_nowait()
            except queue.Empty:
                break

        # Do not let an unfinished scan expose the previous folder's choices.
        self.folder = None
        self._scan_folder = folder
        self._scan_notice = ""
        self.all_items = []
        self.index = 0
        self.kept = set()
        self.pair_modes = {}
        self.current_source_image = None
        self.current_source_path = None
        if self._gpu_preview_controller is not None and self._gpu_preview_controller.is_open:
            self._gpu_preview_controller.clear_image()
        self._reset_thumbnail_state()
        # Increment the preload generation and discard the previous folder's
        # memory cache while the new tree is being enumerated.
        self._start_jpeg_preload([])
        self.folder_label.configure(text=f"正在扫描：{folder.name or str(folder)}")
        self._set_status("正在扫描：已发现 0 张照片，已访问 0 个文件夹")
        self._show_preview_message("正在扫描照片…")
        self._scan_future = self._scan_executor.submit(self._scan_folder_worker, generation, folder)

    def _scan_folder_worker(self, generation: int, folder: Path) -> None:
        def report(found: int, directories: int, skipped_links: int, errors: int) -> None:
            if generation == self._scan_generation:
                self._scan_events.put((generation, "progress", (found, directories, skipped_links, errors)))

        try:
            result = scan_photo_tree(
                folder,
                progress=report,
                should_cancel=lambda: generation != self._scan_generation,
            )
        except Exception as exc:  # Keep an unexpected filesystem error on the UI thread.
            if generation == self._scan_generation:
                self._scan_events.put((generation, "error", exc))
            return
        if not result.cancelled and generation == self._scan_generation:
            self._scan_events.put((generation, "done", result))

    def _poll_scan_events(self) -> None:
        generation = self._scan_generation
        latest_progress: tuple[int, int, int, int] | None = None
        completed: ScanResult | None = None
        failure: Exception | None = None
        while True:
            try:
                event_generation, kind, payload = self._scan_events.get_nowait()
            except queue.Empty:
                break
            if event_generation != generation:
                continue
            if kind == "progress":
                latest_progress = payload  # type: ignore[assignment]
            elif kind == "done":
                completed = payload  # type: ignore[assignment]
            elif kind == "error" and isinstance(payload, Exception):
                failure = payload

        if latest_progress is not None and completed is None and failure is None:
            found, directories, skipped_links, errors = latest_progress
            self._set_status(f"正在扫描：已发现 {found} 张照片，已访问 {directories} 个文件夹")
            detail = []
            if skipped_links:
                detail.append(f"跳过链接 {skipped_links}")
            if errors:
                detail.append(f"读取异常 {errors}")
            self.preload_label.configure(text="；".join(detail))

        if failure is not None:
            self._scan_future = None
            self._scan_folder = None
            self.preload_label.configure(text="")
            messagebox.showerror(APP_NAME, f"无法扫描这个文件夹：\n{failure}")

        if completed is not None:
            self._scan_future = None
            folder = self._scan_folder
            self._scan_folder = None
            if folder is not None:
                self._apply_scan_result(folder, completed)

        if self.winfo_exists():
            self._scan_poll_job = self.after(
                self._gpu_background_poll_delay(60),
                self._poll_scan_events,
            )

    def _apply_scan_result(self, folder: Path, result: ScanResult) -> None:
        """Install one complete, sorted scan snapshot on the Tk thread."""
        self.folder = folder
        paths = sorted(result.paths, key=lambda path: _relative_path_sort_key(path, folder))
        self.all_items = build_photo_groups(list(paths), root=folder)
        saved, saved_pair_modes = self._load_selection()
        current_keys = {item.key for item in self.all_items}
        self.kept = saved.intersection(current_keys)
        pair_keys = {item.key for item in self.all_items if item.paired_raw_jpeg}
        self.pair_modes = {
            key: mode for key, mode in saved_pair_modes.items() if key in pair_keys and mode in {"both", "raw", "jpg"}
        }
        self.folder_label.configure(text=folder.name or str(folder))
        notices = []
        if result.skipped_links:
            notices.append(f"跳过链接目录/文件 {result.skipped_links} 个")
        if result.errors:
            notices.append(f"跳过读取异常 {len(result.errors)} 项")
        self._scan_notice = "    " + "；".join(notices) if notices else ""
        jpeg_paths = [path for path in paths if path.suffix.casefold() in JPEG_EXTENSIONS]
        self._start_jpeg_preload(jpeg_paths)
        if not self.all_items:
            self.current_source_image = None
            self.current_source_path = None
            self._show_preview_message("这个文件夹中没有受支持的照片")
            self._set_status("支持 JPG、JPEG、PNG、TIFF、DNG" + self._scan_notice)
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
        self._show_current(center=False)

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
            self._show_current(center=True)
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

    def _display_path(self, path: Path) -> str:
        """Prefer a root-relative path so duplicate names in subfolders are clear."""
        if self.folder is not None:
            try:
                return str(path.relative_to(self.folder))
            except ValueError:
                pass
        return path.name

    def _show_current(self, center: bool) -> None:
        item = self.current_item
        if item is None:
            return
        path = item.primary
        display_path = self._display_path(path)
        self._cancel_preview_jobs()
        self._set_status("正在载入：" + display_path)
        self.update_idletasks()
        try:
            path_changed = self.current_source_path != path or self.current_source_image is None
            if path_changed:
                self._cancel_zoom_animation()
                self.current_source_image = self._load_image(path, thumbnail=False)
                self.current_source_path = path
            self._render_preview(reset_view=path_changed)
        except Exception as exc:  # Do not stop an entire culling session for one bad image.
            self.current_source_image = None
            self.current_source_path = None
            if self._gpu_preview_controller is not None and self._gpu_preview_controller.is_open:
                self._gpu_preview_controller.clear_image()
            self._show_preview_message(f"无法显示\n{display_path}\n\n{exc}")
        if self.current_source_image is not None and self.current_source_path is not None:
            self._sync_gpu_preview()
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
        self._cancel_zoom_animation()
        self._cancel_preview_jobs()
        if self._gpu_preview_controller is not None and self._gpu_preview_controller.is_open:
            self._gpu_preview_controller.clear_image()
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
        if hasattr(self, "zoom_label"):
            self.zoom_label.configure(text="—")

    def _render_preview(self, reset_view: bool = False) -> None:
        """Render one Bicubic viewport immediately."""
        image = self.current_source_image
        if image is None:
            return
        geometry = self._preview_geometry(image, reset_view=reset_view)
        frame = self._build_preview_frame(image, geometry)
        self._apply_preview_frame(frame, geometry)
        self._update_zoom_label()
        self._update_preview_cursor()

    def _sync_fit_scale(self, image: Image.Image, reset_view: bool = False) -> tuple[int, int]:
        """Update the fit scale while preserving an intentional magnified view."""
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        previous_fit = self.fit_scale
        new_fit = min(canvas_width / image.width, canvas_height / image.height, 1.0)
        was_at_fit = (
            abs(self.zoom_scale - previous_fit) <= 0.0001
            and abs(self.zoom_target_scale - previous_fit) <= 0.0001
        )
        self.fit_scale = new_fit
        if reset_view or was_at_fit:
            self.zoom_scale = new_fit
            self.zoom_target_scale = new_fit
            self.pan_x = 0.0
            self.pan_y = 0.0
        else:
            self.zoom_scale = clamp_zoom_scale(self.zoom_scale, new_fit)
            self.zoom_target_scale = clamp_zoom_scale(self.zoom_target_scale, new_fit)
        self._constrain_pan(canvas_width, canvas_height)
        return canvas_width, canvas_height

    def _preview_geometry(self, image: Image.Image, reset_view: bool = False) -> PreviewGeometry:
        """Calculate the visible source crop plus a small drag buffer."""
        canvas_width, canvas_height = self._sync_fit_scale(image, reset_view=reset_view)
        scale = max(self.zoom_scale, 1e-9)
        display_width = image.width * scale
        display_height = image.height * scale
        left = canvas_width / 2 + self.pan_x - display_width / 2
        top = canvas_height / 2 + self.pan_y - display_height / 2

        overscan = 0.0
        if scale > self.fit_scale + 0.0001:
            overscan = min(max(canvas_width, canvas_height) * PREVIEW_OVERSCAN_FRACTION, PREVIEW_OVERSCAN_MAX_PX)

        source_left = max(0, math.floor((-overscan - left) / scale))
        source_top = max(0, math.floor((-overscan - top) / scale))
        source_right = min(image.width, math.ceil((canvas_width + overscan - left) / scale))
        source_bottom = min(image.height, math.ceil((canvas_height + overscan - top) / scale))
        source_left = min(source_left, image.width - 1)
        source_top = min(source_top, image.height - 1)
        source_right = max(source_left + 1, source_right)
        source_bottom = max(source_top + 1, source_bottom)
        target_size = (
            max(1, round((source_right - source_left) * scale)),
            max(1, round((source_bottom - source_top) * scale)),
        )
        return PreviewGeometry(
            source_box=(source_left, source_top, source_right, source_bottom),
            target_size=target_size,
            origin=(
                left + source_left * scale,
                top + source_top * scale,
            ),
        )

    @staticmethod
    def _build_preview_frame(image: Image.Image, geometry: PreviewGeometry) -> Image.Image:
        full_box = (0, 0, image.width, image.height)
        if geometry.source_box == full_box and image.size == geometry.target_size:
            return image
        return image.resize(
            geometry.target_size,
            PREVIEW_RESAMPLING_FILTER,
            box=geometry.source_box,
            reducing_gap=PREVIEW_REDUCING_GAP,
        )

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

    def _cancel_preview_jobs(self) -> None:
        if self._preview_render_job is not None:
            try:
                self.after_cancel(self._preview_render_job)
            except tk.TclError:
                pass
            self._preview_render_job = None
        self._preview_render_generation += 1
        for future in self._preview_futures:
            future.cancel()
        self._preview_futures.clear()

    def _schedule_preview_render(self, delay: int = PREVIEW_RENDER_DELAY_MS) -> None:
        """Coalesce view changes so only the newest viewport reaches Tk."""
        self._preview_render_generation += 1
        for future in self._preview_futures:
            future.cancel()
        self._preview_futures = {future for future in self._preview_futures if not future.done()}
        if self._preview_render_job is not None:
            try:
                self.after_cancel(self._preview_render_job)
            except tk.TclError:
                pass
        generation = self._preview_render_generation
        self._preview_render_job = self.after(
            max(0, int(delay)),
            lambda generation=generation: self._request_preview_render(generation),
        )

    def _request_preview_render(self, generation: int) -> None:
        """Resize outside Tk's event loop; only Tk image creation stays on the UI thread."""
        self._preview_render_job = None
        if generation != self._preview_render_generation:
            return
        image = self.current_source_image
        path = self.current_source_path
        if image is None or path is None:
            return
        for future in self._preview_futures:
            future.cancel()
        self._preview_futures = {future for future in self._preview_futures if not future.done()}
        try:
            geometry = self._preview_geometry(image, reset_view=False)
        except Exception:
            return
        future = self._preview_executor.submit(
            self._preview_render_worker,
            generation,
            str(path.resolve()),
            image,
            geometry,
        )
        self._preview_futures.add(future)

    def _preview_render_worker(
        self,
        generation: int,
        path_key: str,
        image: Image.Image,
        geometry: PreviewGeometry,
    ) -> None:
        try:
            frame = self._build_preview_frame(image, geometry)
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
            self._preview_poll_job = self.after(
                self._gpu_background_poll_delay(16),
                self._poll_preview_render_events,
            )

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
        if not hasattr(self, "zoom_label"):
            return
        if self.current_source_image is None:
            self.zoom_label.configure(text="—")
            return
        percent = round(self.zoom_scale * 100)
        if abs(self.zoom_scale - self.fit_scale) <= 0.0001:
            self.zoom_label.configure(text=f"适合 {percent}%")
        else:
            self.zoom_label.configure(text=f"{percent}%")

    def _update_preview_cursor(self) -> None:
        cursor = "fleur" if self.zoom_scale > self.fit_scale + 0.0001 else "arrow"
        self.preview_canvas.configure(cursor=cursor)

    def _cancel_zoom_animation(self) -> None:
        if self._zoom_animation_job is not None:
            try:
                self.after_cancel(self._zoom_animation_job)
            except tk.TclError:
                pass
        self._zoom_animation_job = None
        self._zoom_animation_last_time = None
        self.zoom_target_scale = self.zoom_scale
        self._zoom_anchor = None

    def _capture_zoom_anchor(self, anchor: tuple[float, float]) -> None:
        """Remember which source pixel is currently under the pointer."""
        image = self.current_source_image
        if image is None:
            return
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        self._constrain_pan(canvas_width, canvas_height)
        scale = max(self.zoom_scale, 1e-9)
        left = canvas_width / 2 + self.pan_x - image.width * scale / 2
        top = canvas_height / 2 + self.pan_y - image.height * scale / 2
        anchor_x, anchor_y = anchor
        source_x = (anchor_x - left) / scale
        source_y = (anchor_y - top) / scale
        if not (0.0 <= source_x <= image.width and 0.0 <= source_y <= image.height):
            anchor_x = canvas_width / 2
            anchor_y = canvas_height / 2
            source_x = image.width / 2
            source_y = image.height / 2
        self._zoom_anchor = (anchor_x, anchor_y, source_x, source_y)

    def _apply_zoom_anchor(self) -> None:
        image = self.current_source_image
        anchor = self._zoom_anchor
        if image is None or anchor is None:
            return
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        anchor_x, anchor_y, source_x, source_y = anchor
        self.pan_x = anchor_x - canvas_width / 2 - (source_x - image.width / 2) * self.zoom_scale
        self.pan_y = anchor_y - canvas_height / 2 - (source_y - image.height / 2) * self.zoom_scale
        self._constrain_pan(canvas_width, canvas_height)

    def _start_zoom_animation(self) -> None:
        if self._zoom_animation_job is not None:
            return
        self._zoom_animation_last_time = monotonic()
        self._zoom_last_render_at = 0.0
        self._zoom_animation_job = self.after(ZOOM_ANIMATION_INTERVAL_MS, self._animate_zoom)

    def _animate_zoom(self) -> None:
        self._zoom_animation_job = None
        if self.current_source_image is None:
            self._cancel_zoom_animation()
            return
        now = monotonic()
        previous_time = self._zoom_animation_last_time or now
        self._zoom_animation_last_time = now
        next_scale = advance_zoom_scale(self.zoom_scale, self.zoom_target_scale, now - previous_time)
        settled = abs(self.zoom_target_scale - next_scale) <= max(
            self.zoom_target_scale * ZOOM_SETTLE_RELATIVE_EPSILON,
            0.00001,
        )
        self.zoom_scale = self.zoom_target_scale if settled else next_scale
        self.zoom_scale = clamp_zoom_scale(self.zoom_scale, self.fit_scale)
        self._apply_zoom_anchor()
        self._update_zoom_label()
        self._update_preview_cursor()

        if settled or now - self._zoom_last_render_at >= ZOOM_RENDER_INTERVAL_MS / 1000.0:
            self._zoom_last_render_at = now
            self._schedule_preview_render(delay=0)

        if settled:
            self._zoom_animation_last_time = None
            self._zoom_anchor = None
            return
        self._zoom_animation_job = self.after(ZOOM_ANIMATION_INTERVAL_MS, self._animate_zoom)

    def _set_zoom_target(self, scale: float, anchor: tuple[float, float]) -> None:
        image = self.current_source_image
        if image is None:
            return
        self._sync_fit_scale(image, reset_view=False)
        target = clamp_zoom_scale(scale, self.fit_scale)
        if abs(target - self.zoom_target_scale) <= 0.000001 and self._zoom_animation_job is None:
            return
        self._capture_zoom_anchor(anchor)
        self.zoom_target_scale = target
        self._start_zoom_animation()

    def zoom_fit(self) -> None:
        if self.current_source_image is None:
            return
        self._cancel_zoom_animation()
        self._cancel_preview_jobs()
        self._render_preview(reset_view=True)

    def zoom_actual(self) -> None:
        image = self.current_source_image
        if image is None:
            return
        self._cancel_zoom_animation()
        self._sync_fit_scale(image, reset_view=False)
        self.zoom_scale = clamp_zoom_scale(1.0, self.fit_scale)
        self.zoom_target_scale = self.zoom_scale
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._cancel_preview_jobs()
        self._render_preview(reset_view=False)

    def toggle_zoom(self) -> None:
        image = self.current_source_image
        if image is None:
            return
        self._sync_fit_scale(image, reset_view=False)
        if abs(self.zoom_target_scale - self.fit_scale) <= 0.0001:
            self.zoom_actual()
        else:
            self.zoom_fit()

    def zoom_step(self, direction: int) -> None:
        if self.current_source_image is None:
            return
        center = (self.preview_canvas.winfo_width() / 2, self.preview_canvas.winfo_height() / 2)
        factor = ZOOM_FACTOR_PER_STEP if direction > 0 else 1 / ZOOM_FACTOR_PER_STEP
        self._set_zoom_target(self.zoom_target_scale * factor, center)

    def _preview_mouse_wheel(self, event: tk.Event) -> str:
        if self.current_source_image is None or event.delta == 0:
            return "break"
        image = self.current_source_image
        self._sync_fit_scale(image, reset_view=False)
        target = zoom_target_after_wheel(self.zoom_target_scale, event.delta / 120.0, self.fit_scale)
        self._set_zoom_target(target, (event.x, event.y))
        return "break"

    def _preview_drag_start(self, event: tk.Event) -> None:
        self._cancel_zoom_animation()
        if self.current_source_image is None or self.zoom_scale <= self.fit_scale + 0.0001:
            self._drag_state = None
            return
        self._cancel_preview_jobs()
        self._render_preview(reset_view=False)
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
        delay = 0 if self._preview_frame_needs_refresh() else PREVIEW_DRAG_RENDER_DELAY_MS
        self._schedule_preview_render(delay=delay)

    def _preview_drag_end(self, _event: tk.Event) -> None:
        if self._drag_state is None:
            return
        self._drag_state = None
        self._schedule_preview_render(delay=0)

    def _move_preview_item(self, dx: float, dy: float) -> None:
        if self.preview_image_item is None or (abs(dx) < 0.001 and abs(dy) < 0.001):
            return
        self.preview_canvas.move(self.preview_image_item, dx, dy)
        if self._preview_item_origin is not None:
            self._preview_item_origin = (self._preview_item_origin[0] + dx, self._preview_item_origin[1] + dy)

    def _preview_frame_needs_refresh(self) -> bool:
        image = self.current_source_image
        if image is None or self.preview_image_item is None:
            return True
        bounds = self.preview_canvas.bbox(self.preview_image_item)
        if bounds is None:
            return True
        canvas_width = max(self.preview_canvas.winfo_width(), 1)
        canvas_height = max(self.preview_canvas.winfo_height(), 1)
        display_width = image.width * self.zoom_scale
        display_height = image.height * self.zoom_scale
        display_left = canvas_width / 2 + self.pan_x - display_width / 2
        display_top = canvas_height / 2 + self.pan_y - display_height / 2
        visible_left = max(0.0, display_left)
        visible_top = max(0.0, display_top)
        visible_right = min(float(canvas_width), display_left + display_width)
        visible_bottom = min(float(canvas_height), display_top + display_height)
        frame_left, frame_top, frame_right, frame_bottom = bounds
        return (
            frame_left > visible_left + 1
            or frame_top > visible_top + 1
            or frame_right < visible_right - 1
            or frame_bottom < visible_bottom - 1
        )

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
            self.after(
                self._gpu_background_poll_delay(75),
                lambda: self._poll_preload_events(generation),
            )

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
        label = self._display_path(item.primary)
        if len(label) > 18:
            # Keep the filename end visible when a nested relative path is long.
            label = "…" + label[-17:]
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
            self._thumbnail_poll_job = self.after(
                self._gpu_background_poll_delay(THUMB_RENDER_POLL_MS),
                self._poll_thumbnail_events,
            )

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
        # Keep the current source image and redraw only after the layout settles.
        if self.current_source_image is not None and self.preview_photo is not None:
            self._cancel_zoom_animation()
            self._cancel_preview_jobs()
            self._schedule_preview_render(delay=0)

    def _gpu_max_magnification(self) -> float:
        try:
            value = float(self.gpu_preview_max_zoom.get())
        except (TypeError, ValueError):
            value = float(GPU_PREVIEW_DEFAULT_MAX_MAGNIFICATION)
        return max(1.0, min(64.0, value))

    def _set_gpu_preview_status(self, text: str) -> None:
        if hasattr(self, "gpu_preview_status_label"):
            self.gpu_preview_status_label.configure(text=text)

    def toggle_gpu_preview(self) -> None:
        """Open or close the optional PySide6/VisPy preview debug window."""
        controller = self._gpu_preview_controller
        if controller is not None and controller.is_open:
            controller.close()
            self._gpu_preview_controller = None
            self.gpu_preview_button.configure(text="打开 GPU 调试窗口")
            self._set_gpu_preview_status("已关闭 GPU 调试窗口")
            return
        try:
            controller = _GpuPreviewController(self)
            controller.open()
        except Exception as exc:
            self._gpu_preview_controller = None
            self.gpu_preview_button.configure(text="打开 GPU 调试窗口")
            self._set_gpu_preview_status("启动失败：" + str(exc))
            messagebox.showerror(
                APP_NAME,
                "无法启动 GPU 调试窗口。请确认构建环境安装了 PySide6、VisPy、PyOpenGL 和 NumPy。\n\n"
                + str(exc),
            )
            return
        self._gpu_preview_controller = controller
        self.gpu_preview_button.configure(text="关闭 GPU 调试窗口")
        self._set_gpu_preview_status("GPU 调试窗口已打开")
        self._on_gpu_preview_settings_changed()
        self._sync_gpu_preview()

    def _sync_gpu_preview(self) -> None:
        controller = self._gpu_preview_controller
        if controller is None or not controller.is_open:
            return
        image = self.current_source_image
        path = self.current_source_path
        if image is None or path is None:
            return
        controller.set_image(image, path)

    def _on_gpu_preview_settings_changed(self, _event: tk.Event | None = None) -> None:
        settings = normalize_gpu_preview_settings(
            {
                "interpolation": self.gpu_preview_interpolation.get(),
                "smooth_zoom": self.gpu_preview_smooth_zoom.get(),
                "max_magnification": self.gpu_preview_max_zoom.get(),
            }
        )
        self.gpu_preview_interpolation.set(str(settings["interpolation"]))
        self.gpu_preview_smooth_zoom.set(bool(settings["smooth_zoom"]))
        self.gpu_preview_max_zoom.set(str(settings["max_magnification"]))
        self._save_gpu_preview_settings()
        controller = self._gpu_preview_controller
        if controller is not None and controller.is_open:
            controller.update_settings()
            self._set_gpu_preview_status(
                f"已应用：{settings['interpolation']} / 最大 {settings['max_magnification']}×"
            )

    def _gpu_preview_window_closed(self) -> None:
        self._gpu_preview_controller = None
        if hasattr(self, "gpu_preview_button"):
            self.gpu_preview_button.configure(text="打开 GPU 调试窗口")
        self._set_gpu_preview_status("GPU 调试窗口已关闭")

    def _gpu_preview_error(self, error: Exception) -> None:
        self._set_gpu_preview_status("GPU 预览错误：" + str(error))

    def _gpu_background_poll_delay(self, normal_ms: int) -> int:
        """Give the GPU camera priority while keeping Tk background work alive."""
        controller = self._gpu_preview_controller
        if controller is not None and controller.is_animating:
            return max(normal_ms, GPU_BACKGROUND_POLL_INTERVAL_MS)
        return normal_ms

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

    def _load_settings_data(self) -> dict[str, object]:
        try:
            data = json.loads(self._settings_file().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_settings_data(self, data: dict[str, object]) -> None:
        try:
            settings_file = self._settings_file()
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _load_sidebar_width(self) -> int:
        data = self._load_settings_data()
        try:
            width = int(data.get("sidebar_width", SIDEBAR_WIDTH_DEFAULT))
        except (ValueError, TypeError):
            width = SIDEBAR_WIDTH_DEFAULT
        return max(SIDEBAR_WIDTH_MIN, min(SIDEBAR_WIDTH_MAX, width))

    def _save_sidebar_width(self) -> None:
        data = self._load_settings_data()
        data["sidebar_width"] = self.sidebar_width
        self._write_settings_data(data)

    def _load_gpu_preview_settings(self) -> dict[str, object]:
        data = self._load_settings_data()
        nested = data.get("gpu_preview", {})
        return normalize_gpu_preview_settings(nested)

    def _save_gpu_preview_settings(self) -> None:
        data = self._load_settings_data()
        data["gpu_preview"] = {
            "interpolation": self.gpu_preview_interpolation.get(),
            "smooth_zoom": bool(self.gpu_preview_smooth_zoom.get()),
            "max_magnification": self.gpu_preview_max_zoom.get(),
        }
        self._write_settings_data(data)

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
            return f"保留 {len(self.kept)} 张照片" + self._scan_notice
        prefix = "★ 已保留" if item.key in self.kept else "未保留"
        paired = f"    绑定组：{self._pair_mode_label(self._pair_mode(item))}" if item.paired_raw_jpeg and item.key in self.kept else ("    RAW+JPG 绑定组" if item.paired_raw_jpeg else "")
        return f"{self.index + 1} / {len(self.visible_items)}    {prefix}    已保留 {len(self.kept)} 个项目{paired}    {self._display_path(item.primary)}{self._scan_notice}"

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _on_close(self) -> None:
        """Stop background preview jobs before Tk tears down its image runtime."""
        self._scan_generation += 1
        if self._scan_future is not None:
            self._scan_future.cancel()
            self._scan_future = None
        if getattr(self, "_scan_poll_job", None) is not None:
            try:
                self.after_cancel(self._scan_poll_job)
            except tk.TclError:
                pass
        self._cancel_zoom_animation()
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
        if self._gpu_preview_controller is not None:
            self._gpu_preview_controller.close()
            self._gpu_preview_controller = None
        self._preview_executor.shutdown(wait=False, cancel_futures=True)
        self._thumbnail_executor.shutdown(wait=False, cancel_futures=True)
        self._scan_executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()


if QT_PHOTO_CULLER_AVAILABLE:

    class QtSmoothPanZoomCamera(scene.PanZoomCamera):
        """Pointer-anchored VisPy camera used by the production preview."""

        def __init__(self, *, max_magnification: float = 16.0, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.max_magnification = max(1.0, float(max_magnification))
            self.smooth_enabled = True
            self._fit_width: float | None = None
            self._pending_log_factor = 0.0
            self._anchor: tuple[float, float] | None = None
            self._last_tick = monotonic()
            self._timer = vispy_app.Timer(interval=1 / 120, connect=self._tick, start=False)

        @property
        def magnification(self) -> float:
            if not self._fit_width or self.rect.width <= 0:
                return 1.0
            return self._fit_width / self.rect.width

        @property
        def animation_active(self) -> bool:
            return self._timer.running or abs(self._pending_log_factor) >= 0.00035

        def remember_fit(self) -> None:
            self.stop_animation()
            self._fit_width = float(self.rect.width)

        def set_smooth_enabled(self, enabled: bool) -> None:
            self.smooth_enabled = bool(enabled)
            if not self.smooth_enabled:
                self.stop_animation()

        def set_max_magnification(self, value: float) -> None:
            self.max_magnification = max(1.0, float(value))
            if not self._fit_width or self.rect.width <= 0:
                return
            minimum_width = self._fit_width / self.max_magnification
            if self.rect.width >= minimum_width:
                return
            center = tuple(self.center[:2])
            half_width = minimum_width / 2.0
            half_height = max(1.0, self.rect.height * minimum_width / max(self.rect.width * 2.0, 1.0))
            self.set_range(
                x=(center[0] - half_width, center[0] + half_width),
                y=(center[1] - half_height, center[1] + half_height),
                margin=0,
            )

        def stop_animation(self) -> None:
            self._pending_log_factor = 0.0
            self._anchor = None
            if self._timer.running:
                self._timer.stop()

        def smooth_zoom_factor(self, factor: float, center: tuple[float, float] | None = None) -> None:
            if factor <= 0 or not self._fit_width or self.rect.width <= 0:
                return
            anchor = center or tuple(self.center[:2])
            if not self.smooth_enabled:
                super().zoom(factor, anchor)
                return
            requested = self.rect.width * math.exp(self._pending_log_factor) * factor
            minimum = self._fit_width / self.max_magnification
            target_width = min(self._fit_width, max(minimum, requested))
            self._pending_log_factor = math.log(max(target_width, 1e-9) / self.rect.width)
            self._anchor = anchor
            self._last_tick = monotonic()
            if abs(self._pending_log_factor) > 1e-5 and not self._timer.running:
                self._timer.start()

        def _queue_wheel_zoom(self, wheel_delta: float, pos: Any) -> None:
            if not self._fit_width or self.rect.width <= 0:
                return
            try:
                mapped = self._scene_transform.imap(pos)
                anchor = (float(mapped[0]), float(mapped[1]))
            except Exception:
                anchor = tuple(self.center[:2])
            wheel_delta = max(-4.0, min(4.0, float(wheel_delta)))
            requested = self.rect.width * math.exp(self._pending_log_factor)
            requested *= math.exp(-wheel_delta * math.log(2.0) / 5.0)
            minimum = self._fit_width / self.max_magnification
            target_width = min(self._fit_width, max(minimum, requested))
            if not self.smooth_enabled:
                super().zoom(target_width / self.rect.width, anchor)
                return
            self._pending_log_factor = math.log(max(target_width, 1e-9) / self.rect.width)
            self._anchor = anchor
            self._last_tick = monotonic()
            if abs(self._pending_log_factor) > 1e-5 and not self._timer.running:
                self._timer.start()

        def _tick(self, _event: Any = None) -> None:
            if abs(self._pending_log_factor) < 0.00035 or self._anchor is None:
                if self._anchor is not None and self._pending_log_factor:
                    super().zoom(math.exp(self._pending_log_factor), self._anchor)
                self.stop_animation()
                return
            now = monotonic()
            dt = min(0.05, max(1 / 240, now - self._last_tick))
            self._last_tick = now
            fraction = 1.0 - math.exp(-dt / 0.065)
            step = self._pending_log_factor * fraction
            super().zoom(math.exp(step), self._anchor)
            self._pending_log_factor -= step

        def viewbox_mouse_event(self, event: Any) -> None:
            if event.handled or not self.interactive:
                return
            if event.type == "mouse_wheel":
                self._queue_wheel_zoom(float(event.delta[1]), event.pos)
                event.handled = True
                return
            if event.type == "mouse_press" and event.button in (1, 2):
                self.stop_animation()
            super().viewbox_mouse_event(event)


    class QtGpuPreviewWidget(QWidget):
        """The production central preview: VisPy's native OpenGL widget."""

        def __init__(self, owner: "QtPhotoCuller") -> None:
            super().__init__()
            self.owner = owner
            self.setObjectName("gpuPreview")
            self.setMinimumSize(420, 300)
            self.current_path: Path | None = None
            self.image_size = (0, 0)
            self.source_size = (0, 0)
            self._pixels: np.ndarray | None = None
            self._pending_image: tuple[Image.Image, Path] | None = None
            self._gpu_initialized = False
            self.canvas = scene.SceneCanvas(keys=None, show=False, bgcolor="#0d1015", vsync=True)
            self.view = self.canvas.central_widget.add_view()
            self.camera = QtSmoothPanZoomCamera(
                aspect=1,
                max_magnification=owner._gpu_max_magnification(),
            )
            self.camera.flip = (False, True, False)
            self.view.camera = self.camera
            self.visual = scene.visuals.Image(
                None,
                interpolation=owner.gpu_preview_interpolation,
                method="subdivide",
                parent=self.view.scene,
            )
            self.native = self.canvas.native
            self.native.setObjectName("vispyCanvas")
            self.native.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.native.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.message_label = QLabel("打开一个照片文件夹开始选片")
            self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.message_label.setStyleSheet("color: #bdc3cd; font-size: 18px; background: #0d1015;")
            self.message_label.setWordWrap(True)
            self._stack = QStackedLayout(self)
            self._stack.setContentsMargins(0, 0, 0, 0)
            self._stack.addWidget(self.native)
            self._stack.addWidget(self.message_label)
            self._stack.setCurrentWidget(self.message_label)
            self.gpu_info: dict[str, object] = {
                "backend": self.canvas.app.backend_name,
                "vendor": "pending",
                "renderer": "pending",
                "opengl": "pending",
                "max_texture_size": 0,
            }

        @staticmethod
        def _decode_gl_string(value: bytes | None) -> str:
            return (value or b"").decode("utf-8", "replace") or "unknown"

        def initialize_after_show(self) -> None:
            """Create/query the GL context only after the native canvas is shown."""
            if self._gpu_initialized:
                return
            try:
                self.canvas.set_current()
                self.gpu_info = {
                    "backend": self.canvas.app.backend_name,
                    "vendor": self._decode_gl_string(GL.glGetString(GL.GL_VENDOR)),
                    "renderer": self._decode_gl_string(GL.glGetString(GL.GL_RENDERER)),
                    "opengl": self._decode_gl_string(GL.glGetString(GL.GL_VERSION)),
                    "max_texture_size": int(GL.glGetIntegerv(GL.GL_MAX_TEXTURE_SIZE)),
                }
                self._gpu_initialized = True
            except Exception as exc:
                self.gpu_info = {
                    "backend": self.canvas.app.backend_name,
                    "vendor": "unavailable",
                    "renderer": f"unavailable ({exc})",
                    "opengl": "unavailable",
                    "max_texture_size": 0,
                }
                # Keep the widget usable when the driver does not expose a
                # queryable extension; VisPy will report a drawing error later.
                self._gpu_initialized = True
            if self._pending_image is not None:
                image, path = self._pending_image
                self._pending_image = None
                self._set_image_internal(image, path)

        def showEvent(self, event: Any) -> None:  # noqa: N802
            super().showEvent(event)
            QTimer.singleShot(0, self.initialize_after_show)

        def resizeEvent(self, event: Any) -> None:  # noqa: N802
            super().resizeEvent(event)
            if self.current_path is not None and self.camera.magnification <= 1.01:
                QTimer.singleShot(0, self.fit_image)

        def set_image(self, image: Image.Image, path: Path) -> None:
            if not self._gpu_initialized:
                self._pending_image = (image, path)
                if self.isVisible():
                    self.initialize_after_show()
                return
            self._set_image_internal(image, path)

        def _set_image_internal(self, image: Image.Image, path: Path) -> None:
            self.canvas.set_current()
            rgb = image.convert("RGB")
            self.source_size = (rgb.width, rgb.height)
            max_texture = int(self.gpu_info.get("max_texture_size") or 0)
            if max_texture and max(rgb.width, rgb.height) > max_texture:
                scale = max_texture / max(rgb.width, rgb.height)
                rgb = rgb.resize(
                    (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            pixels = np.ascontiguousarray(np.asarray(rgb))
            self.visual.set_data(pixels)
            # VisPy can retain the previous interpolation lookup dimensions;
            # explicitly invalidate them when a new capture has another size.
            if hasattr(self.visual, "_need_interpolation_update"):
                self.visual._need_interpolation_update = True
            self._pixels = pixels
            self.current_path = Path(path)
            self.image_size = (int(pixels.shape[1]), int(pixels.shape[0]))
            self._stack.setCurrentWidget(self.native)
            self.fit_image()
            self.canvas.update()

        def clear_image(self, message: str = "打开一个照片文件夹开始选片") -> None:
            self._pending_image = None
            self.camera.stop_animation()
            if self._gpu_initialized:
                try:
                    self.canvas.set_current()
                except Exception:
                    pass
            try:
                self.visual.set_data(None)
                if hasattr(self.visual, "_need_interpolation_update"):
                    self.visual._need_interpolation_update = True
            except Exception:
                pass
            self._pixels = None
            self.current_path = None
            self.image_size = (0, 0)
            self.source_size = (0, 0)
            self.show_message(message)

        def show_message(self, message: str) -> None:
            self.message_label.setText(message)
            self._stack.setCurrentWidget(self.message_label)

        def fit_image(self) -> None:
            width, height = self.image_size
            if not width or not height or not self._gpu_initialized:
                return
            self.camera.stop_animation()
            self.camera.set_range(x=(0, width), y=(0, height), margin=0)
            self.camera.remember_fit()
            self.canvas.update()

        def actual_pixels(self) -> None:
            if not self.camera._fit_width or self.camera.rect.width <= 0:
                return
            physical_width = max(1.0, float(self.canvas.physical_size[0]))
            target_width = min(self.camera._fit_width, physical_width)
            factor = target_width / (self.camera.rect.width * math.exp(self.camera._pending_log_factor))
            self.camera.smooth_zoom_factor(factor)

        def set_interpolation(self, name: str) -> None:
            if name not in GPU_PREVIEW_INTERPOLATIONS:
                return
            if self._gpu_initialized:
                try:
                    self.canvas.set_current()
                except Exception:
                    pass
            self.visual.interpolation = name
            if hasattr(self.visual, "_need_interpolation_update"):
                self.visual._need_interpolation_update = True
            self.canvas.update()

        def set_smooth_zoom(self, enabled: bool) -> None:
            self.camera.set_smooth_enabled(enabled)

        def set_max_magnification(self, value: float) -> None:
            self.camera.set_max_magnification(value)

        @property
        def magnification(self) -> float:
            return self.camera.magnification

        def close_canvas(self) -> None:
            self.camera.stop_animation()
            # Detach VisPy's QGLWidget before its QMainWindow parent starts
            # tearing down children.  QGLWidget's backend performs a
            # make-current/done-current cycle in ``canvas.close()``; keeping
            # it in a layout that is being destroyed can crash frozen
            # windowed builds during interpreter shutdown.
            try:
                self._stack.removeWidget(self.native)
                self.native.hide()
                self.native.setParent(None)
            except Exception:
                pass
            try:
                self.canvas.close()
            except Exception:
                pass


    class QtThumbnailModel(QAbstractListModel):
        def __init__(self, owner: "QtPhotoCuller") -> None:
            super().__init__(owner)
            self.owner = owner

        def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
            if parent.isValid():
                return 0
            return len(self.owner.visible_items)

        def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
            if not index.isValid() or not (0 <= index.row() < len(self.owner.visible_items)):
                return None
            item = self.owner.visible_items[index.row()]
            if role == int(Qt.ItemDataRole.UserRole):
                return item
            if role == int(Qt.ItemDataRole.DisplayRole):
                return self.owner._display_path(item.primary)
            return None

        def refresh(self) -> None:
            self.beginResetModel()
            self.endResetModel()

        def notify_all(self) -> None:
            count = self.rowCount()
            if count:
                self.dataChanged.emit(self.index(0, 0), self.index(count - 1, 0))


    class QtThumbnailDelegate(QStyledItemDelegate):
        def __init__(self, owner: "QtPhotoCuller", parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.owner = owner

        def sizeHint(self, option: Any, index: QModelIndex) -> QSize:  # noqa: N802
            return QSize(THUMB_SLOT, 124)

        def paint(self, painter: QPainter, option: Any, index: QModelIndex) -> None:
            item = index.data(int(Qt.ItemDataRole.UserRole))
            if not isinstance(item, PhotoGroup):
                return
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            tile = option.rect.adjusted(3, 4, -3, -4)
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            painter.fillRect(tile, QColor("#15171b"))
            border = QColor("#4f9cff" if selected else "#343944")
            painter.setPen(QPen(border, 3 if selected else 1))
            painter.drawRoundedRect(tile, 3, 3)

            image_rect = tile.adjusted(5, 5, -5, -24)
            cache_key = self.owner._thumbnail_cache_key(item.primary)
            pixmap = self.owner._thumbnail_cache_get(cache_key)
            if pixmap is not None and not pixmap.isNull():
                scaled = pixmap.scaled(
                    image_rect.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                draw_x = image_rect.x() + (image_rect.width() - scaled.width()) // 2
                draw_y = image_rect.y() + (image_rect.height() - scaled.height()) // 2
                painter.drawPixmap(draw_x, draw_y, scaled)
            else:
                painter.fillRect(image_rect, QColor("#202329"))
                if cache_key in self.owner._thumbnail_errors:
                    painter.setPen(QColor("#aab0ba"))
                    painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, "无法预览")
                else:
                    self.owner._request_thumbnail_job(cache_key, item.primary)
                    painter.setPen(QColor("#68717f"))
                    painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, "加载中…")

            if item.key in self.owner.kept:
                painter.setPen(QColor("#ffd35a"))
                painter.setFont(QFont("Segoe UI Symbol", 12, QFont.Weight.Bold))
                painter.drawText(tile.adjusted(7, 5, 0, 0), "★")
            if item.paired_raw_jpeg:
                painter.setPen(QColor("#8bd7ff"))
                painter.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
                mode = self.owner._pair_mode_label(self.owner._pair_mode(item)) if item.key in self.owner.kept else "未保留"
                painter.drawText(tile.adjusted(0, 5, -7, 0), Qt.AlignmentFlag.AlignRight, mode)

            label = self.owner._display_path(item.primary)
            if len(label) > 21:
                label = "…" + label[-20:]
            painter.setPen(QColor("#d9dde5"))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(tile.adjusted(5, tile.height() - 21, -5, -3), Qt.AlignmentFlag.AlignCenter, label)
            painter.restore()


    class QtPhotoCuller(QMainWindow):
        """Production Qt gallery using the prototype's native VisPy render path."""

        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle(APP_NAME)
            self.resize(1280, 820)
            self.setMinimumSize(900, 620)
            self.setStyleSheet(
                """
                QMainWindow, QWidget { background: #17191d; color: #e7e9ed; }
                QFrame#toolbar, QFrame#info, QFrame#sidebar { background: #202329; }
                QLabel { color: #e7e9ed; }
                QLabel#muted { color: #a8adb7; }
                QLabel#header { color: #e7e9ed; font-size: 14px; font-weight: 600; }
                QPushButton { min-height: 31px; padding: 4px 9px; }
                QPushButton#keepButton { font-weight: 600; min-height: 36px; }
                QGroupBox { border: 1px solid #343944; border-radius: 4px; margin-top: 10px; padding-top: 8px; }
                QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #e7e9ed; }
                QComboBox, QCheckBox { min-height: 27px; }
                QSplitter::handle { background: #343944; }
                QScrollBar:horizontal { height: 12px; background: #17191d; }
                """
            )

            self.folder: Path | None = None
            self._scan_folder: Path | None = None
            self._scan_notice = ""
            self._scan_generation = 0
            self._scan_future: Future[None] | None = None
            self._scan_events: queue.Queue[tuple[int, str, object]] = queue.Queue()
            self._scan_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photo-culler-scan")
            self._closing = False
            self.sidebar_width = self._load_sidebar_width()
            gpu_settings = self._load_gpu_preview_settings()
            self.gpu_preview_interpolation = str(gpu_settings["interpolation"])
            self.gpu_preview_smooth_zoom = bool(gpu_settings["smooth_zoom"])
            self.gpu_preview_max_zoom = str(gpu_settings["max_magnification"])

            self.all_items: list[PhotoGroup] = []
            self.index = 0
            self.kept: set[str] = set()
            self.pair_modes: dict[str, str] = {}
            self.show_kept_only = False
            self.current_source_image: Image.Image | None = None
            self.current_source_path: Path | None = None

            self.jpeg_cache: dict[str, Image.Image] = {}
            self._jpeg_cache_lock = Lock()
            self._preload_events: queue.Queue[tuple[int, int, int, bool]] = queue.Queue()
            self._preload_generation = 0
            self._preload_done = True

            self.thumb_width = THUMB_WIDTH
            self.thumb_height = THUMB_HEIGHT
            self.thumbnail_cache: OrderedDict[ThumbnailKey, QPixmap] = OrderedDict()
            self._thumbnail_jobs: dict[ThumbnailKey, Future[None]] = {}
            self._thumbnail_events: queue.Queue[tuple[int, ThumbnailKey, Image.Image | None, Exception | None]] = queue.Queue()
            self._thumbnail_generation = 0
            self._thumbnail_errors: set[ThumbnailKey] = set()
            self._thumbnail_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="photo-culler-thumbnail")

            self._build_qt_ui()
            self._bind_qt_keys()
            self.scan_timer = QTimer(self)
            self.scan_timer.timeout.connect(self._poll_scan_events)
            self.scan_timer.start(60)
            self.thumbnail_timer = QTimer(self)
            self.thumbnail_timer.timeout.connect(self._poll_thumbnail_events)
            self.thumbnail_timer.start(THUMB_RENDER_POLL_MS)
            self.preload_timer = QTimer(self)
            self.preload_timer.timeout.connect(self._poll_preload_events)
            self.preload_timer.start(75)
            self.status_timer = QTimer(self)
            self.status_timer.timeout.connect(self._refresh_status)
            self.status_timer.start(100)
            QTimer.singleShot(0, self._restore_sidebar_width)
            QTimer.singleShot(250, self.open_folder)

        @property
        def visible_items(self) -> list[PhotoGroup]:
            if not self.show_kept_only:
                return self.all_items
            return [item for item in self.all_items if item.key in self.kept]

        @staticmethod
        def _pair_mode_label(mode: str) -> str:
            return {"both": "RAW+JPG", "raw": "仅 RAW", "jpg": "仅 JPG"}.get(mode, "RAW+JPG")

        def _pair_mode(self, item: PhotoGroup) -> str:
            mode = self.pair_modes.get(item.key, "both")
            return mode if mode in {"both", "raw", "jpg"} else "both"

        @property
        def current_item(self) -> PhotoGroup | None:
            items = self.visible_items
            if not items:
                return None
            self.index = min(max(self.index, 0), len(items) - 1)
            return items[self.index]

        def _build_qt_ui(self) -> None:
            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.setChildrenCollapsible(False)
            self.layout_splitter = splitter

            main_column = QWidget()
            main_layout = QVBoxLayout(main_column)
            main_layout.setContentsMargins(16, 16, 8, 8)
            main_layout.setSpacing(8)
            toolbar = QFrame()
            toolbar.setObjectName("toolbar")
            toolbar_layout = QHBoxLayout(toolbar)
            toolbar_layout.setContentsMargins(14, 8, 14, 8)
            self.folder_label = QLabel("尚未打开文件夹")
            self.folder_label.setObjectName("header")
            toolbar_layout.addWidget(self.folder_label)
            toolbar_layout.addStretch(1)
            main_layout.addWidget(toolbar)

            self.preview_widget = QtGpuPreviewWidget(self)
            main_layout.addWidget(self.preview_widget, 1)

            info = QFrame()
            info.setObjectName("info")
            info_layout = QHBoxLayout(info)
            info_layout.setContentsMargins(8, 4, 8, 4)
            self.status_label = QLabel("")
            self.preload_label = QLabel("")
            self.preload_label.setObjectName("muted")
            self.help_label = QLabel("[ ] 切换 · Space 保留 · F 模式 · 滚轮缩放 · 拖动平移 · Z 适合/100%")
            self.help_label.setObjectName("muted")
            info_layout.addWidget(self.status_label, 1)
            info_layout.addWidget(self.preload_label)
            info_layout.addStretch(1)
            info_layout.addWidget(self.help_label)
            main_layout.addWidget(info)

            self.thumbnail_model = QtThumbnailModel(self)
            self.thumbnail_view = QListView()
            self.thumbnail_view.setObjectName("thumbnailView")
            self.thumbnail_view.setModel(self.thumbnail_model)
            self.thumbnail_view.setItemDelegate(QtThumbnailDelegate(self, self.thumbnail_view))
            self.thumbnail_view.setViewMode(QListView.ViewMode.IconMode)
            self.thumbnail_view.setFlow(QListView.Flow.LeftToRight)
            self.thumbnail_view.setWrapping(False)
            self.thumbnail_view.setResizeMode(QListView.ResizeMode.Adjust)
            self.thumbnail_view.setMovement(QListView.Movement.Static)
            self.thumbnail_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.thumbnail_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
            self.thumbnail_view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
            self.thumbnail_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.thumbnail_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.thumbnail_view.setFixedHeight(142)
            self.thumbnail_view.setSpacing(0)
            self.thumbnail_view.setUniformItemSizes(True)
            self.thumbnail_view.clicked.connect(self._thumbnail_clicked)
            self.thumbnail_view.horizontalScrollBar().valueChanged.connect(
                lambda _value: self._request_visible_thumbnails()
            )
            main_layout.addWidget(self.thumbnail_view)

            sidebar = QFrame()
            sidebar.setObjectName("sidebar")
            sidebar.setMinimumWidth(SIDEBAR_WIDTH_MIN)
            sidebar.setMaximumWidth(SIDEBAR_WIDTH_MAX)
            sidebar_layout = QVBoxLayout(sidebar)
            sidebar_layout.setContentsMargins(12, 14, 12, 14)
            sidebar_layout.setSpacing(8)
            header = QLabel("操作")
            header.setObjectName("header")
            sidebar_layout.addWidget(header)

            def add_button(text: str, slot: Callable[[], object], object_name: str = "") -> QPushButton:
                button = QPushButton(text)
                if object_name:
                    button.setObjectName(object_name)
                button.clicked.connect(slot)
                sidebar_layout.addWidget(button)
                return button

            add_button("打开照片文件夹  O", self.open_folder)
            self.keep_button = add_button("保留  Space", self.toggle_keep, "keepButton")
            self.keep_mode_button = add_button("模式：单文件  F", self.cycle_keep_mode)
            add_button("全不保留", self.clear_all_kept)
            add_button("重置模式", self.reset_all_pair_modes)
            add_button("导出保留照片  E", self.export_kept)
            sidebar_layout.addSpacing(4)
            add_button("适合屏幕  Z", self.zoom_fit)
            add_button("100%  1", self.zoom_actual)
            self.zoom_label = QLabel("适合屏幕")
            self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.zoom_label.setStyleSheet("color: #8bd7ff; font-weight: 600;")
            sidebar_layout.addWidget(self.zoom_label)

            gpu_group = QGroupBox("GPU 预览设置")
            gpu_layout = QVBoxLayout(gpu_group)
            interpolation_row = QHBoxLayout()
            interpolation_row.addWidget(QLabel("插值"))
            self.gpu_preview_interpolation_combo = QComboBox()
            self.gpu_preview_interpolation_combo.addItems(list(GPU_PREVIEW_INTERPOLATIONS))
            self.gpu_preview_interpolation_combo.setCurrentText(self.gpu_preview_interpolation)
            interpolation_row.addWidget(self.gpu_preview_interpolation_combo, 1)
            gpu_layout.addLayout(interpolation_row)
            self.gpu_preview_smooth_check = QCheckBox("平滑滚轮缩放")
            self.gpu_preview_smooth_check.setChecked(self.gpu_preview_smooth_zoom)
            gpu_layout.addWidget(self.gpu_preview_smooth_check)
            max_row = QHBoxLayout()
            max_row.addWidget(QLabel("最大倍率"))
            self.gpu_preview_max_zoom_combo = QComboBox()
            self.gpu_preview_max_zoom_combo.addItems(list(GPU_PREVIEW_MAX_MAGNIFICATIONS))
            self.gpu_preview_max_zoom_combo.setCurrentText(self.gpu_preview_max_zoom)
            max_row.addWidget(self.gpu_preview_max_zoom_combo, 1)
            gpu_layout.addLayout(max_row)
            self.gpu_preview_status_label = QLabel("GPU 主预览已启用")
            self.gpu_preview_status_label.setObjectName("muted")
            self.gpu_preview_status_label.setWordWrap(True)
            gpu_layout.addWidget(self.gpu_preview_status_label)
            sidebar_layout.addWidget(gpu_group)
            self.gpu_preview_interpolation_combo.currentTextChanged.connect(self._on_gpu_preview_settings_changed)
            self.gpu_preview_smooth_check.toggled.connect(self._on_gpu_preview_settings_changed)
            self.gpu_preview_max_zoom_combo.currentTextChanged.connect(self._on_gpu_preview_settings_changed)

            self.show_kept_checkbox = QCheckBox("只看保留")
            self.show_kept_checkbox.toggled.connect(self.toggle_filter)
            sidebar_layout.addWidget(self.show_kept_checkbox)
            sidebar_layout.addStretch(1)
            note = QLabel("拖动分隔线调整宽度")
            note.setObjectName("muted")
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sidebar_layout.addWidget(note)

            splitter.addWidget(main_column)
            splitter.addWidget(sidebar)
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 0)
            self.control_panel = sidebar
            self.setCentralWidget(splitter)
            splitter.splitterMoved.connect(
                lambda _pos, _index: QTimer.singleShot(180, self._save_sidebar_from_splitter)
            )

        def _bind_qt_keys(self) -> None:
            def add_shortcut(sequence: str, slot: Callable[[], object]) -> None:
                action = QAction(self)
                action.setShortcut(QKeySequence(sequence))
                action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
                action.triggered.connect(slot)
                self.addAction(action)

            add_shortcut("[", lambda: self.change_index(-1))
            add_shortcut("]", lambda: self.change_index(1))
            add_shortcut("Space", self.toggle_keep)
            add_shortcut("F", self.cycle_keep_mode)
            add_shortcut("O", self.open_folder)
            add_shortcut("E", self.export_kept)
            add_shortcut("Z", self.toggle_zoom)
            add_shortcut("1", self.zoom_actual)
            add_shortcut("+", lambda: self.zoom_step(1))
            add_shortcut("=", lambda: self.zoom_step(1))
            add_shortcut("-", lambda: self.zoom_step(-1))
            add_shortcut("Ctrl+Shift+X", self.clear_all_kept)
            add_shortcut("Ctrl+Shift+M", self.reset_all_pair_modes)

        def _restore_sidebar_width(self) -> None:
            sizes = self.layout_splitter.sizes()
            total = sum(sizes) or self.width()
            sidebar = max(SIDEBAR_WIDTH_MIN, min(SIDEBAR_WIDTH_MAX, self.sidebar_width))
            main = max(600, total - sidebar)
            self.layout_splitter.setSizes([main, sidebar])

        def _save_sidebar_from_splitter(self) -> None:
            sizes = self.layout_splitter.sizes()
            if len(sizes) < 2 or sizes[1] <= 1:
                return
            width = max(SIDEBAR_WIDTH_MIN, min(SIDEBAR_WIDTH_MAX, int(sizes[1])))
            if width != self.sidebar_width:
                self.sidebar_width = width
                self._save_sidebar_width()

        def _update_keep_mode_ui(self) -> None:
            item = self.current_item
            if item is not None and item.paired_raw_jpeg:
                self.keep_mode_button.setText(f"模式：{self._pair_mode_label(self._pair_mode(item))}  F")
                self.keep_mode_button.setEnabled(True)
            else:
                self.keep_mode_button.setText("模式：单文件  F")
                self.keep_mode_button.setEnabled(False)
            self.keep_button.setText("取消保留  Space" if item is not None and item.key in self.kept else "保留  Space")

        def open_folder(self) -> None:
            chosen = QFileDialog.getExistingDirectory(
                self,
                "选择包含照片的文件夹",
                str(self.folder) if self.folder else "",
            )
            if chosen:
                self._begin_folder_scan(Path(chosen))

        def _begin_folder_scan(self, folder: Path) -> None:
            self._scan_generation += 1
            generation = self._scan_generation
            if self._scan_future is not None:
                self._scan_future.cancel()
                self._scan_future = None
            while True:
                try:
                    self._scan_events.get_nowait()
                except queue.Empty:
                    break
            self.folder = None
            self._scan_folder = folder
            self._scan_notice = ""
            self.all_items = []
            self.index = 0
            self.kept = set()
            self.pair_modes = {}
            self.current_source_image = None
            self.current_source_path = None
            self._reset_thumbnail_state()
            self.thumbnail_model.refresh()
            self._start_jpeg_preload([])
            self.folder_label.setText(f"正在扫描：{folder.name or str(folder)}")
            self._set_status("正在扫描：已发现 0 张照片，已访问 0 个文件夹")
            self.preview_widget.clear_image("正在扫描照片…")
            self._scan_future = self._scan_executor.submit(self._scan_folder_worker, generation, folder)

        def _scan_folder_worker(self, generation: int, folder: Path) -> None:
            def report(found: int, directories: int, skipped_links: int, errors: int) -> None:
                if generation == self._scan_generation:
                    self._scan_events.put((generation, "progress", (found, directories, skipped_links, errors)))

            try:
                result = scan_photo_tree(
                    folder,
                    progress=report,
                    should_cancel=lambda: generation != self._scan_generation,
                )
            except Exception as exc:
                if generation == self._scan_generation:
                    self._scan_events.put((generation, "error", exc))
                return
            if not result.cancelled and generation == self._scan_generation:
                self._scan_events.put((generation, "done", result))

        def _poll_scan_events(self) -> None:
            generation = self._scan_generation
            latest_progress: tuple[int, int, int, int] | None = None
            completed: ScanResult | None = None
            failure: Exception | None = None
            while True:
                try:
                    event_generation, kind, payload = self._scan_events.get_nowait()
                except queue.Empty:
                    break
                if event_generation != generation:
                    continue
                if kind == "progress":
                    latest_progress = payload  # type: ignore[assignment]
                elif kind == "done":
                    completed = payload  # type: ignore[assignment]
                elif kind == "error" and isinstance(payload, Exception):
                    failure = payload
            if latest_progress is not None and completed is None and failure is None:
                found, directories, skipped_links, errors = latest_progress
                self._set_status(f"正在扫描：已发现 {found} 张照片，已访问 {directories} 个文件夹")
                details = []
                if skipped_links:
                    details.append(f"跳过链接 {skipped_links}")
                if errors:
                    details.append(f"读取异常 {errors}")
                self.preload_label.setText("；".join(details))
            if failure is not None:
                self._scan_future = None
                self._scan_folder = None
                self.preload_label.setText("")
                QMessageBox.critical(self, APP_NAME, f"无法扫描这个文件夹：\n{failure}")
            if completed is not None:
                self._scan_future = None
                folder = self._scan_folder
                self._scan_folder = None
                if folder is not None:
                    self._apply_scan_result(folder, completed)

        def _apply_scan_result(self, folder: Path, result: ScanResult) -> None:
            self.folder = folder
            paths = sorted(result.paths, key=lambda path: _relative_path_sort_key(path, folder))
            self.all_items = build_photo_groups(list(paths), root=folder)
            saved, saved_pair_modes = self._load_selection()
            keys = {item.key for item in self.all_items}
            self.kept = saved.intersection(keys)
            pair_keys = {item.key for item in self.all_items if item.paired_raw_jpeg}
            self.pair_modes = {
                key: mode
                for key, mode in saved_pair_modes.items()
                if key in pair_keys and mode in {"both", "raw", "jpg"}
            }
            self.folder_label.setText(folder.name or str(folder))
            notices = []
            if result.skipped_links:
                notices.append(f"跳过链接目录/文件 {result.skipped_links} 个")
            if result.errors:
                notices.append(f"跳过读取异常 {len(result.errors)} 项")
            self._scan_notice = "    " + "；".join(notices) if notices else ""
            jpeg_paths = [path for path in paths if path.suffix.casefold() in JPEG_EXTENSIONS]
            self._start_jpeg_preload(jpeg_paths)
            self.thumbnail_model.refresh()
            if not self.all_items:
                self.current_source_image = None
                self.current_source_path = None
                self.preview_widget.clear_image("这个文件夹中没有受支持的照片")
                self._set_status("支持 JPG、JPEG、PNG、TIFF、DNG" + self._scan_notice)
                self._update_keep_mode_ui()
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
            if item.key in self.kept:
                self.kept.remove(item.key)
            else:
                self.kept.add(item.key)
            self._save_selection()
            if self.show_kept_only and item.key not in self.kept:
                items = self.visible_items
                self.index = min(self.index, max(len(items) - 1, 0))
            self.thumbnail_model.refresh()
            if self.current_item is not None:
                self._show_current(center=False)
            else:
                self.preview_widget.clear_image("没有保留的照片")
                self._set_status("保留 0 张照片")
                self._update_keep_mode_ui()

        def cycle_keep_mode(self) -> None:
            item = self.current_item
            if item is None or not item.paired_raw_jpeg:
                return
            modes = ["both", "jpg", "raw"]
            current = self._pair_mode(item)
            self.pair_modes[item.key] = modes[(modes.index(current) + 1) % len(modes)]
            self._save_selection()
            self._update_keep_mode_ui()
            self._set_status(self._status_text(item))
            self.thumbnail_model.notify_all()
            self.thumbnail_view.viewport().update()

        def clear_all_kept(self) -> None:
            if not self.kept:
                QMessageBox.information(self, APP_NAME, "当前没有已保留的照片。")
                return
            answer = QMessageBox.question(
                self,
                APP_NAME,
                f"确定要取消全部 {len(self.kept)} 个保留项目吗？\n\n各组的 RAW/JPG 模式不会改变。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.kept.clear()
            self._save_selection()
            if self.show_kept_only:
                self.show_kept_only = False
                self.show_kept_checkbox.blockSignals(True)
                self.show_kept_checkbox.setChecked(False)
                self.show_kept_checkbox.blockSignals(False)
            self.index = min(self.index, max(len(self.visible_items) - 1, 0))
            self.thumbnail_model.refresh()
            if self.visible_items:
                self._show_current(center=True)
            else:
                self.preview_widget.clear_image("打开一个照片文件夹开始选片")
                self._update_keep_mode_ui()

        def reset_all_pair_modes(self) -> None:
            pair_items = [item for item in self.all_items if item.paired_raw_jpeg]
            if not pair_items:
                QMessageBox.information(self, APP_NAME, "当前文件夹没有 RAW+JPG 绑定组。")
                return
            answer = QMessageBox.question(
                self,
                APP_NAME,
                f"确定要将 {len(pair_items)} 个 RAW+JPG 绑定组的模式全部重置为 RAW+JPG 吗？\n\n各组的保留/不保留状态不会改变。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.pair_modes = {item.key: "both" for item in pair_items}
            self._save_selection()
            self._update_keep_mode_ui()
            self._set_status(self._status_text(self.current_item))
            self.thumbnail_model.notify_all()
            self.thumbnail_view.viewport().update()

        def toggle_filter(self, checked: bool | None = None) -> None:
            if checked is None:
                checked = self.show_kept_checkbox.isChecked()
            active = self.current_item
            self.show_kept_only = bool(checked)
            items = self.visible_items
            if active is not None and active in items:
                self.index = items.index(active)
            else:
                self.index = 0
            self.thumbnail_model.refresh()
            if not items:
                self.preview_widget.clear_image("没有保留的照片")
                self._set_status("保留 0 张照片")
                self._update_keep_mode_ui()
                return
            self._show_current(center=True)

        def _display_path(self, path: Path) -> str:
            if self.folder is not None:
                try:
                    return str(path.relative_to(self.folder))
                except ValueError:
                    pass
            return path.name

        def _show_current(self, center: bool) -> None:
            item = self.current_item
            if item is None:
                return
            path = item.primary
            display_path = self._display_path(path)
            self._set_status("正在载入：" + display_path)
            try:
                path_changed = self.current_source_path != path or self.current_source_image is None
                if path_changed:
                    self.current_source_image = self._load_image(path, thumbnail=False)
                    self.current_source_path = path
                    self.preview_widget.set_image(self.current_source_image, path)
                elif self.preview_widget.current_path != path:
                    self.preview_widget.set_image(self.current_source_image, path)
            except Exception as exc:
                self.current_source_image = None
                self.current_source_path = None
                self.preview_widget.clear_image(f"无法显示\n{display_path}\n\n{exc}")
            self._set_status(self._status_text(item))
            self._update_keep_mode_ui()
            self.thumbnail_model.notify_all()
            self.thumbnail_view.viewport().update()
            self._sync_thumbnail_selection(center)

        def _load_image(self, path: Path, thumbnail: bool) -> Image.Image:
            if path.suffix.lower() != ".dng":
                if path.suffix.lower() in JPEG_EXTENSIONS:
                    key = str(path.resolve())
                    with self._jpeg_cache_lock:
                        cached = self.jpeg_cache.get(key)
                    if cached is not None:
                        return cached
                    image = self._read_raster_image(path)
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
                    array = raw.postprocess(use_camera_wb=True, no_auto_bright=False, half_size=True, output_bps=8)
                    return Image.fromarray(array).convert("RGB")

        @staticmethod
        def _read_raster_image(path: Path) -> Image.Image:
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened)
                return image.convert("RGB").copy()

        @staticmethod
        def _fit_for_display(image: Image.Image, max_width: int, max_height: int) -> Image.Image:
            scale = min(max_width / image.width, max_height / image.height, 1.0)
            if scale >= 1.0:
                return image
            size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            return image.resize(size, Image.Resampling.LANCZOS)

        def _set_status(self, text: str) -> None:
            self.status_label.setText(text)

        def _status_text(self, item: PhotoGroup | None) -> str:
            if item is None:
                return f"保留 {len(self.kept)} 张照片" + self._scan_notice
            prefix = "★ 已保留" if item.key in self.kept else "未保留"
            if item.paired_raw_jpeg:
                paired = f"    绑定组：{self._pair_mode_label(self._pair_mode(item))}"
            else:
                paired = ""
            return (
                f"{self.index + 1} / {len(self.visible_items)}    {prefix}    "
                f"已保留 {len(self.kept)} 个项目{paired}    {self._display_path(item.primary)}{self._scan_notice}"
            )

        @staticmethod
        def _pixmap_from_image(image: Image.Image) -> QPixmap:
            rgb = image.convert("RGB")
            raw = rgb.tobytes("raw", "RGB")
            qimage = QImage(raw, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888).copy()
            return QPixmap.fromImage(qimage)

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

        def _thumbnail_cache_get(self, key: ThumbnailKey) -> QPixmap | None:
            pixmap = self.thumbnail_cache.get(key)
            if pixmap is not None:
                self.thumbnail_cache.move_to_end(key)
            return pixmap

        def _thumbnail_cache_put(self, key: ThumbnailKey, pixmap: QPixmap) -> None:
            self.thumbnail_cache[key] = pixmap
            self.thumbnail_cache.move_to_end(key)
            while len(self.thumbnail_cache) > THUMB_CACHE_LIMIT:
                self.thumbnail_cache.popitem(last=False)

        def _request_thumbnail_job(self, key: ThumbnailKey, path: Path) -> None:
            if self._closing or key in self.thumbnail_cache or key in self._thumbnail_jobs or key in self._thumbnail_errors:
                return
            self._thumbnail_jobs[key] = self._thumbnail_executor.submit(
                self._thumbnail_worker,
                self._thumbnail_generation,
                key,
                path,
            )

        def _thumbnail_worker(self, generation: int, key: ThumbnailKey, path: Path) -> None:
            try:
                image = self._load_image(path, thumbnail=True)
                image = self._fit_for_display(image, self.thumb_width, self.thumb_height)
                if image.width < self.thumb_width or image.height < self.thumb_height:
                    background = Image.new("RGB", (self.thumb_width, self.thumb_height), "#202329")
                    background.paste(image, ((self.thumb_width - image.width) // 2, (self.thumb_height - image.height) // 2))
                    image = background
                elif image.mode != "RGB":
                    image = image.convert("RGB")
                self._thumbnail_events.put((generation, key, image, None))
            except Exception as exc:
                self._thumbnail_events.put((generation, key, None, exc))

        def _poll_thumbnail_events(self) -> None:
            changed = False
            while True:
                try:
                    generation, key, image, error = self._thumbnail_events.get_nowait()
                except queue.Empty:
                    break
                if generation != self._thumbnail_generation:
                    continue
                self._thumbnail_jobs.pop(key, None)
                if image is None:
                    self._thumbnail_errors.add(key)
                else:
                    try:
                        self._thumbnail_cache_put(key, self._pixmap_from_image(image))
                        self._thumbnail_errors.discard(key)
                    except Exception:
                        self._thumbnail_errors.add(key)
                changed = True
            if changed:
                self.thumbnail_view.viewport().update()
                self._request_visible_thumbnails()

        def _request_visible_thumbnails(self) -> None:
            if self._closing:
                return
            items = self.visible_items
            if not items:
                return
            count = len(items)
            viewport_width = max(1, self.thumbnail_view.viewport().width())
            first_index = self.thumbnail_view.indexAt(QPoint(1, 4))
            last_index = self.thumbnail_view.indexAt(QPoint(viewport_width - 1, 4))
            first = first_index.row() if first_index.isValid() else 0
            last = last_index.row() if last_index.isValid() else min(count - 1, max(0, viewport_width // THUMB_SLOT + 1))
            first = max(0, first - THUMB_RENDER_OVERSCAN)
            last = min(count - 1, last + THUMB_RENDER_OVERSCAN)
            for row in range(first, last + 1):
                item = items[row]
                key = self._thumbnail_cache_key(item.primary)
                if self._thumbnail_cache_get(key) is None:
                    self._request_thumbnail_job(key, item.primary)

        def _reset_thumbnail_state(self) -> None:
            self._thumbnail_generation += 1
            for future in self._thumbnail_jobs.values():
                future.cancel()
            self._thumbnail_jobs.clear()
            self.thumbnail_cache.clear()
            self._thumbnail_errors.clear()
            while True:
                try:
                    self._thumbnail_events.get_nowait()
                except queue.Empty:
                    break

        def _thumbnail_clicked(self, model_index: QModelIndex) -> None:
            if model_index.isValid() and 0 <= model_index.row() < len(self.visible_items):
                self.index = model_index.row()
                self._show_current(center=False)

        def _sync_thumbnail_selection(self, center: bool) -> None:
            if not hasattr(self, "thumbnail_view"):
                return
            model_index = self.thumbnail_model.index(self.index, 0) if self.thumbnail_model.rowCount() else QModelIndex()
            selection_model = self.thumbnail_view.selectionModel()
            if selection_model is not None:
                selection_model.blockSignals(True)
                self.thumbnail_view.setCurrentIndex(model_index)
                if model_index.isValid():
                    selection_model.select(model_index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                else:
                    selection_model.clearSelection()
                selection_model.blockSignals(False)
            if center and model_index.isValid():
                self.thumbnail_view.scrollTo(model_index, QAbstractItemView.ScrollHint.PositionAtCenter)
            self._request_visible_thumbnails()

        def zoom_fit(self) -> None:
            self.preview_widget.fit_image()
            self._refresh_status()

        def zoom_actual(self) -> None:
            self.preview_widget.actual_pixels()
            self._refresh_status()

        def toggle_zoom(self) -> None:
            if self.preview_widget.magnification <= 1.01:
                self.preview_widget.actual_pixels()
            else:
                self.preview_widget.fit_image()
            self._refresh_status()

        def zoom_step(self, direction: int) -> None:
            self.preview_widget.camera.smooth_zoom_factor(1.25 if direction > 0 else 1 / 1.25)
            self._refresh_status()

        def _refresh_status(self) -> None:
            if self.preview_widget.current_path is not None:
                self.zoom_label.setText(f"缩放：{self.preview_widget.magnification:.2f}×（相对适屏）")
            else:
                self.zoom_label.setText("适合屏幕")
            info = self.preview_widget.gpu_info
            if info.get("renderer") not in {"pending", "unknown"}:
                self.gpu_preview_status_label.setText(
                    f"GPU 主预览已启用\n{info.get('renderer', 'unknown')}\nOpenGL {info.get('opengl', 'unknown')}"
                )

        def _gpu_max_magnification(self) -> float:
            try:
                value = float(self.gpu_preview_max_zoom)
            except (TypeError, ValueError):
                value = float(GPU_PREVIEW_DEFAULT_MAX_MAGNIFICATION)
            return max(1.0, min(64.0, value))

        def _on_gpu_preview_settings_changed(self, _value: object = None) -> None:
            settings = normalize_gpu_preview_settings(
                {
                    "interpolation": self.gpu_preview_interpolation_combo.currentText(),
                    "smooth_zoom": self.gpu_preview_smooth_check.isChecked(),
                    "max_magnification": self.gpu_preview_max_zoom_combo.currentText(),
                }
            )
            self.gpu_preview_interpolation = str(settings["interpolation"])
            self.gpu_preview_smooth_zoom = bool(settings["smooth_zoom"])
            self.gpu_preview_max_zoom = str(settings["max_magnification"])
            self._save_gpu_preview_settings()
            self.preview_widget.set_interpolation(self.gpu_preview_interpolation)
            self.preview_widget.set_smooth_zoom(self.gpu_preview_smooth_zoom)
            self.preview_widget.set_max_magnification(self._gpu_max_magnification())
            self.gpu_preview_status_label.setText(
                f"GPU 主预览已启用\n{self.gpu_preview_interpolation} / 最大 {self.gpu_preview_max_zoom}×"
            )

        def export_kept(self) -> None:
            if not self.kept:
                QMessageBox.information(self, APP_NAME, "还没有保留照片。按 Space 标记后再导出。")
                return
            destination = QFileDialog.getExistingDirectory(self, "选择导出保留照片的文件夹", "")
            if not destination:
                return
            destination_path = Path(destination)
            kept_items = [item for item in self.all_items if item.key in self.kept]
            sources = [path for item in kept_items for path in self._selected_members(item)]
            answer = QMessageBox.question(
                self,
                APP_NAME,
                f"将导出 {len(kept_items)} 个保留项目（共 {len(sources)} 个原始文件）到：\n{destination_path}\n\nRAW+JPG 组按当前模式导出。原照片不会被移动或修改。继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            copied = 0
            failures: list[str] = []
            for source in sources:
                try:
                    shutil.copy2(source, self._unique_destination(destination_path, source.name))
                    copied += 1
                except OSError as exc:
                    failures.append(f"{source.name}: {exc}")
                self._set_status(f"正在导出 {copied}/{len(sources)}…")
                QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            if failures:
                QMessageBox.warning(self, APP_NAME, f"已复制 {copied} 张；{len(failures)} 张未能复制。\n\n" + "\n".join(failures[:3]))
            else:
                QMessageBox.information(self, APP_NAME, f"已复制 {copied} 张保留照片。")
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

        def _start_jpeg_preload(self, jpeg_paths: list[Path]) -> None:
            self._preload_generation += 1
            generation = self._preload_generation
            self._preload_done = not jpeg_paths
            with self._jpeg_cache_lock:
                self.jpeg_cache = {}
            if not jpeg_paths:
                self.preload_label.setText("")
                return
            self.preload_label.setText(f"正在预载 JPG：0 / {len(jpeg_paths)}")
            cache = self.jpeg_cache
            Thread(
                target=self._preload_jpegs,
                args=(generation, jpeg_paths, cache),
                daemon=True,
                name="photo-culler-jpeg-preload",
            ).start()

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
                    pass
                if number == 1 or number == total or number % 10 == 0:
                    self._preload_events.put((generation, number, total, False))
            self._preload_events.put((generation, total, total, True))

        def _poll_preload_events(self) -> None:
            generation = self._preload_generation
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
                self.preload_label.setText(
                    f"JPG 已预载：{completed} 张" if done else f"正在预载 JPG：{completed} / {total}"
                )

        def _settings_file(self) -> Path:
            return Path.home() / "AppData" / "Local" / "PhotoCuller" / "settings.json"

        def _load_settings_data(self) -> dict[str, object]:
            try:
                data = json.loads(self._settings_file().read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        def _write_settings_data(self, data: dict[str, object]) -> None:
            try:
                settings_file = self._settings_file()
                settings_file.parent.mkdir(parents=True, exist_ok=True)
                settings_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass

        def _load_sidebar_width(self) -> int:
            data = self._load_settings_data()
            try:
                width = int(data.get("sidebar_width", SIDEBAR_WIDTH_DEFAULT))
            except (ValueError, TypeError):
                width = SIDEBAR_WIDTH_DEFAULT
            return max(SIDEBAR_WIDTH_MIN, min(SIDEBAR_WIDTH_MAX, width))

        def _save_sidebar_width(self) -> None:
            data = self._load_settings_data()
            data["sidebar_width"] = self.sidebar_width
            self._write_settings_data(data)

        def _load_gpu_preview_settings(self) -> dict[str, object]:
            data = self._load_settings_data()
            return normalize_gpu_preview_settings(data.get("gpu_preview", {}))

        def _save_gpu_preview_settings(self) -> None:
            data = self._load_settings_data()
            data["gpu_preview"] = {
                "interpolation": self.gpu_preview_interpolation,
                "smooth_zoom": self.gpu_preview_smooth_zoom,
                "max_magnification": self.gpu_preview_max_zoom,
            }
            self._write_settings_data(data)

        def _selection_file(self) -> Path:
            assert self.folder is not None
            digest = hashlib.sha256(str(self.folder.resolve()).encode("utf-8")).hexdigest()[:20]
            folder = Path.home() / "AppData" / "Local" / "PhotoCuller" / "selections"
            folder.mkdir(parents=True, exist_ok=True)
            return folder / f"{digest}.json"

        def _load_selection(self) -> tuple[set[str], dict[str, str]]:
            if self.folder is None:
                return set(), {}
            try:
                data = json.loads(self._selection_file().read_text(encoding="utf-8"))
                kept = {key for key in data.get("kept", []) if isinstance(key, str)}
                modes = data.get("pair_modes", {})
                if not isinstance(modes, dict):
                    modes = {}
                pair_modes = {
                    key: mode
                    for key, mode in modes.items()
                    if isinstance(key, str) and isinstance(mode, str)
                }
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

        def closeEvent(self, event: Any) -> None:  # noqa: N802
            self._on_close()
            event.accept()

        def _on_close(self) -> None:
            if self._closing:
                return
            self._closing = True
            self._scan_generation += 1
            self._preload_generation += 1
            self._thumbnail_generation += 1
            for timer_name in ("scan_timer", "thumbnail_timer", "preload_timer", "status_timer"):
                timer = getattr(self, timer_name, None)
                if timer is not None:
                    timer.stop()
            if self._scan_future is not None:
                self._scan_future.cancel()
            for future in self._thumbnail_jobs.values():
                future.cancel()
            self._thumbnail_jobs.clear()
            self.preview_widget.close_canvas()
            self._thumbnail_executor.shutdown(wait=False, cancel_futures=True)
            self._scan_executor.shutdown(wait=False, cancel_futures=True)


else:

    class QtPhotoCuller:  # pragma: no cover - dependency fallback
        def __init__(self) -> None:
            detail = f": {QT_PHOTO_CULLER_IMPORT_ERROR}" if QT_PHOTO_CULLER_IMPORT_ERROR else ""
            raise RuntimeError("PySide6 + VisPy GPU 主预览依赖未安装" + detail)


def _run_qt_main_self_test() -> int:
    """Smoke-test the production Qt main window without opening a folder dialog."""
    if not QT_PHOTO_CULLER_AVAILABLE:
        print(json.dumps({"passed": False, "error": str(QT_PHOTO_CULLER_IMPORT_ERROR)}, ensure_ascii=False))
        return 1
    original_open_folder = QtPhotoCuller.open_folder
    try:
        vispy_app.use_app("pyside6")
        qt_app = QApplication.instance() or QApplication(["Photo Culler Qt main self-test"])
        # Keep Qt objects alive until the process exits.  Releasing the local
        # QApplication/QGLWidget references immediately after ``quit()`` can
        # destroy the VisPy context in the wrong order on Windows.
        globals()["_qt_self_test_app"] = qt_app
        QtPhotoCuller.open_folder = lambda self: None  # type: ignore[method-assign]
        window = QtPhotoCuller()
        globals()["_qt_self_test_window"] = window
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        qt_app.processEvents()
        window.preview_widget.initialize_after_show()
        image = Image.new("RGB", (1800, 1200), (35, 110, 180))
        window.preview_widget.set_image(image, Path("qt-main-self-test.jpg"))
        qt_app.processEvents()
        frame = window.preview_widget.canvas.render(size=(960, 640), alpha=False)
        lookup_shape = None
        if getattr(window.preview_widget.visual, "_data_lookup_fn", None) is not None:
            lookup_shape = list(window.preview_widget.visual._data_lookup_fn["shape"]._value)
        initial_width = float(window.preview_widget.camera.rect.width)
        window.preview_widget.camera._queue_wheel_zoom(1.0, (480.0, 320.0))
        for _ in range(180):
            window.preview_widget.camera._last_tick -= 1 / 120
            window.preview_widget.camera._tick()
        zoomed_width = float(window.preview_widget.camera.rect.width)
        result = {
            "passed": (
                frame.shape == (640, 960, 3)
                and bool(frame.any())
                and window.preview_widget.current_path == Path("qt-main-self-test.jpg")
                and lookup_shape == [1800, 1200]
                and 1.05 < initial_width / zoomed_width < 1.30
            ),
            "gpu": window.preview_widget.gpu_info,
            "frame_shape": list(frame.shape),
            "frame_nonzero": bool(frame.any()),
            "interpolation_shape": lookup_shape,
            "zoom_ratio": round(initial_width / zoomed_width, 4),
        }
        window.close()
        qt_app.processEvents()
        qt_app.quit()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    except Exception as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        QtPhotoCuller.open_folder = original_open_folder  # type: ignore[method-assign]


if __name__ == "__main__":
    try:
        if "--qt-main-self-test" in sys.argv:
            raise SystemExit(_run_qt_main_self_test())
        if "--gpu-self-test" in sys.argv:
            raise SystemExit(_run_gpu_preview_self_test())
        if "--self-test" in sys.argv:
            # Used by the build verification command; no window is created.
            probe = tk.Tcl()
            probe.eval("package require Tk")
            print("Photo Culler runtime OK")
            raise SystemExit(0)
        enable_windows_high_dpi()
        if not QT_PHOTO_CULLER_AVAILABLE:
            raise RuntimeError(
                "PySide6 + VisPy GPU 主预览依赖未安装。请安装 requirements.txt 中的依赖。"
                f"\n{QT_PHOTO_CULLER_IMPORT_ERROR}"
            )
        vispy_app.use_app("pyside6")
        qt_app = QApplication.instance() or QApplication(sys.argv)
        qt_app.setApplicationName(APP_NAME)
        qt_app.setStyle("Fusion")
        window = QtPhotoCuller()
        window.show()
        raise SystemExit(qt_app.exec())
    except Exception as error:
        # When distributed without a console, show an actionable error instead of silently exiting.
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, f"程序启动失败：\n{error}")
        root.destroy()
        raise
