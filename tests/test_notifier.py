import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notifications.notifier import Notifier


class DummyBot:
    def __init__(self, token: str):
        self.token = token
        self.sent = None

    async def send_message(self, **kwargs):
        self.sent = kwargs
        return True


def test_notify_alive_sends_keepalive_message(monkeypatch):
    notifier = Notifier(token="abc", chat_id="123")
    dummy_bot = DummyBot("abc")
    monkeypatch.setattr(notifier, "_get_bot", lambda: dummy_bot)

    async def run_test():
        result = await notifier.notify_alive(15)
        assert result is True
        assert dummy_bot.sent is not None
        assert "Bot Alive" in dummy_bot.sent["text"]
        assert "15m" in dummy_bot.sent["text"]

    asyncio.run(run_test())
