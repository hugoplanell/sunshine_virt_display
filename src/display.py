"""
Connect and disconnect virtual displays by managing EDIDs and sysfs connector state.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.drm import (
    find_empty_slot,
    force_crtc_assignment,
    get_card_name_from_device,
    get_connected_displays,
    get_drm_device_for_card,
    get_drm_devices,
    release_crtc,
    run_command,
    wait_for_output_ready,
)
from src.drm.de.kwin import clear_kwin_output_config
from src.edid import create_edid, find_best_vic_resolution, get_pixel_clock_info

SCRIPT_DIR = Path(__file__).parent.parent.absolute()


def connect(
    width: int,
    height: int,
    refresh_rate: int,
    device: str | None = None,
    disable_physical_displays: bool = False,
) -> bool:
    """
    Connect a virtual display:
    1. Generate custom EDID
    2. Find empty display slot
    3. Override EDID
    4. Turn off connected displays
    5. Turn on virtual display
    6. Wait for output to be ready
    """
    print(f"Connecting virtual display: {width}x{height}@{refresh_rate}Hz")

    # Step 1: Generate custom EDID
    print("Step 1: Generating custom EDID...")
    print(f"  Requested: {width}x{height} @ {refresh_rate}Hz")

    pixel_clock_mhz, max_mhz, will_break = get_pixel_clock_info(
        width, height, refresh_rate
    )
    print(f"  Pixel clock: {pixel_clock_mhz:.2f} MHz (max: {max_mhz:.2f} MHz)")

    if will_break:
        print(
            f"  ⚠️  WARNING: Pixel clock exceeds limit by {pixel_clock_mhz - max_mhz:.2f} MHz!"
        )
        print(f"  Finding best VIC standard resolution...")

        vic_result = find_best_vic_resolution(width, height, refresh_rate)
        if vic_result:
            vic_width, vic_height, vic_refresh, vic_code, vic_name = vic_result
            print(
                f"  → Falling back to VIC {vic_code}: {vic_width}x{vic_height} @ {vic_refresh}Hz ({vic_name})"
            )

            new_clock_mhz, _, _ = get_pixel_clock_info(
                vic_width, vic_height, vic_refresh
            )
            print(f"  → New pixel clock: {new_clock_mhz:.2f} MHz")

            width, height, refresh_rate = vic_width, vic_height, vic_refresh
        else:
            print(f"  ⚠️  No suitable VIC found, attempting custom resolution anyway...")
    else:
        print(f"  ✓ Pixel clock within limits")
        print(f"  ✓ Using custom resolution: {width}x{height} @ {refresh_rate}Hz")

    edid_data = create_edid(
        width=width,
        height=height,
        refresh_rate=refresh_rate,
        enable_hdr=True,
        display_name="Virtual Display",
    )

    edid_file = SCRIPT_DIR / "custom_edid.bin"
    _ = edid_file.write_bytes(edid_data)
    print(f"  ✓ Created EDID file: {edid_file}")
    print(f"  ✓ Final resolution: {width}x{height} @ {refresh_rate}Hz")
    print(f"  ✓ EDID size: {len(edid_data)} bytes")

    # Step 2: Find DRM devices and list connected displays
    print("\nStep 2: Scanning displays...")
    drm_devices = get_drm_devices()

    if not drm_devices:
        print("Error: No DRM devices found")
        return False

    if device:
        # User explicitly specified a card name (e.g., card0, card1)
        drm_device = get_drm_device_for_card(device)
        if not drm_device:
            available = [get_card_name_from_device(d) for d in drm_devices]
            print(f"Error: device '{device}' not found. Available: {available}")
            return False
        card_name = device
    else:
        # Pick the device that has the most connected displays — on multi-GPU
        # systems this ensures we land on the card with physical monitors rather
        # than an idle iGPU that happens to sort first by PCI address.
        best_device = drm_devices[0]
        best_count = -1
        for dev in drm_devices:
            c = get_card_name_from_device(dev)
            n = len(get_connected_displays(c))
            if n > best_count:
                best_count = n
                best_device = dev
        drm_device = best_device
        card_name = get_card_name_from_device(drm_device)
    print(f"  Using device: {drm_device.name} ({card_name})")

    connected_displays = get_connected_displays(card_name)
    print(
        f"  Connected displays: {connected_displays if connected_displays else 'None'}"
    )

    # Step 3: Find empty slot
    print("\nStep 3: Finding empty display slot...")
    empty_port, slot_device = find_empty_slot(drm_device, card_name)

    # Auto-selection can pick the card with active physical displays but no free
    # virtual connector (common on hybrid GPU laptops). If that happens, try the
    # remaining cards before giving up.
    if not empty_port and not device:
        print(f"  No empty slot on {card_name}, trying other DRM devices...")
        for alt_device in drm_devices:
            if alt_device == drm_device:
                continue

            alt_card = get_card_name_from_device(alt_device)
            alt_connected = get_connected_displays(alt_card)
            print(
                f"  Trying device: {alt_device.name} ({alt_card}), connected: {alt_connected if alt_connected else 'None'}"
            )

            alt_port, alt_slot_device = find_empty_slot(alt_device, alt_card)
            if alt_port:
                drm_device = alt_device
                card_name = alt_card
                connected_displays = alt_connected
                empty_port = alt_port
                slot_device = alt_slot_device
                print(f"  ✓ Switched to device: {drm_device.name} ({card_name})")
                break

    if not empty_port:
        print("Error: No empty display slots available")
        return False

    print(f"  ✓ Selected slot: {empty_port}")

    # Step 4: Override EDID
    print(f"\nStep 4: Overriding EDID for {empty_port}...")
    edid_override_path = slot_device / empty_port / "edid_override"

    cmd = f"sh -c 'cat {edid_file.absolute()} > {edid_override_path}'"
    result = run_command(cmd)

    if result.returncode != 0:
        print(f"  Error overriding EDID: {result.stderr}")
        return False

    print(f"  ✓ EDID override applied")

    # Step 5: Optionally turn off connected displays on all cards.
    # This is opt-in because it can blank local displays unexpectedly.
    disabled_displays_by_card: dict[str, list[str]] = {}
    if disable_physical_displays:
        print("\nStep 5: Turning off connected physical displays (all cards)...")
        for dev in drm_devices:
            card = get_card_name_from_device(dev)
            card_connected = get_connected_displays(card)
            if not card_connected:
                continue

            for display in card_connected:
                _ = release_crtc(card, display)
                status_path = f"/sys/class/drm/{card}-{display}/status"
                cmd = f"sh -c 'echo off > {status_path}'"
                _ = run_command(cmd)
                disabled_displays_by_card.setdefault(card, []).append(display)
                print(f"  ✓ Turned off {card}-{display}")
    else:
        print("\nStep 5: Skipping physical display shutdown (use --disable-physical-displays to enable)")

    # Step 6: Clear any stale KWin output config, then turn on virtual display
    print(f"\nStep 6: Preparing virtual display ({empty_port})...")
    clear_kwin_output_config(empty_port)
    print(f"  Turning on virtual display ({empty_port})...")
    status_path = f"/sys/class/drm/{card_name}-{empty_port}/status"
    cmd = f"sh -c 'echo on > {status_path}'"
    result = run_command(cmd)

    if result.returncode != 0:
        print(f"  Error turning on display: {result.stderr}")
        return False

    print(f"  ✓ Virtual display enabled on {empty_port}")

    # Step 7: Wait for compositor to assign CRTC naturally, then fall back to forcing
    print(f"\nStep 7: Waiting for output to be ready...")
    ready, mode = wait_for_output_ready(card_name, empty_port, width, height, timeout=5.0)

    if ready:
        print(f"  ✓ Output ready ({mode})")
    else:
        print(f"  ⚠ Compositor did not assign CRTC — forcing assignment...")
        _ = force_crtc_assignment(card_name, empty_port)
        ready, mode = wait_for_output_ready(card_name, empty_port, width, height, timeout=5.0)
        if ready:
            print(f"  ✓ Output ready ({mode})")
        else:
            print(f"  ⚠ Timed out waiting for output, proceeding anyway")

    # Save state for disconnect
    state_file = SCRIPT_DIR / "virt_display.state"
    # Keep line 3 compatible with old state format (selected card displays), and
    # append structured data for multi-card restore when option is enabled.
    _ = state_file.write_text(
        f"{card_name}\n"
        f"{empty_port}\n"
        f"{','.join(disabled_displays_by_card.get(card_name, []))}\n"
        f"disabled_by_card_json={json.dumps(disabled_displays_by_card, sort_keys=True)}\n"
        f"disable_physical_displays={1 if disable_physical_displays else 0}\n"
    )

    print(f"\n✓ Virtual display successfully connected!")
    print(f"  Port: {card_name}-{empty_port}")
    print(f"  Resolution: {width}x{height}@{refresh_rate}Hz")

    return True


def disconnect() -> bool:
    """
    Disconnect virtual display:
    1. Turn off virtual display
    2. Turn on previously connected displays
    """
    print("Disconnecting virtual display...")

    state_file = SCRIPT_DIR / "virt_display.state"
    if not state_file.exists():
        print("Error: No state file found. Was a virtual display connected?")
        return False

    state_data = state_file.read_text().strip().split("\n")
    if len(state_data) < 2:
        print("Error: Invalid state file")
        return False

    card_name = state_data[0]
    virtual_port = state_data[1]
    previous_displays = state_data[2].split(",") if len(state_data) > 2 and state_data[2] else []
    disabled_displays_by_card: dict[str, list[str]] = {card_name: previous_displays}

    for line in state_data[3:]:
        if line.startswith("disabled_by_card_json="):
            raw_json = line.split("=", 1)[1].strip()
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    normalized: dict[str, list[str]] = {}
                    for parsed_card, parsed_displays in parsed.items():
                        if isinstance(parsed_card, str) and isinstance(parsed_displays, list):
                            normalized[parsed_card] = [d for d in parsed_displays if isinstance(d, str)]
                    disabled_displays_by_card = normalized
            except Exception:
                pass

    print(f"  Virtual display: {card_name}-{virtual_port}")
    print(
        f"  Previous displays: {disabled_displays_by_card if disabled_displays_by_card else 'None'}"
    )

    # Step 1: Turn on physical displays FIRST — avoid a zero-output window
    # that can confuse the compositor (KWin crashes or stops rendering if
    # all outputs disappear at once).
    print("\nStep 1: Turning on previous displays...")
    for restore_card, restore_displays in disabled_displays_by_card.items():
        for display in restore_displays:
            if display:
                status_path = f"/sys/class/drm/{restore_card}-{display}/status"
                cmd = f"sh -c 'echo on > {status_path}'"
                _ = run_command(cmd)
                print(f"  ✓ Turned on {restore_card}-{display}")

    # Step 2: Force CRTC assignment for restored displays
    # On AMD, sysfs hotplug alone doesn't assign CRTCs
    print("\nStep 2: Forcing CRTC assignment for restored displays...")
    for restore_card, restore_displays in disabled_displays_by_card.items():
        for display in restore_displays:
            if display:
                _ = force_crtc_assignment(restore_card, display)

    # Step 3: Release CRTC from virtual display and turn it off
    print(f"\nStep 3: Releasing CRTC from virtual display ({virtual_port})...")
    _ = release_crtc(card_name, virtual_port)

    print(f"\nStep 4: Turning off virtual display ({virtual_port})...")
    status_path = f"/sys/class/drm/{card_name}-{virtual_port}/status"
    cmd = f"sh -c 'echo off > {status_path}'"
    result = run_command(cmd)

    if result.returncode != 0:
        print(f"  Warning: Could not turn off virtual display: {result.stderr}")
    else:
        print(f"  ✓ Virtual display turned off")

    state_file.unlink()

    print("\n✓ Virtual display disconnected!")
    return True
