# vicoa-coinpay-overlay

Crypto billing for self-hosted [Vicoa](https://github.com/vicoa-ai/vicoa), through [CoinPay](https://coinpayportal.com).

A self-hosted Vicoa has no plans and no limits: billing is the closed part of
vicoa.ai. Vicoa's backend leaves a slot for it, though. If a Python package
called `cloud` is on the path, `backend/main.py` imports it and lets it mount
routers and register hooks. The web, desktop and mobile clients already call
`/api/v1/billing/*`. This package is a `cloud` that answers those calls with
CoinPay instead of Stripe.

What a user sees: the Upgrade button in Vicoa's dashboard, a page to pick a
network (USDC on Base, Bitcoin, ...), CoinPay's payment page, and Pro turned
on when the chain confirms. No card, no account with anybody but you.

## How it works

Crypto cannot be pulled again next month, so a "subscription" here is a
prepaid pass. Monthly buys 30 days of Pro, annual buys 365, and buying again
stacks on whatever is left. When the pass runs out the account goes back to
free. `cancel` only records that the user does not intend to renew.

| Vicoa calls | This overlay does |
|---|---|
| `GET /billing/subscription` | the row the settings page renders (`provider: "coinpay"`) |
| `GET /billing/usage` | agents started and messages sent this month |
| `POST /billing/checkout` | returns a signed checkout link on this backend |
| `GET/POST /billing/coinpay/checkout/{token}` | the network picker; creates the CoinPay payment and redirects to its pay page |
| `POST /billing/coinpay/webhook` | verifies CoinPay's HMAC and grants the pass |
| `POST /billing/cancel` | marks the pass as not renewing |
| `POST /billing/portal` | a fresh checkout link (there is no card to manage) |
| `GET /billing/mobile/status` | `{plan_type, provider, is_pro, ...}` for the mobile apps |
| `GET /billing/coinpay/payments/{id}` | polls CoinPay for one of your payments and grants on the spot |

Also registered:

- **A free-plan gate.** Free accounts may send `COINPAY_FREE_MESSAGE_LIMIT`
  messages per calendar month through the web or mobile API (default 20,
  Vicoa's own mobile free tier). Over that, `POST
  /api/v1/agent-instances/{id}/messages` answers 402 with an upgrade URL.
  Pro passes through. The gate fails open: if it cannot decide, the message
  goes through.
- **A sweeper** that lapses expired passes every minute and polls pending
  payments for up to two hours, so a payment confirms even when CoinPay's
  webhook cannot reach your host (a localhost trial, a firewall).
- **A user-delete hook**, because `subscriptions` references `users` without
  cascade and account deletion would otherwise fail for anyone who ever
  subscribed.
- **Schema bootstrap.** Vicoa's historical migrations created `subscriptions`
  and `billing_events` but not the `current_period_end` /
  `cancel_at_period_end` columns the web reads. The overlay adds those and its
  own `coinpay_payments` table at startup with idempotent DDL.

## Install

You need a CoinPay business with a merchant API key and a webhook secret. Set
the business's webhook URL to `https://<your backend>/api/v1/billing/coinpay/webhook`.

```bash
cd vicoa
git clone https://github.com/profullstack/vicoa-coinpay-overlay backend/overlays/coinpay
cat backend/overlays/coinpay/.env.coinpay.example >> .env   # then fill in the three secrets
docker compose -f docker-compose.selfhost.yml -f backend/overlays/coinpay/docker-compose.coinpay.yml up -d --build
docker compose -f docker-compose.selfhost.yml logs backend | grep coinpay
```

The log line to look for:

```
coinpay overlay 0.1.0 loaded (checkout enabled, free limit 20 msgs/month, chains USDC_BASE,...)
```

`curl https://<your backend>/api/v1/billing/coinpay/config` shows what is for
sale without any secrets.

Outside Docker, `pip install git+https://github.com/profullstack/vicoa-coinpay-overlay`
into the backend's environment is enough; the package installs as `cloud`.

### Prices

The overlay quotes `COINPAY_PRO_MONTHLY_USD` / `COINPAY_PRO_ANNUAL_USD`
(defaults $12 / $108). Vicoa's web UI hard-codes the same numbers in
`apps/web/components/billing/pricing-cards.tsx` and `apps/web/lib/desktop-paywall.ts`;
change both if you change the price.

### What the UI shows

The Upgrade page and the paywalls work unchanged. On the settings page a
CoinPay subscriber sees "Plan: Pro" and a note that this subscription type is
not configurable there, because the open web only knows Stripe and the app
stores. A small upstream change would render the pass end date and a Renew
button; until then `POST /billing/portal` already returns a renewal link.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r ../vicoa/backend/src/backend/requirements.txt pytest
make unit                                  # no database needed
make migrate-test-db test                  # needs Postgres; see Makefile variables
```

Integration tests run against a real Vicoa schema and a fake CoinPay
(`httpx.MockTransport`): checkout, webhook grant, idempotency, stacking,
lapse, the gate, the sweeper, the delete hook.

## Security notes

- Webhooks are verified with HMAC-SHA256 over the raw body and a 300 second
  timestamp window, the same computation CoinPay's SDK does. A valid webhook
  for a payment this server did not create is acknowledged and ignored.
- Checkout links are HMAC-signed intents that expire in 30 minutes and carry
  the user id, so the picker page needs no session cookie. `success_url` /
  `cancel_url` must be on `PUBLIC_WEB_URL` or `PUBLIC_BACKEND_URL`.
- Nothing in this package stores a private key. Payments go to the wallet
  configured on the CoinPay business.

## License

AGPL-3.0-or-later, the same as Vicoa. This package imports Vicoa's modules at
runtime and is distributed as part of a Vicoa deployment.
