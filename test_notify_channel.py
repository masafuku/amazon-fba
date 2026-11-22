import os
import unittest
from unittest import mock

import notify


class NotifyChannelTest(unittest.TestCase):
    def test_split_keeps_lines_and_limit(self):
        text = "\n".join(f"行{i} " + "あ" * 50 for i in range(100))
        chunks = notify.split_for_discord(text, limit=500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("\n".join(chunks), text)

    def test_long_single_line_is_cut(self):
        chunks = notify.split_for_discord("x" * 4500)
        self.assertEqual([len(c) for c in chunks], [2000, 2000, 500])

    def test_discord_used_when_webhook_set(self):
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.example/hook"}), \
             mock.patch.object(notify, "send_discord") as send:
            self.assertEqual(notify.send(["a", "", "b"]), "Discord")
        send.assert_called_once_with("https://discord.example/hook", ["a", "b"])

    def test_digest_also_goes_to_line_and_line_failure_is_tolerated(self):
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.example/hook"}), \
             mock.patch.object(notify, "send_discord") as send, \
             mock.patch.object(notify, "_send_line", side_effect=RuntimeError("monthly limit")) as line:
            self.assertEqual(notify.send("まとめ", also_line=True), "Discord (LINEは失敗)")
        send.assert_called_once()
        line.assert_called_once_with(["まとめ"])


if __name__ == "__main__":
    unittest.main()
