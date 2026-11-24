#!/usr/bin/env python3
"""Pump.fun Replay Sprint - paper trading bot for agent-fun/replay/v2.

Protocol: newline-delimited JSON on stdin. The harness sends one hello
message, then one batch per slot, then done. The bot answers every batch
with one reply line on stdout: {"type": "reply", "seq": <batch seq>,
"orders": [...]}. No reply to hello; exit 0 after done (or on EOF).

The batch messages carry the slot, the chain events of that slot
(TradeEvent / CreateEvent / CompleteEvent), marks (current reserves for
held and pending mints), fills (resolutions of earlier orders) and the
account snapshot (cash + open positions). The orders the bot sends in a
reply are decided on that batch and resolve two slots later (latency),
so every decision is made on the latest reserves the batch shows.

Strategy: buy quiet accumulators, sell the run with a take-profit.
  * Entry: a mint with at least 10 trades whose price still sits near its
    creation price (0.90x-1.30x), with at least 2 SOL of real reserves so
    a >= 0.02 SOL buy fits the 1% of real reserves size cap. Size is
    min(0.15 SOL, 1% of real reserves). The dev window shows the big
    winners cannot be bought on the breakout itself: 27 of 35 graduates
    create, spike to 14.7x and complete inside a single slot, so a
    breakout buy only ever fills at the spike top and the trailing stop
    then locks in a -25% loss (v4 graded -2.53 SOL). The coins that pay
    are the slow accumulators that grind up over thousands of slots, so
    the bot buys the base and lets the run come to it.
  * Exit: take-profit at +50% over the entry price (bank the run), a
    trailing stop at 30% below the peak since entry (looser than v3's
    25% so runners are not cut short), and a time exit after 12000
    slots held below +25% so a non-runner stops occupying one of the 5
    position slots. The long hold is what makes this version work: on
    the dev window the runners need thousands of slots to develop, and
    cutting them at 6000 slots (v3) threw the edge away.
  * Hygiene: an order the harness rejects (slippage, size, cash) is not
    retried for the same mint for 50 slots, so a stale decision cannot
    spam the action log with retries.
  * Risk: at most 5 open positions, no shorts, no leverage, no new entries
    once equity falls under 75% of starting capital.
  * A mint that completes its bonding curve is closed by the harness at the
    graduation price; most graduates complete inside their creation slot
    and are untradable, so this strategy does not chase them.

Determinism: integer arithmetic only, no randomness, no clock, no network,
no file access. The same input stream always yields byte-identical replies.
Stdlib only.
"""

import json
import sys
from collections import deque

LAMPORTS_PER_SOL = 1_000_000_000

# --- tunables -----------------------------------------------------------
TARGET_BUY_LAMPORTS = 150_000_000       # 0.15 SOL per entry (before the 1% cap)
MIN_BUY_LAMPORTS = 20_000_000           # 0.02 SOL; clears the 0.01 SOL round-trip bar
MIN_REAL_RESERVES_LAMPORTS = 2_000_000_000  # 2 SOL of real reserves to enter
MAX_ENTRY_AGE_SLOTS = 7200
MIN_TRADES_TO_ENTER = 10
ENTRY_PRICE_MIN_BPS = 9000              # entry band: >= 0.90x creation price
ENTRY_PRICE_MAX_BPS = 13000             # entry band: <= 1.30x creation price
RECENT_WINDOW = 10
MAX_ENTRIES_PER_MINT = 1
REENTRY_COOLDOWN_SLOTS = 100
REJECT_COOLDOWN_SLOTS = 50             # do not retry a rejected order for 50 slots
TAKE_PROFIT_BPS = 15000                # sell at +50% over the entry price
TRAIL_BPS = 7000                        # sell on a 30% pullback from the peak
MAX_HOLD_SLOTS = 12000
TIME_EXIT_BPS = 12500                   # held > MAX_HOLD and < +25% -> exit
BUY_SLIPPAGE_BPS = 3000
SELL_SLIPPAGE_BPS = 1500
EQUITY_GUARD_BPS = 7500                 # stop entering below 75% of start equity


def gte_ratio(vq, vt, ref_vq, ref_vt, bps):
    """True when vq/vt >= (bps/10000) * (ref_vq/ref_vt). Integer math only."""
    return vq * ref_vt * 10000 >= bps * ref_vq * vt


def lte_ratio(vq, vt, ref_vq, ref_vt, bps):
    """True when vq/vt <= (bps/10000) * (ref_vq/ref_vt). Integer math only."""
    return vq * ref_vt * 10000 <= bps * ref_vq * vt


def gt_ratio(vq, vt, ref_vq, ref_vt, bps):
    """True when vq/vt > (bps/10000) * (ref_vq/ref_vt). Integer math only."""
    return vq * ref_vt * 10000 > bps * ref_vq * vt


class Bot:
    def __init__(self):
        # Config from hello (fallbacks match the competition's published values).
        self.start_cash = 10 * LAMPORTS_PER_SOL
        self.max_open_positions = 5
        self.max_order_bps = 100
        self.max_orders_per_batch = 16
        self.tx_fee = 105000
        # Per-mint market state, keyed by mint, insertion ordered (deterministic).
        self.mints = {}
        # Fresh mints (slot, mint) in creation order, pruned past the entry
        # age window: the re-scan for missed entries only walks these, never
        # the whole mint table, so per-batch work stays flat as the window grows.
        self.young = deque()
        # Open positions, keyed by mint: cost, tokens, entry_slot, peak, armed.
        self.held = {}
        # Orders sent but not yet resolved, keyed by mint -> client_id.
        self.pending_buy = {}
        self.pending_sell = {}
        self.order_counter = 0
        self.cash = self.start_cash

    def on_hello(self, msg):
        capital = msg.get("capital", {})
        try:
            self.start_cash = int(capital.get("lamports", self.start_cash))
        except (TypeError, ValueError):
            pass
        self.cash = self.start_cash
        caps = msg.get("caps", {})
        self.max_open_positions = int(caps.get("max_open_positions", 5))
        self.max_order_bps = int(caps.get("max_order_bps_of_real_reserve", 100))
        self.max_orders_per_batch = int(caps.get("max_orders_per_batch", 16))
        fees = msg.get("fees", {})
        try:
            self.tx_fee = int(fees.get("base_fee_lamports", 5000)) + int(
                fees.get("priority_fee_lamports", 100000)
            )
        except (TypeError, ValueError):
            pass

    # --- per-batch state updates -----------------------------------------
    def update_from_events(self, batch):
        """Fold this batch's chain events into per-mint state. Returns the
        mints that had a trade in this batch (entry candidates), in order."""
        slot = batch.get("slot", 0)
        touched = []
        for ev in batch.get("events") or []:
            kind = ev.get("event")
            mint = ev.get("mint")
            if not mint:
                continue
            st = self.mints.get(mint)
            if kind == "CreateEvent":
                if st is None:
                    try:
                        cvq = int(ev["virtual_sol_reserves"])
                        cvt = int(ev["virtual_token_reserves"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    self.mints[mint] = {
                        "create_slot": slot,
                        "create_vq": cvq,
                        "create_vt": cvt,
                        "last_vq": cvq,
                        "last_vt": cvt,
                        "last_rq": 0,
                        "last_slot": slot,
                        "trades": 0,
                        "recent": deque(maxlen=RECENT_WINDOW),
                        "entries": 0,
                        "last_exit_slot": -10**9,
                        "complete": False,
                        "buy_block_until": 0,
                        "sell_block_until": 0,
                    }
                    self.young.append((slot, mint))
            elif kind == "TradeEvent":
                if st is None:
                    # Defensive: a trade without a seen CreateEvent. Seed the
                    # state from the trade itself so the mint stays tradable.
                    try:
                        cvq = int(ev["virtual_sol_reserves"])
                        cvt = int(ev["virtual_token_reserves"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    st = {
                        "create_slot": slot,
                        "create_vq": cvq,
                        "create_vt": cvt,
                        "last_vq": cvq,
                        "last_vt": cvt,
                        "last_rq": 0,
                        "last_slot": slot,
                        "trades": 0,
                        "recent": deque(maxlen=RECENT_WINDOW),
                        "entries": 0,
                        "last_exit_slot": -10**9,
                        "complete": False,
                        "buy_block_until": 0,
                        "sell_block_until": 0,
                    }
                    self.mints[mint] = st
                    self.young.append((slot, mint))
                try:
                    vq = int(ev["virtual_sol_reserves"])
                    vt = int(ev["virtual_token_reserves"])
                    rq = int(ev["real_sol_reserves"])
                    sol = int(ev["sol_amount"])
                except (KeyError, TypeError, ValueError):
                    continue
                st["last_vq"] = vq
                st["last_vt"] = vt
                st["last_rq"] = rq
                st["last_slot"] = slot
                st["trades"] += 1
                st["recent"].append((bool(ev.get("is_buy")), sol))
                touched.append(mint)
            elif kind == "CompleteEvent":
                if st is not None:
                    st["complete"] = True
        return touched

    def process_fills(self, batch):
        """Clear pending orders the harness resolved; count filled buys.
        A rejected order blocks the same side on that mint for a while, so
        a stale decision cannot spam retries every batch."""
        slot = batch.get("slot", 0)
        for f in batch.get("fills") or []:
            cid = f.get("client_id")
            mint = f.get("mint")
            if not cid or not mint:
                continue
            st = self.mints.get(mint)
            if self.pending_buy.get(mint) == cid:
                del self.pending_buy[mint]
                if f.get("status") == "filled":
                    if st is not None:
                        st["entries"] += 1
                elif f.get("status") == "rejected" and st is not None:
                    st["buy_block_until"] = slot + REJECT_COOLDOWN_SLOTS
            if self.pending_sell.get(mint) == cid:
                del self.pending_sell[mint]
                if f.get("status") == "filled":
                    if st is not None:
                        st["last_exit_slot"] = slot
                elif f.get("status") == "rejected" and st is not None:
                    st["sell_block_until"] = slot + REJECT_COOLDOWN_SLOTS

    def reconcile_account(self, batch):
        """Sync open positions with the harness account snapshot. A position
        that appears gets a held record (entry basis = cost / tokens); a
        position that vanished without a sell fill (harness close on a
        CompleteEvent) clears the held record and starts the cooldown."""
        slot = batch.get("slot", 0)
        account = batch.get("account") or {}
        try:
            self.cash = int(account.get("cash_lamports", self.cash))
        except (TypeError, ValueError):
            pass
        live = {}
        for pos in account.get("positions") or []:
            mint = pos.get("mint")
            if not mint:
                continue
            try:
                tokens = int(pos["token_amount"])
                cost = int(pos["cost_lamports"])
            except (KeyError, TypeError, ValueError):
                continue
            if tokens <= 0:
                continue
            live[mint] = (cost, tokens)
        for mint in list(self.held):
            if mint not in live and mint not in self.pending_sell:
                # Closed by the harness (curve completed / flattened).
                del self.held[mint]
                st = self.mints.get(mint)
                if st is not None:
                    st["last_exit_slot"] = slot
        for mint, (cost, tokens) in live.items():
            if mint not in self.held:
                st = self.mints.get(mint)
                price = self.price_of(mint)
                self.held[mint] = {
                    "cost": cost,
                    "tokens": tokens,
                    "entry_slot": slot,
                    "peak_vq": price[0],
                    "peak_vt": price[1],
                }
            else:
                self.held[mint]["cost"] = cost
                self.held[mint]["tokens"] = tokens

    def price_of(self, mint):
        """Current (virtual quote, virtual token) reserves for a mint: the
        latest mark-style price the bot has seen for it."""
        st = self.mints.get(mint)
        if st is None:
            return (1, 1)
        return (st["last_vq"], st["last_vt"])

    def apply_marks(self, batch):
        """Marks carry the reserves the bot meets right now (its own impact
        included) for held and pending mints. They are the freshest price
        for exit decisions, so refresh the per-mint last price from them."""
        for mk in batch.get("marks") or []:
            mint = mk.get("mint")
            st = self.mints.get(mint)
            if st is None:
                continue
            try:
                vq = int(mk["virtual_quote_reserves"])
                vt = int(mk["virtual_token_reserves"])
                rq = int(mk["real_quote_reserves"])
            except (KeyError, TypeError, ValueError):
                continue
            if vt <= 0:
                continue
            st["last_vq"] = vq
            st["last_vt"] = vt
            st["last_rq"] = rq
            st["last_slot"] = batch.get("slot", st["last_slot"])

    # --- decisions --------------------------------------------------------
    def equity_lamports(self):
        """Cash plus open positions at current mark, fees haircut by 3%."""
        total = self.cash
        for mint, h in self.held.items():
            vq, vt = self.price_of(mint)
            if vt <= 0:
                continue
            total += (h["tokens"] * vq // vt) * 97 // 100
        return total

    def open_positions(self):
        return len(self.held)

    def entry_signal(self, mint, slot):
        """True when a mint qualifies for a fresh buy right now: a quiet
        accumulator - at least 10 trades, price still near creation
        (0.90x-1.30x), with enough real reserves for a minimum-size buy."""
        st = self.mints.get(mint)
        if st is None or st["complete"]:
            return False
        if mint in self.held or mint in self.pending_buy:
            return False
        if st["entries"] >= MAX_ENTRIES_PER_MINT:
            return False
        if slot < st["buy_block_until"]:
            return False
        if slot - st["last_exit_slot"] < REENTRY_COOLDOWN_SLOTS:
            return False
        if slot - st["create_slot"] > MAX_ENTRY_AGE_SLOTS:
            return False
        if st["trades"] < MIN_TRADES_TO_ENTER:
            return False
        if st["last_rq"] < MIN_REAL_RESERVES_LAMPORTS:
            return False
        # Price still near creation: not dumped, not spiked.
        if not gte_ratio(st["last_vq"], st["last_vt"],
                         st["create_vq"], st["create_vt"], ENTRY_PRICE_MIN_BPS):
            return False
        if not lte_ratio(st["last_vq"], st["last_vt"],
                         st["create_vq"], st["create_vt"], ENTRY_PRICE_MAX_BPS):
            return False
        return True

    def buy_size(self, mint):
        """min(target, 1% of real reserves), floored at the minimum buy."""
        st = self.mints[mint]
        cap = st["last_rq"] * self.max_order_bps // 10000
        size = TARGET_BUY_LAMPORTS if TARGET_BUY_LAMPORTS < cap else cap
        if size < MIN_BUY_LAMPORTS:
            return 0
        return size

    def decide_entries(self, candidates, slot, orders):
        """Buy qualifying fresh mints while position slots and cash allow."""
        if self.equity_lamports() * 10000 < EQUITY_GUARD_BPS * self.start_cash:
            return
        free = self.max_open_positions - self.open_positions() - len(self.pending_buy)
        if free <= 0:
            return
        for mint in candidates:
            if free <= 0 or len(orders) >= self.max_orders_per_batch:
                return
            if not self.entry_signal(mint, slot):
                continue
            size = self.buy_size(mint)
            if size <= 0:
                continue
            if self.cash < size + self.tx_fee:
                continue
            self.order_counter += 1
            cid = "e%d-%d" % (slot, self.order_counter)
            orders.append({
                "side": "buy",
                "client_id": cid,
                "mint": mint,
                "quote_lamports": str(size),
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            })
            self.pending_buy[mint] = cid
            self.cash -= size + self.tx_fee  # reserve it; fills reconcile later
            free -= 1

    def decide_exits(self, slot, orders, account_positions):
        """Sell held positions: take-profit at +50% over entry, trailing
        stop 30% below the peak since entry, or the time exit for
        non-runners. One pending sell per mint; full position each time."""
        for mint in sorted(self.held):
            if len(orders) >= self.max_orders_per_batch:
                return
            if mint in self.pending_sell:
                continue
            h = self.held[mint]
            st = self.mints.get(mint)
            if st is not None and slot < st["sell_block_until"]:
                continue
            tokens = account_positions.get(mint, 0)
            if tokens <= 0:
                continue
            vq, vt = self.price_of(mint)
            if vt <= 0:
                continue
            # Track the peak since entry.
            if gt_ratio(vq, vt, h["peak_vq"], h["peak_vt"], 10000):
                h["peak_vq"] = vq
                h["peak_vt"] = vt
            sell = False
            if gte_ratio(vq, vt, h["cost"], h["tokens"], TAKE_PROFIT_BPS):
                sell = True  # +50% over entry: bank the run
            elif lte_ratio(vq, vt, h["peak_vq"], h["peak_vt"], TRAIL_BPS):
                sell = True  # 30% pullback from the peak
            elif (slot - h["entry_slot"] > MAX_HOLD_SLOTS
                    and lte_ratio(vq, vt, h["cost"], h["tokens"], TIME_EXIT_BPS)):
                sell = True  # held too long without running
            if not sell:
                continue
            self.order_counter += 1
            cid = "x%d-%d" % (slot, self.order_counter)
            orders.append({
                "side": "sell",
                "client_id": cid,
                "mint": mint,
                "token_amount": str(tokens),
                "max_slippage_bps": SELL_SLIPPAGE_BPS,
            })
            self.pending_sell[mint] = cid

    def decide(self, batch, touched):
        """One batch in, one reply out: update state, then emit orders."""
        slot = batch.get("slot", 0)
        self.process_fills(batch)
        self.apply_marks(batch)
        self.reconcile_account(batch)
        account_positions = {}
        for pos in (batch.get("account") or {}).get("positions") or []:
            try:
                account_positions[pos["mint"]] = int(pos["token_amount"])
            except (KeyError, TypeError, ValueError):
                continue
        orders = []
        self.decide_exits(slot, orders, account_positions)
        # Entry candidates: mints traded in this batch, plus young mints
        # that qualified before but could not enter (no free slot / cash).
        # The young queue is pruned to the entry age window first.
        while self.young and slot - self.young[0][0] > MAX_ENTRY_AGE_SLOTS:
            self.young.popleft()
        candidates = list(dict.fromkeys(touched))
        seen = set(candidates)
        for _, mint in self.young:
            if mint in seen:
                continue
            st = self.mints.get(mint)
            if st is None or st["complete"] or st["trades"] < MIN_TRADES_TO_ENTER:
                continue
            if (gte_ratio(st["last_vq"], st["last_vt"],
                          st["create_vq"], st["create_vt"], ENTRY_PRICE_MIN_BPS)
                    and lte_ratio(st["last_vq"], st["last_vt"],
                                  st["create_vq"], st["create_vt"], ENTRY_PRICE_MAX_BPS)):
                candidates.append(mint)
                seen.add(mint)
        self.decide_entries(candidates, slot, orders)
        return {"type": "reply", "seq": batch.get("seq", 0), "orders": orders}

    # --- main loop ---------------------------------------------------------
    def run(self):
        inp = sys.stdin.buffer
        out = sys.stdout
        while True:
            line = inp.readline()
            if not line:
                return 0
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            if mtype == "hello":
                try:
                    self.on_hello(msg)
                except Exception:
                    pass
            elif mtype == "batch":
                try:
                    touched = self.update_from_events(msg)
                except Exception:
                    touched = []
                try:
                    reply = self.decide(msg, touched)
                except Exception:
                    reply = {"type": "reply", "seq": msg.get("seq", 0), "orders": []}
                out.write(json.dumps(reply, separators=(",", ":")) + "\n")
                out.flush()
            elif mtype == "done":
                return 0
            # Unknown message types are ignored; the lockstep continues.


def main():
    try:
        return Bot().run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
