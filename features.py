"""Takip listesi, fiyat hedefleri, portföy, tahmin karnesi, sabah özeti ve karşılaştırma.

Bu fonksiyonlar senkron çalışır (ağ çağrısı yapar); bot bunları asyncio.to_thread ile çağırır.
Varlık gösterimi: kripto 'BTC', Borsa İstanbul 'BIST:THYAO'.
"""
import data  # noqa: F401  (SSL ayarları)

import html
import logging
import re
from datetime import datetime, timedelta, timezone

import numpy as np

import market
import store
from analysis import CURRENCY, compute_item, fmt_num, score_label

log = logging.getLogger("features")
TR = timezone(timedelta(hours=3))
DEFAULT_WATCHLIST = ["BTC", "SOL"]
HZ = {1: "24s", 3: "3g", 7: "1H", 30: "1A"}
e = html.escape


# ---------------- Yardımcılar ----------------
def is_bist(item: str) -> bool:
    return item.upper().startswith("BIST:")


def code_of(item: str) -> str:
    return item.split(":", 1)[1] if is_bist(item) else item


def label(item: str) -> str:
    return f"{code_of(item)} (BIST)" if is_bist(item) else item


def cur_of(item: str) -> str:
    if is_bist(item):
        return "" if code_of(item).startswith("XU") else "₺"
    return "$"


def money(item: str, v: float) -> str:
    return cur_of(item) + fmt_num(v)


def parse_number(s: str) -> float:
    s = s.strip().replace("₺", "").replace("$", "")
    if "," in s and "." in s:          # 1.234,56 -> 1234.56
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return float(s)


def resolve_item(word: str, stock: bool) -> str | None:
    """Kullanıcının yazdığını ('eth', 'thyao') standart varlık koduna çevirir."""
    if stock:
        from stockinfo import find_stock
        t = find_stock(word)
        return f"BIST:{t.removesuffix('.IS')}" if t else None
    from coininfo import find_coin
    code = word.upper()
    try:
        c = find_coin(word)
        if not c:
            return None  # bilinmeyen coin/yazım hatası listeye kaydedilmesin
        code = c["symbol"].upper()
    except Exception as ex:  # noqa: BLE001  (arama servisi çöktüyse yazılanı kabul et)
        log.warning("Coin arama hatası: %s", ex)
    return code if re.fullmatch(r"[A-Z0-9]{1,15}", code) else None


# ---------------- Ayarlar / alıcılar ----------------
def ensure_chat(chat_id: int):
    if not store.select("settings", {"chat_id": chat_id}):
        store.insert("settings", {"chat_id": chat_id, "alerts_on": True, "summary_on": True}, upsert=True)


def set_setting(chat_id: int, **values):
    ensure_chat(chat_id)
    store.update("settings", values, {"chat_id": chat_id})


def get_settings(chat_id: int) -> dict:
    rows = store.select("settings", {"chat_id": chat_id})
    return rows[0] if rows else {"chat_id": chat_id, "alerts_on": False, "summary_on": False}


def recipients(kind: str = "alerts_on") -> list[int]:
    return [r["chat_id"] for r in store.select("settings", {kind: True})]


# ---------------- Takip listesi ----------------
def get_watchlist(chat_id: int) -> list[str]:
    rows = store.select("watchlist", {"chat_id": chat_id})
    return [r["item"] for r in rows] or list(DEFAULT_WATCHLIST)


def add_watch(chat_id: int, item: str):
    if not store.select("watchlist", {"chat_id": chat_id}):  # ilk eklemede varsayılanları da yaz
        for d in DEFAULT_WATCHLIST:
            store.insert("watchlist", {"chat_id": chat_id, "item": d}, upsert=True)
    store.insert("watchlist", {"chat_id": chat_id, "item": item}, upsert=True)


def remove_watch(chat_id: int, item: str) -> bool:
    if not store.select("watchlist", {"chat_id": chat_id}):
        for d in DEFAULT_WATCHLIST:
            store.insert("watchlist", {"chat_id": chat_id, "item": d}, upsert=True)
    return bool(store.delete("watchlist", {"chat_id": chat_id, "item": item}))


def alert_items(chat_id: int) -> list[str]:
    """Alarm kontrolü yapılacak varlıklar: takip listesi + portföy."""
    items = get_watchlist(chat_id)
    for p in store.select("portfolio", {"chat_id": chat_id}):
        if p["item"] not in items:
            items.append(p["item"])
    return items


# ---------------- Fiyat hedefleri ----------------
def add_target(chat_id: int, item: str, target: float) -> dict:
    price = market.current_price(item)
    direction = "above" if target > price else "below"
    row = store.insert("price_targets", {"chat_id": chat_id, "item": item, "target": target,
                                         "direction": direction, "triggered": False})
    row["price"] = price
    return row


def list_targets(chat_id: int) -> list[dict]:
    return store.select("price_targets", {"chat_id": chat_id, "triggered": False}, order="id.asc")


def remove_target(chat_id: int, target_id: int) -> bool:
    return bool(store.delete("price_targets", {"chat_id": chat_id, "id": target_id}))


def check_targets() -> list[tuple[int, str]]:
    """Gerçekleşen hedefleri bulur, işaretler; (chat_id, mesaj) listesi döner."""
    out = []
    rows = store.select("price_targets", {"triggered": False})
    prices = {}
    for r in rows:
        item = r["item"]
        if is_bist(item) and not bist_open():
            continue
        try:
            if item not in prices:
                prices[item] = market.current_price(item)
            p = prices[item]
        except Exception as ex:  # noqa: BLE001
            log.warning("Hedef fiyatı alınamadı (%s): %s", item, ex)
            continue
        t = float(r["target"])
        hit = (r["direction"] == "above" and p >= t) or (r["direction"] == "below" and p <= t)
        if hit:
            store.update("price_targets", {"triggered": True}, {"id": r["id"]})
            arrow = "⬆️ yukarı kırdı" if r["direction"] == "above" else "⬇️ aşağı kırdı"
            cmd = f"/hisse {code_of(item).lower()}" if is_bist(item) else f"/analiz {item.lower()}"
            out.append((r["chat_id"], f"🎯 <b>HEDEF GERÇEKLEŞTİ</b> — {label(item)}\n"
                                      f"Fiyat {money(item, p)}, hedefin {money(item, t)} seviyesini {arrow}.\n👉 {cmd}"))
    return out


def bist_open() -> bool:
    now = datetime.now(TR)
    return now.weekday() < 5 and (10, 0) <= (now.hour, now.minute) <= (18, 10)


# ---------------- Portföy ----------------
def add_position(chat_id: int, item: str, amount: float, buy_price: float) -> dict:
    return store.insert("portfolio", {"chat_id": chat_id, "item": item, "amount": amount, "buy_price": buy_price})


def remove_position(chat_id: int, pos_id: int) -> bool:
    return bool(store.delete("portfolio", {"chat_id": chat_id, "id": pos_id}))


def portfolio_report(chat_id: int) -> str:
    rows = store.select("portfolio", {"chat_id": chat_id}, order="id.asc")
    if not rows:
        return ("💼 Portföyün boş.\n\nEklemek için:\n/portfoy ekle btc 0.05 62000\n"
                "/portfoy ekle thyao hisse 100 300\n(miktar ve alış fiyatı)")
    L = ["<b>💼 Portföyüm</b>", ""]
    totals = {"$": [0.0, 0.0], "₺": [0.0, 0.0]}  # [maliyet, değer]
    for r in rows:
        item, amt, buy = r["item"], float(r["amount"]), float(r["buy_price"])
        try:
            p = market.current_price(item)
        except Exception:  # noqa: BLE001
            p = None
        cost = amt * buy
        line = f"<b>#{r['id']} {e(label(item))}</b>: {amt:g} adet · alış {money(item, buy)}"
        if p is not None:
            val = amt * p
            pl = val - cost
            pct = (p / buy - 1) * 100 if buy else 0
            icon = "🟢" if pl >= 0 else "🔴"
            line += (f"\n   Şimdi {money(item, p)} · değer {money(item, val)} · "
                     f"{icon} {'+' if pl >= 0 else '-'}{money(item, abs(pl))} ({pct:+.1f}%)")
            c = "₺" if is_bist(item) else "$"
            totals[c][0] += cost
            totals[c][1] += val
        else:
            line += "\n   (güncel fiyat alınamadı)"
        L.append(line)
    L.append("")
    L.append("<b>— Toplam —</b>")
    usdtry = market.usd_try()
    grand_cost = grand_val = 0.0
    for c, (cost, val) in totals.items():
        if cost:
            pl = val - cost
            L.append(f"{'Kripto' if c == '$' else 'BIST'}: {c}{fmt_num(val)} · "
                     f"{'🟢 +' if pl >= 0 else '🔴 -'}{c}{fmt_num(abs(pl))} ({(val / cost - 1) * 100:+.1f}%)")
            k = usdtry if (c == "$" and usdtry) else 1.0
            if c == "$" and not usdtry:
                continue
            grand_cost += cost * k
            grand_val += val * k
    if totals["$"][0] and totals["₺"][0] and usdtry and grand_cost:
        pl = grand_val - grand_cost
        L.append(f"<b>Genel toplam: ₺{fmt_num(grand_val)}</b> · "
                 f"{'🟢 +' if pl >= 0 else '🔴 -'}₺{fmt_num(abs(pl))} ({(grand_val / grand_cost - 1) * 100:+.1f}%)"
                 f"\n<i>(USD/TRY {usdtry:.2f} ile)</i>")
    L.append("\nSilmek için: /portfoy sil &lt;#numara&gt;")
    return "\n".join(L)


# ---------------- Tahmin kaydı ve karne ----------------
def record_prediction(ctx: dict):
    """Analizdeki 1 hafta / 1 ay tahminlerini kaydeder (varlık+ufuk başına günde bir kez)."""
    now = datetime.now(timezone.utc)
    today = datetime.now(TR).date().isoformat()
    h1, h2 = ctx.get("horizons", (7, 30))
    for days, score, fc in ((h1, ctx["week_score"], ctx["fw"]), (h2, ctx["month_score"], ctx["fm"])):
        row = {"item": ctx["item"], "horizon_days": days, "created_date": today,
               "created_at": now.isoformat(), "due_at": (now + timedelta(days=days)).isoformat(),
               "price": ctx["price"], "score": round(score, 4), "target": fc["center"],
               "low": fc["low"], "high": fc["high"], "resolved": False}
        try:
            if not store.select("predictions", {"item": ctx["item"], "horizon_days": days, "created_date": today}):
                store.insert("predictions", row)
        except Exception as ex:  # noqa: BLE001
            log.warning("Tahmin kaydedilemedi: %s", ex)


def expected_dir(score: float) -> int:
    return 1 if score >= 0.2 else -1 if score <= -0.2 else 0


def resolve_predictions() -> int:
    """Süresi dolan tahminleri gerçek fiyatla karşılaştırır."""
    now = datetime.now(timezone.utc).isoformat()
    rows = store.select("predictions", {"resolved": False, "due_at": ("lte", now)})
    prices, n = {}, 0
    for r in rows:
        item = r["item"]
        try:
            if item not in prices:
                prices[item] = market.current_price(item)
        except Exception as ex:  # noqa: BLE001
            log.warning("Tahmin çözümleme fiyatı alınamadı (%s): %s", item, ex)
            continue
        final = prices[item]
        start, low, high = float(r["price"]), float(r["low"]), float(r["high"])
        exp = expected_dir(float(r["score"]))
        in_range = low <= final <= high
        correct = in_range if exp == 0 else (np.sign(final - start) == exp)
        store.update("predictions", {"resolved": True, "final_price": final, "correct": bool(correct),
                                     "in_range": bool(in_range)}, {"id": r["id"]})
        n += 1
    return n


def scorecard() -> str:
    rows = store.select("predictions", {}, order="created_at.desc")
    done = [r for r in rows if r.get("resolved")]
    pending = len(rows) - len(done)
    L = ["<b>📒 Tahmin Karnesi</b>", ""]
    if not done:
        L.append("Henüz süresi dolan tahmin yok.")
        L.append(f"Bekleyen tahmin: {pending} (ilk 1 haftalık sonuçlar kayıttan 7 gün sonra gelir)")
        L.append("\nHer /analiz ve sabah özeti tahminleri otomatik kaydeder.")
        return "\n".join(L)

    def stats(rs):
        c = sum(1 for r in rs if r["correct"])
        ir = sum(1 for r in rs if r["in_range"])
        return c, ir, len(rs)

    c, ir, n = stats(done)
    L.append(f"Toplam sonuçlanan: <b>{n}</b> · bekleyen: {pending}")
    L.append(f"🎯 Yön doğruluğu: <b>%{c / n * 100:.0f}</b> ({c}/{n})")
    L.append(f"📏 Fiyat aralığı içinde kalma: <b>%{ir / n * 100:.0f}</b> ({ir}/{n})")
    for days, name in ((1, "24 Saat (kısa vade)"), (3, "3 Gün (kısa vade)"), (7, "1 Hafta"), (30, "1 Ay")):
        rs = [r for r in done if r["horizon_days"] == days]
        if rs:
            c2, ir2, n2 = stats(rs)
            L.append(f"   {name}: yön %{c2 / n2 * 100:.0f} · aralık %{ir2 / n2 * 100:.0f} ({n2} tahmin)")
    by_item = {}
    for r in done:
        by_item.setdefault(r["item"], []).append(r)
    if len(by_item) > 1:
        L.append("")
        L.append("<b>Varlık bazında yön doğruluğu</b>")
        for item, rs in sorted(by_item.items(), key=lambda kv: -len(kv[1]))[:8]:
            c3, _, n3 = stats(rs)
            L.append(f"• {e(label(item))}: %{c3 / n3 * 100:.0f} ({n3})")
    L.append("")
    L.append("<b>Son sonuçlar</b>")
    arrows = {1: "↑", -1: "↓", 0: "→"}
    for r in done[:6]:
        exp = expected_dir(float(r["score"]))
        chg = (float(r["final_price"]) / float(r["price"]) - 1) * 100
        L.append(f"{'✅' if r['correct'] else '❌'} {e(label(r['item']))} {HZ.get(r['horizon_days'], str(r['horizon_days']) + 'g')}: "
                 f"beklenen {arrows[exp]}, gerçekleşen {chg:+.1f}% ({r['created_date']})")
    L.append("\n<i>Yön: yükseliş/düşüş beklentisi tuttu mu (nötr tahminde fiyat aralıkta kaldı mı).</i>")
    return "\n".join(L)


# ---------------- Karşılaştırma ----------------
def _arrow(s):
    return "↑↑" if s >= 0.5 else "↑" if s >= 0.2 else "↓↓" if s <= -0.5 else "↓" if s <= -0.2 else "→"


def compare(items: list[str]) -> str:
    rows = []
    for item in items:
        try:
            ctx = compute_item(item)
            record_prediction(ctx)
            daily = ctx["daily"]["close"]
            ch7 = (ctx["price"] / float(daily.iloc[-8]) - 1) * 100 if len(daily) > 8 else 0.0
            rsi = float(ctx["d"]["signals"]["RSI"][1])
            rows.append((item, ctx, ch7, rsi))
        except Exception as ex:  # noqa: BLE001
            log.warning("Karşılaştırma hatası (%s): %s", item, ex)
            rows.append((item, None, 0, 0))
    L = ["<b>⚖️ Karşılaştırma</b>", "<pre>"]
    L.append(f"{'Varlık':<8}{'Gün%':>7}{'7g%':>7}{'RSI':>5}{'1H':>4}{'1A':>4}")
    for item, ctx, ch7, rsi in rows:
        name = code_of(item)[:7]
        if ctx is None:
            L.append(f"{name:<8}  veri yok")
            continue
        L.append(f"{name:<8}{ctx['chg']:>+7.1f}{ch7:>+7.1f}{rsi:>5.0f}"
                 f"{_arrow(ctx['week_score']):>4}{_arrow(ctx['month_score']):>4}")
    L.append("</pre>")
    ok = [(i, c) for i, c, _, _ in rows if c]
    if len(ok) >= 2:
        best = max(ok, key=lambda x: x[1]["month_score"])
        worst = min(ok, key=lambda x: x[1]["month_score"])
        L.append(f"💪 En güçlü görünüm: <b>{e(label(best[0]))}</b> ({score_label(best[1]['month_score'])})")
        L.append(f"🥶 En zayıf görünüm: <b>{e(label(worst[0]))}</b> ({score_label(worst[1]['month_score'])})")
        hot = [label(i) for i, c, _, r in rows if c and r >= 70]
        cold = [label(i) for i, c, _, r in rows if c and r <= 30]
        if hot:
            L.append(f"🔥 Aşırı alımda: {e(', '.join(hot))}")
        if cold:
            L.append(f"🧊 Aşırı satımda: {e(', '.join(cold))}")
    L.append("\n<i>↑↑ güçlü yükseliş · ↑ yükseliş · → nötr · ↓ düşüş · ↓↓ güçlü düşüş (1H=1 hafta, 1A=1 ay)</i>")
    return "\n".join(L)


# ---------------- Sabah özeti ----------------
def morning_summary(chat_id: int) -> str:
    now = datetime.now(TR)
    L = [f"<b>☀️ Günaydın! Sabah Özeti — {now.strftime('%d.%m.%Y')}</b>", ""]
    L.append(market.market_overview())
    try:
        import events
        tb = events.today_block()
        if tb:
            L.append("")
            L.append(tb)
    except Exception as ex:  # noqa: BLE001
        log.warning("Takvim özete eklenemedi: %s", ex)
    L.append("")
    L.append("<b>📋 Takip Listen</b>")
    for item in get_watchlist(chat_id):
        try:
            ctx = compute_item(item)
            record_prediction(ctx)
            CURRENCY.set(ctx["currency"])
            rsi = float(ctx["d"]["signals"]["RSI"][1])
            note = " 🔥aşırı alım" if rsi >= 70 else " 🧊aşırı satım" if rsi <= 30 else ""
            L.append(f"<b>{e(label(item))}</b> {money(item, ctx['price'])} ({ctx['chg']:+.2f}%)\n"
                     f"   1H: {score_label(ctx['week_score'])} · 1A: {score_label(ctx['month_score'])}{note}")
        except Exception as ex:  # noqa: BLE001
            L.append(f"{e(label(item))}: veri alınamadı")
            log.warning("Özet hatası (%s): %s", item, ex)
    pos = store.select("portfolio", {"chat_id": chat_id})
    if pos:
        L.append("")
        L.append("💼 Portföy durumu için: /portfoy")
    tg = list_targets(chat_id)
    if tg:
        L.append(f"🎯 Bekleyen fiyat hedefi: {len(tg)} (/hedef)")
    L.append("🔎 Günün fırsatları birkaç dakika içinde ayrıca gelecek (/firsat).")
    L.append("\n<i>Özeti kapatmak için: /ozet kapat</i>")
    return "\n".join(L)
