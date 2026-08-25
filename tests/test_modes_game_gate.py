"""GAMER mode must only trigger on a *real* game from the Windows helper.

The Mini-PC (this service) is the authority: it filters every helper
announcement locally, so BOTH helper versions behave identically —
  * old helper: announces ANY focused window (no is_game field);
  * new helper: may send is_game — it is ignored, local list decides.
A Desktop remoto stream whose focused window is not a recognized game must
not surface a game, so the little screen stays on the clock.
"""

import unittest

import modes
from modes import (
    GamerDetector,
    _classify_helper_payload,
    _match_game_from_foreground,
    _match_game_name_from_text,
    _resolve_moonlight_game_info,
)


class TestMatchGameNameFromText(unittest.TestCase):
    def test_known_game_in_text(self):
        self.assertEqual(_match_game_name_from_text("Palworld"), ("Palworld", "1623730"))

    def test_known_game_substring(self):
        name, _ = _match_game_name_from_text("Valorant - Ranked")
        self.assertEqual(name, "Valorant")

    def test_no_game_returns_empty(self):
        self.assertEqual(_match_game_name_from_text("Mozilla Firefox"), ("", ""))
        self.assertEqual(_match_game_name_from_text("Bloco de Notas"), ("", ""))

    def test_empty_text(self):
        self.assertEqual(_match_game_name_from_text(""), ("", ""))


class TestClassifyHelperPayload(unittest.TestCase):
    """Local filter applied to every announcement (old or new helper)."""

    def test_old_helper_browser_focus_is_filtered(self):
        # Old helper announces the focused browser — must NOT become a game.
        self.assertEqual(
            _classify_helper_payload(
                {"title": "Mozilla Firefox", "exe": r"C:\Program Files\firefox.exe"}
            ),
            ("", ""),
        )

    def test_old_helper_office_focus_is_filtered(self):
        self.assertEqual(
            _classify_helper_payload(
                {"title": "Relatório.xlsx - Excel", "exe": r"C:\Program Files\Office\EXCEL.EXE"}
            ),
            ("", ""),
        )

    def test_old_helper_shell_focus_fallback_to_running_game_passes(self):
        # Old helper: shell focused, but Palworld running → payload carries
        # the game exe. Local list recognizes it → passes.
        name, appid = _classify_helper_payload(
            {
                "title": "Palworld",
                "exe": r"C:\Games\Palworld\Palworld-Win64-Shipping.exe",
            }
        )
        self.assertEqual(name, "Palworld")
        self.assertEqual(appid, "1623730")

    def test_new_helper_flag_is_ignored_local_list_decides(self):
        # New helper with is_game=True but an exe the local list does not
        # recognize: still filtered (the service is the authority).
        self.assertEqual(
            _classify_helper_payload(
                {"title": "Some New Game", "exe": "somegame.exe", "is_game": True}
            ),
            ("", ""),
        )
        # ...and a recognized game passes regardless of the flag being absent
        # or present.
        self.assertEqual(
            _classify_helper_payload(
                {"title": "Counter-Strike 2", "exe": r"C:\cs2\cs2.exe"}
            ),
            ("Counter-Strike 2", "730"),
        )
        self.assertEqual(
            _classify_helper_payload(
                {"title": "Counter-Strike 2", "exe": r"C:\cs2\cs2.exe", "is_game": True}
            ),
            ("Counter-Strike 2", "730"),
        )

    def test_non_game_exe_guard_blocks_title_spoofing(self):
        # Even if the title contains a game name, a browser exe is filtered.
        self.assertEqual(
            _classify_helper_payload(
                {"title": "Palworld - Mozilla Firefox", "exe": r"C:\Program Files\firefox.exe"}
            ),
            ("", ""),
        )

    def test_known_local_game_not_in_helper_list_passes(self):
        # Local list is a superset of the helper's (e.g. hl2): recognized here
        # even if the old helper didn't know it.
        self.assertEqual(
            _classify_helper_payload(
                {"title": "Half-Life 2", "exe": r"C:\hl2\hl2.exe"}
            ),
            ("Half-Life 2", "420"),
        )


class TestMatchGameFromForeground(unittest.TestCase):
    def test_game_exe_match(self):
        name, appid = _match_game_from_foreground(
            "Palworld", r"C:\Games\Palworld\Palworld-Win64-Shipping.exe"
        )
        self.assertEqual(name, "Palworld")
        self.assertEqual(appid, "1623730")

    def test_non_game_window_is_not_a_game(self):
        # Regression: any foreground window used to count as a game.
        self.assertEqual(
            _match_game_from_foreground("Mozilla Firefox", r"C:\Program Files\firefox.exe"),
            ("", ""),
        )


class _FakeProcess:
    def __init__(self, name, pid, ppid, cmdline=None, exe=""):
        self.info = {"name": name, "pid": pid, "ppid": ppid}
        self._cmdline = cmdline or []
        self._exe = exe

    def cmdline(self):
        return list(self._cmdline)

    def exe(self):
        return self._exe


def _run_detect_game(processes, helper_payload=None, streaming=True):
    """Run GamerDetector._detect_game with psutil + network calls mocked."""
    detector = GamerDetector.__new__(GamerDetector)  # skip __init__ (no UDP listener)
    saved = (
        modes.psutil.process_iter,
        modes._moonlight_hosts,
        modes._proc_has_gamestream_traffic,
        modes._query_host_game_helper,
    )
    modes.psutil.process_iter = lambda attrs=None: iter(processes)
    modes._moonlight_hosts = lambda: []
    modes._proc_has_gamestream_traffic = lambda proc, ips: streaming
    modes._query_host_game_helper = lambda hosts: dict(helper_payload) if helper_payload is not None else None
    try:
        return detector._detect_game()
    finally:
        (
            modes.psutil.process_iter,
            modes._moonlight_hosts,
            modes._proc_has_gamestream_traffic,
            modes._query_host_game_helper,
        ) = saved


class TestResolveMoonlightGameInfo(unittest.TestCase):
    def _with_helper(self, payload):
        saved = modes._query_host_game_helper
        modes._query_host_game_helper = lambda hosts: dict(payload)
        try:
            return _resolve_moonlight_game_info(None, [], [])
        finally:
            modes._query_host_game_helper = saved

    def test_old_helper_browser_foreground_is_not_a_game(self):
        # The exact failure mode: old helper announces the focused browser.
        info = self._with_helper(
            {"title": "Mozilla Firefox", "exe": r"C:\Program Files\firefox.exe"}
        )
        self.assertFalse(info["is_game"])
        self.assertEqual(info["display_name"], "")

    def test_old_helper_desktop_title_is_not_a_game(self):
        info = self._with_helper({"title": "Desktop", "exe": r"C:\Windows\explorer.exe"})
        self.assertFalse(info["is_game"])
        self.assertEqual(info["display_name"], "")

    def test_helper_running_game_fallback_enters(self):
        # Old helper: shell focused, running game announced with its exe.
        info = self._with_helper(
            {
                "title": "Palworld",
                "exe": r"C:\Games\Palworld\Palworld-Win64-Shipping.exe",
            }
        )
        self.assertTrue(info["is_game"])
        self.assertEqual(info["display_name"], "Palworld")

    def test_new_helper_flag_does_not_bypass_local_filter(self):
        info = self._with_helper(
            {"title": "Some New Game", "exe": "somegame.exe", "is_game": True}
        )
        self.assertFalse(info["is_game"])
        self.assertEqual(info["display_name"], "")

    def test_new_helper_recognized_game_enters(self):
        info = self._with_helper(
            {"title": "Counter-Strike 2", "exe": r"C:\cs2\cs2.exe", "is_game": True}
        )
        self.assertTrue(info["is_game"])
        self.assertEqual(info["display_name"], "Counter-Strike 2")

    def _no_helper(self, cmdline):
        # No helper, no Sunshine/journal fallback — pure cmdline classification.
        saved = (
            modes._query_host_game_helper,
            modes._query_sunshine_current_game,
            modes._moonlight_last_launch_appid,
        )
        modes._query_host_game_helper = lambda hosts: None
        modes._query_sunshine_current_game = lambda ip: None
        modes._moonlight_last_launch_appid = lambda: None
        try:
            return _resolve_moonlight_game_info(None, [], cmdline)
        finally:
            (
                modes._query_host_game_helper,
                modes._query_sunshine_current_game,
                modes._moonlight_last_launch_appid,
            ) = saved

    def test_cmdline_game_when_no_helper(self):
        info = self._no_helper(["moonlight", "stream", "192.168.0.10", "Palworld"])
        self.assertTrue(info["is_game"])
        self.assertEqual(info["display_name"], "Palworld")

    def test_cmdline_desktop_when_no_helper(self):
        # "Desktop" (or "área de trabalho") is the remote-desktop app, not a game.
        info = self._no_helper(["moonlight", "stream", "192.168.0.10", "Desktop"])
        self.assertFalse(info["is_game"])
        self.assertEqual(info["display_name"], "")
        info = self._no_helper(["moonlight", "stream", "192.168.0.10", "Área de Trabalho"])
        self.assertFalse(info["is_game"])


class TestDetectGameGatesOnIsGame(unittest.TestCase):
    MOONLIGHT = _FakeProcess("moonlight", 1234, 500, cmdline=[], exe="/snap/bin/moonlight")

    def test_recognized_game_enters(self):
        hit = _run_detect_game(
            [self.MOONLIGHT],
            helper_payload={
                "title": "Palworld",
                "exe": r"C:\Games\Palworld\Palworld-Win64-Shipping.exe",
            },
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit["display_name"], "Palworld")

    def test_old_helper_non_game_foreground_never_enters(self):
        # Desktop remoto stream, old helper announcing the focused browser.
        hit = _run_detect_game(
            [self.MOONLIGHT],
            helper_payload={
                "title": "Mozilla Firefox",
                "exe": r"C:\Program Files\firefox.exe",
            },
        )
        self.assertIsNone(hit)

    def test_no_helper_never_enters(self):
        hit = _run_detect_game([self.MOONLIGHT], helper_payload=None)
        self.assertIsNone(hit)


if __name__ == "__main__":
    unittest.main()
