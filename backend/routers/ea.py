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

    sideways = params.get("sideways_filter", "none")
    breakeven_r = params.get("breakeven_r")
    risk_pct_pct = float(params.get("risk_pct", 0.02)) * 100

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

    # ---- Sideways filter description ----
    if sideways == "adx":
        filter_desc = (
            f"ADX filter (Wilder's method):\n"
            f"  - Period: {params.get('adx_period', 14)}\n"
            f"  - Skip signal when ADX < {params.get('adx_threshold', 25.0)} (market is ranging)"
        )
    elif sideways == "ema_slope":
        p = params.get("ema_slope_period", 10)
        m = params.get("ema_slope_min", 0.5)
        filter_desc = (
            f"EMA Slope filter:\n"
            f"  - Measure slope over {p} bars: (ema[0] - ema[{p}]) / {p}\n"
            f"  - Skip signal when |slope| < {m} (EMA is flat)"
        )
    elif sideways == "choppiness":
        filter_desc = (
            f"Choppiness Index filter:\n"
            f"  - Period: {params.get('choppiness_period', 14)}\n"
            f"  - CI = 100 × log10(Σ ATR(1,N)) / (HH(N) - LL(N)) / log10(N)\n"
            f"  - Skip signal when CI >= {params.get('choppiness_max', 61.8)} (choppy/ranging)"
        )
    elif sideways == "alligator":
        filter_desc = (
            f"Williams Alligator filter:\n"
            f"  - Jaw SMMA({params.get('alligator_jaw', 13)}), "
            f"Teeth SMMA({params.get('alligator_teeth', 8)}), "
            f"Lips SMMA({params.get('alligator_lips', 5)})\n"
            f"  - BUY allowed when lips > teeth > jaw\n"
            f"  - SELL allowed when jaw > teeth > lips\n"
            f"  - Skip when lines are tangled"
        )
    elif sideways == "stochrsi":
        filter_desc = (
            f"Stochastic RSI filter:\n"
            f"  - RSI period: {params.get('stochrsi_rsi_period', 14)}, "
            f"Stoch period: {params.get('stochrsi_stoch_period', 14)}\n"
            f"  - BUY allowed when StochRSI < {params.get('stochrsi_oversold', 20.0)} (oversold)\n"
            f"  - SELL allowed when StochRSI > {params.get('stochrsi_overbought', 80.0)} (overbought)"
        )
    else:
        filter_desc = "None — all signals pass through"

    breakeven_section = ""
    if breakeven_r:
        breakeven_section = (
            f"\n### Break-Even Stop\n"
            f"When price moves {breakeven_r}R in favour of the trade, move SL to entry price.\n"
        )

    compounding = "Yes — use current account balance each trade" if params.get("compound") else "No — risk is fixed percentage of balance each trade"

    prompt = f"""You are an expert MetaTrader {mt_ver} developer. Generate a complete, compilable {lang} Expert Advisor implementing the strategy below.

## Strategy: William Fractal Breakout
## Platform: MetaTrader {mt_ver} ({lang})
## Instrument: XAUUSD (Gold vs USD)
## Timeframe: {params.get('timeframe', 'H1')}

### Backtest Performance (include as comment header in the EA)
{perf}

### Parameters Used (all must be input variables)
{param_lines}

---

## Entry Logic

1. **EMA Trend Filter**
   - Calculate EMA({params.get('ema_period', 200)}) on close prices
   - Price above EMA → uptrend (buy side only)
   - Price below EMA → downtrend (sell side only)

2. **William Fractals**
   - fractal_n = {params.get('fractal_n', 9)} (candles on EACH side of the centre bar)
   - TOP fractal: high[i] > high[i±j] for all j in 1..fractal_n
   - BOTTOM fractal: low[i] < low[i±j] for all j in 1..fractal_n
   - A fractal is CONFIRMED fractal_n bars after it forms (shift by fractal_n to avoid lookahead)
   - Track `last_top` = price of most recent confirmed top fractal (forward-filled)
   - Track `last_bot` = price of most recent confirmed bottom fractal (forward-filled)

3. **Signal Conditions** (checked on bar close, executed at NEXT bar open)
   - **BUY** : close > EMA AND close crosses above last_top (close > last_top AND prev_close <= last_top) AND sideways filter allows
   - **SELL**: close < EMA AND close crosses below last_bot (close < last_bot AND prev_close >= last_bot) AND sideways filter allows

4. **SL / TP**
   - BUY : SL = low of signal bar, TP = signal_close + {params.get('rr_ratio', 1.5)} × (signal_close − SL)
   - SELL: SL = high of signal bar, TP = signal_close − {params.get('rr_ratio', 1.5)} × (SL − signal_close)

5. **One position at a time** — skip new signals when a position is already open

6. **One trade per fractal level** — once a fractal price level has triggered an entry, that exact level MUST NOT trigger another entry, even if price oscillates back through it after the position closes. Track the last used `last_top` and `last_bot` price separately; only allow a new entry when `last_top` / `last_bot` has changed to a different price (i.e. a new fractal has formed).

---

## Sideways Filter: {sideways}
{filter_desc}

---

## Risk Management
- Risk per trade: {risk_pct_pct:.1f}% of account balance
- Lot size = (balance × risk_pct) / (sl_distance_in_price × contract_size)
  - For XAUUSD: contract_size = 100 (oz per lot)
  - sl_distance_in_price is in price units (e.g. 1.50 = $1.50/oz)
- Compounding: {compounding}
{breakeven_section}
---

## Code Requirements
- All parameters must be `input` variables so they appear in the EA dialog
- Calculate all indicators from scratch in OnTick/OnCalculate — do NOT use iCustom or external files
- The EA must be self-contained and compile without errors in MetaEditor
- Include the backtest performance stats in a block comment at the top
- Use proper {lang} syntax for MetaTrader {mt_ver}
- For {lang}: use {'OrderSend/OrderClose' if platform == 'MT4' else 'CTrade class or trade.Buy/trade.Sell'} for order management
- Handle edge cases: minimum bars check, spread, invalid prices
- Add clear inline comments explaining the fractal detection and signal logic

Output ONLY the raw {lang} code. No markdown fences, no explanation text before or after."""

    return prompt


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
