# daheng-camera-gui

A minimal PyQt5 GUI for a single Daheng (Galaxy / MER2) USB3 camera: live view, exposure time, gain,
white balance, and snapshots. Nothing else.

## Requirements

- Windows with the [Daheng Galaxy SDK](https://www.get-cameras.com/customerdownloads) installed (it provides
  `GxIAPI.dll` / `DxImageProc.dll`). On Linux the SDK installs `libgxiapi.so` instead.
- Python 3.9+

```
pip install -r requirements.txt
```

Daheng's Python binding (`gxipy`) is vendored in this repo, so there is nothing else to install.

## Run

```
python camera_gui.py                 # opens the first camera found and starts live view
python camera_gui.py --sn FCU23080688
python camera_gui.py --list          # print connected cameras and exit
python camera_gui.py --save-dir D:\snaps
```

## Controls

| Group | What it does |
|---|---|
| Camera | Connect / disconnect, start / stop live view. |
| Exposure | Log-spaced slider plus spin box, in ms, over the camera's own range. |
| Gain | Slider plus spin box, in dB, over the camera's own range. |
| White balance | Red / green / blue gain ratios, and a one-shot auto white balance. Disabled for mono cameras. |
| Snapshot | Saves the latest full-resolution frame as TIFF or PNG with a timestamped name. |

On connect the app turns off the camera's auto exposure, auto gain and auto white balance, switches to free-running
acquisition, and picks an 8-bit pixel format (Bayer RG8 for color, Mono8 for mono). Color frames are demosaiced to
RGB by the SDK; snapshots are saved as 8-bit RGB (color) or 8-bit gray (mono).

## Why auto exposure is forced off

Some color models (seen on the MER2-1220-32U3C) power up with `ExposureAuto = Continuous`. While that is on, the
`ExposureTime` feature is read-only and gxipy only prints a warning instead of raising, so exposure changes silently
do nothing. The wrapper disables it before touching any setting.

## Layout

- `camera_gui.py` - the Qt window and all controls.
- `daheng_camera.py` - small wrapper over gxipy: open/close, exposure, gain, white balance, streaming thread.
- `gxipy/` - Daheng's Python binding, unmodified apart from package-local imports.
