# daheng-camera-gui

A minimal PyQt5 GUI for a single Daheng (Galaxy / MER2) USB3 camera: live view, exposure time, gain,
white balance, and snapshots. Nothing else.

## Install

Daheng's Python binding (`gxipy`) is vendored in this repo. What you need besides Python is the Daheng Galaxy SDK,
which provides the camera driver library (`GxIAPI.dll` on Windows, `libgxiapi.so` on Linux).

### Ubuntu

```
git clone https://github.com/hongquanli/daheng-camera-gui.git
cd daheng-camera-gui
./install_ubuntu.sh
```

The script installs the apt packages, downloads and runs the Galaxy SDK installer non-interactively (x86_64; on
ARM it runs interactively), and creates a `.venv` with the Python dependencies. The SDK installer puts
`libgxiapi.so` and the USB3/GigE transport layers in `/usr/lib`, adds a udev rule so a normal user can open the
camera, raises `usbfs_memory_mb` to 1000 (now, and via grub for future boots), and leaves Daheng's own
`GalaxyView` tool under `/opt/daheng/Galaxy_camera/bin`. Unplug and replug the camera once after installing.

Options: `--sdk-run <file>` to use a `Galaxy_camera.run` you already downloaded, `--skip-driver` for the Python side
only, `--force-driver` to reinstall the SDK. Note that the SDK's grub step appends a `GRUB_CMDLINE_LINUX=` line to
`/etc/default/grub`; the script keeps a copy at `/etc/default/grub.before-daheng-sdk` and warns if you now have two.

### Windows

1. Install the [Galaxy SDK](https://www.get-cameras.com/customerdownloads) for Windows.
2. `pip install -r requirements.txt` (Python 3.9+).

## Run

```
python camera_gui.py                 # opens the first camera found and starts live view
python camera_gui.py --sn FCU23080688
python camera_gui.py --list          # print connected cameras and exit
python camera_gui.py --save-dir D:\snaps
```

On Ubuntu use `.venv/bin/python camera_gui.py` (or activate the venv first).

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
