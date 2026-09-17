// Telegram webhook relay (DRF-1870 / STK relay issue).
//
// Why: the staging VPS (RUVDS) filters Telegram ASN both ways. Outbound is
// solved by the application ProxyPool; inbound webhooks from Telegram time
// out before nginx. This worker terminates Telegram's webhook POST outside
// RU and forwards it unchanged to the staging backend.
//
// Hard rules (see deploy/telegram-webhook-relay/README.md):
// - zero business logic, transport only;
// - POST only, exact secret path only;
// - request body streams upstream untouched — the worker never reads,
//   parses or transforms the Telegram payload;
// - the existing webhook secret header (X-Telegram-Bot-Api-Secret-Token)
//   is forwarded untouched and validated by Django as before;
// - 2xx to Telegram ONLY when upstream answered 2xx (no fire-and-forget);
// - upstream timeout/network error -> 502 so Telegram keeps retrying;
// - logs carry only event, upstream HTTP status and latency — no update_id,
//   message, chat/user data, payload, headers or secrets.

const UPSTREAM_URL = "https://stg.stickme.art/telegram/webhook/";
const MAX_BODY_BYTES = 1024 * 1024;
const UPSTREAM_TIMEOUT_MS = 10000;

export default {
  async fetch(request, env) {
    const started = Date.now();
    const url = new URL(request.url);

    // Relay-level auth: unguessable path prefix; keeps the worker from being
    // a generic open proxy and hides the real endpoint. Backend auth stays
    // the Telegram secret_token header, validated by Django.
    const expectedPath = `/tgwh/${env.RELAY_PATH_SECRET || ""}/`;
    if (
      request.method !== "POST" ||
      !env.RELAY_PATH_SECRET ||
      url.pathname !== expectedPath
    ) {
      return new Response("not found", { status: 404 });
    }

    // Defensive guard: rejects requests that declare an oversized
    // Content-Length. Streamed bodies without Content-Length are forwarded
    // as-is — Telegram webhooks are small JSON, and we deliberately do not
    // buffer the body just to enforce a hard limit.
    const length = Number(request.headers.get("content-length") || "0");
    if (length > MAX_BODY_BYTES) {
      return new Response("payload too large", { status: 413 });
    }

    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.delete("cf-connecting-ip");
    headers.delete("cf-ray");

    let upstream;
    try {
      upstream = await fetch(UPSTREAM_URL, {
        method: "POST",
        headers,
        body: request.body,
        signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      });
    } catch {
      // Timeout / network error: update not delivered -> non-2xx so Telegram
      // retries with its own backoff.
      console.log(
        JSON.stringify({ event: "upstream_error", status: 502, ms: Date.now() - started })
      );
      return new Response("upstream unavailable", { status: 502 });
    }

    console.log(
      JSON.stringify({ event: "relay", status: upstream.status, ms: Date.now() - started })
    );
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") || "application/json" },
    });
  },
};
