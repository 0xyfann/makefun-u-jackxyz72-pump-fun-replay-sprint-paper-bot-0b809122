# BUILD-NOTES — Pump.fun Replay Sprint (paper bot)

Competition: `u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122`
("Pump.fun Replay Sprint — Paper Bot, Real Ranking"). Pot $2, escrowed
USDC, free to submit. Entries close 27 Nov 2026 11:50 WIB.
Harness: `pumpfun-replay/v2`.

## Kit contract (from RULES.txt / competition.yaml / harness source)

- Protocol `agent-fun/replay/v2`: NDJSON over stdin/stdout. The harness
  sends `hello` (protocol, seed, universe = program
  `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`, quote SOL, mayhem
  excluded, sample sha256 modulus 16 residue 15; capital 10 SOL; caps:
  max 5 open positions, max order 100 bps of real reserves, max 16
  orders per batch; no shorts, no leverage; latency 2 slots; fees:
  protocol 95 bps + creator 30 bps fallback, base 5000 + priority
  100000 lamports per order, 2% seeded failed-tx rate), then one
  `batch` per slot (universe events, marks, fills, account), then
  `done`. The bot replies one line per batch
  `{"type":"reply","seq":N,"orders":[...]}` and exits 0 on `done`.
- Orders decided on slot `s` resolve at `s+2` against that mint's last
  trade in the fill slot (post-trade reserves) or latest earlier
  reserves. Buys are exact-in, sells exact token amount. Every order
  pays base+priority tx fees. Rejections: batch size, duplicate
  client id, zero amount, not in universe, token complete, insufficient
  tokens, max open positions, max order size (buy <= 100 bps of
  decision-time real quote reserves per mint per reply; sell payout <=
  real quote reserves), zero fill, max slippage, insufficient cash.
  CompleteEvent closes the position at the last trade's reserves; at
  end of data all positions are flattened (or written off if proceeds
  would not cover tx fees). The bot's own trades shift reserves
  persistently. Deterministic: same (dataset, hello, replies) gives
  byte-identical results; the grader runs the bot twice and the two
  action logs must match.
- Scoring: final window = 24h of SOL-quoted, non-mayhem pump.fun
  bonding-curve events collected after entries close; places by net PnL
  (lamports), higher better; a prize needs net PnL > 0 SOL after every
  fee; a house bot in a paid place holds it. Minimum bar: a round trip
  of >= 0.01 SOL on each of >= 20 coins AND max drawdown <= 50%;
  entries missing it rank below every entry meeting it.
- Sandbox: 1 CPU, 1024 MB, 64 pids, wall 1800s. Runtime
  `python3 -P -s /bot/main.py` with PYTHONHASHSEED=0.
- The bot writes NOTHING to /output/ — the action log is captured from
  stdout by the harness. Transcript gate: thinking_reproduces >= 0.30,
  thinking_on_task terms hello/batch/orders/slot, >= 5 assistant turns,
  >= 2 file writes, transcripts_run_record required.

## Dev-window analysis (kit's frozen window, 2026-11-02T03:09Z–2026-11-03T03:09Z)

128,058 events, 1,222 mints, 63,369 slots with events; 1,222
CreateEvent, 126,801 TradeEvent (66,790 buys / 60,011 sells), 35
CompleteEvent. Peak multiple off creation: p50 1.24x, p75 1.95x, p90
3.58x, max 14.70x. Peak >= 1.5x: 479 (39.2%); >= 2x: 286 (23.4%);
>= 3x: 145 (11.9%). 1,064 of 1,222 mints end at 0.9–1.1x creation;
35 end at 5x+ (exactly the 35 graduates). 14.70x is the graduation
price (virtual SOL 30 -> ~115, tokens 1.073e9 -> ~280M). Median coin
has 12 trades; prices essentially never go far below creation.

Key structural findings (these drove the final strategy):

1. **27 of the 35 graduates create, spike to 14.7x and complete inside
   a single slot** (28 within <= 5 slots). A batch decision can never
   buy them: by the time the bot decides, the CompleteEvent is already
   in the same batch and `entry_signal` rejects the mint as complete.
   Any backtest that assumes buying a graduate at its 1.5x trade is
   wrong for this harness.
2. **The batch-end price is already the spike top.** The bot decides on
   the batch's last trade and fills 2 slots later, so breakout buying
   systematically buys the top and sells the reversion. Reconstructed
   v4 fills: median 1.66x creation, p90 2.48x; median round trip -26%.
3. **The 5-position cap and the 1% size cap bind.** Real reserves at a
   1.5x crossing are large (median ~8 SOL), so size is ~0.1 SOL, but
   the 5 slots fill fast and the equity guard (stop entering below 75%
   of start equity) is what stops the bleeding on losing strategies.
4. Non-graduate 1.5x-crossers (444) peak after crossing at median
   2.17x (p90 5.03x) and stay above 1.5x for a median of 24 slots —
   there is a tradable window, but only for coins bought near the base,
   not at the breakout.

## Strategy evolution (all graded locally on the dev window)

- **v1 — early-momentum sniper** (entry >= +12% + buy dominance,
  stop -22%, trailing 28% armed at +50%, time exit 4000 slots):
  net **-2.517 SOL**, 193 round trips, win 15.0%, max DD 26.8%.
  Entries chased the spike and sold the reversion.
- **v2 — earlier entry** (>= +5%, scale-out half at +40%, trailing
  25%, stop -30%, time exit 2500): net **-2.587 SOL**, 268 RTs, win
  20.9%, 9,030 rejected (retry spam on rejected orders — fixed in v3
  with a 50-slot rejection cooldown).
- **v3 — quiet accumulators** (0.90–1.30x, >= 10 trades, buy
  dominance, trailing 25%, time exit 6000): net **-0.626 SOL**, 203
  RTs, win 10.8%, max DD 7.4%, only 16 rejected. Big improvement, but
  the <= 1.30x band filters out the runners and the 6000-slot time
  exit cuts them early.
- **v4 — Rule A, buy the first 1.5x breakout** (trailing stop only,
  -25%, time exit 6000): net **-2.533 SOL**, 140 RTs, win 19.3%, max
  DD 26.8%, L1 reproduced. The offline rule backtest had promised
  +5.80 SOL — it was wrong because it ignored the batch boundary, the
  2-slot fill latency, the 5-position cap, the 1% size cap, and the
  single-slot graduates. Reconstructed fills prove it buys spike tops.
- **v5 (FINAL) — accumulators + take-profit + long hold** (entry
  0.90–1.30x, >= 10 trades; exit take-profit +50% over entry,
  trailing stop -30% from peak, time exit 12000 slots below +25%):
  net **-0.527 SOL**, 192 RTs, win 13.0%, max DD 5.8%, 30 coins with
  a >= 0.01 SOL round trip (bar needs >= 20), 25 winners / 167
  losers, median win +0.0168 SOL, median loss -0.0057 SOL, 0 rejected,
  9 failed txs, L1 reproduced. Fees 0.145 SOL curve + 0.041 SOL tx.

A batch-level simulator mirroring the engine (batch decisions, 2-slot
fills, completes, 5-slot cap, 1% size cap, fees, equity guard) was
built to rank variants fast; it reproduces the real grades closely
(v3 sim -0.648 vs real -0.626; v4 sim -2.609 vs real -2.533). It
ranked v5's parameter set best among ~30 variants tested (breakout
bands, take-profit 30–100%, trailing 15–50%, holds 2000–24000,
buy-dominance / inflow / gradual-rise filters, second-breakout
entries). The sim is optimistic for long-hold variants (v5 sim -0.163
vs real -0.527), but the ranking held: v5 grades better than v3 and
far better than v4 in reality.

## Local grading (final version, grade-local --driver process)

Command (Docker is unavailable on this VM, so the process driver is
used with a python3.13 shim pointing at python3.12):

```
PATH="$HOME/workspace/tmp/shims:$PATH" MAKEFUN_ALLOW_PROCESS_SANDBOX=1 \
TMPDIR=/home/hatch/workspace/tmp node cli/makefun.mjs grade-local \
--kit . --bot ~/workspace/makefun/micro-20261124/pump-fun-replay-sprint \
--worktree --driver process --save ~/workspace/tmp/grade-pump-sprint-v5-save --json
```

Exit 0. Net PnL -0.526642525 SOL (return -5.27%), max drawdown
5.8%, win rate 13.0%, 192 round trips / 380 fills / 0 rejected /
9 failed / 192 coins. Top 3 coins made 22% of gains. L1 regrade
reproduced byte for byte (determinism confirmed). Note: the local
result_sha256 cannot equal the server's (local harness v24.22.1 vs
server v26.13.1); the final grading runs in the server docker
sandbox on a fresh 24h window, so these numbers are a dev-window
calibration, not a prediction.

## Files

- `main.py` — the whole bot (stdlib only, integer arithmetic only, no
  randomness / clock / network / file I/O). Written in multiple chunks
  with revision edits.
- `README.md` — entry summary and strategy.
- `prompt.txt` — the exact prompt this entry was built from.
- `BUILD-NOTES.md` — this file.

Not submitted, no GitHub repo created — bundling/check/repo/submit is
the parent agent's step.
