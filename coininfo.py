"""CoinGecko'dan coin arama ve temel bilgiler (API anahtarı gerekmez)."""
import data  # noqa: F401  (SSL ayarı; requests'ten önce yüklenmeli)

import html
import re

import requests

CG = "https://api.coingecko.com/api/v3"
HEADERS = {"User-Agent": "crypto-ta-bot/1.0", "Accept": "application/json"}


def _get(path, **params):
    r = requests.get(CG + path, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def find_coin(query: str) -> dict | None:
    """'eth', 'ethereum', 'Pepe' gibi bir girdiyi CoinGecko coin'ine çevirir."""
    q = query.strip().lower()
    coins = _get("/search", query=q).get("coins", [])
    if not coins:
        return None
    rank = lambda c: c.get("market_cap_rank") or 10**9  # noqa: E731
    exact = [c for c in coins if q in (c["symbol"].lower(), c["id"].lower(), c["name"].lower())]
    return min(exact, key=rank) if exact else coins[0]


def fmt_big(x) -> str:
    if x is None:
        return "—"
    for div, suf in ((1e12, " Trilyon"), (1e9, " Milyar"), (1e6, " Milyon"), (1e3, " Bin")):
        if abs(x) >= div:
            return f"{x / div:,.2f}{suf}"
    return f"{x:,.2f}"


def fmt_usd(x) -> str:
    if x is None:
        return "—"
    from analysis import fmt_price
    return fmt_price(x)


def pct(x) -> str:
    return "—" if x is None else f"{x:+.1f}%"


def clean_desc(text: str, limit: int = 900) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+\n", "\n", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        text = cut[: cut.rfind(". ") + 1] if ". " in cut else cut + "…"
    return text


def get_info(coin_id: str) -> dict:
    return _get(f"/coins/{coin_id}", localization="true", tickers="false",
                community_data="false", developer_data="false", sparkline="false")


def build_info_message(info: dict) -> str:
    e = html.escape
    md = info.get("market_data", {})
    usd = lambda k: (md.get(k) or {}).get("usd")  # noqa: E731
    desc_all = info.get("description", {}) or {}
    desc = clean_desc(desc_all.get("tr") or desc_all.get("en") or "")
    lang_note = "" if desc_all.get("tr") else " <i>(kaynak İngilizce)</i>"
    cats = ", ".join([c for c in (info.get("categories") or []) if c][:5])
    home = next((u for u in (info.get("links", {}).get("homepage") or []) if u), "")
    ath_date = (md.get("ath_date") or {}).get("usd", "")[:10]
    atl_date = (md.get("atl_date") or {}).get("usd", "")[:10]

    L = [f"<b>ℹ️ {e(info['name'])} ({e(info['symbol'].upper())}) Hakkında</b>"]
    if info.get("market_cap_rank"):
        L.append(f"🏆 Piyasa değeri sıralaması: <b>#{info['market_cap_rank']}</b>")
    if info.get("genesis_date"):
        L.append(f"📅 Çıkış tarihi: {info['genesis_date']}")
    if cats:
        L.append(f"🏷 Kategori: {e(cats)}")
    if home:
        L.append(f"🌐 {e(home)}")
    L.append("")
    L.append("<b>— Piyasa Verileri —</b>")
    L.append(f"💰 Fiyat: {fmt_usd(usd('current_price'))}")
    L.append(f"📈 Değişim: 24s {pct(md.get('price_change_percentage_24h'))} · "
             f"7g {pct(md.get('price_change_percentage_7d'))} · "
             f"30g {pct(md.get('price_change_percentage_30d'))} · "
             f"1y {pct(md.get('price_change_percentage_1y'))}")
    L.append(f"🏦 Piyasa değeri: ${fmt_big(usd('market_cap'))}")
    L.append(f"🔄 24s işlem hacmi: ${fmt_big(usd('total_volume'))}")
    if usd("market_cap") and usd("total_volume"):
        L.append(f"   Hacim / piyasa değeri: %{usd('total_volume') / usd('market_cap') * 100:.1f}")
    L.append(f"🪙 Dolaşımdaki arz: {fmt_big(md.get('circulating_supply'))}"
             f" · Maks. arz: {fmt_big(md.get('max_supply')) if md.get('max_supply') else 'sınırsız'}")
    L.append(f"🔝 Zirve (ATH): {fmt_usd(usd('ath'))} ({ath_date}) · zirveden {pct(usd('ath_change_percentage'))}")
    L.append(f"🔻 Dip (ATL): {fmt_usd(usd('atl'))} ({atl_date})")
    if desc:
        L.append("")
        L.append(f"<b>📖 Nedir, neden oluşturuldu?</b>{lang_note}")
        L.append(e(desc))
    return "\n".join(L)
