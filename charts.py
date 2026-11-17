"""Analiz grafiği: mumlar + Bollinger + SAR + destek/direnç, altında RSI ve MACD."""
import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

import indicators as ta  # noqa: E402
from analysis import fmt_num  # noqa: E402

BARS = 120
UP, DOWN = "#16a34a", "#dc2626"


def make_chart(ctx: dict) -> bytes:
    df = ctx["daily"]
    c, h, l, o = df["close"], df["high"], df["low"], df["open"]
    up, mid, lo, _, _ = ta.bollinger(c)
    sar, trend = ta.parabolic_sar(h, l)
    r = ta.rsi(c)
    macd, sig, hist = ta.macd(c)

    sl = slice(-BARS, None)
    x = range(len(df.iloc[sl]))
    dates = df.index[sl]

    fig, (ax, ax_rsi, ax_macd) = plt.subplots(
        3, 1, figsize=(10, 7.5), dpi=110, sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1, 1], "hspace": 0.06})
    fig.patch.set_facecolor("white")

    # Mumlar
    for i, (oo, hh, ll, cc) in enumerate(zip(o.iloc[sl], h.iloc[sl], l.iloc[sl], c.iloc[sl])):
        col = UP if cc >= oo else DOWN
        ax.vlines(i, ll, hh, color=col, linewidth=0.8)
        ax.vlines(i, min(oo, cc), max(oo, cc), color=col, linewidth=3.2)
    # Bollinger
    ax.plot(x, up.iloc[sl].values, color="#6366f1", linewidth=0.9, label="Bollinger")
    ax.plot(x, lo.iloc[sl].values, color="#6366f1", linewidth=0.9)
    ax.plot(x, mid.iloc[sl].values, color="#6366f1", linewidth=0.6, linestyle="--")
    ax.fill_between(x, lo.iloc[sl].values, up.iloc[sl].values, color="#6366f1", alpha=0.06)
    # SAR
    tr = trend.iloc[sl].values
    sv = sar.iloc[sl].values
    ax.scatter([i for i in x if tr[i] == 1], [sv[i] for i in x if tr[i] == 1], s=6, color=UP, label="SAR")
    ax.scatter([i for i in x if tr[i] == -1], [sv[i] for i in x if tr[i] == -1], s=6, color=DOWN)
    # Destek / direnç (son 20 gün)
    d = ctx["d"]
    ax.axhline(d["swing_high"], color=DOWN, linewidth=0.8, linestyle=":", alpha=0.8)
    ax.axhline(d["swing_low"], color=UP, linewidth=0.8, linestyle=":", alpha=0.8)
    ax.text(len(x) - 1, d["swing_high"], f" Direnç {fmt_num(d['swing_high'])}", color=DOWN, fontsize=8,
            va="bottom", ha="right")
    ax.text(len(x) - 1, d["swing_low"], f" Destek {fmt_num(d['swing_low'])}", color=UP, fontsize=8,
            va="top", ha="right")
    ax.set_title(f"{ctx['name']} ({ctx['pair']}) · Günlük · Son fiyat {ctx['currency']}{fmt_num(ctx['price'])}",
                 fontsize=11, loc="left")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_num(v)))
    ax.yaxis.tick_right()
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.grid(alpha=0.2)

    # RSI
    ax_rsi.plot(x, r.iloc[sl].values, color="#9333ea", linewidth=1)
    ax_rsi.axhline(70, color=DOWN, linewidth=0.7, linestyle="--")
    ax_rsi.axhline(30, color=UP, linewidth=0.7, linestyle="--")
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI", fontsize=8)
    ax_rsi.yaxis.tick_right()
    ax_rsi.grid(alpha=0.2)

    # MACD
    hv = hist.iloc[sl].values
    ax_macd.bar(x, hv, color=[UP if v >= 0 else DOWN for v in hv], width=0.8, alpha=0.6)
    ax_macd.plot(x, macd.iloc[sl].values, color="#0ea5e9", linewidth=1, label="MACD")
    ax_macd.plot(x, sig.iloc[sl].values, color="#f97316", linewidth=1, label="Sinyal")
    ax_macd.set_ylabel("MACD", fontsize=8)
    ax_macd.yaxis.tick_right()
    ax_macd.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_num(v)))
    ax_macd.legend(loc="upper left", fontsize=7, frameon=False)
    ax_macd.grid(alpha=0.2)

    ticks = list(range(0, len(x), 20))
    ax_macd.set_xticks(ticks)
    ax_macd.set_xticklabels([dates[i].strftime("%d.%m") for i in ticks], fontsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
