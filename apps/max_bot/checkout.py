from apps.max_bot.payments import HttpExternalPaymentProvider, MaxExternalPaymentAdapter

def start_checkout(*, identity, client):
    adapter = MaxExternalPaymentAdapter(provider=HttpExternalPaymentProvider.from_env())
    _payment, session = adapter.create_checkout(identity=identity)
    return client.send_message(
        user_id=identity.external_user_id,
        text="Фотографии приняты. Перейдите к оплате заказа.",
        buttons=[[{"text": "Оплатить заказ", "url": session.checkout_url}]],
    )
