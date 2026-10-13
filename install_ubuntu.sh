#!/usr/bin/env bash
#
# Ubuntu installer for daheng-camera-gui.
#
#   1. apt packages (Python, venv, Qt runtime libraries)
#   2. Daheng Galaxy SDK = the camera driver (libgxiapi.so, the USB3/GigE transport layers,
#      the udev rule that lets a normal user open the camera, and the usbfs memory bump)
#   3. a Python virtual environment with this app's dependencies
#
# Usage:
#   ./install_ubuntu.sh                          # everything, SDK downloaded automatically
#   ./install_ubuntu.sh --sdk-run ~/Downloads/Galaxy_camera.run   # use an installer you already have
#   ./install_ubuntu.sh --skip-driver            # Python side only
#   ./install_ubuntu.sh --force-driver           # reinstall the SDK even if it is already present
#
# Tested target: Ubuntu 22.04 / 24.04 on x86_64. ARM (Jetson) is handled best-effort: the ARM
# installer is run interactively so you can answer its prompts yourself.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
SDK_INSTALL_PARENT="/opt/daheng"          # the SDK unpacks itself into $SDK_INSTALL_PARENT/Galaxy_camera
GXIAPI_LIB="/usr/lib/libgxiapi.so"

# The Galaxy SDK installers are redistributed in the public Cephla-Lab/Squid repository.
SQUID_RAW="https://raw.githubusercontent.com/Cephla-Lab/Squid/master/software/drivers%20and%20libraries/daheng%20camera"
SDK_URL_X86="$SQUID_RAW/Galaxy_Linux-x86_Gige-U3_32bits-64bits_1.2.1911.9122/Galaxy_camera.run"
SDK_URL_ARM="$SQUID_RAW/Galaxy_Linux-armhf_Gige-U3_32bits-64bits_1.3.1911.9271/Galaxy_camera.run"

SDK_RUN=""
INSTALL_DRIVER=1
FORCE_DRIVER=0

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --sdk-run)       SDK_RUN="$2"; shift 2 ;;
        --skip-driver)   INSTALL_DRIVER=0; shift ;;
        --force-driver)  FORCE_DRIVER=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARNING: %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------------------------- preflight
command -v apt-get >/dev/null 2>&1 || die "This script is for Ubuntu/Debian (apt-get not found)."
[ "$(id -u)" -eq 0 ] && die "Run this as a normal user, not root. It will call sudo where needed."
command -v sudo >/dev/null 2>&1 || die "sudo is required."

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64)          SDK_URL="$SDK_URL_X86"; SDK_INTERACTIVE=0 ;;
    aarch64|armv7l)  SDK_URL="$SDK_URL_ARM"; SDK_INTERACTIVE=1 ;;
    *) die "Unsupported architecture: $ARCH" ;;
esac

log "Requesting sudo"
sudo -v

# ----------------------------------------------------------------------------- apt
log "Installing system packages"
sudo apt-get update
# python3-pyqt5 from apt gives a working Qt on every architecture (PyPI has no PyQt5 wheels for ARM).
# The lib* packages are the Qt xcb platform plugin's runtime dependencies.
sudo apt-get install -y \
    python3 python3-venv python3-pip python3-pyqt5 \
    libgl1 libegl1 libxcb-xinerama0 libxcb-cursor0 libxkbcommon-x11-0 \
    curl ca-certificates usbutils

# ----------------------------------------------------------------------------- camera driver
install_driver() {
    if [ -f "$GXIAPI_LIB" ] && [ "$FORCE_DRIVER" -eq 0 ]; then
        log "Galaxy SDK already installed ($GXIAPI_LIB exists); skipping. Use --force-driver to reinstall."
        return
    fi

    local work
    work="$(mktemp -d)"
    local run_file="$work/Galaxy_camera.run"

    if [ -n "$SDK_RUN" ]; then
        [ -f "$SDK_RUN" ] || die "--sdk-run file not found: $SDK_RUN"
        cp "$SDK_RUN" "$run_file"
    elif [ -f "$SCRIPT_DIR/Galaxy_camera.run" ]; then
        cp "$SCRIPT_DIR/Galaxy_camera.run" "$run_file"
    else
        log "Downloading Galaxy SDK installer for $ARCH"
        curl -fL --progress-bar -o "$run_file" "$SDK_URL" \
            || die "Download failed. Get Galaxy_camera.run from Daheng (https://www.get-cameras.com/customerdownloads) and rerun with --sdk-run <file>."
    fi
    chmod +x "$run_file"

    # The installer unpacks into $PWD/Galaxy_camera and leaves the SDK (GalaxyView, docs, samples) there,
    # so run it from a permanent location rather than the temp dir.
    sudo mkdir -p "$SDK_INSTALL_PARENT"
    sudo cp "$run_file" "$SDK_INSTALL_PARENT/Galaxy_camera.run"

    # The vendor's set_usbfs_memory_size.sh appends a GRUB_CMDLINE_LINUX line to /etc/default/grub.
    # Keep a copy so a pre-existing kernel command line can be restored by hand if needed.
    if [ -f /etc/default/grub ]; then
        sudo cp -n /etc/default/grub "/etc/default/grub.before-daheng-sdk"
    fi

    log "Running the Galaxy SDK installer"
    if [ "$SDK_INTERACTIVE" -eq 1 ]; then
        echo "The ARM installer is interactive: accept the defaults (Enter), choose English, confirm overwrite if asked."
        (cd "$SDK_INSTALL_PARENT" && sudo ./Galaxy_camera.run)
    else
        # Prompt order when run as root: [Enter to continue] [confirm x86_64 SDK: Enter]
        # [language: En] [overwrite existing libs if present: y]. Extra answers are ignored.
        (cd "$SDK_INSTALL_PARENT" && printf '\n\nEn\ny\n' | sudo ./Galaxy_camera.run)
    fi
    sudo rm -f "$SDK_INSTALL_PARENT/Galaxy_camera.run"
    rm -rf "$work"

    [ -f "$GXIAPI_LIB" ] || die "Installer finished but $GXIAPI_LIB is missing. Run 'cd $SDK_INSTALL_PARENT && sudo ./Galaxy_camera.run' by hand and check its output."
    [ -f /usr/lib/GxU3VTL.cti ] || warn "USB3 transport layer /usr/lib/GxU3VTL.cti not found; USB cameras will not enumerate."

    # Make the udev rule (installed by the SDK) apply to an already-plugged camera.
    if [ -f /etc/udev/rules.d/99-galaxy-u3v.rules ]; then
        sudo udevadm control --reload-rules
        sudo udevadm trigger --subsystem-match=usb
    else
        warn "udev rule /etc/udev/rules.d/99-galaxy-u3v.rules was not installed; you may need sudo to open the camera."
    fi

    # usbfs memory: the SDK sets this in grub for the next boot; apply it now as well.
    if [ -f /sys/module/usbcore/parameters/usbfs_memory_mb ]; then
        echo 1000 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb >/dev/null
    fi

    if [ -f /etc/default/grub ] && [ "$(grep -c '^GRUB_CMDLINE_LINUX=' /etc/default/grub)" -gt 1 ]; then
        warn "/etc/default/grub now has more than one GRUB_CMDLINE_LINUX= line (the SDK appended one)."
        warn "If you had custom kernel parameters, merge them into one line; the original is in /etc/default/grub.before-daheng-sdk."
    fi
}

if [ "$INSTALL_DRIVER" -eq 1 ]; then
    install_driver
else
    log "Skipping camera driver (--skip-driver)"
fi

# ----------------------------------------------------------------------------- python
log "Creating Python environment in $VENV_DIR"
# --system-site-packages exposes apt's PyQt5 inside the venv.
python3 -m venv --system-site-packages "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install numpy Pillow

if ! "$VENV_DIR/bin/python" -c "import PyQt5.QtWidgets" 2>/dev/null; then
    log "apt PyQt5 not importable from the venv; installing PyQt5 from PyPI"
    "$VENV_DIR/bin/pip" install "PyQt5>=5.15"
fi

"$VENV_DIR/bin/python" -c "import PyQt5.QtWidgets, numpy, PIL" || die "Python dependencies failed to import."

# ----------------------------------------------------------------------------- check
log "Checking the camera driver from Python"
if (cd "$SCRIPT_DIR" && "$VENV_DIR/bin/python" camera_gui.py --list); then
    :
else
    warn "Camera enumeration failed. If the SDK was just installed, unplug and replug the camera (or reboot) and retry."
fi

cat <<EOF

Done.

  Run the GUI:          cd "$SCRIPT_DIR" && .venv/bin/python camera_gui.py
  List cameras:         .venv/bin/python camera_gui.py --list
  Daheng's own viewer:  $SDK_INSTALL_PARENT/Galaxy_camera/bin/GalaxyView

If a camera was plugged in during the install, unplug and replug it once so the new
udev permissions apply. A reboot also applies the usbfs memory setting permanently.
EOF
