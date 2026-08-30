"""Unit tests for the dependency-free parts of the Telegram bridge."""

import importlib.util
import pathlib
import sys
from unittest.mock import MagicMock, patch

MODULE_PATH = pathlib.Path(__file__).with_name("telegram_bot.py")
SPEC = importlib.util.spec_from_file_location("nat_chatbot_telegram_bot", MODULE_PATH)
bot_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bot_module
SPEC.loader.exec_module(bot_module)


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, *_args):
        return self.body


def test_nat_client_request_shapes():
    responses = [Response(b'{"result":"hello"}'), Response(b'{"value":2}')]
    with patch.object(bot_module.urllib.request, "urlopen", side_effect=responses) as urlopen:
        client = bot_module.NatChatbotClient("http://nat:8080/")
        assert client.chat("hi") == "hello"
        assert client.reset("telegram-1") == {"value": 2}

    first = urlopen.call_args_list[0].args[0]
    second = urlopen.call_args_list[1].args[0]
    assert first.full_url == "http://nat:8080/v1/workflow"
    assert first.data == b'{"message": "hi"}'
    assert second.full_url == "http://nat:8080/maintenance/reset"
    assert second.data == b'{"chat_id": "telegram-1"}'


def test_dispatch_routes_text_and_rejects_another_chat():
    nat = MagicMock()
    nat.chat.return_value = "answer"
    telegram = MagicMock()
    bot = bot_module.TelegramBot(nat, telegram, "backend", "42")
    bot.dispatch({"message": {"chat": {"id": 7}, "text": "secret"}})
    bot.dispatch({"message": {"chat": {"id": 42}, "text": "hello"}})
    nat.chat.assert_called_once_with("hello")
    telegram.send_message.assert_called_once_with("42", "answer")


def test_reset_uses_configured_backend_chat_id():
    nat = MagicMock()
    nat.reset.return_value = {"value": 3}
    telegram = MagicMock()
    bot = bot_module.TelegramBot(nat, telegram, "configured-chat", "42")
    bot.dispatch({"message": {"chat": {"id": 42}, "text": "/reset"}})
    nat.reset.assert_called_once_with("configured-chat")
    assert "Reset complete" in telegram.send_message.call_args.args[1]


def test_text_attachment_becomes_prompt():
    nat = MagicMock()
    nat.chat.return_value = "summary"
    telegram = MagicMock()
    telegram.get_file.return_value = {"ok": True, "result": {"file_path": "docs/a.txt"}}
    telegram.download_file.return_value = b"some notes"
    bot = bot_module.TelegramBot(nat, telegram, "backend", "42")
    bot.dispatch({"message": {"chat": {"id": 42}, "caption": "Summarize", "document": {
        "file_id": "abc", "file_name": "a.txt", "mime_type": "text/plain", "file_size": 10,
    }}})
    assert "Attached text file a.txt" in nat.chat.call_args.args[0]
    assert "some notes" in nat.chat.call_args.args[0]
