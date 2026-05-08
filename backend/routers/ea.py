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
    error: str = ""


class EAPromptResponse(BaseModel):
    prompt: str
    filename: str


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


def _risk_block_fixed_lot(params: dict, lang: str, fixed_lot: float) -> str:
    """Risk block variant for fixed-lot mode (no risk-% calculation)."""
    from backtest import PAIR_CONFIG
    symbol          = params.get("symbol", "XAUUSD")
    pair_cfg        = PAIR_CONFIG.get(symbol, PAIR_CONFIG["XAUUSD"])
    contract_size   = pair_cfg["contract_size"]
    initial_capital = float(params.get("initial_capital", 10000))
    commission_per_lot_v = float(params.get("commission_per_lot", 3.5))
    breakeven_r     = params.get("breakeven_r")
    breakeven_sl_r  = float(params.get("breakeven_sl_r", 0.0))
    max_sl          = params.get("max_sl_per_period")
    sl_period       = params.get("sl_period", "none")

    compute_lots_spec = (
        f"\n### ComputeLots Helper Function\n"
        f"Implement `double ComputeLots(double entry, double sl)` — call this before every order placement:\n"
        f"```\n"
        f"double ComputeLots(double entry, double sl) {{\n"
        f"    double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);\n"
        f"    double lots = MathFloor(InpFixedLot / step) * step;\n"
        f"    return MathMax(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN),\n"
        f"                   MathMin(lots, SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX)));\n"
        f"}}\n"
        f"```\n"
        f"- `InpFixedLot` is an `input double` (default {fixed_lot:g}) — the same lot size used on every trade.\n"
        f"- `entry` and `sl` parameters are accepted for API consistency but not used for lot sizing.\n"
        f"- Always expose `InpInitialCapital` as an `input double` (default {initial_capital:.0f}) — used only for equity tracking in the comment block.\n"
    )

    commission_note = (
        f"- Commission: {commission_per_lot_v:.2f} USD per lot per side "
        f"(round-trip = {fixed_lot:g} × {commission_per_lot_v:.2f} × 2 = {fixed_lot * commission_per_lot_v * 2:.2f} USD per trade). "
        f"Expose `InpCommissionPerLot` as an `input double` (default {commission_per_lot_v:.2f}). "
        f"In ECN/STP accounts the broker charges commission automatically — informational only.\n"
    )

    return (
        f"## Risk Management\n"
        f"- **Risk mode: Fixed Lot** — {fixed_lot:g} lots on every entry, regardless of SL distance.\n"
        f"- Max open positions: {params.get('max_positions', 1)}\n"
        f"- {symbol} contract_size = {contract_size:,} per lot\n"
        f"- {commission_note}"
        + compute_lots_spec
        + _breakeven_section(breakeven_r, breakeven_sl_r)
        + _sl_limit_section(max_sl, sl_period)
    )


def _risk_block(params: dict, lang: str) -> str:
    from backtest import PAIR_CONFIG
    fixed_lot = params.get("fixed_lot")
    if fixed_lot is not None:
        return _risk_block_fixed_lot(params, lang, float(fixed_lot))

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
# Chart stats / comment panel section
# ---------------------------------------------------------------------------

def _chart_stats_section(strategy_name: str) -> str:
    return f"""## Live Chart Statistics Panel

Implement a real-time statistics overlay using `Comment()` so traders can monitor the EA's performance directly on the chart.

### Global Stat Variables

Declare these globals (initialise all to 0 / 0.0 / false):
```
int    g_stat_trades   = 0;    // total closed trades
int    g_stat_wins     = 0;    // winning trades (profit > 0)
int    g_stat_losses   = 0;    // losing trades  (profit < 0)
int    g_stat_be       = 0;    // break-even trades (profit == 0)
double g_stat_net_pnl  = 0.0;  // net P&L in account currency (sum of closed profits)
double g_stat_peak_eq  = 0.0;  // peak equity reached so far
double g_stat_max_dd   = 0.0;  // maximum drawdown % observed
```

Seed `g_stat_peak_eq = AccountInfoDouble(ACCOUNT_BALANCE)` in `OnInit` (use balance, not equity, so the panel starts correctly after restarts).

### OnTradeTransaction — Update Counters on Close

```
void OnTradeTransaction(const MqlTradeTransaction& trans,
                        const MqlTradeRequest& req,
                        const MqlTradeResult& res)
{{
    if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
    ulong ticket = trans.deal;
    if(!HistoryDealSelect(ticket)) return;
    if(HistoryDealGetInteger(ticket, DEAL_MAGIC)  != InpMagicNumber) return;
    if(HistoryDealGetString(ticket, DEAL_SYMBOL)  != _Symbol)        return;
    if((ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY) != DEAL_ENTRY_OUT) return;

    double profit = HistoryDealGetDouble(ticket, DEAL_PROFIT)
                  + HistoryDealGetDouble(ticket, DEAL_COMMISSION)
                  + HistoryDealGetDouble(ticket, DEAL_SWAP);

    g_stat_trades++;
    g_stat_net_pnl += profit;
    if(profit > 0.0)       g_stat_wins++;
    else if(profit < 0.0)  g_stat_losses++;
    else                   g_stat_be++;
}}
```

### UpdatePanel() — Comment Display

```
void UpdatePanel()
{{
    double balance  = AccountInfoDouble(ACCOUNT_BALANCE);
    double equity   = AccountInfoDouble(ACCOUNT_EQUITY);

    // Track peak equity and max drawdown
    if(equity > g_stat_peak_eq) g_stat_peak_eq = equity;
    double dd_pct = (g_stat_peak_eq > 0.0)
                    ? (g_stat_peak_eq - equity) / g_stat_peak_eq * 100.0
                    : 0.0;
    if(dd_pct > g_stat_max_dd) g_stat_max_dd = dd_pct;

    double win_rate = (g_stat_trades > 0)
                      ? (double)g_stat_wins / g_stat_trades * 100.0
                      : 0.0;

    // Open position info
    string pos_info = "None";
    for(int i = 0; i < PositionsTotal(); i++)
    {{
        if(PositionGetSymbol(i) != _Symbol) continue;
        if(PositionGetInteger(POSITION_MAGIC) != InpMagicNumber) continue;
        string  dir    = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";
        double  entry  = PositionGetDouble(POSITION_PRICE_OPEN);
        double  sl_p   = PositionGetDouble(POSITION_SL);
        double  tp_p   = PositionGetDouble(POSITION_TP);
        double  upnl   = PositionGetDouble(POSITION_PROFIT);
        pos_info = StringFormat("%s @ %.5f  SL:%.5f  TP:%.5f  uPnL:%.2f",
                                dir, entry, sl_p, tp_p, upnl);
        break;  // show first matching position
    }}

    Comment(StringFormat(
        "=== {strategy_name} ===\\n"
        "Symbol / TF  : %s  %s\\n"
        "─────────────────────────\\n"
        "Trades       : %d  (W:%d  L:%d  BE:%d)\\n"
        "Win Rate     : %.1f%%\\n"
        "Net P&L      : %.2f\\n"
        "Max DD       : %.2f%%\\n"
        "─────────────────────────\\n"
        "Balance      : %.2f\\n"
        "Equity       : %.2f\\n"
        "─────────────────────────\\n"
        "Open Pos     : %s\\n",
        _Symbol, EnumToString(Period()),
        g_stat_trades, g_stat_wins, g_stat_losses, g_stat_be,
        win_rate,
        g_stat_net_pnl,
        g_stat_max_dd,
        balance,
        equity,
        pos_info
    ));
}}
```

- Call `UpdatePanel()` at the **end** of every `OnTick()` execution (after all order logic).
- Call `Comment("")` in `OnDeinit` to clear the overlay when the EA is removed.
"""


# ---------------------------------------------------------------------------
# Shared helpers for prompt builders
# ---------------------------------------------------------------------------

def _risk_input_group_line(params: dict) -> str:
    """Builds the === Risk Management === input group line dynamically."""
    fixed_lot      = params.get("fixed_lot")
    risk_recovery  = float(params.get("risk_recovery", 0.0))
    trail_recovery = bool(params.get("trail_recovery", False))

    if fixed_lot is not None:
        inputs = ["initial_capital", "fixed_lot"]
    else:
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

{_chart_stats_section("William Fractal Breakout")}

---

{_code_req(lang, mt_ver)}"""


# ---------------------------------------------------------------------------
# Momentum Candle prompt
# ---------------------------------------------------------------------------

def _prompt_momentum_candle(
    params: dict, lang: str, mt_ver: str,
    perf: str, param_lines_str: str,
) -> str:
    body_ratio       = params.get("body_ratio_min", 0.70)
    vol_factor       = params.get("volume_factor", 1.5)
    vol_lookback     = int(params.get("volume_lookback", 23))
    retracement_pct  = params.get("retracement_pct", 0.50)
    sl_mult          = params.get("sl_mult", 1.0)
    tp_mult          = params.get("tp_mult", 1.0)
    max_pending_bars = int(params.get("max_pending_bars", 5))
    sessions         = params.get("sessions", "all")
    ema_mode         = params.get("ema_filter_mode", "single")
    ema_tf           = params.get("ema_timeframe", "same")
    body_pct         = int(round(body_ratio * 100))
    session_sec      = _session_filter_section(sessions)
    filter_desc      = _sideways_filter_desc(params)
    risk_mgmt_line   = _risk_input_group_line(params)

    ema_g_decls, ema_init_code, ema_copy_code = _ema_init_block(params)

    ema_filter_label = {
        "none":   "None — signals in both directions regardless of EMA",
        "single": "Single EMA — bullish MC only when close[1] > EMA, bearish when close[1] < EMA",
        "dual":   "Dual EMA — bullish MC only when fast EMA > slow EMA, bearish when fast EMA < slow EMA",
    }.get(ema_mode, ema_mode)
    tf_note = "" if ema_tf == "same" else f" (sourced from **{ema_tf}** timeframe)"
    ema_dual_extra = ", ema_fast_period" if ema_mode == "dual" else ""

    if ema_mode == "dual":
        ema_signal_logic = (
            "- **Bullish MC**: `close[1] > open[1]` AND `fast_ema1 > slow_ema1` → BUY signal\n"
            "- **Bearish MC**: `close[1] < open[1]` AND `fast_ema1 < slow_ema1` → SELL signal"
        )
    elif ema_mode == "single":
        ema_signal_logic = (
            "- **Bullish MC**: `close[1] > open[1]` AND `close[1] > ema1` → BUY signal\n"
            "- **Bearish MC**: `close[1] < open[1]` AND `close[1] < ema1` → SELL signal"
        )
    else:
        ema_signal_logic = (
            "- **Bullish MC**: `close[1] > open[1]` → BUY signal\n"
            "- **Bearish MC**: `close[1] < open[1]` → SELL signal"
        )

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
- `"=== Trend Filter ==="` — ema_filter_mode, ema_period{ema_dual_extra}, ema_timeframe
- `"=== Momentum Candle ==="` — body_ratio_min, volume_factor, volume_lookback
- `"=== Entry & Exit ==="` — retracement_pct, sl_mult, tp_mult, max_pending_bars
- `"=== Session Filter ==="` — sessions (display label)
- `"=== Sideways Filter ==="` — sideways_filter and sub-parameters
{risk_mgmt_line}
- `"=== Execution ==="` — magic_number, commission_per_lot

---

## Global State

```
{ema_g_decls}datetime g_last_bar_time   // new-bar guard
ulong    g_pending_ticket  // ticket of active limit order (0 = none)
int      g_pending_dir     // 1 = buy limit, -1 = sell limit, 0 = none
double   g_mc_high         // high of the momentum candle that placed the order
double   g_mc_low          // low  of the momentum candle that placed the order
int      g_pending_bars    // bars elapsed since order was placed
```

---

## OnInit

```
{ema_init_code}
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
**EMA filter mode**: {ema_filter_label}{tf_note}
```
{ema_copy_code}
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

Signal direction (apply EMA filter from Step 5):
{ema_signal_logic}
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

{_chart_stats_section("Momentum Candle Scalping")}

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
    sl_tp_mode       = params.get("sl_tp_mode", "rr")
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
    _TF_MAP = {"M1": "PERIOD_M1", "M5": "PERIOD_M5", "M15": "PERIOD_M15", "M30": "PERIOD_M30",
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

    # SL/TP mode: pips — fixed pip distances override swing-based SL
    if sl_tp_mode == "pips":
        from backtest import PAIR_CONFIG
        _symbol   = params.get("symbol", "XAUUSD")
        _pip_mult = PAIR_CONFIG.get(_symbol, PAIR_CONFIG["XAUUSD"])["pip_mult"]
        _sl_pips  = float(params.get("sl_pips", 200.0))
        _tp_pips  = float(params.get("tp_pips", 400.0))
        _step9_sl_tp = f"""**SL/TP mode: pips** — `sl_mode` and `rr_ratio` are ignored.
Expose `InpPipMult` as `input double` (default **{_pip_mult:g}** for {_symbol}).

```
double sl_d = InpSlPips / InpPipMult;   // default {_sl_pips:g} pips → {_sl_pips / _pip_mult:g} price units
double tp_d = InpTpPips / InpPipMult;   // default {_tp_pips:g} pips → {_tp_pips / _pip_mult:g} price units
```

Long:  `sl = stop_price - sl_d`,  `tp = stop_price + tp_d`
Short: `sl = stop_price + sl_d`,  `tp = stop_price - tp_d`"""
    else:
        _step9_sl_tp = f"""**SL/TP mode: RR** — SL anchored to swing structure, TP = `stop_price ± {rr} × sl_dist`.

SL mode: **{sl_mode}** — {sl_mode_desc}.

BUY STOP:
  `sl = {sl_long_formula}`
  `sl_dist = stop_price - sl`
  `tp = stop_price + {rr} * sl_dist`

SELL STOP:
  `sl = {sl_short_formula}`
  `sl_dist = sl - stop_price`
  `tp = stop_price - {rr} * sl_dist`"""

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

{_step9_sl_tp}

**BUY STOP at g_last_sh:**
```
double stop_price = g_last_sh;
// Compute sl and tp per SL/TP mode described above
double ask        = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
// Guard: sl_dist > 0, stop_price > ask (stop must be above current price)
double lots       = ComputeLots(stop_price, sl);
trade.BuyStop(lots, stop_price, _Symbol, sl, tp, ORDER_TIME_GTC, 0, "NS_BUY_STOP");
g_pending_buy_ticket  = trade.ResultOrder();{_store_buy_cancel}{_store_buy_bars}
g_hl = 0.0;                                     // reset — need new structure before arming again
```

**SELL STOP at g_last_sl:**
```
double stop_price = g_last_sl;
// Compute sl and tp per SL/TP mode described above
double bid        = SymbolInfoDouble(_Symbol, SYMBOL_BID);
// Guard: sl_dist > 0, stop_price < bid (stop must be below current price)
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

{_chart_stats_section("N Structure Breakout")}

---

{_code_req(lang, mt_ver)}"""


# ---------------------------------------------------------------------------
# Fair Value Gap prompt builder
# ---------------------------------------------------------------------------

def _prompt_fair_value_gap(
    params: dict, lang: str, mt_ver: str,
    perf: str, param_lines_str: str,
) -> str:
    rr           = params.get("rr_ratio", 2.0)
    ema_p        = params.get("ema_period", 200)
    fvg_min      = params.get("fvg_min_size", 0.0)
    min_sl_pips  = params.get("min_sl_pips", 5.0)
    max_bars     = int(params.get("max_fvg_bars", 20))
    entry_mode   = params.get("entry_mode", "zone_mid")
    sl_mode      = params.get("sl_mode", "fvg_edge")
    sessions     = params.get("sessions", "all")
    sideways     = params.get("sideways_filter", "none")
    filter_desc  = _sideways_filter_desc(params)
    session_sec  = _session_filter_section(sessions)
    risk_mgmt_line = _risk_input_group_line(params)

    # MQL bar indexing (used throughout): bar[0]=forming, bar[1]=last closed (signal bar),
    # bar[2]=impulse candle, bar[3]=first candle of the 3-bar FVG pattern.

    if entry_mode == "zone_top":
        entry_desc = "zone_high (upper boundary of the FVG gap) — same reference for both long and short"
    elif entry_mode == "zone_bottom":
        entry_desc = "zone_low (lower boundary of the FVG gap) — same reference for both long and short"
    else:
        entry_desc = "midpoint of the FVG zone: (zone_low + zone_high) / 2"

    if sl_mode == "signal_candle":
        sl_desc_long  = "iLow(NULL,0,1)  — low of bar[1] (the last closed signal bar)"
        sl_desc_short = "iHigh(NULL,0,1) — high of bar[1] (the last closed signal bar)"
    elif sl_mode == "impulse_candle":
        sl_desc_long  = "zone.impulse_low  — low of bar[2] stored when the FVG was detected"
        sl_desc_short = "zone.impulse_high — high of bar[2] stored when the FVG was detected"
    else:  # fvg_edge
        sl_desc_long  = "zone.zone_low  — lower edge of the bullish FVG gap"
        sl_desc_short = "zone.zone_high — upper edge of the bearish FVG gap"

    return f"""## Strategy: Fair Value Gap (FVG)
## Platform: MetaTrader {mt_ver} ({lang})
## Instrument: {params.get('symbol', 'XAUUSD')}
## Timeframe: {params.get('timeframe', 'H1')}

### Backtest Performance
{perf}

### Parameters
{param_lines_str}

---

## Bar Index Convention (MQL)

All bar references use standard MQL indexing on the **chart timeframe**:
- `bar[0]` — current forming bar (only its open is valid)
- `bar[1]` — last **closed** bar → this is the **signal bar** where entry conditions are evaluated
- `bar[2]` — 2 bars ago → the **impulse candle** (middle of the 3-bar FVG pattern)
- `bar[3]` — 3 bars ago → the **first candle** of the 3-bar FVG pattern

The EA fires once per bar via a new-bar guard, so "current Ask/Bid" at entry time equals bar[0]'s open price — matching the backtest engine's no-lookahead rule (signal on bar[1] → fill at bar[0] open).

---

## Input Groups

Group all `input` variables under labelled sections using `input group "..."` (MQL5) or string separator inputs (MQL4):
- `"=== Trend Filter ==="` — ema_period, ema_timeframe
- `"=== FVG Detection ==="` — fvg_min_size, max_fvg_bars, min_sl_pips
- `"=== Entry & Exit ==="` — rr_ratio, entry_mode, sl_mode
- `"=== Session Filter ==="` — sessions (display label)
- `"=== Sideways Filter ==="` — sideways_filter and its sub-parameters
{risk_mgmt_line}
- `"=== Execution ==="` — magic_number, commission_per_lot

---

## Global State

```
int      g_ema_handle      // created in OnInit
datetime g_last_bar_time   // new-bar guard

struct FVGZone {{
  int      direction;      // 1=bullish, -1=bearish
  double   zone_low;       // lower bound of the gap
  double   zone_high;      // upper bound of the gap
  double   impulse_low;    // bar[2].low at creation time
  double   impulse_high;   // bar[2].high at creation time
  int      age;            // bars elapsed since creation (starts at 0, not incremented on creation bar)
  datetime created_time;   // iTime(NULL,0,1) at creation — used to skip same-bar entry
  bool     active;
}};
FVGZone g_fvg_zones[20];
int     g_fvg_count;
```

---

## OnTick Logic

### Step 1 — New-bar guard
Read `iTime(NULL,0,0)`. If equal to `g_last_bar_time` → return (same bar, nothing to do).
Otherwise update `g_last_bar_time = iTime(NULL,0,0)` and proceed.

### Step 2 — Detect new FVG using bar[1], bar[2], bar[3]

Read OHLC for the three relevant closed bars:
```
double h1 = iHigh(NULL,0,1),  l1 = iLow(NULL,0,1);   // bar[1] — last closed (signal bar)
double h2 = iHigh(NULL,0,2),  l2 = iLow(NULL,0,2);   // bar[2] — impulse candle
double h3 = iHigh(NULL,0,3),  l3 = iLow(NULL,0,3);   // bar[3] — first FVG candle
datetime bar1_time = iTime(NULL,0,1);
```

**Bullish FVG** — `h3 < l1` (gap between bar[3].high and bar[1].low):
- gap_size = l1 - h3
- If gap_size >= {fvg_min}: add zone {{ direction=1, zone_low=h3, zone_high=l1, impulse_low=l2, impulse_high=h2, age=0, created_time=bar1_time, active=true }}

**Bearish FVG** — `l3 > h1` (gap between bar[1].high and bar[3].low):
- gap_size = l3 - h1
- If gap_size >= {fvg_min}: add zone {{ direction=-1, zone_low=h1, zone_high=l3, impulse_low=l2, impulse_high=h2, age=0, created_time=bar1_time, active=true }}

**IMPORTANT — gap_size comparison**: `{fvg_min}` is a raw price value (e.g. 2.0 means a $2 gap for XAUUSD). Compare `gap_size >= {fvg_min}` directly. Do **NOT** multiply by `_Point` — that would shrink the threshold by a factor of 100 and accept nearly every gap.

### Step 3 — Age and prune stale zones

For each active zone where `zone.created_time != iTime(NULL,0,1)` (i.e., NOT created this bar):
- Increment `zone.age`
- If `zone.age > {max_bars}`: mark inactive and skip

Zones created this bar (created_time == iTime(NULL,0,1)) keep age=0 and are NOT eligible for entry this bar.

### Step 4 — EMA trend filter

Read EMA({ema_p}) at bar[1] (the last closed bar):
- MQL4: `double ema_val = iMA(NULL,0,{ema_p},0,MODE_EMA,PRICE_CLOSE,1);`
- MQL5: `double ema_buf[1]; CopyBuffer(g_ema_handle,0,1,1,ema_buf); double ema_val=ema_buf[0];`

### Step 5 — Sideways filter
{filter_desc}

### Step 6 — Check active FVG zones for entry

Read bar[1] values (the signal bar — last closed bar):
```
double sig_low   = iLow(NULL,0,1);
double sig_high  = iHigh(NULL,0,1);
double sig_close = iClose(NULL,0,1);
```

Iterate active zones. Skip any zone where `zone.created_time == iTime(NULL,0,1)` (just formed — no same-bar entry). Process at most one entry per bar (first qualifying zone wins); after a trade is placed, stop iterating.

**Bullish FVG entry** (all conditions must be true):
- `zone.direction == 1`
- `sig_low <= zone.zone_high`  — bar[1] retraced down into the gap
- `sig_close >= zone.zone_low` — bar[1] closed inside or above the gap bottom
- `sig_close > ema_val`        — bar[1] closed above EMA (uptrend)
- `trend_ok_long`              — sideways filter passed for longs
- No open BUY position with this EA's magic number

If all true:
- `entry_ref = {entry_desc}`  ← used only for TP calculation
- `sl_price  = {sl_desc_long}`
- `ask       = SymbolInfoDouble(_Symbol, SYMBOL_ASK)`  ← actual fill price
- `lot_sl_dist = ask - sl_price`   ← **use Ask→SL for lot sizing**, NOT entry_ref→SL
- If `lot_sl_dist <= 0`: skip (SL above Ask — degenerate)
- `tp_sl_dist = entry_ref - sl_price`
- If `tp_sl_dist < {min_sl_pips}`: skip — 1R distance too small (below min_sl_pips={min_sl_pips})
- `tp_price  = entry_ref + {rr} * tp_sl_dist`
- Compute lots using `lot_sl_dist` (Ask to SL): `lots = risk_amount / (lot_sl_dist * contract_size)`
- Place **BUY** market order at Ask; set SL=sl_price, TP=tp_price
- Mark zone inactive

**IMPORTANT — lot sizing for BUY**: use `|Ask - sl_price|` as the SL distance, not `|entry_ref - sl_price|`. The entry happens at Ask (the real fill price), not at entry_ref (which is only used to anchor the TP level).

**Bearish FVG entry** (all conditions must be true):
- `zone.direction == -1`
- `sig_high >= zone.zone_low`   — bar[1] retraced up into the gap
- `sig_close <= zone.zone_high` — bar[1] closed inside or below the gap top
- `sig_close < ema_val`         — bar[1] closed below EMA (downtrend)
- `trend_ok_short`              — sideways filter passed for shorts
- No open SELL position with this EA's magic number

If all true:
- `entry_ref = {entry_desc}`  ← used only for TP calculation
- `sl_price  = {sl_desc_short}`
- `bid       = SymbolInfoDouble(_Symbol, SYMBOL_BID)`  ← actual fill price
- `lot_sl_dist = sl_price - bid`   ← **use SL→Bid for lot sizing**, NOT sl_price→entry_ref
- If `lot_sl_dist <= 0`: skip (SL below Bid — degenerate)
- `tp_sl_dist = sl_price - entry_ref`
- If `tp_sl_dist < {min_sl_pips}`: skip — 1R distance too small (below min_sl_pips={min_sl_pips})
- If `tp_sl_dist <= 0`: skip this zone
- `tp_price  = entry_ref - {rr} * tp_sl_dist`
- Compute lots using `lot_sl_dist` (SL to Bid): `lots = risk_amount / (lot_sl_dist * contract_size)`
- Place **SELL** market order at Bid; set SL=sl_price, TP=tp_price
- Mark zone inactive

**IMPORTANT — lot sizing for SELL**: use `|sl_price - Bid|` as the SL distance, not `|sl_price - entry_ref|`. The entry happens at Bid (the real fill price), not at entry_ref.

### Step 7 — Session filter
{session_sec}

---

## Risk & Lot Sizing
{_risk_block(params, lang)}

---

{_chart_stats_section("Fair Value Gap")}

---

## Code Requirements
{_code_req(lang, mt_ver)}"""


# ---------------------------------------------------------------------------
# Pip Breakout prompt
# ---------------------------------------------------------------------------

_TF_CONST: dict[str, str] = {
    "M1": "PERIOD_M1", "M5": "PERIOD_M5", "M15": "PERIOD_M15", "M30": "PERIOD_M30",
    "H1": "PERIOD_H1", "H4": "PERIOD_H4", "D1": "PERIOD_D1",
}


def _ema_init_block(params: dict, slow_handle: str = "g_ema_handle", fast_handle: str = "g_fast_ema_handle") -> tuple[str, str, str]:
    """
    Returns (global_decls, oninit_code, ontick_copy_code) for the EMA filter.
    Handles none / single / dual modes and same vs. HTF timeframes.
    """
    ema_p      = int(params.get("ema_period", 200))
    ema_fast_p = int(params.get("ema_fast_period", 50))
    ema_tf     = params.get("ema_timeframe", "same")
    mode       = params.get("ema_filter_mode", "single")

    tf_const = f"PERIOD_CURRENT" if ema_tf == "same" else _TF_CONST.get(ema_tf, f"PERIOD_{ema_tf}")
    tf_label = "chart timeframe" if ema_tf == "same" else f"{ema_tf} timeframe (higher-timeframe filter)"

    if mode == "none":
        return (
            "",
            "// EMA filter: none — no indicator handle needed",
            "// EMA filter: none\nbool ema_ok_long = true;\nbool ema_ok_short = true;",
        )

    if mode == "dual":
        g_decls = (
            f"int {slow_handle};   // EMA({ema_p}) on {tf_label}\n"
            f"int {fast_handle};   // EMA({ema_fast_p}) on {tf_label}\n"
        )
        init_code = (
            f"{slow_handle} = iMA(_Symbol, {tf_const}, {ema_p}, 0, MODE_EMA, PRICE_CLOSE);\n"
            f"if({slow_handle} == INVALID_HANDLE) return INIT_FAILED;\n"
            f"{fast_handle} = iMA(_Symbol, {tf_const}, {ema_fast_p}, 0, MODE_EMA, PRICE_CLOSE);\n"
            f"if({fast_handle} == INVALID_HANDLE) return INIT_FAILED;"
        )
        copy_code = (
            f"double slow_ema_buf[2], fast_ema_buf[2];\n"
            f"ArraySetAsSeries(slow_ema_buf, true);\n"
            f"ArraySetAsSeries(fast_ema_buf, true);\n"
            f"if(CopyBuffer({slow_handle}, 0, 0, 2, slow_ema_buf) < 2) return;\n"
            f"if(CopyBuffer({fast_handle}, 0, 0, 2, fast_ema_buf) < 2) return;\n"
            f"double slow_ema1 = slow_ema_buf[1];   // EMA({ema_p}) at bar[1]\n"
            f"double fast_ema1 = fast_ema_buf[1];   // EMA({ema_fast_p}) at bar[1]\n"
            f"// Dual EMA filter: fast > slow → uptrend (long allowed); fast < slow → downtrend (short allowed)\n"
            f"bool ema_ok_long  = (fast_ema1 > slow_ema1);\n"
            f"bool ema_ok_short = (fast_ema1 < slow_ema1);"
        )
    else:  # single
        g_decls = f"int {slow_handle};   // EMA({ema_p}) on {tf_label}\n"
        init_code = (
            f"{slow_handle} = iMA(_Symbol, {tf_const}, {ema_p}, 0, MODE_EMA, PRICE_CLOSE);\n"
            f"if({slow_handle} == INVALID_HANDLE) return INIT_FAILED;"
        )
        copy_code = (
            f"double ema_buf[2];\n"
            f"ArraySetAsSeries(ema_buf, true);\n"
            f"if(CopyBuffer({slow_handle}, 0, 0, 2, ema_buf) < 2) return;\n"
            f"double ema1 = ema_buf[1];   // EMA({ema_p}) at bar[1] on {tf_label}\n"
            f"// Single EMA filter: close[1] vs EMA\n"
            f"bool ema_ok_long  = (close1 > ema1);\n"
            f"bool ema_ok_short = (close1 < ema1);"
        )

    return g_decls, init_code, copy_code


def _prompt_pip_breakout(
    params: dict, lang: str, mt_ver: str,
    perf: str, param_lines_str: str,
) -> str:
    from backtest import PAIR_CONFIG

    symbol          = params.get("symbol", "XAUUSD")
    pip_mult        = PAIR_CONFIG.get(symbol, PAIR_CONFIG["XAUUSD"])["pip_mult"]

    level_detector  = params.get("level_detector", "rolling")
    lookback        = int(params.get("lookback_bars", 20))
    frac_n_before   = int(params.get("fractal_n_before", 5))
    frac_n_after    = int(params.get("fractal_n_after", 5))

    sl_tp_mode      = params.get("sl_tp_mode", "pips")
    sl_pips         = float(params.get("sl_pips", 200.0))
    tp_pips         = float(params.get("tp_pips", 400.0))
    sl_pct          = float(params.get("sl_pct", 1.0))
    tp_pct          = float(params.get("tp_pct", 2.0))

    entry_mode      = params.get("entry_mode", "close")
    entry_offset    = float(params.get("entry_offset_pips", 0.0))
    pending_cancel  = params.get("pending_cancel", "max_bars")
    max_pending_bars = int(params.get("max_pending_bars", 10))
    buf_pips        = float(params.get("pending_cancel_buffer_pips", 0.0))

    ema_mode        = params.get("ema_filter_mode", "single")
    ema_tf          = params.get("ema_timeframe", "same")
    sessions        = params.get("sessions", "all")

    session_sec     = _session_filter_section(sessions)
    filter_desc     = _sideways_filter_desc(params)
    risk_mgmt_line  = _risk_input_group_line(params)

    ema_g_decls, ema_init_code, ema_copy_code = _ema_init_block(params)

    # Derived flags
    has_stop_order  = (entry_mode == "touch") or (entry_offset > 0)
    use_max_bars    = has_stop_order and pending_cancel in ("max_bars", "both")
    use_sl_break    = has_stop_order and pending_cancel in ("sl_break", "both")

    ema_filter_label = {
        "none":   "None — signals in both directions regardless of EMA",
        "single": f"Single EMA — longs only when close[1] > EMA, shorts when close[1] < EMA",
        "dual":   f"Dual EMA — longs when fast EMA > slow EMA, shorts when fast EMA < slow EMA",
    }.get(ema_mode, ema_mode)
    tf_note = "" if ema_tf == "same" else f" (sourced from **{ema_tf}** timeframe)"

    # ── Level detection section ───────────────────────────────────────────────
    if level_detector == "fractal":
        level_det_input_lines = (
            f"- `\"=== Level Detection ===\"` — level_detector, fractal_n_before, fractal_n_after"
        )
        level_det_globals = (
            f"double   g_frac_high = 0.0;   // most-recently confirmed fractal high (carry-forward)\n"
            f"double   g_frac_low  = 0.0;   // most-recently confirmed fractal low  (carry-forward)\n"
        )
        level_det_init = (
            f"// Fractal level detection: no indicator handle needed — computed from price arrays"
        )
        level_det_step = f"""### Step 4 — Fractal level detection

At each new bar check bar index `cand = InpFractalNAfter + 1` (so the right side = bars 1..cand-1 are confirmed closed):

**Fractal high at `cand`:**
```
bool frac_high_ok = true;
for(int k = 1; k <= InpFractalNAfter; k++)  // right (more-recent) side
    if(highs[cand] <= highs[cand - k]) {{ frac_high_ok = false; break; }}
for(int k = 1; k <= InpFractalNBefore; k++) // left  (older) side
    if(highs[cand] <= highs[cand + k]) {{ frac_high_ok = false; break; }}
if(frac_high_ok) g_frac_high = highs[cand];
```

**Fractal low at `cand`:**
```
bool frac_low_ok = true;
for(int k = 1; k <= InpFractalNAfter; k++)
    if(lows[cand] >= lows[cand - k]) {{ frac_low_ok = false; break; }}
for(int k = 1; k <= InpFractalNBefore; k++)
    if(lows[cand] >= lows[cand + k]) {{ frac_low_ok = false; break; }}
if(frac_low_ok) g_frac_low = lows[cand];
```

Use `rh = g_frac_high` and `rl = g_frac_low` as the current resistance/support levels.
Carry-forward: `g_frac_high`/`g_frac_low` are only updated when a new fractal is confirmed; otherwise they hold their last value.

Required buffer size: `InpFractalNAfter + 1 + InpFractalNBefore + 1` bars.
```
int   cand        = InpFractalNAfter + 1;
int   total_frac  = InpFractalNAfter + 1 + InpFractalNBefore + 1;
if(Bars(_Symbol, PERIOD_CURRENT) < total_frac + 5) return;
double highs[], lows[], closes[];
ArraySetAsSeries(highs,  true);
ArraySetAsSeries(lows,   true);
ArraySetAsSeries(closes, true);
if(CopyHigh (_Symbol, PERIOD_CURRENT, 0, total_frac, highs)  < total_frac) return;
if(CopyLow  (_Symbol, PERIOD_CURRENT, 0, total_frac, lows)   < total_frac) return;
if(CopyClose(_Symbol, PERIOD_CURRENT, 0, total_frac, closes) < total_frac) return;
close1 = closes[1];   // refresh from full-window copy (close1 declared in Step 1B)
double rh = g_frac_high;
double rl = g_frac_low;
```"""
    else:
        level_det_input_lines = (
            f"- `\"=== Level Detection ===\"` — level_detector, lookback_bars"
        )
        level_det_globals = ""
        level_det_init = (
            f"// Rolling Donchian level detection: no indicator handle needed"
        )
        level_det_step = f"""### Step 4 — Rolling Donchian level detection

Window = bars[2..{lookback + 1}] (the {lookback} bars before bar[1], excluding bar[1] — no lookahead):

```
int   copy_count = InpLookbackBars + 2;
double highs[], lows[], closes[];
ArraySetAsSeries(highs,  true);
ArraySetAsSeries(lows,   true);
ArraySetAsSeries(closes, true);
if(Bars(_Symbol, PERIOD_CURRENT) < copy_count + 2) return;
if(CopyHigh (_Symbol, PERIOD_CURRENT, 0, copy_count, highs)  < copy_count) return;
if(CopyLow  (_Symbol, PERIOD_CURRENT, 0, copy_count, lows)   < copy_count) return;
if(CopyClose(_Symbol, PERIOD_CURRENT, 0, copy_count, closes) < copy_count) return;
close1 = closes[1];   // refresh from full-window copy (close1 declared in Step 1B)

double rh = highs[2];
double rl = lows[2];
for(int k = 3; k <= InpLookbackBars + 1; k++) {{
    if(highs[k] > rh) rh = highs[k];
    if(lows[k]  < rl) rl = lows[k];
}}
```"""

    # ── SL/TP computation section ─────────────────────────────────────────────
    atr_period   = int(params.get("atr_period", 14))
    atr_sl_mult  = float(params.get("atr_sl_mult", 1.5))
    atr_tp_mult  = float(params.get("atr_tp_mult", 3.0))

    if sl_tp_mode == "pct":
        sl_tp_input_group = "sl_tp_mode, sl_pct, tp_pct"
        sl_tp_desc = f"""**SL/TP mode: pct** — distances are a percentage of the anchor price.
```
double sl_dist = anchor * InpSlPct / 100.0;   // default {sl_pct:g}%
double tp_dist = anchor * InpTpPct / 100.0;   // default {tp_pct:g}%
```"""
    elif sl_tp_mode == "atr":
        sl_tp_input_group = "sl_tp_mode, atr_period, atr_sl_mult, atr_tp_mult"
        sl_tp_desc = f"""**SL/TP mode: atr** — distances are multiples of ATR(Wilder's EWM), adapting to volatility.
Create an ATR indicator handle with period {atr_period} (Wilder's smoothing).
```
int g_atr_handle = iATR(_Symbol, PERIOD_CURRENT, InpAtrPeriod);
double atr_buf[1];
CopyBuffer(g_atr_handle, 0, 1, 1, atr_buf);
double atr_val = atr_buf[0];
double sl_dist  = atr_val * InpAtrSlMult;   // default {atr_sl_mult:g} × ATR
double tp_dist  = atr_val * InpAtrTpMult;   // default {atr_tp_mult:g} × ATR
```"""
    else:
        sl_tp_input_group = f"sl_tp_mode, sl_pips, tp_pips, pip_mult"
        sl_tp_desc = f"""**SL/TP mode: pips** — fixed pip distances converted to price units.
Expose `InpPipMult` as `input double` (default **{pip_mult:g}** for {symbol}).
```
double sl_dist = InpSlPips / InpPipMult;   // default {sl_pips:g} pips
double tp_dist = InpTpPips / InpPipMult;   // default {tp_pips:g} pips
```"""

    # ── Entry mode section ────────────────────────────────────────────────────
    offset_dist_line = f"double offset_dist = InpEntryOffsetPips / InpPipMult;   // {entry_offset:g} pips offset"

    if entry_mode == "touch":
        entry_desc = f"""**Entry mode: touch** — place a stop order at the level ± offset immediately when a new level is detected, without waiting for a bar close.
- Long:  BuyStop  at `rh + offset_dist`
- Short: SellStop at `rl - offset_dist`
- Signal fires purely from `rh != g_last_used_high` (no close-above condition)."""
        close_cond_long  = "rh > 0.0 && rh != g_last_used_high"
        close_cond_short = "rl > 0.0 && rl != g_last_used_low"
        anchor_long  = "rh + offset_dist"
        anchor_short = "rl - offset_dist"
        order_type_long  = "BuyStop"
        order_type_short = "SellStop"
    elif entry_offset > 0:
        entry_desc = f"""**Entry mode: close with offset** — signal fires when close[1] crosses above/below the level, then places a stop order offset pips beyond the level.
- Long:  BuyStop  at `rh + offset_dist` when `close[1] > rh`
- Short: SellStop at `rl - offset_dist` when `close[1] < rl`"""
        close_cond_long  = "close1 > rh && rh != g_last_used_high"
        close_cond_short = "close1 < rl && rl != g_last_used_low"
        anchor_long  = "rh + offset_dist"
        anchor_short = "rl - offset_dist"
        order_type_long  = "BuyStop"
        order_type_short = "SellStop"
    else:
        entry_desc = f"""**Entry mode: close (market)** — signal fires when close[1] closes above/below the level; entry is a market order at the next bar's Ask/Bid.
- Long:  market Buy  when `close[1] > rh && rh != g_last_used_high`
- Short: market Sell when `close[1] < rl && rl != g_last_used_low`"""
        close_cond_long  = "close1 > rh && rh != g_last_used_high"
        close_cond_short = "close1 < rl && rl != g_last_used_low"
        anchor_long  = "SymbolInfoDouble(_Symbol, SYMBOL_ASK)"
        anchor_short = "SymbolInfoDouble(_Symbol, SYMBOL_BID)"
        order_type_long  = "Buy (market)"
        order_type_short = "Sell (market)"

    # ── Pending cancel section ────────────────────────────────────────────────
    if not has_stop_order:
        pending_input_lines = ""
        pending_globals     = ""
        pending_step        = "*(Not applicable — entry is a market order, no pending orders.)*"
    else:
        cancel_inputs = ["entry_offset_pips", "pending_cancel"]
        if use_max_bars:
            cancel_inputs.append("max_pending_bars")
        if use_sl_break:
            cancel_inputs.append("pending_cancel_buffer_pips")
        pending_input_lines = f"- `\"=== Entry ===\"` — entry_mode, {', '.join(cancel_inputs)}"

        _pend_globals = (
            "ulong    g_pending_long_ticket  = 0;   // ticket of active long stop order\n"
            "ulong    g_pending_short_ticket = 0;   // ticket of active short stop order\n"
        )
        if use_max_bars:
            _pend_globals += (
                "int      g_pending_long_bars    = 0;   // bars elapsed since long stop was placed\n"
                "int      g_pending_short_bars   = 0;   // bars elapsed since short stop was placed\n"
            )
        if use_sl_break:
            _pend_globals += (
                "double   g_pending_long_sl      = 0.0; // SL of the long stop order (for cancel check)\n"
                "double   g_pending_short_sl     = 0.0; // SL of the short stop order (for cancel check)\n"
            )
        pending_globals = _pend_globals

        cancel_mode_parts = []
        if use_sl_break:
            cancel_mode_parts.append(f"**SL-break**: cancel long if `Bid <= g_pending_long_sl - buf_dist`; cancel short if `Ask >= g_pending_short_sl + buf_dist`")
        if use_max_bars:
            cancel_mode_parts.append(f"**Max bars**: cancel after `{max_pending_bars}` bars without fill")

        buf_line = f"\ndouble buf_dist = InpPendingCancelBufferPips / InpPipMult;  // {buf_pips:g} pips" if use_sl_break else ""

        pending_step = f"""### Step 2 — Manage pending stop orders

Cancel mode: **{pending_cancel}** — {' + '.join(cancel_mode_parts) if cancel_mode_parts else 'none'}.
{buf_line}

**Manage long stop** (if `g_pending_long_ticket > 0`):
```
if(!OrderSelect(g_pending_long_ticket))
{{
    // Order was filled or externally cancelled — clear state
    g_pending_long_ticket = 0;
{"    g_pending_long_bars = 0;" if use_max_bars else ""}
{"    g_pending_long_sl = 0.0;" if use_sl_break else ""}
}}
else  // still pending
{{
{"    g_pending_long_bars++;" if use_max_bars else ""}
{"    // SL-break cancel: price moved against the setup" if use_sl_break else ""}
{"    if(g_pending_long_sl > 0.0 && SymbolInfoDouble(_Symbol, SYMBOL_BID) <= g_pending_long_sl - buf_dist)" if use_sl_break else ""}
{"    {" if use_sl_break else ""}
{"        trade.OrderDelete(g_pending_long_ticket);" if use_sl_break else ""}
{"        g_pending_long_ticket = 0;" if use_sl_break else ""}
{"        g_pending_long_sl = 0.0;" if use_sl_break else ""}
{"        g_pending_long_bars = 0;  return;" if (use_sl_break and use_max_bars) else ("        return;" if use_sl_break else "")}
{"    }" if use_sl_break else ""}
{"    // Max-bars expiry" if use_max_bars else ""}
{"    " + ("else " if use_sl_break else "") + "if(g_pending_long_bars >= InpMaxPendingBars)" if use_max_bars else ""}
{"    {" if use_max_bars else ""}
{"        trade.OrderDelete(g_pending_long_ticket);" if use_max_bars else ""}
{"        g_pending_long_ticket = 0;" if use_max_bars else ""}
{"        g_pending_long_bars = 0;" if use_max_bars else ""}
{"        g_pending_long_sl = 0.0;  return;" if (use_max_bars and use_sl_break) else ("        return;" if use_max_bars else "")}
{"    }" if use_max_bars else ""}
    else return;   // order still valid — skip new signal search this bar
}}
```

**Manage short stop** (if `g_pending_short_ticket > 0`): same pattern, using `g_pending_short_ticket`, `g_pending_short_bars`, `g_pending_short_sl`; cancel trigger: `Ask >= g_pending_short_sl + buf_dist`."""

    # ── Input groups ──────────────────────────────────────────────────────────
    level_group = level_det_input_lines
    if sl_tp_mode == "pct":
        sltp_group = f"- `\"=== SL / TP ===\"` — sl_tp_mode, sl_pct, tp_pct"
    elif sl_tp_mode == "atr":
        sltp_group = f"- `\"=== SL / TP ===\"` — sl_tp_mode, atr_period, atr_sl_mult, atr_tp_mult"
    else:
        sltp_group = f"- `\"=== SL / TP ===\"` — sl_tp_mode, sl_pips, tp_pips, pip_mult"

    if has_stop_order:
        entry_group = pending_input_lines
    else:
        entry_group = f"- `\"=== Entry ===\"` — entry_mode, entry_offset_pips"

    ema_dual_extra = ", ema_fast_period" if ema_mode == "dual" else ""

    return f"""## Strategy: Pip Breakout
## Platform: MetaTrader {mt_ver} ({lang})
## Instrument: {symbol}
## Timeframe: {params.get('timeframe', 'H1')}

### Backtest Performance
{perf}

### Parameters
{param_lines_str}

---

## Strategy Overview

The Pip Breakout EA detects a resistance/support level (rolling Donchian channel or confirmed fractal)
and enters when price breaks above (long) or below (short) that level.
Entry is either a market order (close mode, no offset) or a stop order (touch mode or offset > 0).
SL/TP are fixed distances from the anchor price, in pips or % of price.

**Level detector**: {level_detector}
**SL/TP mode**: {sl_tp_mode}
**Entry mode**: {entry_mode}{f" (offset {entry_offset:g} pips)" if entry_offset > 0 else ""}
**EMA Trend Filter**: {ema_filter_label}{tf_note}

---

## Input Groups

Group all `input` variables using `input group "..."` (MQL5) or string separator inputs (MQL4):
{level_group}
{sltp_group}
{entry_group}
- `"=== EMA Trend Filter ==="` — ema_filter_mode, ema_period{ema_dual_extra}, ema_timeframe
- `"=== Session Filter ==="` — sessions (display label)
- `"=== Sideways Filter ==="` — sideways_filter and its sub-parameters
{risk_mgmt_line}
- `"=== Execution ==="` — magic_number, commission_per_lot

---

## Global State

```
datetime g_last_bar_time     = 0;      // new-bar guard
double   g_last_used_high    = 0.0;    // level that last triggered a long — prevents re-entry on same level
double   g_last_used_low     = 0.0;    // level that last triggered a short
{level_det_globals}{pending_globals}{ema_g_decls}```

---

## OnInit

```
g_stat_peak_eq = AccountInfoDouble(ACCOUNT_BALANCE);
{level_det_init}
{ema_init_code}
```

---

## OnTick Logic

### Step 1 — New-bar guard
Compare `iTime(_Symbol, PERIOD_CURRENT, 0)` with `g_last_bar_time`. If same bar → call `UpdatePanel()` and return.
Update `g_last_bar_time` and proceed.

### Step 1B — Copy close[1] (needed by EMA filter and level detection)
```
double closes_pre[2];
ArraySetAsSeries(closes_pre, true);
if(CopyClose(_Symbol, PERIOD_CURRENT, 0, 2, closes_pre) < 2) return;
double close1 = closes_pre[1];   // last confirmed closed bar
```
Declare `close1` here once. The level detection step copies a larger close array into its own `closes[]` buffer but **must NOT re-declare `close1`** — reuse this variable.

{pending_step}

### Step 3 — EMA trend filter
```
{ema_copy_code}
```

### Step 4A — Sideways filter
{filter_desc}

{level_det_step}

### Step 5 — Session filter
{session_sec}

### Step 6 — Skip if at position limit
If open positions for this symbol + magic number ≥ `InpMaxPositions` → return.

### Step 7 — Compute SL/TP distances

{sl_tp_desc}

### Step 8 — Evaluate signals with level deduplication

{entry_desc}

{offset_dist_line}

**Long signal** — conditions: `{close_cond_long}` AND `ema_ok_long` AND `trend_ok_long`
- anchor = `{anchor_long}`
- `sl = NormalizeDouble(anchor - sl_dist, _Digits)`
- `tp = NormalizeDouble(anchor + tp_dist, _Digits)`

**Short signal** — conditions: `{close_cond_short}` AND `ema_ok_short` AND `trend_ok_short`
- anchor = `{anchor_short}`
- `sl = NormalizeDouble(anchor + sl_dist, _Digits)`
- `tp = NormalizeDouble(anchor - tp_dist, _Digits)`

Use an **if / else-if** structure — long is checked first, short only if long did not fire. This matches the Python strategy's `if-elif` priority rule: long always takes precedence if both conditions are true simultaneously.

### Step 9 — Period SL limit check
Before placing any order, verify the period SL count is within limit (see Risk Management).

### Step 10 — Order placement

**Long** (`{order_type_long}`):
```
double lots = ComputeLots(anchor, sl);
// Guards: sl_dist > 0, anchor > sl, tp > anchor
{"trade.BuyStop(lots, anchor, _Symbol, sl, tp, ORDER_TIME_GTC, 0, \"PB_LONG_STOP\");" if has_stop_order else "double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);"}
{"g_pending_long_ticket = trade.ResultOrder();" if has_stop_order else "trade.Buy(lots, _Symbol, ask, sl, tp, \"PB_LONG\");"}
{"g_pending_long_bars = 0;" if use_max_bars else ""}
{"g_pending_long_sl = sl;" if use_sl_break else ""}
g_last_used_high = rh;   // mark level as used — prevents duplicate signal
```

**Short** (`{order_type_short}`):
```
double lots = ComputeLots(anchor, sl);
// Guards: sl_dist > 0, anchor < sl, tp < anchor
{"trade.SellStop(lots, anchor, _Symbol, sl, tp, ORDER_TIME_GTC, 0, \"PB_SHORT_STOP\");" if has_stop_order else "double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);"}
{"g_pending_short_ticket = trade.ResultOrder();" if has_stop_order else "trade.Sell(lots, _Symbol, bid, sl, tp, \"PB_SHORT\");"}
{"g_pending_short_bars = 0;" if use_max_bars else ""}
{"g_pending_short_sl = sl;" if use_sl_break else ""}
g_last_used_low = rl;    // mark level as used — prevents duplicate signal
```

At the **end** of every `OnTick()` execution: call `UpdatePanel()`.

---

{_risk_block(params, lang)}

---

{_chart_stats_section("Pip Breakout")}

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
    elif strategy == "n_structure":
        return _prompt_n_structure(params, lang, mt_ver, perf, param_lines_str)
    elif strategy == "fair_value_gap":
        return _prompt_fair_value_gap(params, lang, mt_ver, perf, param_lines_str)
    elif strategy == "pip_breakout":
        return _prompt_pip_breakout(params, lang, mt_ver, perf, param_lines_str)
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
            model="claude-opus-4-7",
            max_tokens=8192,
            system=(
                "You are an expert MetaTrader developer. "
                "You write production-quality, compilable MQL4 and MQL5 code. "
                "Output only raw source code — no markdown, no explanations."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIConnectionError as exc:
        return EAResponse(
            code="",
            platform=platform,
            filename=_build_filename(strategy, params, platform),
            prompt=prompt,
            error=f"Could not reach the Anthropic API — check your network connection and try again. ({exc})",
        )
    except anthropic.RateLimitError as exc:
        return EAResponse(
            code="",
            platform=platform,
            filename=_build_filename(strategy, params, platform),
            prompt=prompt,
            error=f"Anthropic rate limit reached — wait a moment and retry. ({exc})",
        )
    except anthropic.APIStatusError as exc:
        return EAResponse(
            code="",
            platform=platform,
            filename=_build_filename(strategy, params, platform),
            prompt=prompt,
            error=f"Anthropic API returned an error (HTTP {exc.status_code}): {exc.message}",
        )

    code = _strip_fences(message.content[0].text)

    with open(cache_path, "w") as f:
        json.dump({"code": code}, f)

    return EAResponse(
        code=code,
        platform=platform,
        filename=_build_filename(strategy, params, platform),
        prompt=prompt,
    )


@router.post("/prompt", response_model=EAPromptResponse)
def get_ea_prompt(req: EARequest) -> EAPromptResponse:
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

    return EAPromptResponse(
        prompt=_build_prompt(strategy, params, results, platform),
        filename=_build_filename(strategy, params, platform),
    )
