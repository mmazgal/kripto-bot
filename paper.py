"""Sanal portföy: bot kendi sinyalleriyle sanal parayla işlem yapar, al-ve-tut ile karşılaştırılır.

Kural (backtest ile aynı): 1H skoru ≥ 0.2 ve pozisyon yoksa AL; ≤ -0.2 ve pozisyon varsa SAT.
Alım tutarı: portföy değeri / sepetteki varlık sayısı (nakit yetmezse kalan nakit). Komisyon %0.1.
Günde bir kez çalışır (bot içindeki zamanlayıcı).
"""
import data  # noqa: F401  (SSL ayarları)

import html
import json
import logging
from datetime import datetime, timedelta, timezone

import market
import store
from analysis import compute_item, fmt_num

log = logging.getLogger("paper")
TR = timezone(timedelta(hours=3))
FEE = 0.001
THRESH = 0.2
e = html.escape

MARKETS = {
    "crypto": {"name": "Kripto", "cur": "$", "start": 10_000.0, "items": ["BTC", "ETH", "SOL", "BNB", "XRP"]},
    "bist": {"name": "BIST", "cur": "₺", "start": 100_000.0,
             "items": ["BIST:THYAO", "BIST:GARAN", "BIST:ASELS", "BIST:BIMAS", "BIST:TUPRS"]},
}


def _label(item):
    return item.split(":", 1)[1] if item.startswith("BIST:") else item


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_state(mk: str) -> dict:
    rows = store.select("paper_state", {"market": mk})
    if rows:
        st = rows[0]
        st["bench"] = json.loads(st["bench"]) if isinstance(st.get("bench"), str) else (st.get("bench") or {})
        return st
    cfg = MARKETS[mk]
    bench = {}
    for it in cfg["items"]:
        try:
            bench[it] = market.current_price(it)
        except Exception as ex:  # noqa: BLE001
            log.warning("Başlangıç fiyatı alınamadı (%s): %s", it, ex)
    st = {"market": mk, "cash": cfg["start"], "start_cash": cfg["start"], "started_at": _now(), "bench": json.dumps(bench)}
    store.insert("paper_state", st, upsert=True)
    st["bench"] = bench
    return st


def _positions(mk):
    return {p["item"]: p for p in store.select("paper_positions", {"market": mk})}


def run_market(mk: str) -> list[str]:
    """Sinyalleri değerlendirir, işlemleri yapar; işlem bildirim mesajlarını döndürür."""
    if mk == "bist" and datetime.now(TR).weekday() >= 5:
        return []
    cfg, st = MARKETS[mk], get_state(mk)
    pos = _positions(mk)
    cash = float(st["cash"])
    ctxs = {}
    for it in cfg["items"]:
        try:
            ctxs[it] = compute_item(it)
        except Exception as ex:  # noqa: BLE001
            log.warning("Sanal portföy analiz hatası (%s): %s", it, ex)
    equity = cash + sum(float(p["qty"]) * ctxs[i]["price"] for i, p in pos.items() if i in ctxs)
    slot = equity / len(cfg["items"])
    msgs = []
    # Önce satışlar (nakit açılsın), sonra alımlar
    for it, ctx in ctxs.items():
        s, price = ctx["week_score"], ctx["price"]
        if it in pos and s <= -THRESH:
            p = pos[it]
            qty, entry = float(p["qty"]), float(p["entry_price"])
            proceeds = qty * price * (1 - FEE)
            pnl = proceeds - qty * entry
            cash += proceeds
            store.delete("paper_positions", {"market": mk, "item": it})
            store.insert("paper_trades", {"market": mk, "item": it, "side": "SAT", "qty": qty, "price": price,
                                          "at": _now(), "reason": f"skor {s:+.2f}", "pnl": pnl})
            msgs.append(f"🔴 SAT {_label(it)} @ {cfg['cur']}{fmt_num(price)} — skor {s:+.2f}, "
                        f"sonuç {'+' if pnl >= 0 else '-'}{cfg['cur']}{fmt_num(abs(pnl))} ({(price / entry - 1) * 100:+.1f}%)")
    for it, ctx in ctxs.items():
        s, price = ctx["week_score"], ctx["price"]
        if it not in pos and s >= THRESH and cash > 1:
            spend = min(slot, cash)
            qty = spend * (1 - FEE) / price
            cash -= spend
            store.insert("paper_positions", {"market": mk, "item": it, "qty": qty, "entry_price": price,
                                             "entry_at": _now()}, upsert=True)
            store.insert("paper_trades", {"market": mk, "item": it, "side": "AL", "qty": qty, "price": price,
                                          "at": _now(), "reason": f"skor {s:+.2f}", "pnl": None})
            msgs.append(f"🟢 AL {_label(it)} @ {cfg['cur']}{fmt_num(price)} — skor {s:+.2f}, tutar {cfg['cur']}{fmt_num(spend)}")
    store.update("paper_state", {"cash": cash}, {"market": mk})
    return msgs


def run_all() -> str | None:
    lines = []
    for mk, cfg in MARKETS.items():
        try:
            m = run_market(mk)
        except Exception as ex:  # noqa: BLE001
            log.warning("Sanal portföy hatası (%s): %s", mk, ex)
            continue
        if m:
            lines.append(f"<b>{cfg['name']}</b>")
            lines.extend(e(x) for x in m)
    if not lines:
        return None
    return "🤖 <b>Sanal portföy işlemleri</b>\n" + "\n".join(lines) + "\n\nDurum için: /sanal"


def report() -> str:
    L = ["<b>🤖 Sanal Portföy</b> — bot kendi sinyalleriyle sanal parayla işlem yapıyor", ""]
    for mk, cfg in MARKETS.items():
        st = get_state(mk)
        pos = _positions(mk)
        cur = cfg["cur"]
        prices = {}
        for it in set(cfg["items"]) | set(pos):
            try:
                prices[it] = market.current_price(it)
            except Exception:  # noqa: BLE001
                pass
        cash = float(st["cash"])
        equity = cash + sum(float(p["qty"]) * prices.get(i, float(p["entry_price"])) for i, p in pos.items())
        start = float(st["start_cash"])
        bench = st["bench"] or {}
        bh = [prices[i] / float(b) for i, b in bench.items() if i in prices and float(b) > 0]
        bh_ret = (sum(bh) / len(bh) - 1) * 100 if bh else None
        since = str(st["started_at"])[:10]
        L.append(f"<b>{cfg['name']}</b> (başlangıç {cur}{fmt_num(start)}, {since})")
        L.append(f"💼 Değer: <b>{cur}{fmt_num(equity)}</b> ({(equity / start - 1) * 100:+.2f}%)"
                 + (f" · al-ve-tut: {bh_ret:+.2f}%" if bh_ret is not None else ""))
        L.append(f"💵 Nakit: {cur}{fmt_num(cash)}")
        if pos:
            for it, p in pos.items():
                pr = prices.get(it)
                ch = f" ({(pr / float(p['entry_price']) - 1) * 100:+.1f}%)" if pr else ""
                L.append(f"   • {e(_label(it))}: alış {cur}{fmt_num(float(p['entry_price']))}{ch}")
        else:
            L.append("   • Açık pozisyon yok (sinyal bekleniyor)")
        trades = store.select("paper_trades", {"market": mk}, order="id.desc")[:3]
        if trades:
            L.append("   Son işlemler: " + ", ".join(
                f"{t['side']} {_label(t['item'])} ({str(t['at'])[5:10]})" for t in trades))
        L.append("")
    L.append("<i>Sepet: kripto BTC, ETH, SOL, BNB, XRP · BIST THYAO, GARAN, ASELS, BIMAS, TUPRS. "
             "Kural: 1H skoru ≥0.2 al, ≤-0.2 sat. Komisyon %0.1. Gerçek para kullanılmaz.</i>")
    return "\n".join(L)
