"""Minimal camera control GUI for a Daheng USB3 camera.

Live view with exposure time, gain, white balance and snapshot controls.

    python camera_gui.py [--sn SERIAL] [--save-dir DIR]
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from datetime import datetime
from typing import Optional

import numpy as np
from PIL import Image
from PyQt5.QtCore import Qt, QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from daheng_camera import CameraError, DahengCamera

PREVIEW_MAX_WIDTH = 1280  # frames are subsampled to about this width before display
SLIDER_STEPS = 1000


class FrameBridge(QObject):
    """Carries frames from the acquisition thread into the Qt main thread."""

    frame_ready = pyqtSignal(object)


class ImageView(QLabel):
    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(480, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("background-color: #202020; color: #a0a0a0;")
        self.setText("No camera")
        self._pixmap: Optional[QPixmap] = None

    def set_frame(self, frame: np.ndarray):
        if frame.ndim == 3:
            h, w, _ = frame.shape
            image = QImage(frame.data, w, h, 3 * w, QImage.Format_RGB888)
        else:
            h, w = frame.shape
            image = QImage(frame.data, w, h, w, QImage.Format_Grayscale8)
        # QImage wraps the numpy buffer; copy so the pixmap outlives the array.
        self._pixmap = QPixmap.fromImage(image.copy())
        self._rescale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if self._pixmap is None:
            return
        self.setPixmap(self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class SliderSpin(QWidget):
    """A slider and a spin box that stay in sync.  The slider is log-spaced when `log` is set."""

    valueChanged = pyqtSignal(float)

    def __init__(self, minimum: float, maximum: float, decimals: int, suffix: str, log: bool = False):
        super().__init__()
        self._min = minimum
        self._max = maximum
        self._log = log and minimum > 0
        self._updating = False

        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(decimals)
        self.spin.setRange(minimum, maximum)
        self.spin.setSuffix(suffix)
        self.spin.setKeyboardTracking(False)
        self.spin.setMinimumWidth(110)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, SLIDER_STEPS)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin)

        self.slider.valueChanged.connect(self._slider_moved)
        self.spin.valueChanged.connect(self._spin_changed)

    def _to_slider(self, value: float) -> int:
        if self._max <= self._min:
            return 0
        if self._log:
            frac = (math.log(value) - math.log(self._min)) / (math.log(self._max) - math.log(self._min))
        else:
            frac = (value - self._min) / (self._max - self._min)
        return int(round(min(max(frac, 0.0), 1.0) * SLIDER_STEPS))

    def _from_slider(self, pos: int) -> float:
        frac = pos / SLIDER_STEPS
        if self._log:
            return math.exp(math.log(self._min) + frac * (math.log(self._max) - math.log(self._min)))
        return self._min + frac * (self._max - self._min)

    def set_value(self, value: float):
        """Update both widgets without emitting valueChanged (used when reading back from the camera)."""
        self._updating = True
        try:
            self.spin.setValue(value)
            self.slider.setValue(self._to_slider(value))
        finally:
            self._updating = False

    def value(self) -> float:
        return self.spin.value()

    def _slider_moved(self, pos: int):
        if self._updating:
            return
        self._updating = True
        try:
            self.spin.setValue(self._from_slider(pos))
        finally:
            self._updating = False
        self.valueChanged.emit(self.spin.value())

    def _spin_changed(self, value: float):
        if self._updating:
            return
        self._updating = True
        try:
            self.slider.setValue(self._to_slider(value))
        finally:
            self._updating = False
        self.valueChanged.emit(value)


class CameraWindow(QMainWindow):
    def __init__(self, serial_number: Optional[str], save_dir: str):
        super().__init__()
        self.setWindowTitle("Daheng Camera Control")
        self.camera = DahengCamera()
        self._requested_sn = serial_number
        self._bridge = FrameBridge()
        self._bridge.frame_ready.connect(self._on_frame)
        self._frame_times: list = []
        self._wb_updating = False

        # ---- image area
        self.view = ImageView()

        # ---- connection
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._toggle_connection)
        self.live_button = QPushButton("Start live")
        self.live_button.setCheckable(True)
        self.live_button.clicked.connect(self._toggle_live)
        self.camera_label = QLabel("Not connected")
        connection_box = QGroupBox("Camera")
        connection_layout = QVBoxLayout(connection_box)
        connection_layout.addWidget(self.camera_label)
        row = QHBoxLayout()
        row.addWidget(self.connect_button)
        row.addWidget(self.live_button)
        connection_layout.addLayout(row)

        # ---- exposure / gain (ranges are replaced with the camera's once connected)
        self.exposure = SliderSpin(0.01, 1000.0, 3, " ms", log=True)
        self.exposure.valueChanged.connect(self._set_exposure)
        self.gain = SliderSpin(0.0, 24.0, 1, " dB")
        self.gain.valueChanged.connect(self._set_gain)
        acquisition_box = QGroupBox("Exposure and gain")
        acquisition_form = QFormLayout(acquisition_box)
        acquisition_form.addRow("Exposure", self.exposure)
        acquisition_form.addRow("Gain", self.gain)

        # ---- white balance
        self.wb_spins = {}
        wb_box = QGroupBox("White balance")
        wb_form = QFormLayout(wb_box)
        for channel in ("Red", "Green", "Blue"):
            spin = QDoubleSpinBox()
            spin.setDecimals(3)
            spin.setRange(0.0, 16.0)
            spin.setSingleStep(0.05)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(self._set_white_balance)
            self.wb_spins[channel] = spin
            wb_form.addRow(channel, spin)
        self.awb_button = QPushButton("Auto white balance (once)")
        self.awb_button.clicked.connect(self._auto_white_balance)
        wb_form.addRow(self.awb_button)
        self.wb_box = wb_box

        # ---- snap
        self.save_dir_edit = QLineEdit(save_dir)
        browse_button = QPushButton("...")
        browse_button.setFixedWidth(32)
        browse_button.clicked.connect(self._browse_save_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.save_dir_edit, 1)
        dir_row.addWidget(browse_button)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["tiff", "png"])
        self.snap_button = QPushButton("Snap")
        self.snap_button.clicked.connect(self._snap)
        snap_box = QGroupBox("Snapshot")
        snap_form = QFormLayout(snap_box)
        snap_form.addRow("Folder", dir_row)
        snap_form.addRow("Format", self.format_combo)
        snap_form.addRow(self.snap_button)

        # ---- layout
        controls = QVBoxLayout()
        controls.addWidget(connection_box)
        controls.addWidget(acquisition_box)
        controls.addWidget(wb_box)
        controls.addWidget(snap_box)
        controls.addStretch(1)
        controls_widget = QWidget()
        controls_widget.setLayout(controls)
        controls_widget.setFixedWidth(360)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(self.view, 1)
        layout.addWidget(controls_widget)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.resize(1280, 760)

        self._set_controls_enabled(False)
        QTimer.singleShot(0, self._connect)

    # ------------------------------------------------------------------ connection

    def _toggle_connection(self):
        if self.camera.is_open:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        try:
            self.camera.open(self._requested_sn)
        except Exception as e:  # noqa: BLE001 - surface whatever the SDK raised
            self.statusBar().showMessage(f"Connect failed: {e}")
            QMessageBox.warning(self, "Camera", str(e))
            return

        self.camera_label.setText(f"{self.camera.model_name}  (SN {self.camera.serial_number})")
        self.connect_button.setText("Disconnect")
        self.setWindowTitle(f"Daheng Camera Control - {self.camera.model_name}")
        self._load_ranges_and_values()
        self._set_controls_enabled(True)
        self.live_button.setChecked(True)
        self._toggle_live(True)

    def _disconnect(self):
        self.live_button.setChecked(False)
        self.camera.close()
        self.camera_label.setText("Not connected")
        self.connect_button.setText("Connect")
        self.live_button.setText("Start live")
        self._set_controls_enabled(False)
        self.statusBar().showMessage("Disconnected")

    def _set_controls_enabled(self, enabled: bool):
        self.live_button.setEnabled(enabled)
        self.exposure.setEnabled(enabled)
        self.gain.setEnabled(enabled)
        self.snap_button.setEnabled(enabled)
        wb_enabled = enabled and self.camera.is_open and self.camera.has_white_balance()
        self.wb_box.setEnabled(wb_enabled)
        self.wb_box.setTitle("White balance" if wb_enabled or not enabled else "White balance (mono camera)")

    def _load_ranges_and_values(self):
        cam = self.camera
        lo, hi = cam.get_exposure_range_ms()
        self.exposure = self._replace_slider(self.exposure, SliderSpin(lo, hi, 3, " ms", log=True), self._set_exposure)
        self.exposure.set_value(cam.get_exposure_ms())

        lo, hi = cam.get_gain_range()
        self.gain = self._replace_slider(self.gain, SliderSpin(lo, hi, 1, " dB"), self._set_gain)
        self.gain.set_value(cam.get_gain())

        if cam.has_white_balance():
            lo, hi = cam.get_white_balance_range()
            for spin in self.wb_spins.values():
                spin.setRange(lo, hi)
            self._show_white_balance(cam.get_white_balance())

    def _replace_slider(self, old: SliderSpin, new: SliderSpin, slot) -> SliderSpin:
        """Swap a SliderSpin for one with a new range, keeping its spot in the form."""
        form = old.parentWidget().layout()
        label = form.labelForField(old)
        label_text = label.text() if label is not None else ""
        row_index, _ = form.getWidgetPosition(old)
        form.removeRow(row_index)  # deletes the old widgets, so read the label before this
        form.insertRow(row_index, label_text, new)
        new.valueChanged.connect(slot)
        return new

    # ------------------------------------------------------------------ live view

    def _toggle_live(self, checked: bool):
        if not self.camera.is_open:
            return
        if checked:
            self.camera.start_streaming(self._bridge.frame_ready.emit)
            self.live_button.setText("Stop live")
            self.statusBar().showMessage("Live")
        else:
            self.camera.stop_streaming()
            self.live_button.setText("Start live")
            self.statusBar().showMessage("Stopped")

    @pyqtSlot(object)
    def _on_frame(self, frame: np.ndarray):
        step = max(1, int(math.ceil(frame.shape[1] / PREVIEW_MAX_WIDTH)))
        preview = np.ascontiguousarray(frame[::step, ::step])
        self.view.set_frame(preview)

        now = time.time()
        self._frame_times.append(now)
        self._frame_times = [t for t in self._frame_times if now - t < 2.0]
        if len(self._frame_times) > 1:
            fps = (len(self._frame_times) - 1) / (self._frame_times[-1] - self._frame_times[0])
            h, w = frame.shape[:2]
            self.statusBar().showMessage(f"Live  {w}x{h}  {fps:.1f} fps")

    # ------------------------------------------------------------------ camera settings

    def _set_exposure(self, value_ms: float):
        if not self.camera.is_open:
            return
        try:
            self.camera.set_exposure_ms(value_ms)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Exposure failed: {e}")

    def _set_gain(self, value: float):
        if not self.camera.is_open:
            return
        try:
            self.camera.set_gain(value)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Gain failed: {e}")

    def _show_white_balance(self, rgb):
        self._wb_updating = True
        try:
            for spin, value in zip(self.wb_spins.values(), rgb):
                spin.setValue(value)
        finally:
            self._wb_updating = False

    def _set_white_balance(self):
        if self._wb_updating or not self.camera.is_open or not self.camera.has_white_balance():
            return
        r, g, b = (self.wb_spins[c].value() for c in ("Red", "Green", "Blue"))
        try:
            self.camera.set_white_balance(r, g, b)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"White balance failed: {e}")

    def _auto_white_balance(self):
        if not self.camera.is_open:
            return
        if not self.camera.is_streaming:
            QMessageBox.information(self, "White balance", "Start live view first so the camera has frames to measure.")
            return
        try:
            rgb = self.camera.auto_white_balance_once()
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Auto white balance failed: {e}")
            return
        self._show_white_balance(rgb)
        self.statusBar().showMessage(f"Auto white balance: R={rgb[0]:.3f} G={rgb[1]:.3f} B={rgb[2]:.3f}")

    # ------------------------------------------------------------------ snapshot

    def _browse_save_dir(self):
        chosen = QFileDialog.getExistingDirectory(self, "Snapshot folder", self.save_dir_edit.text())
        if chosen:
            self.save_dir_edit.setText(chosen)

    def _snap(self):
        if not self.camera.is_open:
            return
        frame = self.camera.get_last_frame()
        if frame is None:
            QMessageBox.information(self, "Snap", "No frame yet. Start live view first.")
            return
        save_dir = self.save_dir_edit.text().strip() or os.getcwd()
        os.makedirs(save_dir, exist_ok=True)
        extension = "tif" if self.format_combo.currentText() == "tiff" else "png"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = os.path.join(save_dir, f"snap_{stamp}.{extension}")
        try:
            Image.fromarray(frame).save(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Snap", f"Could not save image:\n{e}")
            return
        self.statusBar().showMessage(f"Saved {path}")

    # ------------------------------------------------------------------ shutdown

    def closeEvent(self, event):
        try:
            self.camera.close()
        finally:
            event.accept()


def main():
    parser = argparse.ArgumentParser(description="Minimal Daheng camera control GUI")
    parser.add_argument("--sn", help="Serial number of the camera to open (default: first camera found)")
    parser.add_argument("--save-dir", default=os.path.join(os.getcwd(), "snaps"), help="Folder for snapshots")
    parser.add_argument("--list", action="store_true", help="List connected cameras and exit")
    args = parser.parse_args()

    if args.list:
        devices = DahengCamera.list_devices()
        if not devices:
            print("No Daheng cameras found.")
        for model, sn in devices:
            print(f"{model}  SN {sn}")
        return 0

    app = QApplication(sys.argv)
    window = CameraWindow(args.sn, args.save_dir)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
