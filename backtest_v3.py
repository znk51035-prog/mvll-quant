#!/usr/bin/env python3
"""
MVLL 策略第三版：全网调研 top 候选的实测（2026-10-06 deep research 结论）。
候选（报告 ~/workspace/research_notes/best-quant-indicator-combos-20261006-2147/report.md）：
  R1 MACD(12,26,9)+RSI(14)持续极值（Chio 2022）：DIF>DEA 且 RSI<=35 连续6根 -> 买；
      DIF<DEA 且 RSI>=70 连续6根 -> 卖
  R2 MACD(12,26,9)+MFI(14)持续极值：DIF>DEA 且 MFI<=25 连续6根 -> 买；
      DIF<DEA 且 MFI>=70 连续6根 -> 卖
  R3 Connors RSI(2)+200日均线：收盘>200SMA 才做；RSI(2)<10 -> 买；收盘>5日SMA -> 卖
  R4 Donchian+ATR（海龟结构简化）：收盘>前20日最高 -> 买；收盘<前10日最低 -> 卖；
      2×ATR(20) 止损
  R5 MRVL门控趋势带（BestFolio 思想，2x ETF 专用）：信号看 MRVL（正股）相对其200日均线；
      MRVL收盘>200MA×1.04 -> 买 MVLL；MRVL收盘<200MA×0.97 -> 卖 MVLL；之间保持（滞后带）
统一：每笔 $5000，只做多，连续亏3次熔断。R1/R2/R3/R5 用信号出场（无固定止盈）；
R4 用 2×ATR 止损 + 信号出场。
"""
import csv
import json
import os
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "mvll_daily.csv")

NOTIONAL = 5000.0
START_CASH = 100000.0


def load_bars(path=DATA):
    bars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                bars.append({
                    "date": row["Date"],
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row.get("Volume") or 0),
                })
            except (ValueError, KeyError):
                continue
    bars.sort(key=lambda b: b["date"])
    return bars


def ema(values, n):
    k = 2 / (n + 1)
    out, e = [], values[0]
    for i, v in enumerate(values):
        e = sum(values[:i + 1]) / (i + 1) if i < n else v * k + e * (1 - k)
        out.append(e)
    return out


def wilder_smooth(vals, n):
    """Wilder 平滑（RSI/ATR 用），返回与 vals 等长，前面 n 个为 None。"""
    out = [None] * len(vals)
    if len(vals) <= n:
        return out
    avg = sum(vals[1:n + 1]) / n
    out[n] = avg
    for i in range(n + 1, len(vals)):
        avg = (avg * (n - 1) + vals[i]) / n
        out[i] = avg
    return out


def add_macd(bars):
    closes = [b["close"] for b in bars]
    dif = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    dea = ema(dif, 9)
    for b, d, e in zip(bars, dif, dea):
        b["dif"], b["dea"] = d, e


def add_rsi(bars, n=14, key="rsi"):
    closes = [b["close"] for b in bars]
    chg = [0.0] + [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ag = wilder_smooth([max(c, 0) for c in chg], n)
    al = wilder_smooth([max(-c, 0) for c in chg], n)
    for b, g, l in zip(bars, ag, al):
        if g is None or l is None:
            b[key] = None
        elif l == 0:
            b[key] = 100.0
        else:
            b[key] = 100 - 100 / (1 + g / l)


def add_mfi(bars, n=14):
    tp = [(b["high"] + b["low"] + b["close"]) / 3 for b in bars]
    mf = [t * b["volume"] for t, b in zip(tp, bars)]
    out = [None] * len(bars)
    for i in range(n, len(bars)):
        pos = neg = 0.0
        for j in range(i - n + 1, i + 1):
            if tp[j] > tp[j - 1]:
                pos += mf[j]
            elif tp[j] < tp[j - 1]:
                neg += mf[j]
        out[i] = 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg)
    for b, v in zip(bars, out):
        b["mfi"] = v


def add_atr(bars, n=20):
    h, l, c = [b["high"] for b in bars], [b["low"] for b in bars], [b["close"] for b in bars]
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                          for i in range(1, len(bars))]
    for b, v in zip(bars, wilder_smooth(tr, n)):
        b["atr"] = v


def sma(values, n, i):
    return sum(values[i - n + 1:i + 1]) / n if i >= n - 1 else None


def run(bars, name, warmup, decide):
    """decide(bars, i, pos) -> ('buy'|'sell'|None, reason). pos 含 entry/shares/date."""
    cash, pos, trades = START_CASH, None, []
    consec, halted, halt_date = 0, False, None
    peak, max_dd = START_CASH, 0.0

    def close_pos(b, exit_px, reason):
        nonlocal cash, pos, trades, consec, halted, halt_date
        pnl = (exit_px - pos["entry"]) * pos["shares"]
        cash += exit_px * pos["shares"]
        trades.append({"in": pos["date"], "out": b["date"], "entry": round(pos["entry"], 2),
                       "exit": round(exit_px, 2), "pnl": round(pnl, 2), "reason": reason})
        consec = consec + 1 if pnl < 0 else 0
        if consec >= 3 and not halted:
            halted, halt_date = True, b["date"]
        pos = None

    for i in range(warmup, len(bars)):
        b = bars[i]
        if pos and pos.get("stop") and b["low"] <= pos["stop"]:
            close_pos(b, pos["stop"], "ATR止损")
        elif pos:
            action, reason = decide(bars, i, pos)
            if action == "sell":
                close_pos(b, b["close"], reason)
        if not pos and not halted:
            action, reason = decide(bars, i, pos)
            if action == "buy":
                shares = int(NOTIONAL // b["close"])
                if shares > 0 and cash >= shares * b["close"]:
                    cash -= shares * b["close"]
                    pos = {"shares": shares, "entry": b["close"], "date": b["date"]}
                    if reason and reason.startswith("ATR:"):
                        pos["stop"] = float(reason.split(":")[1])
        eq = cash + (pos["shares"] * b["close"] if pos else 0)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak else 0)

    if pos:
        b = bars[-1]
        close_pos(b, b["close"], "期末平仓")

    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "name": name,
        "period": f'{bars[warmup]["date"]}~{bars[-1]["date"]}',
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "total_pnl": round(cash - START_CASH, 2),
        "return_pct": round((cash - START_CASH) / START_CASH * 100, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "halted": halted, "halt_date": halt_date,
        "trades_detail": trades,
    }


def sustained(bars, i, key, thresh, direction, k=6):
    vals = [bars[j][key] for j in range(i - k + 1, i + 1)]
    if any(v is None for v in vals):
        return False
    return all(v <= thresh for v in vals) if direction == "low" else all(v >= thresh for v in vals)


def main():
    bars = load_bars()
    add_macd(bars)
    add_rsi(bars, 14, "rsi14")
    add_rsi(bars, 2, "rsi2")
    add_mfi(bars)
    add_atr(bars)
    closes = [b["close"] for b in bars]
    results = []

    # R1: MACD + RSI 持续极值
    def r1(bars, i, pos):
        b = bars[i]
        if b["dif"] is None or b["rsi14"] is None:
            return None, None
        if b["dif"] > b["dea"] and sustained(bars, i, "rsi14", 35, "low"):
            return "buy", "MACD+RSI持续超卖"
        if b["dif"] < b["dea"] and sustained(bars, i, "rsi14", 70, "high"):
            return "sell", "MACD+RSI持续超买"
        return None, None
    results.append(run(bars, "R1 MACD+RSI持续极值", 40, r1))

    # R2: MACD + MFI 持续极值
    def r2(bars, i, pos):
        b = bars[i]
        if b["dif"] is None or b["mfi"] is None:
            return None, None
        if b["dif"] > b["dea"] and sustained(bars, i, "mfi", 25, "low"):
            return "buy", "MACD+MFI持续超卖"
        if b["dif"] < b["dea"] and sustained(bars, i, "mfi", 70, "high"):
            return "sell", "MACD+MFI持续超买"
        return None, None
    results.append(run(bars, "R2 MACD+MFI持续极值", 40, r2))

    # R3: Connors RSI(2) + 200SMA
    def r3(bars, i, pos):
        b = bars[i]
        s200 = sma(closes, 200, i)
        s5 = sma(closes, 5, i)
        if s200 is None or b["rsi2"] is None:
            return None, None
        if pos and b["close"] > s5:
            return "sell", "RSI2均值回归出场"
        if not pos and b["close"] > s200 and b["rsi2"] < 10:
            return "buy", "RSI2超卖"
        return None, None
    results.append(run(bars, "R3 RSI2+200SMA", 200, r3))

    # R4: Donchian 20/10 + 2×ATR 止损
    def r4(bars, i, pos):
        b = bars[i]
        if b["atr"] is None:
            return None, None
        hi20 = max(x["high"] for x in bars[i - 20:i])
        lo10 = min(x["low"] for x in bars[i - 10:i])
        if not pos and b["close"] > hi20:
            return "buy", f"ATR:{b['close'] - 2 * b['atr']:.2f}"
        if pos and b["close"] < lo10:
            return "sell", "跌破10日低点"
        return None, None
    results.append(run(bars, "R4 Donchian20+2ATR", 25, r4))

    # R5: MRVL 门控趋势带（信号看正股，交易 MVLL）
    mrvl = download_mrvl()
    if mrvl:
        m_by_date = {m["date"]: m for m in mrvl}
        mcloses = [m["close"] for m in mrvl]
        for idx, m in enumerate(mrvl):
            m["ma200"] = sma(mcloses, 200, idx)

        def r5(bars, i, pos):
            b = bars[i]
            m = m_by_date.get(b["date"])
            if not m or not m["ma200"]:
                return None, None
            if not pos and m["close"] > m["ma200"] * 1.04:
                return "buy", "MRVL站上200MA+4%"
            if pos and m["close"] < m["ma200"] * 0.97:
                return "sell", "MRVL跌破200MA-3%"
            return None, None
        results.append(run(bars, "R5 MRVL门控趋势带", 5, r5))
    else:
        results.append({"name": "R5 MRVL门控趋势带", "error": "MRVL数据下载失败"})

    print(json.dumps(results, ensure_ascii=False, indent=2))
    with open(os.path.join(BASE, "backtest_v3.json"), "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def download_mrvl():
    try:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/MRVL"
               "?interval=1d&period1=0&period2=9999999999")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            d = json.load(resp)
        r = d["chart"]["result"][0]
        ts, q = r["timestamp"], r["indicators"]["quote"][0]
        out = []
        for i, t in enumerate(ts):
            if q["close"][i] is None:
                continue
            import datetime
            dt = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date().isoformat()
            out.append({"date": dt, "close": float(q["close"][i])})
        out.sort(key=lambda x: x["date"])
        print(f"MRVL bars: {len(out)} ({out[0]['date']}~{out[-1]['date']})")
        return out
    except Exception as e:
        print(f"MRVL download failed: {e}")
        return None


if __name__ == "__main__":
    main()
