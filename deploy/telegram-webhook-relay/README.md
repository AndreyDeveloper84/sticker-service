# Telegram webhook relay (non-RU ingress)

Transport-only relay for the staging Telegram webhook. RUVDS filters
Telegram ASN in both directions; outbound is handled by the application
ProxyPool (DRF-2041), and this Cloudflare Worker accepts inbound webhook
POSTs outside RU and forwards them unchanged to staging.

```
Telegram → https://<worker>.workers.dev/tgwh/<RELAY_PATH_SECRET>/
        → https://stg.stickme.art/telegram/webhook/  (existing Django handler)
bot replies: Django → ProxyPool → Telegram API (unchanged)
```

## Contract

- POST only; exact path `/tgwh/<RELAY_PATH_SECRET>/` only.
- Requests declaring `Content-Length` above 1 MiB are rejected early
  (defensive guard; streamed bodies without Content-Length are forwarded
  as-is — the body is never buffered just to enforce a hard limit).
- Body streams upstream untouched: the worker never reads, parses or
  transforms the Telegram payload.
- `X-Telegram-Bot-Api-Secret-Token` forwarded untouched; Django validates it
  exactly as before (`apps/telegram_bot/views.py`). Backend auth model is
  unchanged — the path secret only hides/protects the relay endpoint.
- 2xx to Telegram only when upstream answered 2xx. Upstream timeout (10 s),
  network error → 502 → Telegram retries with its own backoff; updates are
  not lost or silently dropped.
- Logs carry only event, upstream HTTP status and latency — no update_id,
  message text, chat/user data, payload, headers or secrets.
- Not a general proxy: fixed upstream, fixed path, fixed method.
- Zero changes in sticker-service code.

## Deploy (owner, ~10 min, no credentials shared)

1. cloudflare.com → sign up / log in (free).
2. Workers & Pages → Create Worker → name e.g. `sticker-tg-relay` → Deploy.
3. Edit code → paste `worker.js` → Deploy.
4. Worker → Settings → Variables and Secrets → add **Secret**
   `RELAY_PATH_SECRET` = 128-bit random string, e.g. locally:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
5. Note the worker URL: `https://<name>.<subdomain>.workers.dev`.
   Webhook URL becomes `https://<name>.<subdomain>.workers.dev/tgwh/<RELAY_PATH_SECRET>/`.
6. Hand the worker URL + path secret to the operator via the secret file
   channel (not chat), or set the webhook yourself:

   ```bash
   # on the staging VPS (proxy required for api.telegram.org)
   curl -x <proxy> -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
     --data-urlencode "url=https://<name>.<subdomain>.workers.dev/tgwh/<RELAY_PATH_SECRET>/" \
     --data-urlencode "secret_token=$TELEGRAM_WEBHOOK_SECRET"
   ```

## Acceptance

1. `getWebhookInfo`: url = relay URL, `pending_update_count` drains,
   `last_error_message` cleared.
2. Pending `/start` (Telegram retries ~24 h) delivers:
   host nginx access log shows `POST /telegram/webhook/` from a CF IP;
   Django returns 200; `ChannelIdentity` (telegram) created; the user
   receives a real reply from @StickersForYouF_bot.
3. Pilot flow proceeds to summary on current dev (DRF-2050 code).

## Rollback

Single operation, no domain state involved:

```bash
curl -x <proxy> -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  --data-urlencode "url=https://stg.stickme.art/telegram/webhook/" \
  --data-urlencode "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

(Or `deleteWebhook` to fully disable ingress.) The worker can be deleted at
any time; pending updates follow whichever webhook URL is current.
