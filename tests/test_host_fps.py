import importlib.util
import io
from pathlib import Path
import time
import tempfile
import unittest
from unittest.mock import patch

import modes

path = Path(__file__).resolve().parents[1] / "tools/host-game-helper/fps_monitor.py"
spec = importlib.util.spec_from_file_location("fps_monitor", path)
fps_monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fps_monitor)
common_spec = importlib.util.spec_from_file_location("host_common", path.with_name("common.py"))
common = importlib.util.module_from_spec(common_spec)
common_spec.loader.exec_module(common)


class TestFps(unittest.TestCase):
    def setUp(self):
        modes._REMOTE_HOST_REGISTRY.clear()

    def tearDown(self):
        modes._REMOTE_HOST_REGISTRY.clear()

    def test_frame_times_reach_remote_game_and_expire(self):
        monitor = fps_monitor.FpsMonitor()
        process = type("Process", (), {"stdout": io.StringIO(
            "Application,msBetweenPresents\nPalworld,10\nPalworld,20\nPalworld,NaN\n"
        )})()
        monitor._process = process
        monitor._pid = 42
        monitor._read(process)
        process.poll = lambda: None
        fps = monitor.sample(42)
        self.assertAlmostEqual(fps, 1000 / 15)
        modes._accept_host_udp_payload({
            "host_id": "pc", "game": {"title": "Palworld", "exe": "Palworld.exe", "pid": 42},
            "metrics": {"fps": fps},
        })
        info = modes.GamerInfo(remote_metrics=modes._remote_game_hit()["remote_metrics"])
        modes.GamerDetector._apply_remote_metrics(info)
        self.assertAlmostEqual(info.fps, 1000 / 15)
        with patch.object(fps_monitor.time, "monotonic", return_value=time.monotonic() + 3):
            self.assertIsNone(monitor.sample(42))

    def test_absent_presentmon_is_optional(self):
        monitor = fps_monitor.FpsMonitor()
        with patch.object(monitor, "_executable", return_value=None):
            self.assertIsNone(monitor.sample(42))

    def test_unknown_steam_game_is_detected_without_a_list_entry(self):
        executable = r"D:\SteamLibrary\steamapps\common\Unlisted Game\Binaries\Game-Win64-Shipping.exe"
        self.assertTrue(common.game_executable_hit(executable))
        self.assertTrue(modes._classify_helper_payload({"title": "Unlisted Game", "exe": executable})[0])
        self.assertFalse(common.game_executable_hit(r"D:\Steam\steamapps\common\Unlisted Game\launcher.exe"))
        self.assertFalse(common.game_executable_hit(r"C:\Windows\notepad.exe"))

    def test_steam_manifest_appid_reaches_remote_game(self):
        with tempfile.TemporaryDirectory() as temp:
            steamapps = Path(temp) / "steamapps"
            steamapps.mkdir()
            (steamapps / "appmanifest_123456.acf").write_text(
                '"AppState" { "appid" "123456" "installdir" "Unlisted Game" }', encoding="utf-8"
            )
            exe = str(steamapps / "common" / "Unlisted Game" / "game.exe")
            appid = common.steam_appid_for_executable(exe)
            self.assertEqual(appid, "123456")
            modes._accept_host_udp_payload({"host_id": "pc", "game": {
                "title": "Unlisted Game", "exe": exe, "pid": 42, "appid": appid,
            }})
            self.assertEqual(modes._remote_game_hit()["appid"], "123456")

    def test_moonlight_uses_matching_host_game_metrics(self):
        modes._accept_host_udp_payload({
            "host_id": "pc", "game": {"title": "Palworld", "exe": "Palworld.exe", "pid": 42},
            "metrics": {"fps": 75},
        }, source_ip="192.168.1.10")
        process = type("Process", (), {"cmdline": lambda self: [], "exe": lambda self: "/usr/bin/moonlight"})()
        detector = modes.GamerDetector.__new__(modes.GamerDetector)
        with patch.object(modes, "_iter_processes", return_value=[(1234, 500, "moonlight")]), \
             patch.object(modes, "_process_handle", return_value=process), \
             patch.object(modes, "_moonlight_hosts", return_value=[{"ips": ["192.168.1.10"]}]), \
             patch.object(modes, "_proc_has_gamestream_traffic", return_value=True), \
             patch.object(modes, "_resolve_moonlight_game_info", return_value={
                 "is_game": True, "display_name": "Palworld", "appid": "1623730"
             }):
            self.assertEqual(detector._detect_game()["remote_metrics"]["fps"], 75)


if __name__ == "__main__":
    unittest.main()
