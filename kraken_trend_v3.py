#!/usr/bin/env python3
"""
Kraken Funded - Trend v3 hourly checker
=======================================

This is the same process Computer has been running for you, written as a
plain script so it can run for free on GitHub instead of using credits.

It does three jobs:

  check    (:02 every hour)  Look at the 1-hour candle that just CLOSED.
                             1) SELL any open trade that closed below its exit line.
                             2) TAKE any coin that just broke out (if you have room).
  headsup  (:45 every hour)  GET READY text if a coin is above its breakout level
                             with ~15 minutes left in the candle. Never a buy signal.
  morning  (7:30 AM Arizona) Short recap: BTC filter, signals in the last 24h,
                             open trades and their exit lines.

THE TREND v3 RULES (all on 1-hour Kraken candles)
-------------------------------------------------
  BUY when ALL are true on a just-closed candle:
    a) Close is ABOVE the highest high of the previous 55 candles  (the breakout)
    b) The coin's EMA50 is above its EMA200                       (coin uptrend)
    c) BTC's close is above its EMA200, and BTC's EMA50 > EMA200   (market filter)

  STARTING EXIT:  entry price - 2 x ATR(14)       (ATR = average hourly range)
  TRAILING EXIT:  lowest low of the previous 20 candles
  EXIT LINE    =  whichever of those two is HIGHER (so it only moves up)
  SELL when a 1-hour candle CLOSES below the exit line. Mid-hour dips don't count.
  No profit target. Winners are allowed to run.

  SIZE:  coins = risk $ / (entry - exit)      buy $ = coins x entry
         So $10 risk buys MORE of a coin with a tight exit and LESS with a wide one.

  CHASE RULE:  skip the buy if price is already up more than half the gap
               between the TAKE price and the exit.

  LIMITS: max 4 open trades, 1 trade per coin, never 2 meme coins at once.

No third-party packages are needed: Python 3.9+ standard library only.
"""

import json
import os
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# SETTINGS - change these freely
# ---------------------------------------------------------------------------
AZ = ZoneInfo("America/Phoenix")

COINS = (
    "AAVE ADA ALGO ARB ASTER ATOM AVAX BCH BNB BONK BTC CRV DOGE DOT ETC ETH FIL "
    "GRASS HBAR INJ JTO JUP KAITO LDO LINK LTC NEAR ONDO OP PNUT POL POPCAT RENDER "
    "S SHIB SOL STX SUI TAO TIA TRUMP TRX UNI WIF XLM XRP AIXBT APT ENA ETHFI "
    "FARTCOIN FLOKI HYPE ICP LIGHTER MON MOODENG PENDLE PENGU PEPE PUMP VIRTUAL "
    "WLD XPL ZEC ZRO"
).split()  # all 66 Kraken Funded coins, including the 20 TradingView missed

MEME_COINS = set(
    "DOGE SHIB PEPE BONK FLOKI WIF PENGU TRUMP PNUT PUMP FARTCOIN MOODENG POPCAT".split()
)

BREAKOUT_LOOKBACK = 55   # candles for the breakout high
TRAIL_LOOKBACK = 20      # candles for the trailing exit low
ATR_LEN = 14
ATR_MULT = 2.0
EMA_FAST = 50
EMA_SLOW = 200

RISK_PER_TRADE = float(os.environ.get("RISK", "6.61"))  # dollars lost if the exit is hit ((balance - 9700) / 20); $9,832.26 on Oct 6 9:43 PM
POSITION_CAP = 4000.0    # never put more than this in one coin
MIN_STOP_PCT, MAX_STOP_PCT = 0.3, 15.0  # skip exits that are unrealistically tight or wide
MAX_OPEN_TRADES = 4
CHASE_FRACTION = 0.5     # skip if price ran more than half the entry-to-exit gap

# Overnight (11 PM - 7 AM Arizona): TAKE texts are still sent, but marked
# "overnight - OK to skip". Set to False to stop overnight TAKE texts entirely.
SEND_OVERNIGHT_TAKES = True
OVERNIGHT_START, OVERNIGHT_END = 23, 7

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
KRAKEN = "https://api.kraken.com/0/public"

# Delivery (set as GitHub Secrets; if missing, messages are just printed)
SMTP_USER = os.environ.get("GMAIL_ADDRESS", "")
SMTP_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
TEXT_TO = os.environ.get("TEXT_TO", "")      # optional email-to-text address
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # your ntfy phone-app topic (main delivery)
EMAIL_TO = os.environ.get("EMAIL_TO", "")    # full-detail backup email
DRY_RUN = os.environ.get("DRY_RUN", "") == "1" or not (NTFY_TOPIC or (SMTP_USER and SMTP_PASS))


# ---------------------------------------------------------------------------
# Kraken data
# ---------------------------------------------------------------------------
def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "trend-v3-checker"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            if data.get("error"):
                raise RuntimeError(data["error"])
            return data["result"]
        except Exception as e:  # network hiccup or rate limit: wait and retry
            if i == tries - 1:
                raise
            time.sleep(2 + i * 3)


def kraken_pair_map():
    """Map 'SOL' -> Kraken's pair code for SOL/USD (BTC is 'XBT' on Kraken)."""
    pairs = get_json(f"{KRAKEN}/AssetPairs")
    by_ws = {v.get("wsname"): k for k, v in pairs.items()}
    alias = {"BTC": "XBT", "DOGE": "XDG"}
    out = {}
    for c in COINS:
        code = by_ws.get(f"{c}/USD") or by_ws.get(f"{alias.get(c, c)}/USD")
        if code:
            out[c] = code
    return out


def candles(pair_code):
    """Hourly candles, oldest first. Kraken's LAST row is the unfinished candle."""
    res = get_json(f"{KRAKEN}/OHLC?pair={pair_code}&interval=60")
    rows = next(v for k, v in res.items() if k != "last")
    return [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def ema(values, length):
    """Exponential moving average, seeded with a simple average like TradingView."""
    if len(values) < length:
        return [None] * len(values)
    k = 2 / (length + 1)
    out = [None] * (length - 1)
    prev = sum(values[:length]) / length
    out.append(prev)
    for v in values[length:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def atr(cs, length=ATR_LEN):
    """Average True Range (Wilder smoothing) = the typical size of one hourly candle."""
    trs = []
    for i, c in enumerate(cs):
        if i == 0:
            trs.append(c["h"] - c["l"])
        else:
            pc = cs[i - 1]["c"]
            trs.append(max(c["h"] - c["l"], abs(c["h"] - pc), abs(c["l"] - pc)))
    if len(trs) < length:
        return [None] * len(trs)
    out = [None] * (length - 1)
    prev = sum(trs[:length]) / length
    out.append(prev)
    for tr in trs[length:]:
        prev = (prev * (length - 1) + tr) / length
        out.append(prev)
    return out


def analyze(cs):
    """Everything the rules need, measured on the last CLOSED candle."""
    closed = cs[:-1]          # drop the unfinished candle
    live = cs[-1]             # the candle still forming (used by GET READY)
    if len(closed) < EMA_SLOW + 5:
        return None
    closes = [c["c"] for c in closed]
    e50, e200, a = ema(closes, EMA_FAST), ema(closes, EMA_SLOW), atr(closed)
    last = closed[-1]
    prior = closed[-1 - BREAKOUT_LOOKBACK:-1]                # 55 candles BEFORE the last
    prior_live = closed[-BREAKOUT_LOOKBACK:]                 # 55 candles before the live one
    return {
        "time": last["t"],
        "close": last["c"],
        "breakout_level": max(c["h"] for c in prior),
        "breakout_level_live": max(c["h"] for c in prior_live),
        "trail_low": min(c["l"] for c in closed[-1 - TRAIL_LOOKBACK:-1]),
        "trail_low_next": min(c["l"] for c in closed[-TRAIL_LOOKBACK:]),
        "ema50": e50[-1],
        "ema200": e200[-1],
        "atr": a[-1],
        "live_price": live["c"],
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def px(v):
    """Show prices with sensible precision (BTC 85,301 vs. BONK 0.00001234)."""
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:.2f}" if v >= 100 else f"{v:.3f}"
    digits = 4
    while v < 10 ** -(digits - 3) and digits < 10:
        digits += 1
    return f"{v:.{digits}f}"


def az_now():
    return datetime.now(timezone.utc).astimezone(AZ)


def is_overnight(dt):
    return dt.hour >= OVERNIGHT_START or dt.hour < OVERNIGHT_END


# ---------------------------------------------------------------------------
# State (your open trades + recent signals), saved in state.json
# ---------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"open_trades": {}, "signals": [], "sent": {}}


def save_state(state):
    cutoff = time.time() - 3 * 86400
    state["signals"] = [s for s in state.get("signals", []) if s["ts"] > cutoff]
    state["sent"] = {k: v for k, v in state.get("sent", {}).items() if v > cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def already_sent(state, key):
    """Prevents duplicate texts if a run happens twice for the same candle."""
    if key in state.setdefault("sent", {}):
        return True
    state["sent"][key] = time.time()
    return False


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def send(text, detail=None, subject="Kraken"):
    """Short text to your phone (email-to-text) + a fuller backup email."""
    print("\n--- TEXT ---\n" + text)
    if detail:
        print("--- EMAIL DETAIL ---\n" + detail)
    if DRY_RUN:
        print("(dry run: nothing sent)")
        return
    if NTFY_TOPIC:  # push to the ntfy app on your phone
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=text.encode(),
            headers={"Title": subject, "Priority": "urgent" if subject.startswith(("TAKE", "SELL")) else "default", "Tags": "rotating_light"},
        )
        urllib.request.urlopen(req, timeout=20).read()
    if not (SMTP_USER and SMTP_PASS):
        return
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(SMTP_USER, SMTP_PASS)
        for to, body in ((TEXT_TO, text), (EMAIL_TO, (detail or text))):
            if not to:
                continue
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = SMTP_USER, to, subject
            m.set_content(body)
            s.send_message(m)


# ---------------------------------------------------------------------------
# The three jobs
# ---------------------------------------------------------------------------
def load_market():
    pmap = kraken_pair_map()
    data = {}
    for coin, code in pmap.items():
        try:
            info = analyze(candles(code))
            if info:
                data[coin] = info
        except Exception as e:
            print(f"skip {coin}: {e}", file=sys.stderr)
        time.sleep(0.6)  # stay under Kraken's public rate limit
    missing = sorted(set(COINS) - set(data))
    if missing:
        print("no data for:", ", ".join(missing), file=sys.stderr)
    return data


def btc_filter(data):
    b = data["BTC"]
    ok = b["close"] > b["ema200"] and b["ema50"] > b["ema200"]
    return ok, b


def exit_line(trade, info):
    """The higher of the starting exit and the 20-hour low. It never moves down."""
    line = max(trade["initial_stop"], info["trail_low"], trade.get("exit_line", 0))
    return line


def job_check(state):
    data = load_market()
    now = az_now()
    trades = state.setdefault("open_trades", {})

    # 1) SELLS first: did any open trade close below its exit line?
    for coin in list(trades):
        info = data.get(coin)
        if not info:
            continue
        t = trades[coin]
        line = exit_line(t, info)
        if info["close"] < line:
            if not already_sent(state, f"sell-{coin}-{info['time']}"):
                pnl = (info["close"] - t["entry"]) * t["qty"]
                send(
                    f"SELL {coin} now. 1h close {px(info['close'])} < exit {px(line)}. "
                    f"Approx P/L ${pnl:+.0f}. Sell > Max > swipe.",
                    f"{coin} closed the hour at {px(info['close'])}, below its exit line "
                    f"{px(line)}.\nEntry was {px(t['entry'])} for {t['qty']:.5f} {coin}.\n"
                    f"Approximate result: ${pnl:+.2f}.\nThe rule: sell on the hourly CLOSE "
                    f"below the exit line. Never tap 'Sell Everything'.",
                    subject=f"SELL {coin}",
                )
            if not DRY_RUN:
                del trades[coin]  # test runs never change your trade list
        else:
            # Ratchet the line up for next hour (it follows the 20-hour low).
            t["exit_line"] = max(line, info["trail_low_next"])

    # 2) BUYS: who broke out on the candle that just closed?
    ok, btc = btc_filter(data)
    if not ok:
        print(f"BTC filter FAIL ({px(btc['close'])}): no new buys this hour.")
        return
    for coin, info in sorted(data.items(), key=lambda kv: kv[0]):
        breakout = info["close"] > info["breakout_level"]
        uptrend = info["ema50"] > info["ema200"]
        if not (breakout and uptrend):
            continue
        entry = info["close"]
        stop = entry - ATR_MULT * info["atr"]
        stop_pct = (entry - stop) / entry * 100
        if stop <= 0 or not (MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT):
            continue
        qty = RISK_PER_TRADE / (entry - stop)
        if qty * entry > POSITION_CAP:
            qty = POSITION_CAP / entry
        dollars = qty * entry
        chase = entry + CHASE_FRACTION * (entry - stop)
        live = info["live_price"]
        if live > chase or live <= stop:  # price already ran away, or fell to the exit
            print(f"signal {coin} skipped: price moved ({px(live)})")
            continue
        state.setdefault("signals", []).append(
            {"ts": time.time(), "coin": coin, "price": entry}
        )

        # Your limits
        reason = None
        if coin in trades:
            reason = "already holding it"
        elif len(trades) >= MAX_OPEN_TRADES:
            reason = f"already at {MAX_OPEN_TRADES} trades"
        elif coin in MEME_COINS and any(c in MEME_COINS for c in trades):
            reason = "would be a 2nd meme coin"
        elif is_overnight(now) and not SEND_OVERNIGHT_TAKES:
            reason = "overnight"
        if reason:
            print(f"signal {coin} skipped: {reason}")
            continue
        if already_sent(state, f"take-{coin}-{info['time']}"):
            continue

        night = " (overnight - OK to skip)" if is_overnight(now) else ""
        send(
            f"TAKE {coin} @{px(entry)} Buy ${dollars:,.0f} ({qty:,.4g} {coin}). "
            f"Exit if 1h close < {px(stop)}. Skip if > {px(chase)}. Risk ${RISK_PER_TRADE:.0f}{night}",
            f"WHY: {coin} closed the hour at {px(entry)}, above its 55-hour high of "
            f"{px(info['breakout_level'])} (the breakout).\n"
            f"Coin trend: EMA50 {px(info['ema50'])} > EMA200 {px(info['ema200'])}.\n"
            f"BTC filter PASS: {px(btc['close'])} above EMA200 {px(btc['ema200'])}, EMA50 {px(btc['ema50'])} above EMA200.\n\n"
            f"EXIT: 2 x ATR ({px(info['atr'])}) below entry = {px(stop)}. Later the exit "
            f"follows the 20-hour low, but only upward.\n"
            f"SIZE: ${RISK_PER_TRADE:.0f} / ({px(entry)} - {px(stop)}) = {qty:,.5f} {coin} = ${dollars:,.2f}.\n"
            f"CHASE RULE: skip if price is already above {px(chase)}.\n"
            f"Open trades after this: {len(trades) + 1} of {MAX_OPEN_TRADES}.",
            subject=f"TAKE {coin}",
        )
        # Track it automatically (same as before). If you skip it, remove it
        # from state.json, or just ignore the later SELL text.
        if DRY_RUN:
            continue  # test runs never change your trade list
        trades[coin] = {
            "entry": entry, "qty": qty, "initial_stop": stop, "exit_line": stop,
            "opened": now.isoformat(timespec="minutes"),
        }


def job_headsup(state):
    data = load_market()
    ok, btc = btc_filter(data)
    if not ok:
        return
    trades = state.get("open_trades", {})
    if len(trades) >= MAX_OPEN_TRADES:
        return
    ready = []
    for coin, info in sorted(data.items()):
        if coin in trades or info["ema50"] <= info["ema200"]:
            continue
        if coin in MEME_COINS and any(c in MEME_COINS for c in trades):
            continue
        if info["live_price"] > info["breakout_level_live"]:
            ready.append(f"{coin} {px(info['live_price'])}>{px(info['breakout_level_live'])}")
    if ready:
        hour_key = az_now().strftime("%Y%m%d%H")
        if not already_sent(state, f"ready-{hour_key}"):
            top = ready[:4]
            more = f" +{len(ready) - 4} more" if len(ready) > 4 else ""
            send(
                "GET READY (don't buy yet): " + ", ".join(top) + more
                + ". TAKE only if it holds at the :00 close.",
                subject="GET READY",
            )


def job_morning(state):
    data = load_market()
    ok, btc = btc_filter(data)
    day_ago = time.time() - 86400
    n_sig = len([s for s in state.get("signals", []) if s["ts"] > day_ago])
    parts = [f"Kraken AM: BTC {'PASS' if ok else 'FAIL'} {px(btc['close'])}. {n_sig} signals 24h."]
    trades = state.get("open_trades", {})
    if not trades:
        parts.append("No open trades.")
    for coin, t in trades.items():
        info = data.get(coin)
        if not info:
            continue
        parts.append(f"HOLD {coin} {px(info['live_price'])} exit<{px(t.get('exit_line', t['initial_stop']))}")
    send(" ".join(parts), subject="Kraken AM")


# ---------------------------------------------------------------------------
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if mode == "auto":  # pick the job from the clock
        now = az_now()
        if now.hour == 7 and 25 <= now.minute <= 40:
            mode = "morning"
        elif now.minute >= 40:
            mode = "headsup"
        else:
            mode = "check"
    state = load_state()
    {"check": job_check, "headsup": job_headsup, "morning": job_morning}[mode](state)
    save_state(state)
    print(f"\n{mode} finished {az_now():%Y-%m-%d %I:%M %p} AZ {'(dry run)' if DRY_RUN else ''}")


if __name__ == "__main__":
    main()
