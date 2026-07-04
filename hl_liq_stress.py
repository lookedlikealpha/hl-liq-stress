#!/usr/bin/env python3
"""
hl-liq-stress — stress-test a Hyperliquid short-carry trade against the token's OWN worst recorded pump.

A funding carry on a thin Hyperliquid token looks like free money. This tool checks the part the napkin
math ignores: where your short *actually* liquidates (canonical HL math, which is SOONER than the textbook
number), and whether a rules-based auto-margin defense survives the token's worst run-up in the price history
HL still serves — or only "survives" by **unwinding the very carry you were trying to harvest.**

Everything is read from Hyperliquid's PUBLIC info API. No API key, no account, no orders. Read-only.

  python3 hl_liq_stress.py --coin PURR --notional 2000
  python3 hl_liq_stress.py --coin HYPE --notional 2000 --lev 3

What you learn (and why the conclusion is "it's a trap", not "free money"):
  * the carry is usually SMALL (thin-token funding sits near HL's structural floor), and
  * to survive a routine pump the defense must DE-LEVER the short to a small fraction of the position —
    i.e. you don't hold the carry through the pump, you abandon it — and
  * the violent 1-minute pumps that would liquidate you OUTRIGHT have already aged out of HL's API.

Honest limitations (read these):
  * HL retains only ~3.5 days of 1-minute candles. This replays the worst pump it CAN see at 15-minute
    resolution — coarser, so it FLATTERS the defense. Every "survives" is provisional.
  * The liquidation point assumes an ISOLATED position, no extra collateral. Cross/extra collateral pushes
    it out; large size in a higher HL margin tier liquidates SOONER (this tool uses the base-tier mmr).
  * Slippage is an illustrative square-root model (a thin-book default; override with --halfspread/--slip-k).
    It folds taker fees into the half-spread rather than modeling them separately.

This is an educational risk tool, not advice. MIT licensed.
"""
import argparse, math, time, sys
try:
    import requests
except ImportError:
    sys.exit("This tool needs `requests`:  pip install requests")

API = "https://api.hyperliquid.xyz/info"

# --- defense params (sane defaults) ---
WARN_MR, MR_TARGET = 0.30, 0.40   # start defending below 0.30 margin-ratio; restore toward 0.40
CUSHION = 0.03                    # a "survives" verdict requires min margin-ratio stay >= maint + 0.03
LPOS_GRID = [1.5, 2.0, 2.5, 3.0]  # leverage settings swept by default
# Illustrative thin-book slippage: bps = halfspread + k*sqrt(order_usd). Symmetric across legs (a thin
# token's perp and spot books are both shallow); override at the CLI. NOT a venue-calibrated number.
DEFAULT_HALFSPREAD_BPS, DEFAULT_SLIP_K = 11.0, 0.94


def post(body):
    try:
        return requests.post(API, json=body, headers={"Content-Type": "application/json"}, timeout=25).json()
    except Exception as e:
        print(f"  ! API error: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Core math — the part most people get wrong
# ---------------------------------------------------------------------------
def mmr_of(maxlev):
    """Base-tier maintenance-margin fraction on HL: mmr = 1/(2*maxlev)."""
    return 1.0 / (2 * maxlev)


def x_liq(Lpos, mmr):
    """Liquidation mark / entry mark for a SHORT held at leverage Lpos.

    x_liq = (1/Lpos + 1) / (1 + mmr)

    A short's notional GROWS as price rises, so it liquidates SOONER than the naive (1/Lpos - mmr) move.
    Example: 3x short, mmr=1/6 -> x_liq=8/7=1.1429 -> a +14.29% move, not the napkin +16.67%.
    HL's own UI shows the correct ~14.3%; this is just *why*.
    """
    return (1.0 / Lpos + 1.0) / (1.0 + mmr)


def slip_bps(order_usd, halfspread, k):
    return halfspread + k * math.sqrt(max(order_usd, 1.0))


def simulate(path, Lpos, mmr, buffer0, notional0, depth_cap_usd, mark0, halfspread, k):
    """Run the auto-margin defense over a price path (one mark per poll). Returns survival + costs.

    Below WARN_MR, first top up from a USDC buffer; if exhausted, DE-LEVER (buy back part of the short +
    unwind the matching spot) to restore the margin ratio. The subtlety: de-levering a delta-neutral
    position must RAISE the margin ratio — you close `size` but keep the position's equity backing a smaller
    notional. (Naively scaling margin and size together leaves the ratio invariant and the defense does nothing.)
    """
    size = size_start = notional0 / mark0
    M = notional0 / Lpos                  # isolated USDC margin posted
    B = buffer0
    n_delever = n_topup = 0
    slip_usd = 0.0
    min_mr = 1.0
    survived = True
    book_too_thin = False
    for mark in path:
        equity = M + size * (mark0 - mark)   # short PnL = size*(entry - mark)
        notional = size * mark
        mr = equity / notional if notional > 0 else 0.0
        min_mr = min(min_mr, mr)
        if mr <= mmr:                        # liquidation: forfeit maintenance margin
            survived = False
            break
        if mr <= WARN_MR:
            need_equity = MR_TARGET * notional
            delta = need_equity - equity
            if delta <= B:                   # top up from the buffer (no slippage, keeps the hedge intact)
                M += delta
                B -= delta
                n_topup += 1
            else:                            # de-lever both legs by fraction f to restore MR_TARGET
                f = max(0.0, min(1.0, 1.0 - (equity / (MR_TARGET * notional))))
                clip_usd = f * notional
                if clip_usd > depth_cap_usd:
                    if clip_usd > 2.0 * depth_cap_usd:   # a single needed clip dwarfs the book -> can't exit cleanly
                        book_too_thin = True
                        survived = False
                        break
                    clip_usd = depth_cap_usd
                    f = clip_usd / notional
                cost = clip_usd * (slip_bps(clip_usd, halfspread, k) + slip_bps(clip_usd, halfspread, k)) / 1e4
                slip_usd += cost
                equity_after = equity - cost
                size *= (1 - f)              # reduce position; keep equity -> ratio rises toward MR_TARGET
                M = equity_after - size * (mark0 - mark)
                n_delever += 1
    return {"survived": survived, "min_mr": min_mr, "n_delever": n_delever, "n_topup": n_topup,
            "slip_usd": round(slip_usd, 2), "ending_frac": round(size / size_start, 3),
            "book_too_thin": book_too_thin}


def worst_window(cs, max_bars=672):
    """Find the worst run-up window; return (entry_price, path_of_highs, peak_fraction).

    Entry anchors at the START of the pump; the path walks candle HIGHS through the run-up (one bar = one
    defense reaction). The reported pump % is the PATH PEAK (max high / entry), i.e. exactly what the sim
    replays. 15m is the best resolution HL serves for a multi-day pump — coarser than a 60s poll would be
    (which would HELP the defense), so this is conservative-flattering.
    """
    if len(cs) < 2:
        return None, [], 0.0
    cl = [float(c["c"]) for c in cs]
    hi = [float(c["h"]) for c in cs]
    best = (0.0, 0, 0)
    for i in range(len(cl)):
        for j in range(i + 1, min(i + max_bars, len(cl))):
            r = (cl[j] - cl[i]) / cl[i]
            if r > best[0]:
                best = (r, i, j)
    _, i, j = best
    entry = cl[i]
    path = [entry] + hi[i:j + 1]
    peak = (max(path) - entry) / entry      # the actual replayed peak (>= the close-to-close figure)
    return entry, path, peak


# ---------------------------------------------------------------------------
# Live public-API data (no key)
# ---------------------------------------------------------------------------
def get_universe():
    r = post({"type": "meta"})
    if not r or "universe" not in r:
        return {}
    return {u["name"]: u for u in r["universe"]}


def get_mark(coin):
    r = post({"type": "allMids"})
    if not r or coin not in r:
        return None
    return float(r[coin])


def get_funding(coin, days):
    """Realized hourly funding over the full `days` window, PAGINATED (fundingHistory caps at 500 rows and
    returns the OLDEST 500 from startTime, so a single call would silently give a stale slice). Returns the
    list of hourly fractions (short RECEIVES when positive)."""
    end = int(time.time() * 1000)
    out, cur = {}, end - days * 86400 * 1000
    for _ in range(30):
        r = post({"type": "fundingHistory", "coin": coin, "startTime": cur, "endTime": end})
        if not isinstance(r, list) or not r:
            break
        before = len(out)
        for x in r:
            out[int(x["time"])] = float(x["fundingRate"])
        cur = int(r[-1]["time"]) + 1
        if len(r) < 500 or len(out) == before:   # last (short) page, or no new rows -> done
            break
        time.sleep(0.1)
    return [out[k] for k in sorted(out)]


def get_depth_1pct(coin):
    """(bid_usd, ask_usd) resting within 1% of mid on each side of the perp book."""
    r = post({"type": "l2Book", "coin": coin})   # l2Book takes coin at the TOP level (not nested in "req")
    if not r or "levels" not in r or len(r["levels"]) < 2:
        return 0.0, 0.0
    bids, asks = r["levels"][0], r["levels"][1]
    if not bids or not asks:
        return 0.0, 0.0
    mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2.0
    def side_usd(book):
        return sum(float(l["px"]) * float(l["sz"]) for l in book if abs(float(l["px"]) - mid) / mid <= 0.01)
    return side_usd(bids), side_usd(asks)


def candles(coin, interval, lookback_ms):
    end = int(time.time() * 1000)
    out, cur = {}, end - lookback_ms
    for _ in range(40):
        r = post({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": cur, "endTime": end}})
        if not isinstance(r, list) or not r:
            break
        before = len(out)
        for cd in r:
            out[int(cd["t"])] = cd
        cur = int(r[-1]["t"]) + 1
        if len(out) == before:    # no new candles -> end of data (robust to page-size changes)
            break
        time.sleep(0.15)
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------------------
def self_test():
    """Sanity checks on the math (run with --self-test)."""
    assert abs((x_liq(3.0, mmr_of(3)) - 1.0) - 0.1429) < 0.0005, "canonical liq formula broken"
    # a working de-lever RAISES the margin ratio, so a defended 3x short survives a gentle +25% ramp past its
    # undefended +14.3% liq point — and ends with a SMALLER position (it survived by unwinding, not holding).
    ramp = [1.0 + 0.01 * i for i in range(26)]
    r = simulate(ramp, 3.0, mmr_of(3), 0.0, 300.0, 1e12, 1.0, DEFAULT_HALFSPREAD_BPS, DEFAULT_SLIP_K)
    assert r["survived"] and r["min_mr"] >= mmr_of(3), "de-lever no longer defends margin ratio"
    assert r["ending_frac"] < 1.0, "de-lever should shrink the position"
    print("self-test OK")


def run(coin, notional, only_lev, days, halfspread, k):
    uni = get_universe()
    if coin not in uni:
        sys.exit(f"'{coin}' is not a Hyperliquid perp (check the ticker; e.g. PURR, HYPE).")
    maxlev = int(uni[coin].get("maxLeverage", 0)) or None
    if not maxlev:
        sys.exit(f"could not read maxLeverage for {coin}")
    mark0 = get_mark(coin)
    if not mark0:
        sys.exit(f"could not read a mark price for {coin}")
    mmr = mmr_of(maxlev)
    bid_usd, ask_usd = get_depth_1pct(coin)
    depth_cap = 0.5 * min(bid_usd, ask_usd)        # conservative: half the thinner side within 1%
    fr = get_funding(coin, days)
    span_h = len(fr)
    carry_mo = (sum(fr) * 720.0 / span_h) * notional if span_h else 0.0   # full-notional funding per ~30d

    c15 = candles(coin, "15m", 46 * 86400 * 1000)
    entry, path, peak = worst_window(c15)
    if entry is None:
        sys.exit(f"no candle history for {coin}")

    print(f"\nHL-LIQ-STRESS  {coin}   maxLev {maxlev}x   maint-margin {mmr:.3f}   mark ${mark0:g}")
    print(f"book within 1%: bid ${bid_usd:,.0f} / ask ${ask_usd:,.0f}   de-lever cap (0.5x thinner) ${depth_cap:,.0f}")
    print(f"worst 15m-replayable run-up HL still serves: +{peak*100:.0f}%  (entry-anchored at the pump's start)")
    print(f"CALM-month carry a ${notional:,.0f} short RECEIVES at full notional: ~${carry_mo:,.2f}/mo  "
          f"(realized funding, last ~{span_h/24:.0f}d)\n")
    levs = [only_lev] if only_lev else LPOS_GRID
    print(f"  {'lev':>4} {'liq@':>7} {'survives the worst pump?':>26} {'delevs':>7} {'slip$':>7} {'endpos%':>8}  verdict")
    for Lpos in levs:
        headroom = x_liq(Lpos, mmr) - 1.0
        buffer0 = notional * 0.15
        res = simulate(path, Lpos, mmr, buffer0, notional, depth_cap if depth_cap > 0 else 1e9, entry, halfspread, k)
        if not res["survived"] and res["book_too_thin"]:
            verdict = "FAIL: book too thin to exit"
        elif not res["survived"]:
            verdict = "LIQUIDATED"
        elif res["min_mr"] < mmr + CUSHION:
            verdict = f"survives* but thin cushion; un-harvestable (de-levers to {res['ending_frac']*100:.0f}%)"
        else:
            verdict = f"un-harvestable: survives only by de-levering to {res['ending_frac']*100:.0f}%"
        surv = ("YES" if res["survived"] else "NO") + f" minMR {res['min_mr']:.2f}"
        print(f"  {Lpos:>4.1f} {'+'+format(headroom*100,'.1f')+'%':>7} {surv:>26} {res['n_delever']:>7} "
              f"{res['slip_usd']:>7.1f} {res['ending_frac']*100:>7.0f}%  {verdict}")
    print(f"\nRead it: the calm-month carry is small (~${carry_mo:,.0f} on ${notional:,.0f}), and to survive the"
          f"\n  worst replayable pump the short de-levers to the 'endpos%' above — i.e. you DON'T hold the carry"
          f"\n  through a pump, you abandon it (then must re-pay the spread to re-lever). You're earning a thin,"
          f"\n  floor-level carry in exchange for a tail you can't hold through.")
    print(f"\n* PROVISIONAL: replayed at 15-minute resolution. The 1-minute intra-bar profile that could liquidate"
          f"\n  you in a single candle is already aged out of HL's API (only ~3.5d of 1m is retained) — so a 'survives'"
          f"\n  here is the optimistic case, not a guarantee.\n")


def main():
    ap = argparse.ArgumentParser(description="Stress-test an HL short-carry against the token's own worst pump (public API, read-only).")
    ap.add_argument("--coin", help="HL perp ticker, e.g. PURR or HYPE (required unless --self-test)")
    ap.add_argument("--notional", type=float, default=2000.0, help="short notional in USD (default 2000)")
    ap.add_argument("--lev", type=float, default=None, help="test a single leverage instead of the 1.5/2/2.5/3 sweep")
    ap.add_argument("--days", type=int, default=30, help="funding lookback window in days (default 30, paginated)")
    ap.add_argument("--halfspread", type=float, default=DEFAULT_HALFSPREAD_BPS, help="slippage half-spread bps (illustrative default 11)")
    ap.add_argument("--slip-k", type=float, default=DEFAULT_SLIP_K, dest="slip_k", help="slippage sqrt coefficient (illustrative default 0.94)")
    ap.add_argument("--self-test", action="store_true", help="run the math sanity checks and exit")
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return
    if not a.coin:
        ap.error("--coin is required (e.g. --coin PURR), unless you pass --self-test")
    run(a.coin, a.notional, a.lev, a.days, a.halfspread, a.slip_k)


if __name__ == "__main__":
    main()
