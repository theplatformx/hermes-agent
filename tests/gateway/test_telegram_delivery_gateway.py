import asyncio
from pathlib import Path

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.telegram import TelegramAdapter


class _FakeMessage:
    def __init__(self, message_id: int = 1):
        self.message_id = message_id


class _FakeBot:
    def __init__(self):
        self.calls = []
        self.next_id = 1

    async def _record(self, method, **kwargs):
        self.calls.append((method, kwargs))
        await asyncio.sleep(0)
        msg = _FakeMessage(self.next_id)
        self.next_id += 1
        return msg

    async def send_message(self, **kwargs):
        return await self._record("send_message", **kwargs)

    async def edit_message_text(self, **kwargs):
        return await self._record("edit_message_text", **kwargs)

    async def send_photo(self, **kwargs):
        return await self._record("send_photo", **kwargs)

    async def send_document(self, **kwargs):
        return await self._record("send_document", **kwargs)


def _adapter(monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_SEND_WORKERS", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_SEND_QUEUE_MAX", "10")
    monkeypatch.setenv("HERMES_TELEGRAM_SEND_MIN_INTERVAL_SECONDS", "0")
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="dummy"))
    adapter._bot = _FakeBot()
    return adapter


@pytest.mark.asyncio
async def test_text_sends_are_serialized_through_delivery_gateway(monkeypatch):
    adapter = _adapter(monkeypatch)

    results = await asyncio.gather(
        adapter.send("123", "first"),
        adapter.send("123", "second"),
        adapter.send("123", "third"),
    )

    assert [result.success for result in results] == [True, True, True]
    assert [call[1]["text"] for call in adapter._bot.calls] == ["first", "second", "third"]
    assert adapter.delivery_health()["queue_depth"] == 0
    assert adapter.delivery_health()["last_successful_send_at"] is not None

    await adapter._stop_delivery_workers()


@pytest.mark.asyncio
async def test_edit_and_media_sends_use_delivery_gateway(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello")

    edit_result = await adapter.edit_message("123", "456", "updated")
    document_result = await adapter.send_document("123", str(file_path))

    assert edit_result.success is True
    assert document_result.success is True
    assert [call[0] for call in adapter._bot.calls] == ["edit_message_text", "send_document"]

    await adapter._stop_delivery_workers()


@pytest.mark.asyncio
async def test_pool_timeout_marks_retryable_fatal(monkeypatch):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="dummy"))
    adapter._delivery_pool_timeout_threshold = 2
    notified = []

    async def _notify():
        notified.append(True)

    monkeypatch.setattr(adapter, "_notify_fatal_error", _notify)

    adapter._record_delivery_failure(
        "telegram.error.TimedOut: Pool timeout: All connections in the connection pool are occupied"
    )
    assert adapter.has_fatal_error is False

    adapter._record_delivery_failure(
        "telegram.error.TimedOut: Pool timeout: All connections in the connection pool are occupied"
    )

    assert adapter.has_fatal_error is True
    assert adapter.fatal_error_code == "telegram_pool_timeout"
    assert adapter.fatal_error_retryable is True


def test_no_direct_bot_outbound_bypass_for_delivery_methods():
    source = Path("gateway/platforms/telegram.py").read_text()
    forbidden = [
        "self._bot.send_message(",
        "self._bot.edit_message_text(",
        "self._bot.send_voice(",
        "self._bot.send_audio(",
        "self._bot.send_photo(",
        "self._bot.send_document(",
        "self._bot.send_video(",
        "self._bot.send_animation(",
        "self._bot.send_chat_action(",
    ]
    assert [pattern for pattern in forbidden if pattern in source] == []
