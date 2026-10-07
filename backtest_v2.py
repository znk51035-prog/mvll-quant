#!/usr/bin/env python3
"""
MVLL 策略第二版：MACD / KDJ 信号回测，与第一版（5/20均线）同口径对比。
统一风控：每笔 $5000 名义，只做多，-5%止损/+10%止盈（盘中 High/Low 判定），
连续亏损 3 次熔断停新仓。
信号定义（日线收盘）：
  MACD(12,26,9)：DIF 上穿 DEA -> 买；DIF 下穿 DEA -> 卖
  KDJ(9,3,3)：K 上穿 D 且 K<30（超卖区金叉）-> 买；K 下穿 D -> 卖
  共振：MACD 金叉 且 KDJ 金叉条件同时成立 -> 买；任一死叉 -> 卖
"""
import csv
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "mvll_daily.csv")

NOTIONAL = 5000.0
STOP_PCT = 0.05
TAKE_PCT = 0.10
START_CASH = 100000.0


def load_bars():
    bars = []
    with open(DATA, newline="") as f:
        for row in csv.DictReader(f):
            bars.append({
                "date": row["Date"],
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
            })
    bars.sort(key=lambda b: b["date"])
    return bars


def ema(values, n):
    k = 2 / (n + 1)
    out = []
    e = values[0]
    for i, v in enumerate(values):
        if i < n:
            # 预热期用 SMA 做种子
            e = sum(values[:i + 1]) / (i + 1)
        else:
            e = v * k + e * (1 - k)
        out.append(e)
    return out


def add_macd(bars):
    closes = [b["close"] for b in bars]
    e12, e26 = ema(closes, 12), ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    for b, d, e in zip(bars, dif, dea):
        b["dif"], b["dea"] = d, e


def add_kdj(bars, n=9):
    k = d = 50.0
    for i, b in enumerate(bars):
        window = bars[max(0, i - n + 1):i + 1]
        ln = min(x["low"] for x in window)
        hn = max(x["high"] for x in window)
        rsv = 0 if hn == ln else (b["close"] - ln) / (hn - ln) * 100
        k = (2 / 3) * k + (1 / 3) * rsv
        d = (2 / 3) * d + (1 / 3) * k
        b["k"], b["d"], b["j"] = k, d, 3 * k - 2 * d


def macd_buy(p, c):
    return p["dif"] <= p["dea"] and c["dif"] > c["dea"]


def macd_sell(p, c):
    return p["dif"] >= p["dea"] and c["dif"] < c["dea"]


def kdj_buy(p, c):
    return p["k"] <= p["d"] and c["k"] > c["d"] and c["k"] < 30


def kdj_sell(p, c):
    return p["k"] >= p["d"] and c["k"] < c["d"]


def combo_buy(p, c):
    return macd_buy(p, c) and (c["k"] > c["d"])


def combo_sell(p, c):
    return macd_sell(p, c) or kdj_sell(p, c)


def run_backtest(bars, buy_fn, sell_fn, warmup, name, stop_pct=STOP_PCT, take_pct=TAKE_PCT):
    cash = START_CASH
    pos = None
    trades = []
    consec_loss = 0
    halted = False
    halt_date = None
    peak, max_dd = START_CASH, 0.0

    for i in range(warmup, len(bars)):
        b, prev = bars[i], bars[i - 1]

        if pos:
            exit_px, reason = None, None
            if b["low"] <= pos["entry"] * (1 - stop_pct):
                exit_px, reason = pos["entry"] * (1 - stop_pct), "止损"
            elif b["high"] >= pos["entry"] * (1 + take_pct):
                exit_px, reason = pos["entry"] * (1 + take_pct), "止盈"
            elif sell_fn(prev, b):
                exit_px, reason = b["close"], "信号卖出"
            if exit_px:
                pnl = (exit_px - pos["entry"]) * pos["shares"]
                cash += exit_px * pos["shares"]
                trades.append({"in": pos["date"], "out": b["date"],
                               "entry": round(pos["entry"], 2), "exit": round(exit_px, 2),
                               "pnl": round(pnl, 2), "reason": reason})
                consec_loss = consec_loss + 1 if pnl < 0 else 0
                if consec_loss >= 3 and not halted:
                    halted, halt_date = True, b["date"]
                pos = None

        if not pos and not halted and buy_fn(prev, b):
            shares = int(NOTIONAL // b["close"])
            if shares > 0 and cash >= shares * b["close"]:
                cash -= shares * b["close"]
                pos = {"shares": shares, "entry": b["close"], "date": b["date"]}

        eq = cash + (pos["shares"] * b["close"] if pos else 0)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)

    if pos:
        b = bars[-1]
        pnl = (b["close"] - pos["entry"]) * pos["shares"]
        cash += b["close"] * pos["shares"]
        trades.append({"in": pos["date"], "out": b["date"], "entry": round(pos["entry"], 2),
                       "exit": round(b["close"], 2), "pnl": round(pnl, 2), "reason": "期末平仓"})

    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "name": name,
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "total_pnl": round(cash - START_CASH, 2),
        "return_pct": round((cash - START_CASH) / START_CASH * 100, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "halted": halted,
        "halt_date": halt_date,
        "reasons": {r: sum(1 for t in trades if t["reason"] == r) for r in set(t["reason"] for t in trades)},
    }


def main():
    bars = load_bars()
    add_macd(bars)
    add_kdj(bars)
    results = [
        run_backtest(bars, macd_buy, macd_sell, 40, "MACD(12,26,9)"),
        run_backtest(bars, kdj_buy, kdj_sell, 15, "KDJ(9,3,3)超卖金叉"),
        run_backtest(bars, combo_buy, combo_sell, 40, "MACD+KDJ共振"),
    ]
    # 风控参数扫描：止损放宽，看瓶颈是否在风控
    for sp in (0.08, 0.10):
        results.append(run_backtest(bars, kdj_buy, kdj_sell, 15,
                                    f"KDJ止损{int(sp*100)}%", stop_pct=sp))
    results.append(run_backtest(bars, macd_buy, macd_sell, 40,
                                "MACD止损8%", stop_pct=0.08))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    with open(os.path.join(BASE, "backtest_v2.json"), "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
