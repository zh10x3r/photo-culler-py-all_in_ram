"""Regression checks for Bicubic zoom and pan math; no GUI is launched."""

import unittest

from PIL import Image

from app import (
    GPU_PREVIEW_DEFAULT_INTERPOLATION,
    GPU_PREVIEW_DEFAULT_MAX_MAGNIFICATION,
    GPU_PREVIEW_DEFAULT_SMOOTH_ZOOM,
    GPU_PREVIEW_INTERPOLATIONS,
    GPU_PREVIEW_MAX_MAGNIFICATIONS,
    GPU_PUMP_ACTIVE_INTERVAL_MS,
    GPU_PUMP_IDLE_INTERVAL_MS,
    PREVIEW_RESAMPLING_FILTER,
    PREVIEW_REDUCING_GAP,
    PreviewGeometry,
    PhotoCuller,
    _GpuPreviewController,
    advance_zoom_scale,
    clamp_zoom_scale,
    normalize_gpu_preview_settings,
    zoom_target_after_wheel,
)


class _CanvasStub:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height

    def winfo_width(self) -> int:
        return self.width

    def winfo_height(self) -> int:
        return self.height


class PreviewTests(unittest.TestCase):
    def test_gpu_event_pump_uses_fast_interval_only_during_animation(self) -> None:
        class AfterStub:
            def __init__(self) -> None:
                self.delay = None

            def after(self, delay: int, _callback: object) -> str:
                self.delay = delay
                return "job"

        class CameraStub:
            animation_active = True

        class WindowStub:
            camera = CameraStub()

        owner = AfterStub()
        controller = object.__new__(_GpuPreviewController)
        controller.owner = owner
        controller.window = WindowStub()
        controller._pump_job = None
        controller._closing = False
        _GpuPreviewController._schedule_pump(controller)
        self.assertEqual(owner.delay, GPU_PUMP_ACTIVE_INTERVAL_MS)

        controller._pump_job = None
        controller.window.camera.animation_active = False
        _GpuPreviewController._schedule_pump(controller)
        self.assertEqual(owner.delay, GPU_PUMP_IDLE_INTERVAL_MS)

    def test_gpu_preview_settings_are_validated_and_defaulted(self) -> None:
        defaults = normalize_gpu_preview_settings({})
        self.assertEqual(defaults["interpolation"], GPU_PREVIEW_DEFAULT_INTERPOLATION)
        self.assertEqual(defaults["max_magnification"], GPU_PREVIEW_DEFAULT_MAX_MAGNIFICATION)
        self.assertEqual(defaults["smooth_zoom"], GPU_PREVIEW_DEFAULT_SMOOTH_ZOOM)

        settings = normalize_gpu_preview_settings(
            {"interpolation": "catrom", "smooth_zoom": False, "max_magnification": "32"}
        )
        self.assertEqual(settings, {"interpolation": "catrom", "smooth_zoom": False, "max_magnification": "32"})
        self.assertEqual(
            normalize_gpu_preview_settings(
                {"interpolation": "invalid", "smooth_zoom": "false", "max_magnification": "999"}
            ),
            {
                "interpolation": GPU_PREVIEW_DEFAULT_INTERPOLATION,
                "smooth_zoom": GPU_PREVIEW_DEFAULT_SMOOTH_ZOOM,
                "max_magnification": GPU_PREVIEW_DEFAULT_MAX_MAGNIFICATION,
            },
        )
        self.assertIn("catrom", GPU_PREVIEW_INTERPOLATIONS)
        self.assertIn("32", GPU_PREVIEW_MAX_MAGNIFICATIONS)

    def test_central_preview_uses_bicubic_resampling(self) -> None:
        self.assertEqual(PREVIEW_RESAMPLING_FILTER, Image.Resampling.BICUBIC)

    def test_wheel_steps_accumulate_smooth_target_and_obey_limits(self) -> None:
        self.assertAlmostEqual(zoom_target_after_wheel(0.25, 6, 0.25), 0.5)
        self.assertEqual(zoom_target_after_wheel(0.25, -20, 0.25), 0.25)
        self.assertEqual(zoom_target_after_wheel(3.9, 20, 0.25), 4.0)
        self.assertEqual(clamp_zoom_scale(0.1, 0.25), 0.25)

    def test_animation_moves_toward_target_without_jumping(self) -> None:
        next_scale = advance_zoom_scale(0.25, 1.0, 0.016)
        self.assertGreater(next_scale, 0.25)
        self.assertLess(next_scale, 1.0)

    def test_geometry_fits_and_centers_without_upscaling(self) -> None:
        app = object.__new__(PhotoCuller)
        app.preview_canvas = _CanvasStub(1000, 700)
        app.current_source_image = Image.new("RGB", (2000, 1000))
        app.fit_scale = 1.0
        app.zoom_scale = 1.0
        app.zoom_target_scale = 1.0
        app.pan_x = 0.0
        app.pan_y = 0.0

        geometry = PhotoCuller._preview_geometry(app, app.current_source_image, reset_view=True)

        self.assertEqual(geometry.source_box, (0, 0, 2000, 1000))
        self.assertEqual(geometry.target_size, (1000, 500))
        self.assertEqual(geometry.origin, (0.0, 100.0))

    def test_zoomed_geometry_only_renders_viewport_and_buffer(self) -> None:
        app = object.__new__(PhotoCuller)
        app.preview_canvas = _CanvasStub(1000, 700)
        app.current_source_image = Image.new("RGB", (2000, 1000))
        app.fit_scale = 0.5
        app.zoom_scale = 1.0
        app.zoom_target_scale = 1.0
        app.pan_x = 0.0
        app.pan_y = 0.0

        geometry = PhotoCuller._preview_geometry(app, app.current_source_image)

        self.assertEqual(geometry.source_box, (320, 0, 1680, 1000))
        self.assertEqual(geometry.target_size, (1360, 1000))
        self.assertEqual(geometry.origin, (-180.0, -150.0))

    def test_pointer_anchor_stays_fixed_when_scale_changes(self) -> None:
        app = object.__new__(PhotoCuller)
        app.preview_canvas = _CanvasStub(1000, 700)
        app.current_source_image = Image.new("RGB", (2000, 1000))
        app.fit_scale = 0.5
        app.zoom_scale = 0.5
        app.zoom_target_scale = 0.5
        app.pan_x = 0.0
        app.pan_y = 0.0
        app._zoom_anchor = None

        PhotoCuller._capture_zoom_anchor(app, (750.0, 350.0))
        app.zoom_scale = 1.0
        PhotoCuller._apply_zoom_anchor(app)

        source_x = 1500.0
        displayed_x = 500.0 + app.pan_x + (source_x - 1000.0) * app.zoom_scale
        self.assertAlmostEqual(displayed_x, 750.0)

    def test_preview_frame_passes_crop_and_bicubic_filter_to_pillow(self) -> None:
        class ResizeSpy:
            width = 2000
            height = 1000
            size = (2000, 1000)

            def __init__(self) -> None:
                self.call: tuple[tuple[int, int], object, tuple[int, int, int, int], float] | None = None

            def resize(
                self,
                size: tuple[int, int],
                resample: object,
                *,
                box: tuple[int, int, int, int],
                reducing_gap: float,
            ) -> str:
                self.call = (size, resample, box, reducing_gap)
                return "frame"

        image = ResizeSpy()
        geometry = PreviewGeometry((300, 100, 900, 700), (900, 900), (-50.0, -50.0))

        frame = PhotoCuller._build_preview_frame(image, geometry)

        self.assertEqual(frame, "frame")
        self.assertEqual(
            image.call,
            ((900, 900), Image.Resampling.BICUBIC, (300, 100, 900, 700), PREVIEW_REDUCING_GAP),
        )


if __name__ == "__main__":
    unittest.main()
