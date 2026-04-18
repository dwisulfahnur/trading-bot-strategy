"""
EA generation endpoint.

POST /ea/generate
  Body: { result_id, platform }
  Returns: { code, platform, filename }
"""

import json
import os
from pathlib import Path

import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/ea")

RESULT_DIR = Path(__file__).parent.parent.parent / "result"


class EARequest(BaseModel):
    result_id: str
    platform: str  # "MT4" or "MT5"


class EAResponse(BaseModel):
    code: str
    platform: str
    filename: str
    prompt: str


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _perf_block(results: dict) -> str:
    return (
        f"  Total trades : {results.get('total_trades', 0)}\n"
        f"  Win rate     : {results.get('win_rate_pct', 0):.1f}%\n"
        f"  Profit factor: {results.get('profit_factor', 0):.3f}\n"
        f"  Total return : {results.get('total_return_pct', 0):.2f}%\n"
        f"  Max drawdown : {results.get('max_drawdown_pct', 0):.2f}%"
    )


def _param_lines(params: dict) -> str:
    return "\n".join(f"  {k} = {v}" for k, v in params.items())


def _breakeven_section(breakeven_r) -> str:
    if not breakeven_r:
        return ""
    return (
        f"\n### Break-Even Stop\n"
        f"Once profit reaches {breakeven_r}R (price moves {breakeven_r} × initial_sl_distance in trade's favour), "
        f"move SL to entry price. Subsequent SL hit closes at break-even (0 loss).\n"
    )


def _sl_limit_section(max_sl: int | None, sl_period: str) -> str:
    if not max_sl or not sl_period or sl_period == "none":
        return ""

    if sl_period == "day":
        period_start_code = (
            "MqlDateTime dt;\n"
            "    TimeToStruct(TimeCurrent(), dt);\n"
            "    dt.hour = 0; dt.min = 0; dt.sec = 0;\n"
            "    return StructToTime(dt);"
        )
    elif sl_period == "week":
        period_start_code = (
            "MqlDateTime dt;\n"
            "    TimeToStruct(TimeCurrent(), dt);\n"
            "    int days_back = (dt.day_of_week == 0) ? 6 : dt.day_of_week - 1; // roll to Monday\n"
            "    datetime monday = TimeCurrent() - (datetime)(days_back * 86400);\n"
            "    TimeToStruct(monday, dt);\n"
            "    dt.hour = 0; dt.min = 0; dt.sec = 0;\n"
            "    return StructToTime(dt);"
        )
    else:  # month
        period_start_code = (
            "MqlDateTime dt;\n"
            "    TimeToStruct(TimeCurrent(), dt);\n"
            "    dt.day = 1; dt.hour = 0; dt.min = 0; dt.sec = 0;\n"
            "    return StructToTime(dt);"
        )

    return f"""
### Period Stop-Loss Limit
Do NOT open new trades for the rest of the current {sl_period} once {max_sl} stop-loss exit(s) have been recorded.
The counter resets automatically at the start of each new {sl_period}.

Implement two helper functions:

```
datetime GetPeriodStart() {{
    {period_start_code}
}}

int CountPeriodSL(int magic) {{
    int count = 0;
    HistorySelect(GetPeriodStart(), TimeCurrent());
    int n = HistoryDealsTotal();
    for(int i = 0; i < n; i++) {{
        ulong ticket = HistoryDealGetTicket(i);
        if(HistoryDealGetInteger(ticket, DEAL_MAGIC) != magic) continue;
        if(HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol)  continue;
        if((ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
        if((ENUM_DEAL_REASON)HistoryDealGetInteger(ticket, DEAL_REASON) == DEAL_REASON_SL) count++;
    }}
    return count;
}}
```

Call `CountPeriodSL(InpMagicNumber)` before placing any order. Skip if count >= {max_sl}.
"""


def _risk_block(params: dict, lang: str) -> str:
    risk_pct_pct = float(params.get("risk_pct", 0.02)) * 100
    compound = params.get("compound", False)
    compound_label = (
        "Yes — recalculate from current account balance each trade"
        if compound
        else "No — always use fixed initial balance"
    )
    breakeven_r  = params.get("breakeven_r")
    max_sl       = params.get("max_sl_per_period")
    sl_period    = params.get("sl_period", "none")

    return (
        f"## Risk Management\n"
        f"- Risk per trade: {risk_pct_pct:.1f}% of account balance\n"
        f"- Lot size: `(balance × risk_pct) / (sl_distance × contract_size)`\n"
        f"  - XAUUSD contract_size = 100 oz/lot\n"
        f"  - sl_distance = absolute price difference of entry to SL\n"
        f"  - Clamp to broker min/max lot, round to lot step\n"
        f"- Compounding: {compound_label}\n"
        + _breakeven_section(breakeven_r)
        + _sl_limit_section(max_sl, sl_period)
    )


def _code_req(lang: str, mt_ver: str) -> str:
    order_api = (
        "OrderSend / OrderClose" if mt_ver == "4"
        else "CTrade class (trade.Buy, trade.Sell, trade.BuyLimit, trade.SellLimit, trade.PositionClose)"
    )
    return (
        f"## Code Requirements\n"
        f"- All parameters must be `input` variables visible in the EA settings dialog\n"
        f"- Group inputs by purpose using `input group` (MQL5) or string separator inputs (MQL4)\n"
        f"- Calculate every indicator from scratch inside the EA — no iCustom, no external DLLs\n"
        f"- Fully self-contained; compiles without errors in MetaEditor\n"
        f"- Place the backtest performance stats in a block comment at the top of the file\n"
        f"- Use correct {lang} syntax for MetaTrader {mt_ver}\n"
        f"- Order management: {order_api}\n"
        f"- Spread: longs enter at Ask, shorts enter at Bid\n"
        f"- Guard against edge cases: insufficient bars, zero SL distance, broker stops level\n"
        f"- Add concise inline comments on every non-trivial logic block\n\n"
        f"Output ONLY the raw {lang} code. No markdown fences, no explanation text."
    )


# ---------------------------------------------------------------------------
# Session filter helpers
# ---------------------------------------------------------------------------

_SESSION_HOURS: dict[str, tuple[int, int]] = {
    "asia":    (0, 8),
    "london":  (8, 16),
    "newyork": (13, 21),
}

_SESSION_LABELS: dict[str, str] = {
    "asia":    "Asia    (00:00–08:59 UTC)",
    "london":  "London  (08:00–16:59 UTC)",
    "newyork": "New York (13:00–21:59 UTC)",
}


def _session_filter_section(sessions: str) -> str:
    if sessions == "all":
        return "**Session Filter:** None — signals generated at any hour.\n"

    parts = [p for p in sessions.split("_") if p in _SESSION_HOURS]
    hour_set: set[int] = set()
    for p in parts:
        s, e = _SESSION_HOURS[p]
        hour_set |= set(range(s, e + 1))

    sorted_hours = sorted(hour_set)
    label_list = "\n".join(f"  - {_SESSION_LABELS[p]}" for p in parts)

    # Build MQL condition
    ranges: list[tuple[int, int]] = []
    start = sorted_hours[0]; prev = sorted_hours[0]
    for h in sorted_hours[1:]:
        if h == prev + 1:
            prev = h
        else:
            ranges.append((start, prev)); start = prev = h
    ranges.append((start, prev))

    cond_parts = []
    for s, e in ranges:
        cond_parts.append(f"bar_hour == {s}" if s == e else f"(bar_hour >= {s} && bar_hour <= {e})")
    condition = " || ".join(cond_parts)

    return (
        f"**Session Filter:** Only generate signals when the signal bar's UTC open hour is inside:\n"
        f"{label_list}\n\n"
        f"```\n"
        f"int bar_hour = (int)((iTime(_Symbol, PERIOD_CURRENT, 1) % 86400) / 3600);\n"
        f"bool in_session = {condition};\n"
        f"if (!in_session) return;\n"
        f"```\n"
    )


# ---------------------------------------------------------------------------
# Sideways filter description
# ---------------------------------------------------------------------------

def _sideways_filter_desc(params: dict) -> str:
    sideways = params.get("sideways_filter", "none")
    if sideways == "adx":
        return (
            f"**ADX filter** (Wilder's smoothing, alpha = 1 / {params.get('adx_period', 14)}):\n"
            f"- Compute TR, +DM, -DM then smooth with EWM(alpha=1/period)\n"
            f"- DX = 100 × |+DI − −DI| / (+DI + −DI);  ADX = EWM of DX\n"
            f"- Skip signal when ADX < {params.get('adx_threshold', 25.0)} (market is ranging)"
        )
    elif sideways == "ema_slope":
        p = params.get("ema_slope_period", 10)
        m = params.get("ema_slope_min", 0.5)
        return (
            f"**EMA Slope filter:**\n"
            f"- slope = (ema[1] − ema[{p + 1}]) / {p}  (uses trend EMA values)\n"
            f"- Skip signal when |slope| < {m}"
        )
    elif sideways == "choppiness":
        per = params.get("choppiness_period", 14)
        return (
            f"**Choppiness Index filter** (period {per}):\n"
            f"- CI = 100 × log10(Σ TR(1) over {per}) / (HH({per}) − LL({per})) / log10({per})\n"
            f"- Skip signal when CI >= {params.get('choppiness_max', 61.8)}"
        )
    elif sideways == "alligator":
        return (
            f"**Williams Alligator filter** (SMMA approximated via EWM, alpha = 1/period):\n"
            f"- Jaw   = SMMA({params.get('alligator_jaw', 13)}) on close\n"
            f"- Teeth = SMMA({params.get('alligator_teeth', 8)}) on close\n"
            f"- Lips  = SMMA({params.get('alligator_lips', 5)}) on close\n"
            f"- BUY  only when lips > teeth > jaw\n"
            f"- SELL only when jaw > teeth > lips\n"
            f"- Skip when lines are tangled"
        )
    elif sideways == "stochrsi":
        rp  = params.get("stochrsi_rsi_period", 14)
        sp  = params.get("stochrsi_stoch_period", 14)
        ov  = params.get("stochrsi_oversold", 20.0)
        ob  = params.get("stochrsi_overbought", 80.0)
        return (
            f"**Stochastic RSI filter:**\n"
            f"- RSI({rp}) using EWM(alpha=1/{rp}) for avg gain/loss (Wilder's method)\n"
            f"- StochRSI = 100 × (RSI − lowest_RSI({sp})) / (highest_RSI({sp}) − lowest_RSI({sp}))\n"
            f"- BUY  only when StochRSI < {ov} (oversold pullback)\n"
            f"- SELL only when StochRSI > {ob} (overbought pullback)"
        )
    else:
        return "**Sideways Filter:** None — all signals pass without additional filtering."


# ---------------------------------------------------------------------------
# William Fractal Breakout prompt
# ---------------------------------------------------------------------------

def _prompt_william_fractals(
    params: dict, lang: str, mt_ver: str,
    perf: str, param_lines_str: str,
) -> str:
    rr           = params.get("rr_ratio", 1.5)
    ema_p        = params.get("ema_period", 200)
    fractal_n    = params.get("fractal_n", 9)
    sessions     = params.get("sessions", "all")
    mc_on        = bool(params.get("momentum_candle_filter", False))
    mc_body      = params.get("mc_body_ratio_min", 0.6)
    mc_vol_fac   = params.get("mc_volume_factor", 1.5)
    mc_vol_lb    = int(params.get("mc_volume_lookback", 20))
    sideways     = params.get("sideways_filter", "none")
    filter_desc  = _sideways_filter_desc(params)
    session_sec  = _session_filter_section(sessions)

    mc_section = ""
    if mc_on:
        mc_section = f"""
### 5. Momentum Candle Gate
The signal bar (bar[1]) must also qualify as a momentum candle:
- Bullish MC (for BUY): close[1] > open[1], body_ratio >= {mc_body}, tick_volume > avg × {mc_vol_fac}
- Bearish MC (for SELL): close[1] < open[1], body_ratio >= {mc_body}, tick_volume > avg × {mc_vol_fac}
- `body_ratio = |close[1] - open[1]| / (high[1] - low[1])`
- `avg` = mean of tick_volume[2] through tick_volume[{mc_vol_lb + 1}] ({mc_vol_lb} prior bars, excludes signal bar)
Skip signal if the momentum candle condition is not met.
"""

    return f"""## Strategy: William Fractal Breakout
## Platform: MetaTrader {mt_ver} ({lang})
## Instrument: XAUUSD
## Timeframe: {params.get('timeframe', 'H1')}

### Backtest Performance
{perf}

### Parameters
{param_lines_str}

---

## Input Groups

Group all `input` variables under labelled sections using `input group "..."` (MQL5) or string separator inputs (MQL4):
- `"=== Signal Generation ==="` — ema_period, fractal_n, rr_ratio
- `"=== Session Filter ==="` — sessions (display label)
- `"=== Momentum Candle Filter ==="` — momentum_candle_filter, mc_body_ratio_min, mc_volume_factor, mc_volume_lookback
- `"=== Sideways Filter ==="` — sideways_filter and its sub-parameters
- `"=== Risk Management ==="` — risk_pct, compound, breakeven_r, max_sl_per_period, sl_period
- `"=== Execution ==="` — magic_number, commission_per_lot

---

## Global State

```
int      g_ema_handle      // created in OnInit
datetime g_last_bar_time   // new-bar guard
double   g_last_top        // most recent confirmed top fractal price (carry-forward)
double   g_last_bot        // most recent confirmed bottom fractal price (carry-forward)
double   g_last_top_used   // last_top that triggered a BUY entry (to prevent re-entry)
double   g_last_bot_used   // last_bot that triggered a SELL entry
```

---

## OnTick Logic

### Step 1 — New-bar guard
Compare current bar open time with `g_last_bar_time`. If same bar → return. Otherwise update `g_last_bar_time` and proceed.

### Step 2 — Update fractal levels
At each new bar, check whether bar[{fractal_n + 1}] is a newly confirmed fractal:

**Top fractal** at bar j: `high[j] > high[j+k] AND high[j] > high[j-k]` for all k = 1..{fractal_n}
**Bottom fractal** at bar j: `low[j] < low[j+k] AND low[j] < low[j-k]` for all k = 1..{fractal_n}

Check bar[{fractal_n + 1}] — this is the fractal center confirmed by bar[1] being its {fractal_n}th right-side bar:
```
if IsTopFractal({fractal_n + 1}, {fractal_n}):
    g_last_top = high[{fractal_n + 1}]
if IsBotFractal({fractal_n + 1}, {fractal_n}):
    g_last_bot = low[{fractal_n + 1}]
```
Only update `g_last_top` / `g_last_bot` when a new fractal is confirmed. They carry forward otherwise.

### Step 3 — Skip if position open
Check if a position for this symbol + magic number is already open. If yes → return.

### Step 4 — Session filter
{session_sec}

### Step 5 — EMA trend filter
```
double ema_buf[2]; CopyBuffer(g_ema_handle, 0, 0, 2, ema_buf);
double ema1 = ema_buf[1];   // EMA({ema_p}) on signal bar
bool in_uptrend   = (close[1] > ema1);
bool in_downtrend = (close[1] < ema1);
```

### Step 6 — Sideways filter
{filter_desc}

### Step 7 — {("Momentum candle gate" if mc_on else "Signal detection")}
{mc_section if mc_on else ""}
**BUY signal** conditions on bar[1]:
- `in_uptrend` AND `g_last_top > 0` AND `close[1] > g_last_top` AND `close[2] <= g_last_top`
- Sideways filter: long allowed
- One-per-level: `g_last_top != g_last_top_used`
{("- Momentum candle: bullish MC conditions met" if mc_on else "")}

**SELL signal** conditions on bar[1]:
- `in_downtrend` AND `g_last_bot > 0` AND `close[1] < g_last_bot` AND `close[2] >= g_last_bot`
- Sideways filter: short allowed
- One-per-level: `g_last_bot != g_last_bot_used`
{("- Momentum candle: bearish MC conditions met" if mc_on else "")}

### Step 8 — Period SL limit check
Before placing any order, verify the period SL count is within limit (see Risk Management section).

### Step 9 — Order placement
Pre-compute SL and TP from the signal bar values (not from entry price):

**BUY:**
```
double sl = low[1];
double tp = close[1] + {rr} * (close[1] - sl);
double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
// Guard: ask > sl AND tp > ask
double lots = ComputeLots(ask, sl);
trade.Buy(lots, _Symbol, ask, sl, tp, "WF_BUY");
g_last_top_used = g_last_top;
```

**SELL:**
```
double sl = high[1];
double tp = close[1] - {rr} * (sl - close[1]);
double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
// Guard: bid < sl AND tp < bid
double lots = ComputeLots(bid, sl);
trade.Sell(lots, _Symbol, bid, sl, tp, "WF_SELL");
g_last_bot_used = g_last_bot;
```

---

{_risk_block(params, lang)}

---

{_code_req(lang, mt_ver)}"""


# ---------------------------------------------------------------------------
# Momentum Candle prompt
# ---------------------------------------------------------------------------

def _prompt_momentum_candle(
    params: dict, lang: str, mt_ver: str,
    perf: str, param_lines_str: str,
) -> str:
    ema_p            = params.get("ema_period", 200)
    body_ratio       = params.get("body_ratio_min", 0.70)
    vol_factor       = params.get("volume_factor", 1.5)
    vol_lookback     = int(params.get("volume_lookback", 23))
    retracement_pct  = params.get("retracement_pct", 0.50)
    sl_mult          = params.get("sl_mult", 1.0)
    tp_mult          = params.get("tp_mult", 1.0)
    max_pending_bars = int(params.get("max_pending_bars", 5))
    sessions         = params.get("sessions", "all")
    body_pct         = int(round(body_ratio * 100))
    session_sec      = _session_filter_section(sessions)
    sideways         = params.get("sideways_filter", "none")
    filter_desc      = _sideways_filter_desc(params)

    return f"""## Strategy: Momentum Candle Scalping
## Platform: MetaTrader {mt_ver} ({lang})
## Instrument: XAUUSD
## Timeframe: {params.get('timeframe', 'H1')}

### Backtest Performance
{perf}

### Parameters
{param_lines_str}

---

## Input Groups

Group all `input` variables using `input group "..."` (MQL5) or string separator inputs (MQL4):
- `"=== Trend Filter ==="` — ema_period
- `"=== Momentum Candle ==="` — body_ratio_min, volume_factor, volume_lookback
- `"=== Entry & Exit ==="` — retracement_pct, sl_mult, tp_mult, max_pending_bars
- `"=== Session Filter ==="` — sessions (display label)
- `"=== Sideways Filter ==="` — sideways_filter and sub-parameters
- `"=== Risk Management ==="` — risk_pct, compound, breakeven_r, max_sl_per_period, sl_period
- `"=== Execution ==="` — magic_number, commission_per_lot

---

## Global State

```
int      g_ema_handle      // created in OnInit
datetime g_last_bar_time   // new-bar guard
ulong    g_pending_ticket  // ticket of active limit order (0 = none)
int      g_pending_dir     // 1 = buy limit, -1 = sell limit, 0 = none
double   g_mc_high         // high of the momentum candle that placed the order
double   g_mc_low          // low  of the momentum candle that placed the order
int      g_pending_bars    // bars elapsed since order was placed
```

---

## OnTick Logic

### Step 1 — New-bar guard
Compare current bar open time with `g_last_bar_time`. If same → return. Update and proceed.

### Step 2 — Manage existing pending limit order
If `g_pending_ticket != 0`:
- Call `OrderSelect(g_pending_ticket)`:
  - **Fails** → order was filled, now a position → clear all `g_` state, continue to Step 3
  - **Succeeds** (still pending):
    1. `g_pending_bars++`
    2. Check cancel conditions on bar[1]:
       - Buy limit: `close[1] > g_mc_high` → delete, clear state, **return**
       - Sell limit: `close[1] < g_mc_low`  → delete, clear state, **return**
    3. Expiry: if `g_pending_bars >= {max_pending_bars}` → delete, clear state, **return**
    4. Still valid → **return** (keep order alive; do not look for new signals)

> MT5 fills limit orders automatically when price reaches the limit level. Do NOT check if price touched the limit manually — only check the cancel/expiry conditions above.

### Step 3 — Skip if position open
If any position is open for this symbol + magic number → return.

### Step 4 — Session filter
{session_sec}

### Step 5 — EMA trend filter
```
double ema_buf[2]; CopyBuffer(g_ema_handle, 0, 0, 2, ema_buf);
double ema1 = ema_buf[1];
```

### Step 6 — Detect momentum candle on bar[1]
Copy with `ArraySetAsSeries = true` so index 0 = current forming bar, index 1 = last closed bar.
Request `{vol_lookback + 2}` bars total so that indices up to `{vol_lookback + 1}` are accessible.

```
range      = high[1] - low[1]
body_ratio = MathAbs(close[1] - open[1]) / range
avg_vol    = mean of tick_volume[2..{vol_lookback + 1}]   // {vol_lookback} prior bars, excludes bar[1]
```

Momentum candle conditions:
- `body_ratio >= {body_ratio}`  ({body_pct}% of range is directional body)
- `tick_volume[1] > avg_vol * {vol_factor}`

Signal direction:
- **Bullish MC**: `close[1] > open[1]` AND `close[1] > ema1` → BUY signal
- **Bearish MC**: `close[1] < open[1]` AND `close[1] < ema1` → SELL signal
- Neither → return

### Step 7 — Sideways filter
{filter_desc}

### Step 8 — Period SL limit check
Before placing any order, verify the period SL count is within limit (see Risk Management).

### Step 9 — Place limit order
```
mc_high = high[1];   mc_low = low[1];   range = mc_high - mc_low;
```

**BUY LIMIT:**
```
limit_price = NormalizeDouble(mc_high - {retracement_pct} * range, digits)
sl          = NormalizeDouble(mc_high - {sl_mult} * range, digits)
tp          = NormalizeDouble(mc_low  + {tp_mult} * range, digits)
sl_distance = limit_price - sl
```

**SELL LIMIT:**
```
limit_price = NormalizeDouble(mc_low  + {retracement_pct} * range, digits)
sl          = NormalizeDouble(mc_low  + {sl_mult} * range, digits)
tp          = NormalizeDouble(mc_high - {tp_mult} * range, digits)
sl_distance = sl - limit_price
```

Validation before placing:
- `sl_distance > 0`
- SL and TP are beyond stops_level from entry
- Buy limit: `limit_price < Ask`;  Sell limit: `limit_price > Bid`

Place with `CTrade::BuyLimit` / `CTrade::SellLimit` using `ORDER_TIME_GTC` and `ORDER_FILLING_RETURN`.
After success: set `g_pending_ticket`, `g_pending_dir`, `g_mc_high`, `g_mc_low`, `g_pending_bars = 0`.

---

## Limit Order Lifecycle

```
Bar N closes as MC
  → place BuyLimit/SellLimit at limit_price
  → store g_mc_high, g_mc_low, g_pending_bars = 0

Each subsequent bar:
  Step 2 runs first:
    1. g_pending_bars++
    2. Cancel if close[1] > g_mc_high (buy) or close[1] < g_mc_low (sell)
    3. Cancel if g_pending_bars >= {max_pending_bars}
    Otherwise → return (keep order alive)

When MT5 fills the order:
  → OrderSelect fails next bar → clear g_ state → position is live via SL/TP
```

---

{_risk_block(params, lang)}

---

{_code_req(lang, mt_ver)}"""


# ---------------------------------------------------------------------------
# Order Block (SMC) prompt
# ---------------------------------------------------------------------------

def _prompt_order_block_smc(
    params: dict, lang: str, mt_ver: str,
    perf: str, param_lines_str: str,
) -> str:
    structure_period = int(params.get("structure_period", 20))
    ob_lookback      = int(params.get("ob_lookback", 5))
    rr               = params.get("rr_ratio", 2.0)
    sl_mode          = params.get("sl_mode", "ob_edge")
    require_fvg      = bool(params.get("require_fvg", False))
    require_ote      = bool(params.get("require_ote", False))
    ote_lo           = params.get("ote_fib_low",  0.618)
    ote_hi           = params.get("ote_fib_high", 0.786)
    sessions         = params.get("sessions", "all")
    session_sec      = _session_filter_section(sessions)

    # SL mode description
    if sl_mode == "ob_edge":
        sl_long_formula  = "sl = ob_low"
        sl_short_formula = "sl = ob_high"
        sl_desc = "OB Edge — SL at the far edge of the Order Block candle"
    elif sl_mode == "ob_midpoint":
        sl_long_formula  = "sl = (ob_high + ob_low) / 2.0"
        sl_short_formula = "sl = (ob_high + ob_low) / 2.0"
        sl_desc = "OB Midpoint — SL at the 50% level of the OB candle"
    else:  # structure
        sl_long_formula  = "sl = recent_low  (rolling min of low over prior structure_period bars)"
        sl_short_formula = "sl = recent_high (rolling max of high over prior structure_period bars)"
        sl_desc = "Structure — SL beyond the swing low/high that defined the BOS"

    fvg_section = ""
    if require_fvg:
        fvg_section = f"""
### FVG Confluence (required)
A Fair Value Gap must exist within the `ob_lookback` window before the BOS bar:
- Bullish FVG at bar j (MQL5 index, j >= 3): `low[j] > high[j+2]`  (3-candle upward gap)
- Bearish FVG at bar j (MQL5 index, j >= 3): `high[j] < low[j+2]`  (3-candle downward gap)
Scan bars j = 2 to j = {ob_lookback + 1}. If no FVG found → skip signal.
"""

    ote_section = ""
    if require_ote:
        ote_section = f"""
### OTE Zone (required)
The Order Block entry level must fall within the Fibonacci OTE zone of the BOS impulse leg.

Bullish OTE (for longs):
```
impulse = close[1] - recent_low          // recent_low = rolling min of low[2..{structure_period+1}]
ote_lo  = recent_low + {ote_lo} * impulse
ote_hi  = recent_low + {ote_hi} * impulse
// Entry ob_high must satisfy: ote_lo <= ob_high <= ote_hi
```

Bearish OTE (for shorts):
```
impulse = recent_high - close[1]         // recent_high = rolling max of high[2..{structure_period+1}]
ote_lo  = recent_high - {ote_hi} * impulse
ote_hi  = recent_high - {ote_lo} * impulse
// Entry ob_low must satisfy: ote_lo <= ob_low <= ote_hi
```
If outside OTE zone → skip signal.
"""

    return f"""## Strategy: Order Block (SMC)
## Platform: MetaTrader {mt_ver} ({lang})
## Instrument: XAUUSD
## Timeframe: {params.get('timeframe', 'H1')}

### Backtest Performance
{perf}

### Parameters
{param_lines_str}

---

## Input Groups

Group all `input` variables using `input group "..."` (MQL5) or string separator inputs (MQL4):
- `"=== Structure & BOS ==="` — structure_period
- `"=== Order Block ==="` — ob_lookback, sl_mode
- `"=== Entry & Exit ==="` — rr_ratio
- `"=== Confluence Filters ==="` — require_fvg, require_ote, ote_fib_low, ote_fib_high
- `"=== Session Filter ==="` — sessions (display label)
- `"=== Risk Management ==="` — risk_pct, compound, breakeven_r, max_sl_per_period, sl_period
- `"=== Execution ==="` — magic_number, commission_per_lot

---

## Concepts

**BOS (Break of Structure):** Price closes above the rolling {structure_period}-bar high (bullish BOS) or below the rolling {structure_period}-bar low (bearish BOS) for the first time.
- `recent_high` at bar 1 = max of `high[2..{structure_period + 1}]`  (prior {structure_period} closed bars)
- `recent_low`  at bar 1 = min of `low[2..{structure_period + 1}]`

Bullish BOS: `close[1] > recent_high` AND `close[2] <= prev_recent_high`
Bearish BOS: `close[1] < recent_low`  AND `close[2] >= prev_recent_low`

Where `prev_recent_high` = max of `high[3..{structure_period + 2}]` (one bar further back).

**Order Block (OB):** The last opposing candle before the BOS within `ob_lookback` bars.
- Bullish OB: scan bars j = 2 to j = {ob_lookback + 1} → last bar where `close[j] < open[j]` (bearish candle)
  - `ob_high = high[j]`,  `ob_low = low[j]`
- Bearish OB: scan bars j = 2 to j = {ob_lookback + 1} → last bar where `close[j] > open[j]` (bullish candle)
  - `ob_high = high[j]`,  `ob_low = low[j]`

If no qualifying candle is found → no signal.

---

## Global State

```
datetime g_last_bar_time    // new-bar guard
ulong    g_pending_ticket   // ticket of active limit order (0 = none)
int      g_pending_dir      // 1 = buy limit, -1 = sell limit, 0 = none
double   g_pending_tp       // TP of the active limit order (used for cancel check)
```

---

## OnTick Logic

### Step 1 — New-bar guard
Compare current bar open time with `g_last_bar_time`. If same → return. Update and proceed.

### Step 2 — Manage existing pending limit order
If `g_pending_ticket != 0`:
- Call `OrderSelect(g_pending_ticket)`:
  - **Fails** → order was filled, a position is now live → clear all `g_` state, continue to Step 3
  - **Succeeds** (still pending) — check cancel conditions on bar[1]:
    - Buy limit:  `high[1] > g_pending_tp` → delete order, clear state, **return**
    - Sell limit: `low[1]  < g_pending_tp` → delete order, clear state, **return**
    - Neither → **return** (keep order alive; do not look for new signals)

> Note: there is no bar-count expiry for this strategy. The order persists until filled or cancelled by the TP-skip condition above.

### Step 3 — Skip if position open
If any position is open for this symbol + magic number → return.

### Step 4 — Session filter
{session_sec}

### Step 5 — Compute rolling structure levels for bar[1]
```
int  hi_idx     = iHighest(_Symbol, PERIOD_CURRENT, MODE_HIGH, {structure_period}, 2);
double recent_high = iHigh(_Symbol, PERIOD_CURRENT, hi_idx);
int  lo_idx     = iLowest (_Symbol, PERIOD_CURRENT, MODE_LOW,  {structure_period}, 2);
double recent_low  = iLow (_Symbol, PERIOD_CURRENT, lo_idx);

// Previous bar's levels (for BOS confirmation):
int  prev_hi_idx   = iHighest(_Symbol, PERIOD_CURRENT, MODE_HIGH, {structure_period}, 3);
double prev_recent_high = iHigh(_Symbol, PERIOD_CURRENT, prev_hi_idx);
int  prev_lo_idx   = iLowest (_Symbol, PERIOD_CURRENT, MODE_LOW,  {structure_period}, 3);
double prev_recent_low  = iLow (_Symbol, PERIOD_CURRENT, prev_lo_idx);
```

### Step 6 — Detect BOS on bar[1]
```
bool bos_long  = (close[1] > recent_high) && (close[2] <= prev_recent_high);
bool bos_short = (close[1] < recent_low)  && (close[2] >= prev_recent_low);
// If both fire (rare), prefer bos_long.
```

If neither BOS → return.

### Step 7 — Find Order Block
Scan bars j = 2 to j = {ob_lookback + 1} in ascending order (j=2 first = most recent):

**Bullish OB** (after bos_long): find the LAST (highest j) bar where `close[j] < open[j]`
**Bearish OB** (after bos_short): find the LAST (highest j) bar where `close[j] > open[j]`

Store `ob_high = high[j]`, `ob_low = low[j]`. If not found → return.
Validate: `ob_high > ob_low` → else return.
{fvg_section}{ote_section}
### Step 8 — Compute SL and TP
SL mode: **{sl_mode}** — {sl_desc}

**Long (after bullish BOS):**
```
double entry = ob_high;
double sl    = {sl_long_formula};
// Guard: entry > sl
double sl_dist = entry - sl;
double tp    = entry + {rr} * sl_dist;
```

**Short (after bearish BOS):**
```
double entry = ob_low;
double sl    = {sl_short_formula};
// Guard: entry < sl
double sl_dist = sl - entry;
double tp    = entry - {rr} * sl_dist;
```

### Step 9 — Period SL limit check
Before placing, verify the period SL count is within limit (see Risk Management).

### Step 10 — Place limit order

**BUY LIMIT at `entry` (= ob_high):**
- Validate: `entry < Ask`, `sl_dist > stops_level * point`, `(tp - entry) > stops_level * point`
- Place with `CTrade::BuyLimit(lots, entry, _Symbol, sl, tp, ...)`

**SELL LIMIT at `entry` (= ob_low):**
- Validate: `entry > Bid`, same stop-level guards
- Place with `CTrade::SellLimit(lots, entry, _Symbol, sl, tp, ...)`

After success: set `g_pending_ticket`, `g_pending_dir`, `g_pending_tp = tp`.

---

## Limit Order Lifecycle

```
Bar N — BOS detected → OB found → limit order placed at ob_high (long) or ob_low (short)

Each subsequent bar (Step 2 runs first):
  If order still pending:
    Buy limit:  cancel if high[1] > g_pending_tp  (price skipped past TP without retracing)
    Sell limit: cancel if low[1]  < g_pending_tp
    Otherwise → return (keep alive)

When MT5 fills the order:
  → OrderSelect fails next bar → clear g_ state → position is live, managed by SL/TP
```

---

{_risk_block(params, lang)}

---

{_code_req(lang, mt_ver)}"""


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _build_prompt(strategy: str, params: dict, results: dict, platform: str) -> str:
    lang   = "MQL4" if platform == "MT4" else "MQL5"
    mt_ver = platform[-1]

    perf         = _perf_block(results)
    param_lines_str = _param_lines(params)

    if strategy == "momentum_candle":
        return _prompt_momentum_candle(params, lang, mt_ver, perf, param_lines_str)
    elif strategy == "order_block_smc":
        return _prompt_order_block_smc(params, lang, mt_ver, perf, param_lines_str)
    else:
        return _prompt_william_fractals(params, lang, mt_ver, perf, param_lines_str)


def _strip_fences(code: str) -> str:
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
    params   = data.get("parameters", {})
    results  = data.get("results", {})

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

    ext           = "mq4" if platform == "MT4" else "mq5"
    tf            = str(params.get("timeframe", "H1"))
    strategy_slug = strategy.replace("_", "")
    filename      = f"{strategy_slug}_{tf}_{platform}.{ext}"

    return EAResponse(code=code, platform=platform, filename=filename, prompt=prompt)
