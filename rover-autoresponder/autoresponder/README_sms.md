# SMS bridge — S1 setup (phone-as-gateway)

Goal of S1: prove the phone↔box bridge **both directions** — the phone forwards
inbound SMS to the Linux box, and the box can send an SMS through the phone. Ingest +
log only; no drafting yet. The email pipeline keeps running untouched.

App: **SMS Gateway for Android** (open-source, `sms-gate.app` / github.com/capcom6/android-sms-gateway),
**Local mode** — no third-party cloud relay; the phone runs an on-device HTTP server and
POSTs webhooks straight to your box.

---

## On the phone (dedicated Android)

1. Install the app (APK from the project's releases). Grant **SEND_SMS** (required to
   send) and **RECEIVE_SMS** (required for inbound webhooks). READ_PHONE_STATE is optional
   (SIM selection).
2. Put the phone's Rover SIM in, and set your **Rover account phone number to this SIM**
   so Rover's per-conversation texts arrive here.
3. Enable **Local Server** mode. Note the phone's LAN IP + port (e.g. `192.168.1.10:8080`)
   and the **username/password** shown on the Home tab → these go in `.env` as
   `SMS_GATEWAY_BASE_URL`, `SMS_GATEWAY_USERNAME`, `SMS_GATEWAY_PASSWORD`.
4. Keep the phone healthy as a gateway: plug it in, disable battery optimization for the
   app, enable start-on-boot.

> **IMPORTANT — Local mode uses BARE paths.** Send is `POST /message`, webhooks are
> `/webhooks`. The `/3rdparty/v1/...` prefix is **CLOUD mode only** and returns 404
> against a local server.
>
> Interactive API docs for your device: open `http://<phone-ip>:8080/docs` in a browser
> (same credentials) for the Swagger UI.

### Registering the inbound webhook
Point the device at the box's receiver. Each webhook is registered for a **single event**,
so register one call per event you want.

In **Local mode** the app requires HTTPS for any target that isn't `127.0.0.1`. Pick one:

- **ADB reverse (easiest first test — no TLS at all):** tether USB, run
  `adb reverse tcp:8899 tcp:8899`, and register the webhook URL as
  `http://127.0.0.1:8899/sms/webhook`.
- **Proper LAN TLS:** issue a cert for the box's LAN IP via the app's
  Certificate Authority, set `SMS_WEBHOOK_CERT`/`SMS_WEBHOOK_KEY` in `.env`, and register
  `https://<box-ip>:8899/sms/webhook`.

Register (creds = the Home tab username/password; `<phone-ip>` = the phone's LAN IP):
```bash
curl -X POST -u "$SMS_GATEWAY_USERNAME:$SMS_GATEWAY_PASSWORD" \
  -H "Content-Type: application/json" \
  -d '{"id":"rover-inbound","url":"http://127.0.0.1:8899/sms/webhook","event":"sms:received"}' \
  http://<phone-ip>:8080/webhooks
```
Repeat with `"event":"sms:sent"` / `"sms:delivered"` / `"sms:failed"` for delivery tracking.

Verify what's registered:
```bash
curl -u "$SMS_GATEWAY_USERNAME:$SMS_GATEWAY_PASSWORD" \
  http://<phone-ip>:8080/webhooks
```
The phone also shows a notification whenever a new `sms:received` webhook is registered.

### Signing key (add after the bridge works)
The key is **randomly generated on the first webhook request** and lives in the app at
**Settings → Webhooks → Signing Key** (it only appears once a webhook is registered —
that's why it's not visible beforehand). Copy it into `.env` as
`SMS_WEBHOOK_SIGNING_KEY`, then restart the receiver.

Our receiver treats an empty `SMS_WEBHOOK_SIGNING_KEY` as "verification disabled", so you
can prove the bridge first and add the key as a hardening step.

---

## On the Linux box

1. Fill the SMS block in `.env` (see `.env.example`).
2. **Run the inbound receiver** and watch it log incoming texts:
   ```bash
   python -m autoresponder.sms_main --serve
   ```
   Text the phone from another phone → you should see
   `INBOUND SMS | from=+1… | 'your message'`.
3. **Test outbound** (box → phone → recipient):
   ```bash
   python -m autoresponder.sms_main --send "+1YOUROTHERNUMBER" "test from the bot"
   ```
   It prints the gateway message id, and the text should arrive on the target phone. If
   you registered the delivery webhooks, the receiver also logs
   `SEND STATUS | sms:sent … | sms:delivered …`.

If both directions work, S1 is proven. S2 adds the SMS parser + the marker state machine
(inquiry vs confirmed) keyed on the sender number.

---

## Notes / gotchas
- **Ack speed:** the receiver replies 2xx immediately; the app retries anything else
  (exponential backoff, ~14 times / 2 days), so we dedupe on the webhook `id`.
- **Signature:** webhooks are HMAC-SHA256 signed over `raw_body + X-Timestamp` **only if
  you set a signing key** (it's `null`/off by default). When a key is set, the receiver
  rejects bad or stale (±5 min) signatures; with no key, verification is skipped.
- **Local vs cloud paths:** local mode = bare `/message`, `/webhooks`, `/health`, `/docs`.
  The `/3rdparty/v1` prefix is cloud-mode only (404s locally). `SMS_GATEWAY_SEND_PATH`
  in `.env` switches this if you ever move to cloud mode.
- **AP isolation:** if the box can't reach the phone over Wi-Fi, check your router for
  "AP isolation"/"client isolation" and disable it.
- **Phone number format:** we send with `skipPhoneValidation=true` because Rover's relay
  numbers may not be clean E.164.
- **Swappable:** send goes through the `SmsGateway` interface, so a different mechanism
  (Tasker, ADB) can replace the app later without touching the pipeline.