"""
Sysfs and debugfs helpers for discovering GPU devices, display ports,
and connector state.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


def run_command(command: str) -> subprocess.CompletedProcess[str]:
    """Run a shell command and return the CompletedProcess."""
    return subprocess.run(shlex.split(command), capture_output=True, text=True)


def get_drm_devices() -> list[Path]:
    """Get list of DRM devices from /sys/kernel/debug/dri/"""
    debug_dri_path = Path("/sys/kernel/debug/dri")
    devices: list[Path] = []

    try:
        entries = sorted(debug_dri_path.iterdir(), key=lambda path: path.name)
    except OSError:
        print(
            "Error: /sys/kernel/debug/dri not found or not accessible. Make sure debugfs is mounted."
        )
        return devices

    for entry in entries:
        if entry.name.startswith("0000:"):
            devices.append(entry)

    return sorted(devices)


def get_display_ports(drm_device: Path) -> dict[str, list[str]]:
    """Get all display ports for a given DRM device."""
    ports: dict[str, list[str]] = {"DP": [], "HDMI": []}

    try:
        entries = sorted(drm_device.iterdir(), key=lambda path: path.name)
    except OSError:
        print(f"DEBUG: get_display_ports: failed to list {drm_device}")
        return ports

    for entry in entries:
        port_name = entry.name
        if port_name.startswith("DP-") or port_name.startswith("eDP-"):
            ports["DP"].append(port_name)
        elif port_name.startswith("HDMI-"):
            ports["HDMI"].append(port_name)

    print(f"DEBUG: get_display_ports({drm_device.name}): DP={ports['DP']} HDMI={ports['HDMI']}")
    return ports


def get_connected_displays(card_name: str) -> list[str]:
    """Get list of currently connected displays from /sys/class/drm/"""
    drm_path = Path("/sys/class/drm")
    connected: list[str] = []

    try:
        displays = sorted(drm_path.iterdir(), key=lambda path: path.name)
    except OSError:
        print(f"DEBUG: get_connected_displays({card_name}): connected=[]")
        return connected

    for display in displays:
        if display.name.startswith(f"{card_name}-"):
            status_file = display / "status"
            if status_file.exists():
                try:
                    status = status_file.read_text().strip()
                    if status == "connected":
                        port_name = display.name.replace(f"{card_name}-", "")
                        connected.append(port_name)
                except Exception:
                    pass

    print(f"DEBUG: get_connected_displays({card_name}): connected={connected}")
    return connected


def find_empty_slot(drm_device: Path, card_name: str) -> tuple[str | None, Path | None]:
    """Find the first empty display slot, preferring DP over HDMI."""
    ports = get_display_ports(drm_device)
    connected = get_connected_displays(card_name)

    for port in sorted(ports["DP"]):
        if port not in connected:
            return port, drm_device

    for port in sorted(ports["HDMI"]):
        if port not in connected:
            return port, drm_device

    # Debug: show why we failed to find a slot
    print(f"DEBUG: find_empty_slot({drm_device.name}, {card_name}) -> no available ports. ports=DP:{ports['DP']} HDMI:{ports['HDMI']}, connected={connected}")
    return None, None


def get_drm_device_for_card(card_name: str) -> Path | None:
    """Find the DRM device path (from debugfs) for a given card name."""
    drm_class_path = Path("/sys/class/drm")
    card_path = drm_class_path / card_name

    if not card_path.exists():
        return None

    device_link = card_path / "device"
    if not device_link.exists():
        return None

    try:
        pci_addr = device_link.readlink().name
        device_path = Path("/sys/kernel/debug/dri") / pci_addr
        if device_path.exists():
            return device_path
    except Exception:
        pass

    return None


def get_card_name_from_device(drm_device_path: Path) -> str:
    """Extract card name (e.g., 'card1') from DRM device path."""
    device_name = drm_device_path.name

    drm_class_path = Path("/sys/class/drm")
    for card_dir in sorted(drm_class_path.iterdir()):
        if card_dir.name.startswith("card") and "-" not in card_dir.name:
            device_link = card_dir / "device"
            if device_link.exists():
                try:
                    target = os.readlink(device_link)
                    if device_name in target:
                        return card_dir.name
                except Exception:
                    pass

    # If no match found, attempt to find any available card
    for card_dir in sorted(drm_class_path.iterdir()):
        if card_dir.name.startswith("card") and "-" not in card_dir.name:
            return card_dir.name

    # Last resort fallback
    return "card0"
