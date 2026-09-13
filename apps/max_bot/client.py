import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class MaxBotClient:
    base_url = "https://platform-api2.max.ru"

    def __init__(self, token: str):
        self.token = token

    def _request(self, method: str, path: str, *, query=None, body=None):
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=10) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}

    def send_message(self, *, user_id: str, text: str, buttons=None):
        attachments = []
        if buttons:
            attachments.append(
                {
                    "type": "inline_keyboard",
                    "payload": {
                        "buttons": [
                            [
                                {"type": "callback", "text": button["text"], "payload": button["payload"]}
                                for button in row
                            ]
                            for row in buttons
                        ]
                    },
                }
            )
        body = {"text": text}
        if attachments:
            body["attachments"] = attachments
        return self._request("POST", "/messages", query={"user_id": user_id}, body=body)

    def answer_callback(self, *, callback_id: str):
        return self._request("POST", "/answers", query={"callback_id": callback_id}, body={})

    def download(self, url: str) -> bytes:
        with urlopen(url, timeout=15) as response:
            return response.read()
