#!/usr/bin/env python3
"""Telegram long-polling bridge for the NAT-servable chatbot example."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import html
import json
import logging
import mimetypes
import os
import pathlib
import re
import tempfile
import urllib.error
import urllib.request
import uuid
from typing import Any

LOG = logging.getLogger("nat_chatbot.telegram")
TELEGRAM_MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_ATTACHMENT_BYTES = 256 * 1024
DEFAULT_TTS_VOICE = "en-US-AriaNeural"


class NatChatbotError(RuntimeError):
    """A NAT endpoint returned an error or an unexpected response."""


class NatChatbotClient:
    """Small synchronous client for the endpoints in ``workflow.yaml``."""

    def __init__(self, base_url: str = "http://127.0.0.1:8080", timeout: float = 180):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise NatChatbotError(f"NAT returned HTTP {exc.code}: {body}") from exc
        except OSError as exc:
            raise NatChatbotError(f"could not reach NAT at {self.base_url}: {exc}") from exc
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise NatChatbotError(f"NAT returned invalid JSON: {body[:200]}") from exc
        if not isinstance(parsed, dict):
            raise NatChatbotError(f"NAT returned an unexpected response: {parsed!r}")
        return parsed

    def chat(self, message: str) -> str:
        response = self._post("/v1/workflow", {"message": message})
        result = response.get("result")
        if not isinstance(result, str):
            raise NatChatbotError(f"NAT response has no string 'result': {response!r}")
        return result

    def reset(self, chat_id: str) -> dict[str, Any]:
        return self._post("/maintenance/reset", {"chat_id": chat_id})

    def sweep(self, chat_id: str) -> dict[str, Any]:
        return self._post("/maintenance/sweep", {"chat_ids": [chat_id]})

    def vectorize(self) -> dict[str, Any]:
        return self._post("/maintenance/vectorize", {})


class TelegramClient:
    """Minimal Telegram Bot HTTP API wrapper."""

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=json.dumps(params or {}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            LOG.error("Telegram %s returned HTTP %s: %s", method, exc.code, body)
            return {"ok": False, "description": body, "error_code": exc.code}

    def request_multipart(self, method: str, fields: dict[str, Any], files: dict[str, tuple[str, bytes, str]]) -> dict[str, Any]:
        boundary = f"----NatTelegram{uuid.uuid4().hex}"
        body = bytearray()
        for name, value in fields.items():
            if value is None:
                continue
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        for name, (filename, data, content_type) in files.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
            body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
            body.extend(data)
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            f"{self.base_url}/{method}", data=bytes(body),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode())

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        response = self.request("getUpdates", payload)
        if response.get("error_code") == 409:
            raise RuntimeError("another process is polling this Telegram bot token")
        return response.get("result", []) if response.get("ok") else []

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        return self.request("sendMessage", {"chat_id": chat_id, "text": text})

    def send_chat_action(self, chat_id: str, action: str = "typing") -> dict[str, Any]:
        return self.request("sendChatAction", {"chat_id": chat_id, "action": action})

    def send_voice(self, chat_id: str, data: bytes) -> dict[str, Any]:
        return self.request_multipart("sendVoice", {"chat_id": chat_id}, {"voice": ("reply.mp3", data, "audio/mpeg")})

    def send_document(self, chat_id: str, filename: str, data: bytes, mime_type: str) -> dict[str, Any]:
        return self.request_multipart("sendDocument", {"chat_id": chat_id}, {"document": (filename, data, mime_type)})

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self.request("getFile", {"file_id": file_id})

    def download_file(self, file_path: str) -> bytes:
        with urllib.request.urlopen(f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}", timeout=35) as response:
            return response.read(TELEGRAM_MAX_FILE_BYTES + 1)

    def set_my_commands(self) -> dict[str, Any]:
        commands = [
            {"command": "help", "description": "Show commands"},
            {"command": "reset", "description": "Clear recent conversation memory"},
            {"command": "sweep", "description": "Move eligible turns to long-term memory"},
            {"command": "vectorize", "description": "Index pending long-term memories"},
            {"command": "voice", "description": "Toggle spoken replies"},
        ]
        return self.request("setMyCommands", {"commands": commands})


def _plain_tts_text(text: str) -> str:
    text = re.sub(r"```.*?```", " code omitted ", text, flags=re.DOTALL)
    text = re.sub(r"[*_`#>]", "", text)
    return html.unescape(text).strip()


def synthesize_speech(text: str, voice: str) -> bytes:
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError("voice replies require: pip install edge-tts") from exc

    async def render(path: str) -> None:
        await edge_tts.Communicate(_plain_tts_text(text), voice).save(path)

    with tempfile.NamedTemporaryFile(suffix=".mp3") as output:
        asyncio.run(render(output.name))
        output.seek(0)
        return output.read()


class TelegramBot:
    def __init__(self, nat: NatChatbotClient, telegram: TelegramClient, backend_chat_id: str,
                 allowed_chat_id: str, voice_enabled: bool = False, tts_voice: str = DEFAULT_TTS_VOICE):
        self.nat = nat
        self.telegram = telegram
        self.backend_chat_id = backend_chat_id
        self.allowed_chat_id = str(allowed_chat_id)
        self.voice_enabled = voice_enabled
        self.tts_voice = tts_voice

    def reply(self, chat_id: str, text: str) -> None:
        # Telegram rejects sendMessage payloads over 4096 characters. Keep
        # every chunk under the limit, preferring a newline boundary.
        remaining = text
        while remaining:
            cut = min(4000, len(remaining))
            if cut < len(remaining):
                newline = remaining.rfind("\n", 0, cut)
                if newline > 2000:
                    cut = newline + 1
            self.telegram.send_message(chat_id, remaining[:cut])
            remaining = remaining[cut:]
        if self.voice_enabled:
            try:
                self.telegram.send_voice(chat_id, synthesize_speech(text, self.tts_voice))
            except Exception as exc:  # noqa: BLE001 - optional TTS must never suppress the text reply
                LOG.warning("voice synthesis failed: %s", exc)

    def prompt(self, chat_id: str, text: str) -> None:
        self.telegram.send_chat_action(chat_id)
        try:
            self.reply(chat_id, self.nat.chat(text))
        except NatChatbotError as exc:
            LOG.exception("chat request failed")
            self.reply(chat_id, f"Chat backend error: {exc}")

    def command(self, chat_id: str, text: str) -> bool:
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            self.reply(chat_id, "Send me a message to chat. Commands: /reset, /sweep, /vectorize, /voice")
        elif command == "/voice":
            self.voice_enabled = not self.voice_enabled
            self.reply(chat_id, f"Voice replies {'enabled' if self.voice_enabled else 'disabled'}.")
        elif command in {"/reset", "/sweep", "/vectorize"}:
            self.telegram.send_chat_action(chat_id)
            try:
                if command == "/reset":
                    result = self.nat.reset(self.backend_chat_id)
                elif command == "/sweep":
                    result = self.nat.sweep(self.backend_chat_id)
                else:
                    result = self.nat.vectorize()
                self.reply(chat_id, f"{command[1:].capitalize()} complete: {json.dumps(result, sort_keys=True)}")
            except NatChatbotError as exc:
                self.reply(chat_id, f"Maintenance error: {exc}")
        else:
            return False
        return True

    def _text_document(self, document: dict[str, Any], caption: str) -> str | None:
        size = document.get("file_size", 0)
        mime_type = document.get("mime_type") or mimetypes.guess_type(document.get("file_name", ""))[0] or ""
        if size > MAX_TEXT_ATTACHMENT_BYTES or not (mime_type.startswith("text/") or mime_type in {"application/json", "application/xml"}):
            return None
        file_response = self.telegram.get_file(document["file_id"])
        path = file_response.get("result", {}).get("file_path")
        if not path:
            raise ValueError("Telegram did not return a file path")
        data = self.telegram.download_file(path)
        if len(data) > MAX_TEXT_ATTACHMENT_BYTES:
            return None
        content = data.decode("utf-8", errors="replace")
        label = pathlib.Path(document.get("file_name") or "attachment.txt").name
        prefix = f"{caption}\n\n" if caption else ""
        return f"{prefix}Attached text file {label}:\n\n{content}"

    def dispatch(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != self.allowed_chat_id:
            return
        text = (message.get("text") or "").strip()
        if text:
            if not (text.startswith("/") and self.command(chat_id, text)):
                self.prompt(chat_id, text)
            return
        if message.get("document"):
            try:
                prompt = self._text_document(message["document"], message.get("caption", ""))
            except Exception as exc:  # noqa: BLE001 - malformed Telegram files get a user-facing rejection
                LOG.warning("attachment download failed: %s", exc)
                prompt = None
            if prompt:
                self.prompt(chat_id, prompt)
            else:
                self.reply(chat_id, "I can read UTF-8 text, JSON, or XML files up to 256 KiB; this attachment is not supported.")
        elif message.get("photo"):
            self.reply(chat_id, "This text-only chatbot cannot inspect photos. Send its caption as a text message instead.")

    def run(self) -> None:
        self.telegram.set_my_commands()
        offset = None
        # Continue polling while a slow model call runs, but serialize work so
        # two messages from the same chat cannot reach memory out of order.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram-update") as executor:
            while True:
                for update in self.telegram.get_updates(offset):
                    offset = update["update_id"] + 1
                    executor.submit(self.dispatch, update)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nat-url", default=os.getenv("NAT_CHATBOT_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--backend-chat-id", default=os.getenv("H_NAT_CHATBOT_CHAT_ID", "chatbot-demo"))
    parser.add_argument("--bot-token", default=os.getenv("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--chat-id", default=os.getenv("TELEGRAM_CHAT_ID", ""), help="Only accept this Telegram chat")
    parser.add_argument("--voice", action="store_true", default=os.getenv("TELEGRAM_VOICE") == "1")
    parser.add_argument("--tts-voice", default=os.getenv("TTS_VOICE", DEFAULT_TTS_VOICE))
    parser.add_argument("--prompt", help="Test the NAT client directly without Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    nat = NatChatbotClient(args.nat_url)
    if args.prompt is not None:
        print(nat.chat(args.prompt))
        return
    if not args.bot_token or not args.chat_id:
        parser.error("--bot-token and --chat-id are required (or set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
    TelegramBot(nat, TelegramClient(args.bot_token), args.backend_chat_id, args.chat_id, args.voice, args.tts_voice).run()


if __name__ == "__main__":
    main()
