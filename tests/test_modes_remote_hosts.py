"""Per-host remote registry: multiple concurrent hosts, tie-break, and
protocol v1 (legacy bare {title, exe, pid}) / v2 (kind="state"/"notification")
payload compatibility.
"""

import queue
import time
import unittest

import modes


def _drain_notification_queue():
    while True:
        try:
            modes._REMOTE_NOTIFICATION_QUEUE.get_nowait()
        except queue.Empty:
            break


class TestRemoteHostRegistry(unittest.TestCase):
    def setUp(self):
        modes._REMOTE_HOST_REGISTRY.clear()
        _drain_notification_queue()

    tearDown = setUp

    def test_two_hosts_tracked_independently(self):
        modes._accept_host_udp_payload(
            {
                "v": 2,
                "service": "turing-host-game",
                "kind": "state",
                "host_id": "win-pc",
                "hostname": "Gaming-PC",
                "game": {"title": "Palworld", "exe": "Palworld-Win64-Shipping.exe", "pid": 1},
            },
            source_ip="192.168.1.10",
        )
        modes._accept_host_udp_payload(
            {
                "v": 2,
                "service": "turing-host-game",
                "kind": "state",
                "host_id": "linux-pc",
                "hostname": "Workstation",
                "media": {"title": "Song", "artist": "Band", "is_playing": True},
            },
            source_ip="192.168.1.11",
        )
        self.assertEqual(len(modes._REMOTE_HOST_REGISTRY), 2)
        self.assertEqual(modes._REMOTE_HOST_REGISTRY["win-pc"]["game"]["title"], "Palworld")
        self.assertEqual(modes._REMOTE_HOST_REGISTRY["linux-pc"]["media"]["title"], "Song")
        # Neither host clobbers the other's slice.
        self.assertIsNone(modes._REMOTE_HOST_REGISTRY["win-pc"]["media"])
        self.assertIsNone(modes._REMOTE_HOST_REGISTRY["linux-pc"]["game"])

    def test_stale_host_pruned_after_ttl(self):
        modes._accept_host_udp_payload(
            {"kind": "state", "host_id": "old-host", "game": {"title": "Palworld", "exe": "Palworld.exe"}},
            source_ip="10.0.0.5",
        )
        modes._REMOTE_HOST_REGISTRY["old-host"]["last_seen_mono"] -= modes._REMOTE_HOST_STALE_SECONDS + 1
        modes._prune_stale_remote_hosts()
        self.assertNotIn("old-host", modes._REMOTE_HOST_REGISTRY)

    def test_notification_payload_does_not_touch_registry(self):
        modes._accept_host_udp_payload(
            {"kind": "notification", "host_id": "win-pc", "app": "Discord", "title": "New message", "body": "hi"},
            source_ip="192.168.1.10",
        )
        self.assertEqual(modes._REMOTE_HOST_REGISTRY, {})
        notes = modes.pop_remote_notifications()
        self.assertEqual(notes, [{"app": "Discord", "title": "New message", "body": "hi"}])
        # Non-blocking drain: a second call returns nothing left.
        self.assertEqual(modes.pop_remote_notifications(), [])


class TestRemoteTieBreak(unittest.TestCase):
    def setUp(self):
        modes._REMOTE_HOST_REGISTRY.clear()

    tearDown = setUp

    def _entry(self, host_id, age, **kwargs):
        entry = {
            "host_id": host_id,
            "hostname": host_id,
            "source_ip": "",
            "last_seen_mono": time.monotonic() - age,
            "game": None,
            "media": None,
            "lock": None,
        }
        entry.update(kwargs)
        modes._REMOTE_HOST_REGISTRY[host_id] = entry

    def test_most_recent_game_host_wins_when_both_recognized(self):
        self._entry("older", age=2.0, game={"title": "Valorant", "exe": "VALORANT-Win64-Shipping.exe", "pid": 1})
        self._entry("newer", age=0.1, game={"title": "Palworld", "exe": "Palworld-Win64-Shipping.exe", "pid": 2})
        hit = modes._remote_game_hit()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["display_name"], "Palworld")

    def test_most_recent_media_host_wins(self):
        base_media = {"artist": "", "album": "", "is_playing": True, "position": 0.0, "length": 0.0, "player_id": ""}
        self._entry("older", age=2.0, media={**base_media, "title": "Old Song"})
        self._entry("newer", age=0.1, media={**base_media, "title": "New Song"})
        info = modes._remote_media_hit()
        self.assertIsNotNone(info)
        self.assertEqual(info.title, "New Song")

    def test_most_recent_lock_host_wins(self):
        self._entry("older", age=2.0, lock={"is_locked": True})
        self._entry("newer", age=0.1, lock={"is_locked": True})
        hit = modes._remote_lock_hit()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["host_id"], "newer")

    def test_non_locked_host_never_wins(self):
        self._entry("locked-host", age=2.0, lock={"is_locked": True})
        self._entry("unlocked-host", age=0.1, lock={"is_locked": False})
        hit = modes._remote_lock_hit()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["host_id"], "locked-host")


class TestLegacyV1Payload(unittest.TestCase):
    def setUp(self):
        modes._REMOTE_HOST_REGISTRY.clear()

    tearDown = setUp

    def test_legacy_bare_payload_accepted_as_state_game(self):
        modes._accept_host_udp_payload(
            {
                "v": 1,
                "service": "turing-host-game",
                "title": "Palworld",
                "exe": r"C:\Games\Palworld\Palworld-Win64-Shipping.exe",
                "pid": 99,
                "updated_at": time.time(),
            },
            source_ip="192.168.1.20",
        )
        entries = list(modes._REMOTE_HOST_REGISTRY.values())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["host_id"], "legacy:192.168.1.20")
        self.assertEqual(entries[0]["game"]["title"], "Palworld")
        self.assertIsNone(entries[0]["media"])
        self.assertIsNone(entries[0]["lock"])

    def test_legacy_payload_without_kind_does_not_crash_on_missing_host_id(self):
        modes._accept_host_udp_payload(
            {"service": "turing-host-game", "title": "Desktop", "exe": r"C:\Windows\explorer.exe", "pid": 1},
            source_ip="192.168.1.21",
        )
        self.assertIn("legacy:192.168.1.21", modes._REMOTE_HOST_REGISTRY)

    def test_legacy_payload_ignored_when_no_title_or_exe(self):
        modes._accept_host_udp_payload(
            {"service": "turing-host-game", "updated_at": time.time()},
            source_ip="192.168.1.22",
        )
        self.assertNotIn("legacy:192.168.1.22", modes._REMOTE_HOST_REGISTRY)

    def test_wrong_service_name_ignored(self):
        modes._accept_host_udp_payload(
            {"service": "some-other-app", "title": "Palworld", "exe": "Palworld.exe"},
            source_ip="192.168.1.23",
        )
        self.assertEqual(modes._REMOTE_HOST_REGISTRY, {})


if __name__ == "__main__":
    unittest.main()
