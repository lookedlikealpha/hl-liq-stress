# hl-liq-stress

**Stress-test a Hyperliquid short-carry trade against the token's _own_ worst recorded pump — using only HL's public API. No key, no account, no orders.**

A funding carry on a thin Hyperliquid token looks like free money: you short the perp, collect funding, maybe hold spot as a hedge. This tool checks the part the napkin math leaves out:

1. **Where your short _actually_ liquidates** — using the canonical HL formula, which liquidates **sooner** than the textbook number (a short's notional grows as price rises).
2. **What a real pump does to it** — it replays the token's worst run-up in the history HL still serves against a rules-based auto-margin defense, and shows that you "survive" only by **de-levering the short to a small fraction of the position** — i.e. you don't hold the carry through the pump, you abandon it.

It's read-only. It pulls max-leverage, mark, order-book depth, funding (paginated), and candles straight from `api.hyperliquid.xyz/info`.

## Install & run

```bash
pip install requests
python3 hl_liq_stress.py --coin PURR --notional 2000
```

(Single file, stdlib + `requests`. Python 3.8+. `--coin` is required.)

```
--coin PURR        HL perp ticker (required; e.g. PURR, HYPE)
--notional 2000    short notional in USD (default 2000)
--lev 3            test one leverage instead of the 1.5/2/2.5/3 sweep
--days 30          funding lookback window, paginated (default 30)
--halfspread 11    slippage half-spread bps (illustrative default)
--slip-k 0.94      slippage sqrt coefficient (illustrative default)
--self-test        run the math sanity checks and exit
```

## What it looks like (PURR, illustrative — numbers are live, re-run for current)

```
HL-LIQ-STRESS  PURR   maxLev 3x   maint-margin 0.167   mark $0.0841
book within 1%: bid $4,205 / ask $4,206   de-lever cap (0.5x thinner) $2,103
worst 15m-replayable run-up HL still serves: +132%  (entry-anchored at the pump's start)
CALM-month carry a $2,000 short RECEIVES at full notional: ~$49.89/mo  (realized funding, last ~30d)

  lev    liq@   survives the worst pump?  delevs   slip$  endpos%  verdict
  1.5  +42.9%             YES minMR 0.25       6    18.6      12%  un-harvestable: survives only by de-levering to 12%
  3.0  +14.3%             YES minMR 0.23       7    14.8       7%  un-harvestable: survives only by de-levering to 7%
```

Read the 3× row: it liquidates at a **+14.3%** move (not the napkin +16.7%). It "survives" the worst replayable pump — but only by **de-levering the short to ~7% of the position**, i.e. it abandons the carry. The carry it was risking that for is a thin, floor-level **~$50/mo on $2,000** — and to keep earning it you'd have to re-pay the spread to re-lever after every pump. The defense works by *unwinding*, not holding.

## The honest part (read this)

This tool is built to be conservative-flattering, and it still says it's a trap. Where it could mislead:

- **HL only keeps ~3.5 days of 1-minute candles.** The violent launch-era 1m pumps that would liquidate you in a *single candle* are already gone from the API. This replays the worst pump it can see at **15-minute** resolution — coarser, so it **flatters** the defense. Every "survives" is provisional.
- **`fundingHistory` is paginated** because HL caps it at 500 rows per call and returns the *oldest* rows first — a naive single call silently gives a stale slice and understates the carry. This walks the full window so the figure is recent.
- **The carry figure is the CALM-month, full-notional funding.** In a pump you de-lever (see `endpos%`), so you'd earn *less* than that — the point is that the carry is both small *and* un-holdable through a pump.
- **Liquidation point uses the base-tier maintenance margin** (`1/(2·maxLev)`), isolated, no extra collateral. Cross/extra collateral pushes it out; **large `--notional` can cross into a higher HL margin tier and liquidate sooner** — this tool does not model tiers.
- **Slippage is an illustrative square-root model** (`bps = halfspread + k·√usd`), not a venue-calibrated number; it folds taker fees into the half-spread. Override with `--halfspread` / `--slip-k` and see how sensitive the result is. The de-lever capacity gate uses the **perp** 1%-book depth; thin-token **spot** depth (often worse) is assumed comparable — if spot is thinner, real exits are harder than shown.

## Why it exists

Most "thin-token funding carry" takes look at the funding rate and the spot hedge and stop there. The trade lives or dies on a tail you can't hedge: in a pump your spot sits in the spot wallet and **does not margin your perp**, so the perp liquidates while your hedge is stranded — and even the auto-margin defense only saves you by unwinding the position you were paid to hold. Point it at any thin book and see for yourself.

Not advice. An educational risk tool. MIT licensed.
