"""
EA generation endpoint.

POST /ea/generate
  Body: { result_id, platform }
  Returns: { code, platform, filename }

Calls Claude claude-sonnet-4-6 to produce a complete, compilable MQL4/MQL5
Expert Advisor that replicates the backtest strategy exactly.
"""

import json
import os
from pathlib import Path

import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/ea")

RESULT_DIR = Path(__file__).parent.parent.parent / "result"


# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------

class EARequest(BaseModel):
    result_id: str
    platform: str  # "MT4" or "MT5"


class EAResponse(BaseModel):
    code: str
    platform: str
    filename: str
    prompt: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_prompt(strategy: str, params: dict, results: dict, platform: str) -> str:
    lang = "MQL4" if platform == "MT4" else "MQL5"
    mt_ver = platform[-1]  # "4" or "5"

    breakeven_r   = params.get("breakeven_r")
    risk_pct_pct  = float(params.get("risk_pct", 0.02)) * 100
    compounding   = (
        "Yes — use current account balance each trade"
        if params.get("compound")
        else "No — risk is fixed percentage of initial balance each trade"
    )
    order_api = "OrderSend/OrderClose" if platform == "MT4" else "CTrade class (trade.Buy / trade.Sell / trade.PositionClose)"

    # ---- Performance header ----
    perf = (
        f"  Total trades : {results.get('total_trades', 0)}\n"
        f"  Win rate     : {results.get('win_rate_pct', 0):.1f}%\n"
        f"  Profit factor: {results.get('profit_factor', 0):.3f}\n"
        f"  Total return : {results.get('total_return_pct', 0):.2f}%\n"
        f"  Max drawdown : {results.get('max_drawdown_pct', 0):.2f}%"
    )

    # ---- Parameter list ----
    param_lines = "\n".join(f"  {k} = {v}" for k, v in params.items())

    # ---- Break-even section (shared) ----
    breakeven_section = ""
    if breakeven_r:
        breakeven_section = (
            f"\n### Break-Even Stop\n"
            f"Once the trade profit reaches {breakeven_r}R (i.e. price moves {breakeven_r} × initial_sl_distance "
            f"in the trade's favour), move SL to the entry price. "
            f"Any subsequent SL hit closes at break-even (0 loss).\n"
        )

    # ---- Shared risk management block ----
    risk_block = f"""## Risk Management
- Risk per trade: {risk_pct_pct:.1f}% of account balance
- Lot size formula: (balance × risk_pct) / (sl_distance_in_price × contract_size)
  - XAUUSD contract_size = 100 oz/lot
  - sl_distance_in_price is the absolute price difference (e.g. 2.50 = $2.50/oz)
  - Clamp result to broker min/max lot and round to lot step
- Compounding: {compounding}
{breakeven_section}"""

    # ---- Shared code requirements ----
    code_req = f"""## Code Requirements
- All strategy parameters must be `input` variables visible in the EA settings dialog
- Calculate every indicator from scratch inside the EA — do NOT use iCustom or external DLLs
- The file must be fully self-contained and compile without errors in MetaEditor
- Include the backtest performance stats in a block comment at the top of the file
- Use correct {lang} syntax for MetaTrader {mt_ver}
- Order management: use {order_api}
- Spread handling: longs enter at Ask, shorts enter at Bid
- Guard against edge cases: insufficient bars, zero SL distance, broker stops level
- Add concise inline comments on every non-trivial logic block

Output ONLY the raw {lang} code. No markdown fences, no explanation text before or after."""

    # =========================================================================
    # Dispatch to strategy-specific prompt builder
    # =========================================================================
    if strategy == "momentum_candle":
        return _prompt_momentum_candle(
            params, lang, mt_ver,
            perf, param_lines, risk_block, code_req,
        )
    else:
        # Default: william_fractals
        return _prompt_william_fractals(
            params, lang, mt_ver,
            perf, param_lines, risk_block, code_req,
        )


# ---------------------------------------------------------------------------
# William Fractal Breakout prompt
# ---------------------------------------------------------------------------

def _sideways_filter_desc(params: dict) -> str:
    sideways = params.get("sideways_filter", "none")
    if sideways == "adx":
        return (
            f"ADX filter (Wilder's method):\n"
            f"  - Period: {params.get('adx_period', 14)}\n"
            f"  - Skip signal when ADX < {params.get('adx_threshold', 25.0)} (market is ranging)"
        )
    elif sideways == "ema_slope":
        p = params.get("ema_slope_period", 10)
        m = params.get("ema_slope_min", 0.5)
        return (
            f"EMA Slope filter:\n"
            f"  - slope = (ema[0] − ema[{p}]) / {p}  (price change per bar over {p} bars)\n"
            f"  - Skip signal when |slope| < {m} (EMA is too flat)"
        )
    elif sideways == "choppiness":
        return (
            f"Choppiness Index filter:\n"
            f"  - Period: {params.get('choppiness_period', 14)}\n"
            f"  - CI = 100 × log10(Σ ATR(1) over N) / (HH(N) − LL(N)) / log10(N)\n"
            f"  - Skip signal when CI >= {params.get('choppiness_max', 61.8)} (choppy/ranging market)"
        )
    elif sideways == "alligator":
        return (
            f"Williams Alligator filter (EWM approximation of SMMA):\n"
            f"  - Jaw   = SMMA({params.get('alligator_jaw', 13)}) on close\n"
            f"  - Teeth = SMMA({params.get('alligator_teeth', 8)}) on close\n"
            f"  - Lips  = SMMA({params.get('alligator_lips', 5)}) on close\n"
            f"  - BUY  allowed only when lips > teeth > jaw (uptrend alignment)\n"
            f"  - SELL allowed only when jaw > teeth > lips (downtrend alignment)\n"
            f"  - Skip when lines are tangled / crossing"
        )
    elif sideways == "stochrsi":
        return (
            f"Stochastic RSI filter:\n"
            f"  - RSI({params.get('stochrsi_rsi_period', 14)}) on close, then Stochastic over {params.get('stochrsi_stoch_period', 14)} bars\n"
            f"  - StochRSI = 100 × (RSI − lowest_RSI(N)) / (highest_RSI(N) − lowest_RSI(N))\n"
            f"  - BUY  allowed when StochRSI < {params.get('stochrsi_oversold', 20.0)} (oversold pullback)\n"
            f"  - SELL allowed when StochRSI > {params.get('stochrsi_overbought', 80.0)} (overbought pullback)"
        )
    else:
        return "None — all signals pass through without additional filtering"


def _prompt_william_fractals(
    params: dict, lang: str, mt_ver: str,
    perf: str, param_lines: str, risk_block: str, code_req: str,
) -> str:
    sideways     = params.get("sideways_filter", "none")
    filter_desc  = _sideways_filter_desc(params)
    rr           = params.get("rr_ratio", 1.5)
    ema_p        = params.get("ema_period", 200)
    fractal_n    = params.get("fractal_n", 9)

    return f"""You are an expert MetaTrader {mt_ver} developer. Generate a complete, compilable {lang} Expert Advisor implementing the strategy below exactly.

## Strategy: William Fractal Breakout
## Platform: MetaTrader {mt_ver} ({lang})
## Instrument: XAUUSD (Gold vs USD)
## Timeframe: {params.get('timeframe', 'H1')}

### Backtest Performance (reproduce verbatim in the EA's block-comment header)
{perf}

### Parameters (every item must be an `input` variable)
{param_lines}

---

## Entry Logic

### 1. EMA Trend Filter
- EMA({ema_p}) calculated on close prices
- close > EMA → uptrend   → only BUY signals allowed
- close < EMA → downtrend → only SELL signals allowed

### 2. William Fractals
- fractal_n = {fractal_n} (candles on EACH side of the centre bar — not half the window)
- TOP fractal    at bar i : high[i] > high[i+j] AND high[i] > high[i−j] for every j = 1..{fractal_n}
- BOTTOM fractal at bar i : low[i]  < low[i+j]  AND low[i]  < low[i−j]  for every j = 1..{fractal_n}
- CONFIRMED {fractal_n} bars after formation (shift by {fractal_n} to eliminate lookahead bias)
- Maintain `last_top` = price of the most recent confirmed top fractal (carry-forward when no new fractal)
- Maintain `last_bot` = price of the most recent confirmed bottom fractal (carry-forward)

### 3. Signal Conditions
Evaluated on the CLOSED bar; trade is opened at the NEXT bar's open.
- **BUY** : close > EMA  AND  close > last_top  AND  prev_close <= last_top  AND  sideways filter allows
- **SELL**: close < EMA  AND  close < last_bot  AND  prev_close >= last_bot  AND  sideways filter allows

### 4. Order Execution
- **BUY**  → enter at Ask (close + spread); SL = low of signal bar; TP = Ask + {rr} × (Ask − SL)
- **SELL** → enter at Bid;                  SL = high of signal bar; TP = Bid − {rr} × (SL − Bid)

### 5. One Position at a Time
Skip any new signal while a position is already open for this EA (match by magic number).

### 6. One Trade per Fractal Level
After an entry is triggered by `last_top` (buy) or `last_bot` (sell), store that price as `used_top` / `used_bot`.
Do NOT open another trade at the same level even if price crosses it again later.
A new entry is allowed only once `last_top` / `last_bot` has advanced to a different (newer) fractal price.

---

## Sideways Filter: {sideways}
{filter_desc}

---

{risk_block}

---

{code_req}"""


# ---------------------------------------------------------------------------
# Momentum Candle prompt
# ---------------------------------------------------------------------------

def _session_filter_desc(sessions: str) -> str:
    """Return the session filter section for the EA prompt."""
    if sessions == "all":
        return "### Session Filter\nNone — signals are generated at any hour of the day.\n"

    # Map session keys to UTC hour ranges (inclusive start, exclusive end)
    _ranges = {
        "asia":    (0,  9),
        "london":  (8,  17),
        "newyork": (13, 22),
    }
    _labels = {
        "asia":    "Asia    (00:00–08:59 UTC)",
        "london":  "London  (08:00–16:59 UTC)",
        "newyork": "New York (13:00–21:59 UTC)",
    }

    parts = sessions.split("_")
    active: list[str] = []
    hour_set: set[int] = set()
    for p in parts:
        if p in _ranges:
            active.append(_labels[p])
            s, e = _ranges[p]
            hour_set |= set(range(s, e))

    sorted_hours = sorted(hour_set)
    # Build contiguous ranges for the prompt
    ranges_str = _hours_to_ranges(sorted_hours)

    session_list = "\n".join(f"  - {lbl}" for lbl in active)
    return (
        f"### Session Filter\n"
        f"Only generate signals when bar[1] open time (UTC hour) falls inside the active sessions:\n"
        f"{session_list}\n\n"
        f"Active UTC hours: {ranges_str}\n\n"
        f"Implementation:\n"
        f"```\n"
        f"int bar_hour = (int)((iTime(_Symbol, inp_timeframe, 1) % 86400) / 3600);\n"
        f"bool in_session = {_hours_to_mql_condition(sorted_hours)};\n"
        f"if (!in_session) return;  // placed at the TOP of Step 3, before any MC detection\n"
        f"```\n"
        f"\n"
        f"Add `input string sessions = \"{sessions}\";` as a display-only label in the inputs section.\n"
    )


def _hours_to_ranges(hours: list[int]) -> str:
    """Convert a sorted list of hours into human-readable range strings."""
    if not hours:
        return ""
    ranges = []
    start = hours[0]
    prev  = hours[0]
    for h in hours[1:]:
        if h == prev + 1:
            prev = h
        else:
            ranges.append(f"{start:02d}:00–{prev:02d}:59")
            start = prev = h
    ranges.append(f"{start:02d}:00–{prev:02d}:59")
    return ", ".join(ranges)


def _hours_to_mql_condition(hours: list[int]) -> str:
    """Convert a sorted list of hours to a compact MQL boolean expression."""
    if not hours:
        return "false"
    # Build contiguous ranges
    ranges: list[tuple[int, int]] = []
    start = hours[0]
    prev  = hours[0]
    for h in hours[1:]:
        if h == prev + 1:
            prev = h
        else:
            ranges.append((start, prev))
            start = prev = h
    ranges.append((start, prev))

    parts = []
    for s, e in ranges:
        if s == e:
            parts.append(f"bar_hour == {s}")
        else:
            parts.append(f"(bar_hour >= {s} && bar_hour <= {e})")
    return " || ".join(parts)


def _prompt_momentum_candle(
    params: dict, lang: str, mt_ver: str,
    perf: str, param_lines: str, risk_block: str, code_req: str,
) -> str:
    ema_p            = params.get("ema_period", 200)
    body_ratio       = params.get("body_ratio_min", 0.70)
    vol_factor       = params.get("volume_factor", 1.5)
    vol_lookback     = params.get("volume_lookback", 23)
    retracement_pct  = params.get("retracement_pct", 0.50)
    sl_mult          = params.get("sl_mult", 1.0)
    tp_mult          = params.get("tp_mult", 1.0)
    max_pending_bars = int(params.get("max_pending_bars", 5))
    sessions         = params.get("sessions", "all")
    body_pct         = int(round(body_ratio * 100))
    session_section  = _session_filter_desc(sessions)

    return f"""You are an expert MetaTrader {mt_ver} developer. Generate a complete, compilable {lang} Expert Advisor implementing the strategy below exactly.

## Strategy: Momentum Candle Scalping
## Platform: MetaTrader {mt_ver} ({lang})
## Instrument: XAUUSD (Gold vs USD)
## Timeframe: {params.get('timeframe', 'H1')}

### Backtest Performance (reproduce verbatim in the EA's block-comment header)
{perf}

### Parameters (every item must be an `input` variable)
{param_lines}

---

## EA Architecture

The EA runs entirely inside `OnTick()` with a **new-bar guard**:
- Copy the current bar open time; compare with a stored `last_bar_time`
- If equal → return immediately (same bar, nothing to do)
- If different → store new `last_bar_time` and execute the logic below

All signal detection uses **bar index 1** (the bar that just closed).
Bar index 0 is the currently forming bar and must NEVER be used for signals.

---

## Required Global State Variables

```
int      g_ema_handle      // iMA indicator handle (created in OnInit)
datetime g_last_bar_time   // new-bar guard
ulong    g_pending_ticket  // ticket of the active limit order (0 = none)
int      g_pending_dir     // direction of the pending order: 1=buy, -1=sell, 0=none
double   g_mc_high         // high of the momentum candle that spawned the pending order
double   g_mc_low          // low  of the momentum candle that spawned the pending order
int      g_pending_bars    // bars elapsed since the limit order was placed
```

---

## OnTick Logic (execute in this exact order every new bar)

### Step 1 — Manage the existing pending limit order (if any)

Check whether `g_pending_ticket` is still in the pending orders list using `OrderSelect`.
- If `OrderSelect` fails: the order was filled by MT5 and is now a position → clear all g_ state variables (including `g_pending_bars`) and continue to Step 2
- If `OrderSelect` succeeds (order is still pending):
  1. Increment `g_pending_bars++`
  2. Check **cancel conditions** on bar[1] (the bar that just closed):
     - `g_pending_dir == 1`  and `close[1] > g_mc_high` → delete order, clear g_ state, **return**
     - `g_pending_dir == -1` and `close[1] < g_mc_low`  → delete order, clear g_ state, **return**
  3. Check **max_pending_bars expiry**: if `g_pending_bars >= {max_pending_bars}` → delete order, clear g_ state, **return**
  4. Neither condition → **return** (limit is still live; do not look for new signals)

> IMPORTANT: Do NOT manually check whether price touched the limit price.
> MT5 fills limit orders automatically when the market reaches the limit level.
> Your only job is to check the cancel/expiry conditions and delete if triggered.

### Step 2 — Skip if an open position exists

Iterate `PositionsTotal()` and check symbol + magic number.
If a position is open → return.

### Step 3 — Detect momentum candle on bar[1]

{session_section}
Copy arrays (ArraySetAsSeries = true, so index 0 = current forming bar, index 1 = last closed bar):
- `open[1], high[1], low[1], close[1]` — OHLC of the signal bar
- `tick_volume[1]` — tick volume of the signal bar
- `ema_buf[1]` — EMA({ema_p}) value on the signal bar
- `tick_volume[2..{vol_lookback+1}]` — prior closed bars for average volume (do NOT include bar[1] the signal bar itself, and do NOT include bar[0])

Copy `n = {vol_lookback} + 2` bars so that index `{vol_lookback} + 1` is accessible.

Compute:
```
range      = high[1] - low[1]
body_ratio = MathAbs(close[1] - open[1]) / range
avg_vol    = mean of tick_volume[2] through tick_volume[{vol_lookback+1}]   // {vol_lookback} PRIOR bars (excludes signal bar)
```

A bar is a momentum candle (MC) when:
- `body_ratio >= {body_ratio}`  ({body_pct}% of range is a strong directional body)
- `tick_volume[1] > avg_vol * {vol_factor}`  (volume spike confirms the move)

Signal direction:
- **Bullish MC**: `close[1] > open[1]`  AND  `close[1] > ema_buf[1]`  → BUY signal
- **Bearish MC**: `close[1] < open[1]`  AND  `close[1] < ema_buf[1]`  → SELL signal

If neither → return.

### Step 4 — Place the limit order

Store `mc_high = high[1]` and `mc_low = low[1]`.

**BUY LIMIT**
```
limit_price = NormalizeDouble(mc_high - {retracement_pct} * range, digits)
sl          = NormalizeDouble(mc_high - {sl_mult} * range, digits)   // {sl_mult}x range below mc_high
tp          = NormalizeDouble(mc_low  + {tp_mult} * range, digits)   // {tp_mult}x range above mc_low
sl_distance = limit_price - sl                                        // used for lot sizing
```

**SELL LIMIT**
```
limit_price = NormalizeDouble(mc_low  + {retracement_pct} * range, digits)
sl          = NormalizeDouble(mc_low  + {sl_mult} * range, digits)   // {sl_mult}x range above mc_low
tp          = NormalizeDouble(mc_high - {tp_mult} * range, digits)   // {tp_mult}x range below mc_high
sl_distance = sl - limit_price                                        // used for lot sizing
```

Validate before placing:
- `sl_distance > 0`
- `(limit_price - sl) > stops_level * point`   — SL is far enough from entry
- `(tp - limit_price) > stops_level * point`   — TP is far enough from entry (buy); invert signs for sell
- For BUY  limit: `limit_price < Ask` (buy limit must be below current market)
- For SELL limit: `limit_price > Bid` (sell limit must be above current market)

Place with `CTrade::BuyLimit` / `CTrade::SellLimit`:
- Use `ORDER_TIME_GTC` so the order persists until we explicitly delete it
- Set `ORDER_FILLING_RETURN` on the CTrade object in OnInit

After a successful placement:
```
g_pending_ticket = trade.ResultOrder()
g_pending_dir    = 1 or -1
g_mc_high        = mc_high
g_mc_low         = mc_low
g_pending_bars   = 0
```

### Step 5 — Lot sizing
```
lots = (AccountInfoDouble(ACCOUNT_BALANCE) * risk_pct) / (sl_distance * contract_size)
contract_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE)  // 100 for XAUUSD
```
Clamp to broker min/max lot and round to lot step.

---

## Limit Order Lifecycle Summary

```
Bar N   closes as momentum candle
         → place BuyLimit / SellLimit at limit_price
         → store g_mc_high, g_mc_low, g_pending_bars = 0

Bar N+1, N+2, …  (each new bar)
  MT5 auto-fills if market reaches limit_price  (you do NOT check this)
  You check:
    1. g_pending_bars++
    2. Cancel if close[1] > g_mc_high  (buy) or close[1] < g_mc_low  (sell)
    3. Cancel if g_pending_bars >= {max_pending_bars}  (order expired)
    Otherwise → keep order alive, return

Once filled, MT5 moves the order to positions.
  Next bar: OrderSelect fails → clear state → EA manages the live position via SL/TP set at placement.
```

---

{risk_block}

---

{code_req}"""


def _strip_fences(code: str) -> str:
    """Remove markdown code fences if Claude wrapped the output."""
    lines = code.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/generate", response_model=EAResponse)
def generate_ea(req: EARequest) -> EAResponse:
    platform = req.platform.upper()
    if platform not in ("MT4", "MT5"):
        raise HTTPException(400, "platform must be 'MT4' or 'MT5'")

    path = RESULT_DIR / f"{req.result_id}.json"
    if not path.exists():
        raise HTTPException(404, f"Result '{req.result_id}' not found")

    with open(path) as f:
        data = json.load(f)

    strategy = data.get("strategy", "")
    params = data.get("parameters", {})
    results = data.get("results", {})

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(500, "ANTHROPIC_API_KEY is not configured — add it to the .env file")

    client = anthropic.Anthropic(api_key=api_key)

    prompt = _build_prompt(strategy, params, results, platform)

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8192,
        system=(
            "You are an expert MetaTrader developer. "
            "You write production-quality, compilable MQL4 and MQL5 code. "
            "Output only raw source code — no markdown, no explanations."
        ),
        messages=[{"role": "user", "content": prompt}],
    )

    code = _strip_fences(message.content[0].text)

    ext = "mq4" if platform == "MT4" else "mq5"
    tf = str(params.get("timeframe", "H1"))
    strategy_slug = strategy.replace("_", "")
    filename = f"{strategy_slug}_{tf}_{platform}.{ext}"

    return EAResponse(code=code, platform=platform, filename=filename, prompt=prompt)
