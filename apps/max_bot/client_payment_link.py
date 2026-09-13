def render_button(button: dict) -> dict:
    if button.get("url"):
        return {
            "type": "link",
            "text": button["text"],
            "url": button["url"],
        }
    return {
        "type": "callback",
        "text": button["text"],
        "payload": button["payload"],
    }
