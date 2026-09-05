"""The one HTML page the overlay serves: pick a chain, go pay."""

from __future__ import annotations

from html import escape

from .config import CoinPayConfig
from .plans import Quote

_STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#111;--muted:#666;--line:#e5e5e5;--accent:#2563eb;--accent-fg:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#0b0b0c;--fg:#f2f2f2;--muted:#9a9a9a;--line:#26262a;--accent:#3b82f6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:520px;margin:8vh auto;padding:0 20px}h1{font-size:22px;margin:0 0 4px}.muted{color:var(--muted)}
.card{border:1px solid var(--line);border-radius:12px;padding:20px;margin-top:20px}
.price{font-size:32px;font-weight:600;margin:8px 0 0}.price small{font-size:14px;font-weight:400;color:var(--muted)}
button{display:block;width:100%;margin-top:10px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:transparent;color:var(--fg);font:inherit;text-align:left;cursor:pointer}
button:hover{border-color:var(--accent)}button.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent);text-align:center;font-weight:600}
a{color:var(--accent)}.foot{margin-top:24px;font-size:13px}
"""


def picker_page(cfg: CoinPayConfig, quote: Quote, *, post_url: str, cancel_url: str, error: str | None = None) -> str:
    chains = cfg.blockchains
    price = f"${quote.amount_usd:,.2f}"
    single = len(chains) == 1
    buttons = []
    for code in chains:
        label = escape(cfg.chain_label(code))
        cls = ' class="primary"' if single else ""
        text = f"Pay {price} with {label}" if single else label
        buttons.append(f'<button type="submit" name="blockchain" value="{escape(code)}"{cls}>{text}</button>')
    err = f'<p style="color:#dc2626">{escape(error)}</p>' if error else ""
    cancel = f'<a href="{escape(cancel_url)}">Cancel and go back</a>' if cancel_url else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vicoa Pro checkout</title><style>{_STYLE}</style></head><body><main>
<h1>Vicoa Pro</h1><p class="muted">{escape(quote.label)}. Paid once, in crypto, through CoinPay. No card, no auto-renew.</p>
<div class="card"><div class="muted">Total</div><p class="price">{price} <small>USD, {quote.days} days of Pro</small></p>
{err}<form method="post" action="{escape(post_url)}"><p class="muted" style="margin:16px 0 4px">{"" if single else "Pay with"}</p>{''.join(buttons)}</form></div>
<p class="foot muted">You will be taken to CoinPay to send the payment. Pro activates as soon as the network confirms it, usually within a few minutes. {cancel}</p>
</main></body></html>"""


def error_page(title: str, detail: str, back_url: str | None = None) -> str:
    back = f'<p><a href="{escape(back_url)}">Go back</a></p>' if back_url else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{_STYLE}</style></head><body><main><h1>{escape(title)}</h1><p class="muted">{escape(detail)}</p>{back}</main></body></html>"""
