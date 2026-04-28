"""Focused tests for the refactored display workflow."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.display import connect, disconnect


class TestConnectRefactor:
    def test_writes_json_state(self, tmp_path):
        with patch("src.display.SCRIPT_DIR", tmp_path), \
             patch("src.display.get_pixel_clock_info", return_value=(100.0, 655.35, False)), \
             patch("src.display.get_drm_devices", return_value=[Path("/sys/kernel/debug/dri/0000:01:00.0")]), \
             patch("src.display.get_card_name_from_device", return_value="card1"), \
             patch("src.display.get_connected_displays", return_value=["HDMI-1"]), \
             patch("src.display.find_empty_slot", return_value=("DP-1", tmp_path)), \
             patch("src.display._write_sysfs_bytes", return_value=True), \
             patch("src.display._write_sysfs_text", return_value=True), \
             patch("src.display.clear_kwin_output_config"), \
             patch("src.display.release_crtc", return_value=True), \
             patch("src.display.force_crtc_assignment", return_value=True), \
             patch("src.display.wait_for_output_ready", return_value=(True, "1920x1080")), \
             patch("src.display.create_edid", return_value=b"\x00" * 256):
            assert connect(1920, 1080, 60, disable_physical_displays=True)

        state = json.loads((tmp_path / "virt_display.state").read_text())
        assert state["card_name"] == "card1"
        assert state["virtual_port"] == "DP-1"
        assert state["disabled_displays_by_card"]["card1"] == ["HDMI-1"]

    def test_fails_when_enable_write_fails(self, tmp_path):
        def write_bytes(path: Path, value: bytes) -> bool:
            return True

        def write_text(path: Path, value: str) -> bool:
            return value != "on"

        with patch("src.display.SCRIPT_DIR", tmp_path), \
             patch("src.display.get_pixel_clock_info", return_value=(100.0, 655.35, False)), \
             patch("src.display.get_drm_devices", return_value=[Path("/sys/kernel/debug/dri/0000:01:00.0")]), \
             patch("src.display.get_card_name_from_device", return_value="card1"), \
             patch("src.display.get_connected_displays", return_value=[]), \
             patch("src.display.find_empty_slot", return_value=("DP-1", tmp_path)), \
             patch("src.display._write_sysfs_bytes", side_effect=write_bytes), \
             patch("src.display._write_sysfs_text", side_effect=write_text), \
             patch("src.display.release_crtc", return_value=True), \
             patch("src.display.clear_kwin_output_config"), \
             patch("src.display.create_edid", return_value=b"\x00" * 256):
            assert connect(1920, 1080, 60) is False


class TestDisconnectRefactor:
    def _write_state(self, path: Path, card: str, port: str, displays: list[str]) -> None:
        (path / "virt_display.state").write_text(
            json.dumps(
                {
                    "card_name": card,
                    "virtual_port": port,
                    "disabled_displays_by_card": {card: displays},
                    "disable_physical_displays": True,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def test_restores_displays_and_deletes_state(self, tmp_path):
        self._write_state(tmp_path, "card1", "DP-2", ["HDMI-1", "DP-1"])
        mock_force = MagicMock(return_value=True)

        with patch("src.display.SCRIPT_DIR", tmp_path), \
             patch("src.display._write_sysfs_text", return_value=True), \
             patch("src.display.force_crtc_assignment", mock_force), \
             patch("src.display.release_crtc", return_value=True):
            assert disconnect() is True

        assert mock_force.call_count == 2
        assert not (tmp_path / "virt_display.state").exists()

    def test_warns_when_turn_off_fails(self, tmp_path, capsys):
        self._write_state(tmp_path, "card1", "DP-2", [])

        def write_text(path: Path, value: str) -> bool:
            return value != "off"

        with patch("src.display.SCRIPT_DIR", tmp_path), \
             patch("src.display._write_sysfs_text", side_effect=write_text), \
             patch("src.display.force_crtc_assignment", return_value=True), \
             patch("src.display.release_crtc", return_value=True):
            assert disconnect() is True

        assert "Warning" in capsys.readouterr().out
