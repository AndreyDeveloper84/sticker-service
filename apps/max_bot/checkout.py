from apps.max_bot.payments import HttpExternalPaymentProvider, MaxExternalPaymentAdapter

def start_checkout(*, identity, client, chat_id=None):
    adapter = MaxExternalPaymentAdapter(provider=HttpExternalPaymentProvider.from_env())
    _payment, session = adapter.create_checkout(identity=identity)
    kwargs = {
        "text": "Фотографии приняты. Перейдите к оплате заказа.",
        "buttons": [[{"text": "Оплатить заказ", "url": session.checkout_url}]],
    }
    # reply inside the dialog when the checkout is triggered by an inbound
    # callback; fall back to the person address for proactive sends
    if chat_id:
        return client.send_message(chat_id=chat_id, **kwargs)
    return client.send_message(user_id=identity.external_user_id, **kwargs)
