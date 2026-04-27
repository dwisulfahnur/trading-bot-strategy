"""
EA generation endpoint.

POST /ea/generate
  Body: { result_id, platform }
  Returns: { code, platform, filename }
"""

import json
import os
import re
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


def _breakeven_section(breakeven_r, breakeven_sl_r: float = 0.0) -> str:
    if not breakeven_r:
        return ""
    sl_r = float(breakeven_sl_r)
    if sl_r == 0.0:
        lock_desc = "entry price (break-even — 0R, no loss)"
    elif sl_r > 0:
        lock_desc = f"entry + {sl_r} × initial_sl_distance (locking in {sl_r}R profit)"
    else:
        lock_desc = f"entry − {abs(sl_r)} × initial_sl_distance (cutting loss to {sl_r}R)"
    return (
        f"\n### SL Move After Trigger\n"
        f"Once profit reaches {breakeven_r}R (price moves {breakeven_r} × initial_sl_distance "
        f"in the trade's favour), move the stop-loss to {lock_desc}. "
        f"A subsequent SL hit closes at {sl_r:+.1f}R.\n"
        f"- Store `initial_sl_distance` at entry and never overwrite it.\n"
        f"- Track whether the SL has already been moved (boolean flag per trade) — "
        f"trigger fires only once per trade.\n"
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
    from backtest import PAIR_CONFIG
    risk_pct_pct    = float(params.get("risk_pct", 0.02)) * 100
    compound        = params.get("compound", False)
    symbol          = params.get("symbol", "XAUUSD")
    pair_cfg        = PAIR_CONFIG.get(symbol, PAIR_CONFIG["XAUUSD"])
    contract_size   = pair_cfg["contract_size"]
    compound_label  = (
        "Yes — recalculate from current account balance each trade"
        if compound
        else "No — always use fixed initial balance"
    )
    breakeven_r     = params.get("breakeven_r")
    breakeven_sl_r  = float(params.get("breakeven_sl_r", 0.0))
    max_sl          = params.get("max_sl_per_period")
    sl_period       = params.get("sl_period", "none")
    risk_recovery   = float(params.get("risk_recovery", 0.0)) * 100
    trail_recovery  = bool(params.get("trail_recovery", False))
    trail_recovery_pct = float(params.get("trail_recovery_pct", 10.0))

    initial_capital      = float(params.get("initial_capital", 10000))
    commission_per_lot_v = float(params.get("commission_per_lot", 3.5))

    if risk_recovery > 0:
        if trail_recovery:
            recovery_label = (
                f"Trailing baseline — recovery risk {risk_recovery:.1f}% activates when balance drops below "
                f"last locked milestone (initial {initial_capital:.0f}, steps every {trail_recovery_pct:.0f}%)"
            )
        else:
            recovery_label = f"Reduced to {risk_recovery:.1f}% of balance when underwater (balance < InpInitialCapital)"
    else:
        recovery_label = "Not configured — use general risk at all times"

    if risk_recovery > 0:
        baseline_var = "g_recovery_baseline"
        if compound:
            lot_formula = (
                f"`active_risk_amount / (sl_distance × {contract_size:,})`  "
                f"where `active_risk_amount = AccountInfoDouble(ACCOUNT_BALANCE) × {risk_pct_pct:.1f}% / 100` "
                f"when balance ≥ {baseline_var}, else `AccountInfoDouble(ACCOUNT_BALANCE) × {risk_recovery:.1f}% / 100`"
            )
        else:
            lot_formula = (
                f"`active_risk_amount / (sl_distance × {contract_size:,})`  "
                f"where `active_risk_amount = InpInitialCapital × {risk_pct_pct:.1f}% / 100` (default {initial_capital:.0f}) "
                f"when balance ≥ {baseline_var}, else `InpInitialCapital × {risk_recovery:.1f}% / 100` (fixed, no compounding)"
            )

        if trail_recovery:
            recovery_impl = (
                f"  - Declare a global `double g_recovery_baseline`.\n"
                f"  - In `OnInit`: set `g_recovery_baseline = InpInitialCapital` (always use the fixed input, NOT AccountBalance — this survives EA restarts correctly).\n"
                f"    Then fast-forward the baseline to reflect gains already made:\n"
                f"    ```\n"
                f"    double next = g_recovery_baseline * (1.0 + InpTrailRecoveryPct / 100.0);\n"
                f"    while(AccountInfoDouble(ACCOUNT_BALANCE) >= next) {{\n"
                f"        g_recovery_baseline = next;\n"
                f"        next = g_recovery_baseline * (1.0 + InpTrailRecoveryPct / 100.0);\n"
                f"    }}\n"
                f"    ```\n"
                f"  - In `ComputeLots` (before the underwater check): update the baseline each call so it trails in real time:\n"
                f"    ```\n"
                f"    double next = g_recovery_baseline * (1.0 + InpTrailRecoveryPct / 100.0);\n"
                f"    while(balance >= next) {{\n"
                f"        g_recovery_baseline = next;\n"
                f"        next = g_recovery_baseline * (1.0 + InpTrailRecoveryPct / 100.0);\n"
                f"    }}\n"
                f"    ```\n"
                f"  - `InpTrailRecoveryPct` is an `input double` (default {trail_recovery_pct:.0f}) — the % profit step between milestones.\n"
                f"  - Compare balance against `g_recovery_baseline` (not `InpInitialCapital`) to decide which risk % to use.\n"
            )
        else:
            recovery_impl = (
                f"  - Declare a global `double g_recovery_baseline`.\n"
                f"  - In `OnInit`: set `g_recovery_baseline = InpInitialCapital` (always use the fixed input, NOT AccountBalance — this survives EA restarts correctly).\n"
                f"  - Before every lot calculation: compare `AccountInfoDouble(ACCOUNT_BALANCE)` against `g_recovery_baseline` to pick the active risk percentage.\n"
            )
    else:
        if compound:
            lot_formula = (
                f"`(AccountInfoDouble(ACCOUNT_BALANCE) × {risk_pct_pct:.1f}% / 100) / (sl_distance × {contract_size:,})`"
            )
        else:
            lot_formula = (
                f"`(InpInitialCapital × {risk_pct_pct:.1f}% / 100) / (sl_distance × {contract_size:,})` "
                f"(InpInitialCapital default {initial_capital:.0f} — fixed lot size, no compounding)"
            )
        recovery_impl = ""

    compute_lots_spec = (
        f"\n### ComputeLots Helper Function\n"
        f"Implement `double ComputeLots(double entry, double sl)` — call this before every order placement:\n"
        f"```\n"
        f"double ComputeLots(double entry, double sl) {{\n"
        f"    double sl_dist = MathAbs(entry - sl);\n"
        f"    if(sl_dist < _Point) return 0.0;   // zero SL distance guard\n"
        f"    double balance = AccountInfoDouble(ACCOUNT_BALANCE);  // use Balance, not Equity\n"
    )
    if risk_recovery > 0:
        if trail_recovery:
            compute_lots_spec += (
                f"    // Trail recovery baseline up to current balance\n"
                f"    double next = g_recovery_baseline * (1.0 + InpTrailRecoveryPct / 100.0);\n"
                f"    while(balance >= next) {{ g_recovery_baseline = next; next = g_recovery_baseline * (1.0 + InpTrailRecoveryPct / 100.0); }}\n"
            )
        compute_lots_spec += (
            f"    bool underwater = (balance < g_recovery_baseline);\n"
        )
        if compound:
            compute_lots_spec += (
                f"    double risk_pct = underwater ? InpRiskRecovery : InpRiskPct;\n"
                f"    double risk_amount = balance * risk_pct;\n"
            )
        else:
            compute_lots_spec += (
                f"    double risk_pct = underwater ? InpRiskRecovery : InpRiskPct;\n"
                f"    double risk_amount = InpInitialCapital * risk_pct;\n"
            )
    else:
        if compound:
            compute_lots_spec += (
                f"    double risk_amount = balance * InpRiskPct;\n"
            )
        else:
            compute_lots_spec += (
                f"    double risk_amount = InpInitialCapital * InpRiskPct;\n"
            )
    compute_lots_spec += (
        f"    double lots = risk_amount / (sl_dist * {contract_size});\n"
        f"    double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);\n"
        f"    lots = MathFloor(lots / step) * step;\n"
        f"    return MathMax(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN),\n"
        f"                   MathMin(lots, SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX)));\n"
        f"}}\n"
        f"```\n"
        f"- Use `ACCOUNT_BALANCE` (not `ACCOUNT_EQUITY`) — this matches the backtest which only updates capital after a trade closes, not on open unrealized P&L.\n"
        f"- `InpRiskPct` and `InpRiskRecovery` are fraction inputs (e.g. 0.02 for 2%), not percentages.\n"
        f"- Always expose `InpInitialCapital` as an `input double` (default {initial_capital:.0f}) — required for recovery baseline and non-compound lot sizing.\n"
    )

    commission_note = (
        f"- Commission: {commission_per_lot_v:.2f} USD per lot per side "
        f"(round-trip cost = lots × {commission_per_lot_v:.2f} × 2 per trade). "
        f"Expose `InpCommissionPerLot` as an `input double` (default {commission_per_lot_v:.2f}). "
        f"In ECN/STP accounts the broker charges commission automatically on fills — "
        f"the input is informational only and no manual deduction is needed in the EA logic.\n"
    )

    return (
        f"## Risk Management\n"
        f"- Risk per trade: {risk_pct_pct:.1f}% of account balance\n"
        f"- Recovery risk when underwater: {recovery_label}\n"
        f"- Max open positions: {params.get('max_positions', 1)}\n"
        f"- Lot size: {lot_formula}\n"
        f"  - {symbol} contract_size = {contract_size:,} per lot\n"
        f"  - sl_distance = absolute price difference of entry to SL (measured from fill/stop price, not bar close)\n"
        f"  - Clamp to broker min/max lot size, round down to lot step\n"
        f"{recovery_impl}"
        f"- Compounding: {compound_label}\n"
        f"- {commission_note}"
        + compute_lots_spec
        + _breakeven_section(breakeven_r, breakeven_sl_r)
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
# Shared helpers for prompt builders
# ---------------------------------------------------------------------------

def _risk_input_group_line(params: dict) -> str:
    """Builds the === Risk Management === input group line dynamically."""
    risk_recovery = float(params.get("risk_recovery", 0.0))
    trail_recovery = bool(params.get("trail_recovery", False))

    inputs = ["initial_capital", "risk_pct", "compound"]
    if risk_recovery > 0:
        inputs.append("risk_recovery")
        if trail_recovery:
            inputs.extend(["trail_recovery", "trail_recovery_pct"])
    inputs.extend(["max_positions", "breakeven_r", "max_sl_per_period", "sl_period"])
    return f'- `"=== Risk Management ==="` — {", ".join(inputs)}'


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

    risk_mgmt_line = _risk_input_group_line(params)

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
## Instrument: {params.get('symbol', 'XAUUSD')}
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
{risk_mgmt_line}
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
    risk_mgmt_line   = _risk_input_group_line(params)

    return f"""## Strategy: Momentum Candle Scalping
## Platform: MetaTrader {mt_ver} ({lang})
## Instrument: {params.get('symbol', 'XAUUSD')}
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
{risk_mgmt_line}
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

    risk_mgmt_line = _risk_input_group_line(params)

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
## Instrument: {params.get('symbol', 'XAUUSD')}
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
{risk_mgmt_line}
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
# N Structure Breakout prompt
# ---------------------------------------------------------------------------

def _prompt_n_structure(
    params: dict, lang: str, mt_ver: str,
    perf: str, param_lines_str: str,
) -> str:
    ema_p            = params.get("ema_period", 200)
    ema_tf           = params.get("ema_timeframe", "same")
    swing_n_before   = int(params.get("swing_n_before", 5))
    swing_n_after    = int(params.get("swing_n_after", 5))
    rr               = params.get("rr_ratio", 2.0)
    sl_mode          = params.get("sl_mode", "swing_midpoint")
    sessions         = params.get("sessions", "all")
    pending_cancel   = params.get("pending_cancel", "max_bars")
    max_pending_bars = int(params.get("max_pending_bars", 10))
    breakeven_r      = params.get("breakeven_r")
    breakeven_sl_r   = float(params.get("breakeven_sl_r", 0.0))
    max_positions    = int(params.get("max_positions", 1))
    filter_desc      = _sideways_filter_desc(params)
    session_sec      = _session_filter_section(sessions)

    use_hl_break  = pending_cancel in ("hl_break", "both")
    use_max_bars  = pending_cancel in ("max_bars",  "both")

    # Build cancel global state vars conditionally
    _cancel_globals = ""
    if use_hl_break:
        _cancel_globals += (
            "double   g_buy_cancel_level      // HL price — cancel buy stop if low[1] breaks below this\n"
            "double   g_sell_cancel_level     // LH price — cancel sell stop if high[1] breaks above this\n"
        )
    if use_max_bars:
        _cancel_globals += (
            "int      g_pending_buy_bars      // bars elapsed since buy stop was placed\n"
            "int      g_pending_sell_bars     // bars elapsed since sell stop was placed\n"
        )

    # Build cancel step 2.5 conditionally
    def _cancel_block(direction: str) -> str:
        ticket = "g_pending_buy_ticket"  if direction == "buy"  else "g_pending_sell_ticket"
        bars   = "g_pending_buy_bars"    if direction == "buy"  else "g_pending_sell_bars"
        clevel = "g_buy_cancel_level"    if direction == "buy"  else "g_sell_cancel_level"
        price_check = f"low[1] < {clevel}"  if direction == "buy"  else f"high[1] > {clevel}"
        label  = "HL broken — setup structurally invalidated" if direction == "buy" else "LH broken — setup structurally invalidated"
        lines  = []
        if use_max_bars:
            lines.append(f"{bars}++;")
            lines.append("")
        if use_hl_break:
            lines.append(f"// 1. {label}")
            lines.append(f"if({clevel} > 0.0 && {price_check}) {{{{")
            lines.append(f"    trade.OrderDelete({ticket});")
            lines.append(f"    {ticket}  = 0;")
            if use_hl_break:
                lines.append(f"    {clevel} = 0.0;")
            if use_max_bars:
                lines.append(f"    {bars}   = 0;")
            lines.append("}")
        if use_max_bars:
            prefix = "else " if use_hl_break else ""
            lines.append(f"// {'2' if use_hl_break else '1'}. Expiry — order not filled within {max_pending_bars} bars")
            lines.append(f"{prefix}if({bars} >= {max_pending_bars}) {{{{")
            lines.append(f"    trade.OrderDelete({ticket});")
            lines.append(f"    {ticket}  = 0;")
            if use_hl_break:
                lines.append(f"    {clevel} = 0.0;")
            lines.append(f"    {bars}   = 0;")
            lines.append("}")
        return "\n".join(lines)

    _cancel_buy_block  = _cancel_block("buy")
    _cancel_sell_block = _cancel_block("sell")
    risk_mgmt_line     = _risk_input_group_line(params)

    if pending_cancel == "none":
        _cancel_step = "No cancellation — pending stop orders stay open until filled."
    else:
        modes = []
        if use_hl_break:
            modes.append("**HL/LH break**: cancel buy stop when `low[1] < HL`; cancel sell stop when `high[1] > LH`")
        if use_max_bars:
            modes.append(f"**Expiry**: cancel after `{max_pending_bars}` bars without fill")
        _cancel_step = f"""Active cancel mode: **{pending_cancel}** — {' + '.join(modes)}.

**Buy Stop cancellation** (if `g_pending_buy_ticket > 0`):
```
{_cancel_buy_block}
```

**Sell Stop cancellation** (if `g_pending_sell_ticket > 0`):
```
{_cancel_sell_block}
```"""

    # Step-9: lines to store cancel state when arming a new stop order
    _store_buy_cancel  = "\ng_buy_cancel_level    = g_hl;                   // cancel if price breaks below the pullback HL" if use_hl_break else ""
    _store_buy_bars    = "\ng_pending_buy_bars    = 0;" if use_max_bars else ""
    _store_sell_cancel = "\ng_sell_cancel_level   = g_lh;                   // cancel if price breaks above the bounce LH" if use_hl_break else ""
    _store_sell_bars   = "\ng_pending_sell_bars   = 0;" if use_max_bars else ""

    # Step-2: lines to reset cancel state when a new swing invalidates an existing order
    _reset_buy_cancel  = "\n        g_buy_cancel_level = 0.0;" if use_hl_break else ""
    _reset_buy_bars    = "\n        g_pending_buy_bars = 0;"   if use_max_bars else ""
    _reset_sell_cancel = "\n        g_sell_cancel_level = 0.0;" if use_hl_break else ""
    _reset_sell_bars   = "\n        g_pending_sell_bars = 0;"   if use_max_bars else ""

    # Break-even tracking globals and OnTick step (conditional on breakeven_r)
    _be_globals = ""
    _be_init    = ""
    _be_step    = ""
    if breakeven_r:
        be_sl_r = float(breakeven_sl_r)
        if be_sl_r == 0.0:
            be_target    = "entry price (break-even, 0R)"
            new_sl_long  = "entry"
            new_sl_short = "entry"
        elif be_sl_r > 0:
            be_target    = f"entry + {be_sl_r} × init_dist (locking {be_sl_r}R profit)"
            new_sl_long  = f"entry + {be_sl_r} * init_dist"
            new_sl_short = f"entry - {be_sl_r} * init_dist"
        else:
            be_target    = f"entry − {abs(be_sl_r)} × init_dist (partial loss cut to {be_sl_r}R)"
            new_sl_long  = f"entry + {be_sl_r} * init_dist"
            new_sl_short = f"entry - {be_sl_r} * init_dist"

        _be_globals = (
            f"// Break-even position tracking ({max_positions} slot(s))\n"
            f"ulong  g_be_tickets[{max_positions}]    // tickets of open positions being monitored\n"
            f"double g_be_init_dist[{max_positions}]  // |entry − SL| recorded at position open\n"
            f"bool   g_be_done[{max_positions}]       // true once SL has been moved for this trade\n"
            f"int    g_be_count                        // slots in use\n"
        )
        _be_init = "\nInitialise break-even arrays: `g_be_count = 0`; zero-fill `g_be_tickets` and `g_be_init_dist`; fill `g_be_done` with `false`.\n"
        _be_step = f"""### Step 0.5 — Break-even SL management (runs every tick, before new-bar guard)

Scan all open positions for this symbol + magic number:

**1. Sync array** — remove any slot where the position ticket no longer appears in `PositionsTotal()`:
```
for each slot i in 0..g_be_count-1:
    if !PositionSelectByTicket(g_be_tickets[i]):
        // position closed — compact array (shift remaining slots down)
        g_be_count--;
```

**2. Register new positions** — for any open position whose ticket is NOT in `g_be_tickets[]`:
```
if(g_be_count < {max_positions}) {{
    g_be_tickets[g_be_count]  = ticket;
    g_be_init_dist[g_be_count] = MathAbs(PositionGetDouble(POSITION_PRICE_OPEN)
                                         - PositionGetDouble(POSITION_SL));
    g_be_done[g_be_count]     = false;
    g_be_count++;
}}
```

**3. Check trigger** — for each tracked slot where `g_be_done[i] == false`:
```
double entry      = PositionGetDouble(POSITION_PRICE_OPEN);
double init_dist  = g_be_init_dist[i];
double trigger    = {breakeven_r} * init_dist;
double current_tp = PositionGetDouble(POSITION_TP);

if(PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) {{
    if(SymbolInfoDouble(_Symbol, SYMBOL_BID) >= entry + trigger) {{
        double new_sl = NormalizeDouble({new_sl_long}, _Digits);
        trade.PositionModify(_Symbol, new_sl, current_tp);
        g_be_done[i] = true;
    }}
}}
else {{
    if(SymbolInfoDouble(_Symbol, SYMBOL_ASK) <= entry - trigger) {{
        double new_sl = NormalizeDouble({new_sl_short}, _Digits);
        trade.PositionModify(_Symbol, new_sl, current_tp);
        g_be_done[i] = true;
    }}
}}
```

SL is moved to **{be_target}**. Fires at most once per trade (`g_be_done` prevents re-triggering).

"""

    # EMA indicator setup
    _TF_MAP = {"M1": "PERIOD_M1", "M5": "PERIOD_M5", "M15": "PERIOD_M15",
               "H1": "PERIOD_H1", "H4": "PERIOD_H4", "D1": "PERIOD_D1"}
    if ema_tf == "same":
        ema_init   = f"iMA(_Symbol, PERIOD_CURRENT, {ema_p}, 0, MODE_EMA, PRICE_CLOSE)"
        ema_label  = f"EMA({ema_p}) on the chart timeframe"
    else:
        tf_const   = _TF_MAP.get(ema_tf, f"PERIOD_{ema_tf}")
        ema_init   = f"iMA(_Symbol, {tf_const}, {ema_p}, 0, MODE_EMA, PRICE_CLOSE)"
        ema_label  = f"EMA({ema_p}) on the {ema_tf} timeframe (higher-timeframe trend filter)"

    ema_copy = (
        f"double ema_buf[];\n"
        f"ArraySetAsSeries(ema_buf, true);\n"
        f"if(CopyBuffer(g_ema_handle, 0, 0, 3, ema_buf) < 3) return;\n"
        f"double ema1 = ema_buf[1];   // {ema_label}, aligned to bar[1]"
    )

    # SL formula strings — computed from the stop-entry level and stored swing points
    sl_long_formula, sl_short_formula = {
        "swing_midpoint": (
            "(g_last_sh + g_hl) / 2.0",
            "(g_last_sl + g_lh) / 2.0",
        ),
        "swing_point": (
            "g_hl",
            "g_lh",
        ),
        "signal_candle": (
            "low[1]",     # low of the HL-confirmation bar (bar where swing low was confirmed)
            "high[1]",    # high of the LH-confirmation bar (bar where swing high was confirmed)
        ),
    }.get(sl_mode, ("low[1]", "high[1]"))

    sl_mode_desc = {
        "swing_midpoint": "midpoint between the breakout level (H1/L1) and the pullback/bounce point (HL/LH)",
        "swing_point":    "at the pullback low (HL) for longs / bounce high (LH) for shorts",
        "signal_candle":  "low of the HL-confirmation bar for longs / high of the LH-confirmation bar for shorts",
    }.get(sl_mode, sl_mode)

    return f"""## Strategy: N Structure Breakout
## Platform: MetaTrader {mt_ver} ({lang})
## Instrument: {params.get('symbol', 'XAUUSD')}
## Timeframe: {params.get('timeframe', 'H1')}

### Backtest Performance
{perf}

### Parameters
{param_lines_str}

---

## Strategy Overview

The N Structure Breakout identifies a three-point swing pattern and enters via a **pending stop order**
placed at the breakout level the moment the structure is armed — no waiting for a close confirmation.

**Bullish N (Buy setup):**
1. Swing High H1 forms — highest high with {swing_n_before} bars to the left and {swing_n_after} bars to the right.
2. Price pulls back to a Swing Low HL *after* H1 (confirmed swing low below H1).
3. **Structure armed:** when HL is confirmed AND `close[1] < g_last_sh` (price hasn't broken out yet)
   AND `close[1] > EMA({ema_p})` → place a **Buy Stop** pending order at `g_last_sh`.

**Bearish Inverted-N (Sell setup):**
1. Swing Low L1 forms — lowest low with {swing_n_before} bars to the left and {swing_n_after} bars to the right.
2. Price bounces to a Swing High LH *after* L1 (confirmed swing high above L1).
3. **Structure armed:** when LH is confirmed AND `close[1] > g_last_sl` (price hasn't broken down yet)
   AND `close[1] < EMA({ema_p})` → place a **Sell Stop** pending order at `g_last_sl`.

Entry fills automatically when price reaches the stop level. SL and TP are anchored to the stop price
(not the bar close), so the RR ratio is exact at fill.

SL mode: **{sl_mode}** — {sl_mode_desc}.
TP: `stop_price ± rr_ratio × (stop_price − SL)`.

---

## Input Groups

Group all `input` variables using `input group "..."` (MQL5) or string separator inputs (MQL4):
- `"=== Signal Generation ==="` — ema_period, ema_timeframe, swing_n_before, swing_n_after, rr_ratio, sl_mode
- `"=== Pending Order ==="` — pending_cancel, max_pending_bars (only relevant when cancel mode uses bar expiry)
- `"=== Session Filter ==="` — sessions (display label)
- `"=== Sideways Filter ==="` — sideways_filter and its sub-parameters
{risk_mgmt_line}
- `"=== Execution ==="` — magic_number, commission_per_lot

---

## Global State

```
int      g_ema_handle            // EMA indicator handle — created in OnInit
datetime g_last_bar_time         // new-bar guard
double   g_last_sh               // most recent confirmed swing high H1  (0.0 = none)
double   g_hl                    // pullback low  HL after H1             (0.0 = not yet formed)
double   g_last_sl               // most recent confirmed swing low  L1   (0.0 = none)
double   g_lh                    // bounce high   LH after L1             (0.0 = not yet formed)
ulong    g_pending_buy_ticket    // ticket of active Buy Stop order   (0 = none)
ulong    g_pending_sell_ticket   // ticket of active Sell Stop order  (0 = none)
{_cancel_globals}{_be_globals}```

Initialise all `g_` doubles to 0.0, tickets to 0, and counters to 0 in OnInit. Reset in OnDeinit / OnTester.{_be_init}
---


## OnInit

Create indicator handle:
```
g_ema_handle = {ema_init};
if (g_ema_handle == INVALID_HANDLE) return INIT_FAILED;
```

---

## OnTick Logic

{_be_step}### Step 1 — New-bar guard
Compare `iTime(_Symbol, PERIOD_CURRENT, 0)` with `g_last_bar_time`. If same bar → return.
Update `g_last_bar_time` and proceed.

### Step 2 — Update swing-point state

Compute buffer size and candidate index as **MQL5 runtime variables** from the `swing_n_before` and `swing_n_after` inputs.
Do NOT hardcode these — they must adapt when the user changes the inputs in EA settings.

**CRITICAL — declare dynamic arrays and call ArraySetAsSeries BEFORE CopyXxx:**
```
int   cand       = swing_n_after + 1;                        // series index of the swing candidate bar
int   total_bars = swing_n_after + 1 + swing_n_before + 1;  // right side + candidate + left side + one extra
if(Bars(_Symbol, PERIOD_CURRENT) < total_bars + 5) return;

double high[], low[], close[];
ArraySetAsSeries(high,  true);   // MUST be set before CopyXxx so index 0 = current bar
ArraySetAsSeries(low,   true);
ArraySetAsSeries(close, true);
if(CopyHigh (_Symbol, PERIOD_CURRENT, 0, total_bars, high)  < total_bars) return;
if(CopyLow  (_Symbol, PERIOD_CURRENT, 0, total_bars, low)   < total_bars) return;
if(CopyClose(_Symbol, PERIOD_CURRENT, 0, total_bars, close) < total_bars) return;
```

In series order: index 0=current bar, index 1=last closed bar, index cand=candidate ({swing_n_after + 1} bars back).
Right side (more recent) = indices 1..cand-1 ({swing_n_after} bars); left side (older) = indices cand+1..cand+{swing_n_before}. No lookahead.

Track two flags this bar: `bool sh_just_fired = false` and `bool sl_just_fired = false`.

**Check for Swing High (H1 candidate):**
```
bool is_sh = true;
for(int k = 1; k <= swing_n_after; k++) {{
    if(high[cand] <= high[cand - k]) {{ is_sh = false; break; }}  // right side (more recent)
}}
for(int k = 1; k <= swing_n_before; k++) {{
    if(high[cand] <= high[cand + k]) {{ is_sh = false; break; }}  // left side  (older)
}}
if(is_sh) {{
    double sh_p = high[cand];
    g_last_sh = sh_p;
    g_hl = 0.0;                                  // reset — need fresh HL after new H1
    sh_just_fired = true;
    // cancel any outstanding buy stop — structure invalidated by new H1
    if(g_pending_buy_ticket > 0) {{
        if(OrderSelect(g_pending_buy_ticket))   // guard: only call Delete if still pending (may have already filled)
            trade.OrderDelete(g_pending_buy_ticket);
        g_pending_buy_ticket = 0;{_reset_buy_cancel}{_reset_buy_bars}
    }}
    if(g_last_sl > 0.0 && sh_p > g_last_sl)
        g_lh = sh_p;                             // this SH also qualifies as LH for bearish N
}}
```

**Check for Swing Low (L1 candidate):**
```
bool is_sl = true;
for(int k = 1; k <= swing_n_after; k++) {{
    if(low[cand] >= low[cand - k]) {{ is_sl = false; break; }}   // right side
}}
for(int k = 1; k <= swing_n_before; k++) {{
    if(low[cand] >= low[cand + k]) {{ is_sl = false; break; }}   // left side
}}
if(is_sl) {{
    double sl_p = low[cand];
    g_last_sl = sl_p;
    g_lh = 0.0;                                  // reset — need fresh LH after new L1
    sl_just_fired = true;
    // cancel any outstanding sell stop — structure invalidated by new L1
    if(g_pending_sell_ticket > 0) {{
        if(OrderSelect(g_pending_sell_ticket))   // guard: only call Delete if still pending (may have already filled)
            trade.OrderDelete(g_pending_sell_ticket);
        g_pending_sell_ticket = 0;{_reset_sell_cancel}{_reset_sell_bars}
    }}
    if(g_last_sh > 0.0 && sl_p < g_last_sh)
        g_hl = sl_p;                             // this SL also qualifies as HL for bullish N
}}
```

> Always run the swing-high check first, then the swing-low check — matching the Python backtest loop order.

### Step 2.5 — Cancel stale pending stop orders

{_cancel_step}

### Step 3 — Skip if at position limit
If open positions for this symbol + magic number ≥ max_positions → return.

### Step 4 — Session filter
{session_sec}

### Step 5 — EMA trend filter
```
{ema_copy}
bool in_uptrend   = (close[1] > ema1);
bool in_downtrend = (close[1] < ema1);
```

### Step 6 — Sideways filter
{filter_desc}

### Step 7 — Arm structure: place pending stop orders

**Arm BUY STOP** — all of the following must be true:
- `sl_just_fired` (HL was confirmed this bar)
- `g_last_sh > 0.0` (have a confirmed H1)
- `g_hl > 0.0` (HL is set — the pullback is in place)
- `close[1] < g_last_sh` (price has NOT yet broken above H1)
- `g_pending_buy_ticket == 0` (no existing buy stop for this setup)
- `in_uptrend` (EMA filter)
- Sideways filter: long direction allowed

**Arm SELL STOP** — all of the following must be true:
- `sh_just_fired` (LH was confirmed this bar)
- `g_last_sl > 0.0` (have a confirmed L1)
- `g_lh > 0.0` (LH is set — the bounce is in place)
- `close[1] > g_last_sl` (price has NOT yet broken below L1)
- `g_pending_sell_ticket == 0` (no existing sell stop for this setup)
- `in_downtrend` (EMA filter)
- Sideways filter: short direction allowed

### Step 8 — Period SL limit check
Before placing any order, verify the period SL count is within limit (see Risk Management section).

### Step 9 — Order placement (pending stop orders)

SL and TP are anchored to the **stop price** (the breakout level), not the current bar close.
This matches the Python backtest where `dist = entry_stop − sl_price` and `tp = entry_stop + rr × dist`.

**BUY STOP at g_last_sh:**
```
double stop_price = g_last_sh;
double sl         = {sl_long_formula};
double sl_dist    = stop_price - sl;            // distance from breakout level to SL
// Guard: sl_dist > 0
double tp         = stop_price + {rr} * sl_dist;
double ask        = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
// Guard: stop_price > ask (stop must be above current price)
double lots       = ComputeLots(stop_price, sl);
trade.BuyStop(lots, stop_price, _Symbol, sl, tp, ORDER_TIME_GTC, 0, "NS_BUY_STOP");
g_pending_buy_ticket  = trade.ResultOrder();{_store_buy_cancel}{_store_buy_bars}
g_hl = 0.0;                                     // reset — need new structure before arming again
```

**SELL STOP at g_last_sl:**
```
double stop_price = g_last_sl;
double sl         = {sl_short_formula};
double sl_dist    = sl - stop_price;            // distance from breakout level to SL
// Guard: sl_dist > 0
double tp         = stop_price - {rr} * sl_dist;
double bid        = SymbolInfoDouble(_Symbol, SYMBOL_BID);
// Guard: stop_price < bid (stop must be below current price)
double lots       = ComputeLots(stop_price, sl);
trade.SellStop(lots, stop_price, _Symbol, sl, tp, ORDER_TIME_GTC, 0, "NS_SELL_STOP");
g_pending_sell_ticket = trade.ResultOrder();{_store_sell_cancel}{_store_sell_bars}
g_lh = 0.0;                                     // reset — need new structure before arming again
```

### Step 10 — Sync filled/cancelled pending tickets

After the new-bar logic, check whether `g_pending_buy_ticket` or `g_pending_sell_ticket` are
still live (use `OrderSelect` or iterate `OrdersTotal()`). If an order no longer exists
(filled or externally cancelled), reset the ticket, cancel level, and bar counter to 0.

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
    elif strategy == "n_structure":
        return _prompt_n_structure(params, lang, mt_ver, perf, param_lines_str)
    else:
        return _prompt_william_fractals(params, lang, mt_ver, perf, param_lines_str)


def _strip_fences(code: str) -> str:
    lines = code.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _to_inp_name(snake: str) -> str:
    """Convert snake_case param name to InpCamelCase MQL variable name."""
    return "Inp" + "".join(w.capitalize() for w in snake.split("_"))


def _patch_ea_code(code: str, params: dict, results: dict) -> str:
    for name, value in params.items():
        mql_val = _format_mql_value(value)
        # Try both snake_case and InpCamelCase variable names
        for var_name in (name, _to_inp_name(name)):
            pattern = rf'(\binput\s+\w+\s+{re.escape(var_name)}\s*=\s*)([^;]+)(;)'
            code = re.sub(pattern, rf'\g<1>{mql_val}\3', code)

    stat_subs = [
        (r'(Total trades\s*:\s*)[\d]+',         rf'\g<1>{results.get("total_trades", 0)}'),
        (r'(Win rate\s*:\s*)[\d.]+',            rf'\g<1>{results.get("win_rate_pct", 0):.1f}'),
        (r'(Profit factor\s*:\s*)[\d.]+',       rf'\g<1>{results.get("profit_factor", 0):.3f}'),
        (r'(Total return\s*:\s*)-?[\d.]+',      rf'\g<1>{results.get("total_return_pct", 0):.2f}'),
        (r'(Max drawdown\s*:\s*)[\d.]+',        rf'\g<1>{results.get("max_drawdown_pct", 0):.2f}'),
    ]
    for pattern, repl in stat_subs:
        code = re.sub(pattern, repl, code)

    return code


def _format_mql_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    return f'"{value}"'


def _build_filename(strategy: str, params: dict, platform: str) -> str:
    ext           = "mq4" if platform == "MT4" else "mq5"
    tf            = str(params.get("timeframe", "H1"))
    strategy_slug = strategy.replace("_", "")
    return f"{strategy_slug}_{tf}_{platform}.{ext}"


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

    prompt = _build_prompt(strategy, params, results, platform)

    risk_recovery  = float(params.get("risk_recovery", 0.0))
    trail_recovery = bool(params.get("trail_recovery", False))
    compound       = bool(params.get("compound", False))
    risk_mode = "trail" if (risk_recovery > 0 and trail_recovery) else ("rec" if risk_recovery > 0 else "base")
    compound_mode = "compound" if compound else "fixed"
    cache_path = RESULT_DIR / f"{strategy}_{platform}_{risk_mode}_{compound_mode}.ea.json"
    if cache_path.exists():
        with open(cache_path) as f:
            cached = json.load(f)
        patched_code = _patch_ea_code(cached["code"], params, results)
        return EAResponse(
            code=patched_code,
            platform=platform,
            filename=_build_filename(strategy, params, platform),
            prompt=prompt,
        )

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(500, "ANTHROPIC_API_KEY is not configured — add it to the .env file")

    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=600.0,   # EA prompts are large — allow up to 10 min
        max_retries=3,   # retry on transient connection errors
    )

    try:
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
    except anthropic.APIConnectionError as exc:
        raise HTTPException(
            503,
            f"Could not reach the Anthropic API — check your network connection and try again. ({exc})",
        ) from exc
    except anthropic.RateLimitError as exc:
        raise HTTPException(429, f"Anthropic rate limit reached — wait a moment and retry. ({exc})") from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(
            502,
            f"Anthropic API returned an error (HTTP {exc.status_code}): {exc.message}",
        ) from exc

    code = _strip_fences(message.content[0].text)

    with open(cache_path, "w") as f:
        json.dump({"code": code}, f)

    return EAResponse(
        code=code,
        platform=platform,
        filename=_build_filename(strategy, params, platform),
        prompt=prompt,
    )
