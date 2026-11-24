[![make.fun · u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/0xyfann/makefun-u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/0xyfann/makefun-u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

# Pump.fun Replay Sprint — Paper Bot, Real Ranking

A paper-trading bot for the `agent-fun/replay/v2` harness. It trades
10 SOL of paper money on a 24h replay of pump.fun bonding-curve activity
and is scored on net PnL after every fee.

## How it runs

The harness speaks newline-delimited JSON on stdin/stdout:

- `hello` — protocol, seed, universe, capital (10 SOL), caps (max 5 open
  positions, max order 100 bps of real reserves, max 16 orders per batch),
  latency (2 slots), fees.
- `batch` — one per slot carrying chain events (`TradeEvent` /
  `CreateEvent` / `CompleteEvent`), marks (current reserves for held and
  pending mints), fills (resolutions of earlier orders) and the account
  snapshot (cash + open positions).
- The bot answers every batch with one `reply` line echoing its `seq` and
  carrying its orders. Orders decided on slot `s` resolve at `s + 2`.
- `done` — the bot exits 0.

Entrypoint: `main.py`, run as `python3 -P -s main.py`. Stdlib only, no
network, no files, no randomness, no clock — the same input stream always
yields byte-identical replies (the grader runs the bot twice and the two
action logs must match).

## Strategy: buy quiet accumulators, sell the run

- **Entry.** A mint with at least 10 trades whose price still sits near
  its creation price (0.90x–1.30x), with at least 2 SOL of real reserves
  so a >= 0.02 SOL buy fits the 1% size cap. Size is `min(0.15 SOL, 1%
  of real reserves)`. One entry per mint.
- **Exits.** Take-profit at +50% over the entry price (bank the run);
  trailing stop at 30% below the peak since entry; a non-runner is sold
  after 12000 slots held below +25% of entry.
- **Risk.** At most 5 open positions, no shorts, no leverage, no new
  entries once equity falls under 75% of starting capital. A rejected
  order is not retried for the same mint for 50 slots. A mint that
  completes its bonding curve is closed by the harness at the graduation
  price — most graduates complete inside their creation slot and are
  untradable, so this strategy does not chase them.

## Why this shape

On the kit's dev window (1,222 mints, 24h) 87% of coins end within ±10%
of their creation price, ~23% touch 2x, ~12% touch 3x, and 35 graduate
at ~14.7x. The edge is in the few slow runners, but they cannot be
bought on the breakout: 27 of the 35 graduates create, spike to 14.7x
and complete inside a single slot, so a breakout buy only ever fills at
the spike top and the trailing stop then locks in a -25% loss (that
version graded -2.53 SOL). The coins that pay are the quiet accumulators
that grind up over thousands of slots, so the bot buys the base, banks
+50% when the run comes, and gives positions 12000 slots to develop
instead of cutting them at 6000 (the 6000-slot version graded -0.63 SOL;
this one -0.53 SOL on the same window, with 30 coins clearing the 0.01
SOL round-trip bar).

## Files

- `main.py` — the whole bot.
- `prompt.txt` — the exact prompt this entry was built from.
- `BUILD-NOTES.md` — build notes: kit contract, dev-window analysis,
  local grading results.

<sub>Entered on [make.fun](https://make.fun/?ref=p1x954ca&utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)</sub>
