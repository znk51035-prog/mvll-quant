#!/usr/bin/env python3
"""
MVLL 第一版量化策略：5/20 日均线金叉死叉 + 风控
规则（用户 2026-10-06 确认的默认版）：
  1. 标的：MVLL（GraniteShares 2x MRVL，每日杠杆ETF）
  2. 信号：5日均线上穿20日均线 -> 买入；跌穿 -> 卖出（只看收盘价）
  3. 仓位：每笔固定 $5,000 名义金额
  4. 止损/止盈：相对入场价 -5% 止损 / +10% 止盈（盘中用 High/Low 判定）
  5. 频率：每日收盘后检查一次
  6. 只做多，不做空
  7. 熔断：连续亏损 3 次 -> 停止开新仓（回测中仅统计）
用法：
  python3 strategy.py backtest   # 全历史回测
  python3 strategy.py signal     # 今日信号（用于每日定时运行）
"""
import csv
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "mvll_daily.csv")
LEDGER = os.path.join(BASE, "ledger.json")

FAST, SLOW = 5, 20
NOTIONAL = 5000.0
STOP_PCT = 0.05
TAKE_PCT = 0.10
START_CASH = 100000.0
# 前向验证锁定策略（用户 2026-10-06 确认）：纯 Donchian —— 收盘突破前20日最高点买入，
# 收盘跌破前10日最低点卖出；无固定止盈止损，完全按指标跑
LIVE_STOP_PCT = 0.08
LIVE_TAKE_PCT = 0.10


def load_bars():
    bars = []
    with open(DATA, newline="") as f:
        for row in csv.DictReader(f):
            try:
                bars.append({
                    "date": row["Date"],
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                })
            except (ValueError, KeyError):
                continue
    bars.sort(key=lambda b: b["date"])
    return bars


def add_ma(bars):
    closes = [b["close"] for b in bars]
    for i, b in enumerate(bars):
        b["ma5"] = sum(closes[max(0, i - 4):i + 1]) / min(FAST, i + 1) if i >= FAST - 1 else None
        b["ma20"] = sum(closes[max(0, i - 19):i + 1]) / min(SLOW, i + 1) if i >= SLOW - 1 else None


def add_kdj(bars, n=9):
    k = d = 50.0
    for i, b in enumerate(bars):
        window = bars[max(0, i - n + 1):i + 1]
        ln = min(x["low"] for x in window)
        hn = max(x["high"] for x in window)
        rsv = 0 if hn == ln else (b["close"] - ln) / (hn - ln) * 100
        k = (2 / 3) * k + (1 / 3) * rsv
        d = (2 / 3) * d + (1 / 3) * k
        b["k"], b["d"] = k, d


def add_atr(bars, n=20):
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                          for i in range(1, len(bars))]
    out, avg = [None] * len(bars), None
    for i in range(len(bars)):
        if i < n:
            if i == n - 1:
                avg = sum(tr[1:n + 1]) / n
                out[i] = avg
        else:
            avg = (avg * (n - 1) + tr[i]) / n
            out[i] = avg
    for b, v in zip(bars, out):
        b["atr"] = v


def backtest():
    bars = load_bars()
    add_ma(bars)
    cash = START_CASH
    pos = None  # dict(shares, entry, date)
    trades = []
    equity_curve = []
    consec_loss = 0
    halted = False

    for i in range(1, len(bars)):
        b, prev = bars[i], bars[i - 1]
        if b["ma5"] is None or prev["ma5"] is None or b["ma20"] is None or prev["ma20"] is None:
            continue

        # --- 持仓风控（用当日 High/Low 判定，先判止损） ---
        if pos:
            exit_px, reason = None, None
            if b["low"] <= pos["entry"] * (1 - STOP_PCT):
                exit_px, reason = pos["entry"] * (1 - STOP_PCT), "止损"
            elif b["high"] >= pos["entry"] * (1 + TAKE_PCT):
                exit_px, reason = pos["entry"] * (1 + TAKE_PCT), "止盈"
            elif prev["ma5"] is not None and prev["ma5"] >= prev["ma20"] and b["ma5"] < b["ma20"]:
                exit_px, reason = b["close"], "死叉"
            if exit_px:
                pnl = (exit_px - pos["entry"]) * pos["shares"]
                cash += exit_px * pos["shares"]
                trades.append({"in": pos["date"], "out": b["date"], "entry": round(pos["entry"], 2),
                               "exit": round(exit_px, 2), "pnl": round(pnl, 2), "reason": reason})
                consec_loss = consec_loss + 1 if pnl < 0 else 0
                if consec_loss >= 3:
                    halted = True
                pos = None

        # --- 开仓信号 ---
        if not pos and not halted and prev["ma5"] <= prev["ma20"] and b["ma5"] > b["ma20"]:
            shares = int(NOTIONAL // b["close"])
            if shares > 0 and cash >= shares * b["close"]:
                cash -= shares * b["close"]
                pos = {"shares": shares, "entry": b["close"], "date": b["date"]}

        equity = cash + (pos["shares"] * b["close"] if pos else 0)
        equity_curve.append((b["date"], equity))

    # 期末平仓（按最后收盘价）
    if pos:
        b = bars[-1]
        pnl = (b["close"] - pos["entry"]) * pos["shares"]
        cash += b["close"] * pos["shares"]
        trades.append({"in": pos["date"], "out": b["date"], "entry": round(pos["entry"], 2),
                       "exit": round(b["close"], 2), "pnl": round(pnl, 2), "reason": "期末平仓"})

    # --- 统计 ---
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    peak, max_dd = START_CASH, 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
    buy_hold = (bars[-1]["close"] / bars[SLOW]["close"] - 1) if len(bars) > SLOW else 0

    result = {
        "period": f'{bars[0]["date"]} ~ {bars[-1]["date"]}',
        "bars": len(bars),
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "total_pnl": round(cash - START_CASH, 2),
        "total_return_pct": round((cash - START_CASH) / START_CASH * 100, 2),
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "buy_hold_pct": round(buy_hold * 100, 2),
        "halted_by_circuit_breaker": halted,
        "final_cash": round(cash, 2),
        "last_close": bars[-1]["close"],
        "last_date": bars[-1]["date"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    with open(os.path.join(BASE, "backtest_result.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    # 明细存档
    with open(os.path.join(BASE, "backtest_trades.json"), "w") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)


def refresh_data():
    """从 Yahoo 拉取最新 MVLL 日线并重建 CSV（复权）。"""
    import urllib.request
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/MVLL"
           "?interval=1d&period1=0&period2=9999999999&events=div%2Csplit")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        d = json.load(resp)
    r = d["chart"]["result"][0]
    ts = r["timestamp"]
    q = r["indicators"]["quote"][0]
    adj = r["indicators"].get("adjclose", [{}])[0].get("adjclose")
    rows = []
    for i, t in enumerate(ts):
        if q["close"][i] is None:
            continue
        f = (adj[i] / q["close"][i]) if adj and adj[i] and q["close"][i] else 1.0
        dt = datetime.fromtimestamp(t, tz=__import__("datetime").timezone.utc).date().isoformat()
        vol = q["volume"][i] or 0
        rows.append([dt, round(q["open"][i] * f, 4), round(q["high"][i] * f, 4),
                     round(q["low"][i] * f, 4), round(q["close"][i] * f, 4), int(vol)])
    with open(DATA, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
        w.writerows(rows)
    return len(rows)


def load_ledger():
    if os.path.exists(LEDGER):
        with open(LEDGER) as f:
            return json.load(f)
    return {"cash": START_CASH, "position": None, "consec_loss": 0,
            "halted": False, "trades": []}


def save_ledger(lg):
    with open(LEDGER, "w") as f:
        json.dump(lg, f, ensure_ascii=False, indent=2)


def daily():
    """每日收盘后运行：纯 Donchian(20)突破 / 跌破10日低点出场，无固定止盈止损。"""
    try:
        n = refresh_data()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"数据刷新失败: {e}"}, ensure_ascii=False))
        return
    bars = load_bars()
    lg = load_ledger()
    b = bars[-1]
    notify, msgs = False, []

    if len(bars) < 30:
        print(json.dumps({"ok": True, "date": b["date"], "note": "数据预热中", "notify": False},
                         ensure_ascii=False))
        return

    hi20 = max(x["high"] for x in bars[-21:-1])
    lo10 = min(x["low"] for x in bars[-11:-1])
    pos = lg["position"]
    # 持仓管理：只看收盘跌破 10 日低点出场，无固定止盈止损
    if pos:
        exit_px, reason = None, None
        if b["close"] < lo10:
            exit_px, reason = b["close"], "跌破10日低点"
        if exit_px:
            pnl = (exit_px - pos["entry"]) * pos["shares"]
            lg["cash"] += exit_px * pos["shares"]
            lg["trades"].append({"in": pos["date"], "out": b["date"],
                                 "entry": round(pos["entry"], 2), "exit": round(exit_px, 2),
                                 "pnl": round(pnl, 2), "reason": reason})
            lg["consec_loss"] = lg["consec_loss"] + 1 if pnl < 0 else 0
            if lg["consec_loss"] >= 3:
                lg["halted"] = True
            lg["position"] = None
            pos = None
            notify = True
            msgs.append(f"卖出信号（{reason}）：{b['date']} 收 ${b['close']:.2f}，"
                        f"本笔盈亏 ${pnl:+.2f}")

    # 开仓：收盘突破前 20 日最高点
    if not pos and not lg["halted"] and b["close"] > hi20:
        shares = int(NOTIONAL // b["close"])
        if shares > 0 and lg["cash"] >= shares * b["close"]:
            lg["cash"] -= shares * b["close"]
            lg["position"] = {"shares": shares, "entry": b["close"], "date": b["date"]}
            notify = True
            msgs.append(f"买入信号（突破20日高点 ${hi20:.2f}）：{b['date']} 收 ${b['close']:.2f}，"
                        f"虚拟买入 {shares} 股（约 ${shares * b['close']:.0f}）")

    save_ledger(lg)
    equity = lg["cash"] + (lg["position"]["shares"] * b["close"] if lg["position"] else 0)
    print(json.dumps({
        "ok": True, "date": b["date"], "close": round(b["close"], 2),
        "donchian_hi20": round(hi20, 2), "donchian_lo10": round(lo10, 2),
        "cash": round(lg["cash"], 2), "equity": round(equity, 2),
        "position": lg["position"], "halted": lg["halted"],
        "notify": notify, "messages": msgs, "bars": n,
    }, ensure_ascii=False, indent=2))


def signal():
    """每日收盘后运行：输出今日信号 + 更新模拟账本。"""
    bars = load_bars()
    add_ma(bars)
    if len(bars) < SLOW + 1:
        print(json.dumps({"error": "数据不足"}, ensure_ascii=False))
        return
    b, prev = bars[-1], bars[-2]
    sig = "无信号"
    if prev["ma5"] is not None and b["ma5"] is not None:
        if prev["ma5"] <= prev["ma20"] and b["ma5"] > b["ma20"]:
            sig = "买入信号（金叉）"
        elif prev["ma5"] >= prev["ma20"] and b["ma5"] < b["ma20"]:
            sig = "卖出信号（死叉）"
    out = {
        "date": b["date"],
        "close": round(b["close"], 2),
        "k": round(b["k"], 2),
        "d": round(b["d"], 2),
        "signal": sig,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"
    if mode == "signal":
        signal()
    elif mode == "daily":
        daily()
    else:
        backtest()
