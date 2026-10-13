"""Thin wrapper around Daheng's gxipy API for a single USB3 camera.

Exposes the handful of controls the GUI needs (exposure, gain, white balance,
continuous streaming, snapshots) and hides the gxipy feature plumbing.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional, Tuple

import numpy as np

import gxipy as gx

FrameCallback = Callable[[np.ndarray], None]


class CameraError(RuntimeError):
    pass


class DahengCamera:
    """One opened Daheng camera, streaming continuously in a background thread."""

    def __init__(self):
        self._device_manager: Optional[gx.DeviceManager] = None
        self._camera = None
        self._is_color = False
        self._streaming = False
        self._thread: Optional[threading.Thread] = None
        self._frame_lock = threading.Lock()
        self._last_frame: Optional[np.ndarray] = None
        self._frame_callback: Optional[FrameCallback] = None
        self.model_name = ""
        self.serial_number = ""

    # ------------------------------------------------------------------ open/close

    @staticmethod
    def list_devices():
        """Return a list of (model_name, serial_number) for every connected Daheng camera."""
        device_manager = gx.DeviceManager()
        count, info = device_manager.update_device_list()
        return [(d["model_name"], d["sn"]) for d in info] if count else []

    def open(self, serial_number: Optional[str] = None):
        if self._camera is not None:
            return
        # The device manager owns the library init/de-init, so it must outlive the camera.
        self._device_manager = gx.DeviceManager()
        count, info = self._device_manager.update_device_list()
        if count == 0:
            raise CameraError("No Daheng camera found. Check the USB cable and that no other program has it open.")

        if serial_number is None:
            serial_number = info[0]["sn"]
        self._camera = self._device_manager.open_device_by_sn(serial_number)
        chosen = next((d for d in info if d["sn"] == serial_number), info[0])
        self.model_name = chosen["model_name"]
        self.serial_number = chosen["sn"]

        cam = self._camera
        self._is_color = bool(cam.PixelColorFilter.is_implemented())

        # Free-running acquisition, manual everything.  Some cameras (e.g. the MER2-1220-32U3C) power up with
        # auto exposure on, which makes ExposureTime read-only, so these must be turned off before any set().
        cam.TriggerMode.set(gx.GxSwitchEntry.OFF)
        for feature in (cam.ExposureAuto, cam.GainAuto, cam.BalanceWhiteAuto):
            if feature.is_implemented() and feature.is_writable():
                feature.set(gx.GxAutoEntry.OFF)

        # 8-bit output keeps the display path simple.  If the camera is already on an 8-bit format, leave it.
        try:
            current_format = cam.PixelFormat.get()[0]
            if current_format & gx.PIXEL_BIT_MASK != gx.GX_PIXEL_8BIT:
                cam.PixelFormat.set(gx.GxPixelFormatEntry.BAYER_RG8 if self._is_color else gx.GxPixelFormatEntry.MONO8)
        except Exception as e:  # noqa: BLE001 - informational only, the camera still works at its current depth
            print(f"Could not switch to an 8-bit pixel format: {e}")

        # Let the exposure time, not a frame-rate cap, set the frame period.
        if cam.AcquisitionFrameRateMode.is_implemented():
            cam.AcquisitionFrameRateMode.set(gx.GxSwitchEntry.OFF)

    def close(self):
        self.stop_streaming()
        if self._camera is not None:
            try:
                self._camera.close_device()
            finally:
                self._camera = None
        self._device_manager = None

    @property
    def is_open(self) -> bool:
        return self._camera is not None

    @property
    def is_color(self) -> bool:
        return self._is_color

    def _require(self):
        if self._camera is None:
            raise CameraError("Camera is not open.")
        return self._camera

    # ------------------------------------------------------------------ exposure / gain

    def get_exposure_range_ms(self) -> Tuple[float, float]:
        r = self._require().ExposureTime.get_range()
        return r["min"] / 1000.0, r["max"] / 1000.0

    def get_exposure_ms(self) -> float:
        return self._require().ExposureTime.get() / 1000.0

    def set_exposure_ms(self, exposure_ms: float):
        lo, hi = self.get_exposure_range_ms()
        self._require().ExposureTime.set(float(min(max(exposure_ms, lo), hi)) * 1000.0)

    def get_gain_range(self) -> Tuple[float, float]:
        r = self._require().Gain.get_range()
        return r["min"], r["max"]

    def get_gain(self) -> float:
        return self._require().Gain.get()

    def set_gain(self, gain: float):
        lo, hi = self.get_gain_range()
        self._require().Gain.set(float(min(max(gain, lo), hi)))

    # ------------------------------------------------------------------ white balance

    def has_white_balance(self) -> bool:
        cam = self._require()
        return (
            self._is_color
            and cam.BalanceRatio.is_implemented()
            and cam.BalanceRatio.is_writable()
            and cam.BalanceRatioSelector.is_implemented()
        )

    def get_white_balance_range(self) -> Tuple[float, float]:
        r = self._require().BalanceRatio.get_range()
        return r["min"], r["max"]

    def get_white_balance(self) -> Tuple[float, float, float]:
        cam = self._require()
        values = []
        for channel in (gx.GxBalanceRatioSelectorEntry.RED, gx.GxBalanceRatioSelectorEntry.GREEN, gx.GxBalanceRatioSelectorEntry.BLUE):
            cam.BalanceRatioSelector.set(channel)
            values.append(cam.BalanceRatio.get())
        return values[0], values[1], values[2]

    def set_white_balance(self, red: float, green: float, blue: float):
        cam = self._require()
        lo, hi = self.get_white_balance_range()
        for channel, value in (
            (gx.GxBalanceRatioSelectorEntry.RED, red),
            (gx.GxBalanceRatioSelectorEntry.GREEN, green),
            (gx.GxBalanceRatioSelectorEntry.BLUE, blue),
        ):
            cam.BalanceRatioSelector.set(channel)
            cam.BalanceRatio.set(float(min(max(value, lo), hi)))

    def auto_white_balance_once(self, timeout_s: float = 3.0) -> Tuple[float, float, float]:
        """Run the camera's one-shot auto white balance and return the resulting gains.

        The camera needs to be streaming so it has frames to measure.
        """
        cam = self._require()
        cam.BalanceWhiteAuto.set(gx.GxAutoEntry.ONCE)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if cam.BalanceWhiteAuto.get()[0] == gx.GxAutoEntry.OFF:
                break
            time.sleep(0.05)
        return self.get_white_balance()

    # ------------------------------------------------------------------ streaming

    def start_streaming(self, frame_callback: Optional[FrameCallback] = None):
        cam = self._require()
        if self._streaming:
            return
        self._frame_callback = frame_callback
        cam.stream_on()
        self._streaming = True
        self._thread = threading.Thread(target=self._acquisition_loop, name="daheng-acquire", daemon=True)
        self._thread.start()

    def stop_streaming(self):
        if not self._streaming:
            return
        self._streaming = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        try:
            self._camera.stream_off()
        except Exception as e:  # noqa: BLE001 - a camera that was unplugged mid-stream raises here
            print(f"stream_off failed: {e}")

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def _acquisition_loop(self):
        stream = self._camera.data_stream[0]
        while self._streaming:
            raw = stream.get_image(timeout=1000)
            if raw is None:
                continue
            if raw.get_status() != gx.GxFrameStatusList.SUCCESS:
                continue
            if self._is_color:
                rgb = raw.convert("RGB")
                frame = None if rgb is None else rgb.get_numpy_array()
            else:
                frame = raw.get_numpy_array()
            if frame is None:
                continue
            frame = np.ascontiguousarray(frame)
            with self._frame_lock:
                self._last_frame = frame
            if self._frame_callback is not None:
                self._frame_callback(frame)

    def get_last_frame(self) -> Optional[np.ndarray]:
        """Most recent full-resolution frame: (H, W, 3) uint8 RGB for color cameras, (H, W) for mono."""
        with self._frame_lock:
            return None if self._last_frame is None else self._last_frame.copy()
