import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TelegramBotClient:
    def __init__(self, token: str):
        if not token:
            raise ValueError("Telegram bot token is required")
        self.token = token
        self.api_base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    def _post(self, method: str, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.api_base}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error in {method}: {data}")
        return data.get("result")

    def send_message(self, *, chat_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._post("sendMessage", payload)

    def answer_callback_query(self, *, callback_query_id, text=None):
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self._post("answerCallbackQuery", payload)

    def get_file(self, file_id: str):
        return self._post("getFile", {"file_id": file_id})

    def download_file(self, file_path: str) -> bytes:
        with urlopen(f"{self.file_base}/{file_path}", timeout=30) as response:
            return response.read()
