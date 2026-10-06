# Kraken Trend v3 Checker

This runs the same hourly Trend v3 checks Computer has been doing for you. It runs on GitHub's free scheduler, so it uses no Perplexity credits.

| When (Arizona) | What it does | Alert in your ntfy app |
|---|---|---|
| :02 every hour | Reads the 1-hour candle that just closed. Checks open trades for exits, then looks for new breakouts. | `SELL ...` or `TAKE ...` |
| 7:30 AM | Morning recap | `Kraken AM: ...` |

It checks all 66 Kraken Funded coins straight from Kraken's free price data.

## The rules in plain English

1. **Breakout:** the hour closes above the highest price of the previous 55 hours.
2. **Coin uptrend:** the 50-hour average (EMA50) is above the 200-hour average (EMA200).
3. **Market filter:** BTC is above its EMA200, and its EMA50 is above its EMA200. If BTC fails this check, there are no buys at all.
4. **Starting exit:** entry price minus 2 × ATR. ATR is the size of a typical hourly candle.
5. **Trailing exit:** the lowest low of the last 20 hours. The exit line is whichever of the two is higher, and it only ever moves up.
6. **Sell** only when an hour closes below the exit line. Dips in the middle of an hour don't count.
7. **Size:** coins = $10 ÷ (entry − exit). A wide exit means you buy fewer coins, and a tight exit means you buy more. Either way the risk is $10.
8. **Chase rule:** skip the buy if the price has already moved up more than half the gap between the entry and the exit.
9. **Limits:** 4 trades at most, 1 trade per coin, and never 2 meme coins at once.

The backup email for every TAKE explains why the coin qualified, with the real numbers, so you can follow the reasoning trade by trade.

## How it runs

- Alerts go to the **ntfy** app on your phone. The topic name is stored as the hidden `NTFY_TOPIC` secret.
- GitHub's built-in schedule often runs late, so a "watcher" job stays awake for about 5.5 hours at a time and runs each check at the exact minute. The hourly schedule makes sure a new watcher always takes over.
- The repo is public so GitHub's run time is free and unlimited. Secrets (like the ntfy topic) are never visible to anyone.

## Updating your trades

The checker adds every TAKE it sends to `state.json` on its own, the same way the Computer checker did.

- **If you skip a TAKE:** either ignore its SELL text later, or delete that coin from `state.json`.
- **If you buy or sell outside a text:** edit `state.json` in GitHub using the pencil icon.

You can also send me a screenshot and I'll update the file for you.

## Good to know

- Settings at the top of the script, like `RISK_PER_TRADE`, `MAX_OPEN_TRADES`, and `SEND_OVERNIGHT_TAKES`, are easy to change.
- To pause everything: **Actions** tab → **Kraken Trend v3 checks** → **...** → **Disable workflow**.
