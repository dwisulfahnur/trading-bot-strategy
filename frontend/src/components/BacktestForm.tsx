import { useState, useEffect, useCallback, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import Select from 'react-select';
import { api } from '../api/client';
import type { BacktestRequest, BacktestResult, StrategyMeta } from '../api/types';

interface Props {
  onResult: (result: BacktestResult) => void;
  initialParams?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Param metadata — human-readable labels + hover descriptions
// ---------------------------------------------------------------------------

interface ParamInfo {
  label: string;
  description: string;
}

const PARAM_META: Record<string, ParamInfo> = {
  // Shared
  ema_period: {
    label: 'EMA Period',
    description:
      'Trend filter. A buy signal is only valid when price closes above this EMA; sell when below. Higher values = slower, smoother trend detection.',
  },

  // William Fractals
  ema_timeframe: {
    label: 'EMA Source Timeframe',
    description:
      'Load EMA from a different (higher) timeframe parquet file instead of computing it on the running timeframe. Useful for multi-timeframe trend confirmation — e.g. run M15 signals filtered by an H1 EMA.',
  },
  fractal_n: {
    label: 'Fractal N (each side)',
    description:
      'Number of candles required on each side of the center bar to confirm a fractal high or low. e.g. N=9 means 9 candles left + center + 9 right must confirm. Higher N = fewer but more reliable signals.',
  },
  rr_ratio: {
    label: 'Risk / Reward Ratio',
    description:
      'Take-profit distance = this × stop-loss distance from entry. e.g. 1.5 means you risk 1 to potentially earn 1.5.',
  },
  sideways_filter: {
    label: 'Sideways / Ranging Filter',
    description:
      'Optional filter to block signals when the market is choppy or ranging. Each option uses a different indicator to detect trend quality.',
  },

  // ADX sub-params
  adx_period: {
    label: 'ADX Period',
    description: "Lookback period for the Average Directional Index (Wilder's smoothing).",
  },
  adx_threshold: {
    label: 'ADX Threshold',
    description:
      'Minimum ADX reading required to allow a signal. Below this value the market is considered ranging and signals are skipped. Typical range: 20–30.',
  },

  // EMA Slope sub-params
  ema_slope_period: {
    label: 'Slope Lookback',
    description:
      'Number of bars used to measure the EMA slope: (ema[0] − ema[N]) / N. Larger values smooth out noise.',
  },
  ema_slope_min: {
    label: 'Min Slope',
    description:
      'Minimum absolute slope value required. Signals are blocked when the EMA is flatter than this threshold (price moving sideways).',
  },

  // Choppiness sub-params
  choppiness_period: {
    label: 'Choppiness Period',
    description:
      'Lookback for the Choppiness Index. Values near 100 indicate a choppy range; values near 0 indicate a strong trend.',
  },
  choppiness_max: {
    label: 'Max Choppiness',
    description:
      'Signals are blocked when the Choppiness Index is above this level. 61.8 (golden ratio) is the classic threshold.',
  },

  // Alligator sub-params
  alligator_jaw: {
    label: 'Jaw Period (slow)',
    description:
      'SMMA period for the Alligator Jaw (blue line — the slowest). Larger period = slower reaction to price.',
  },
  alligator_teeth: {
    label: 'Teeth Period (mid)',
    description: 'SMMA period for the Alligator Teeth (red line — medium speed).',
  },
  alligator_lips: {
    label: 'Lips Period (fast)',
    description:
      'SMMA period for the Alligator Lips (green line — fastest). Buy signals require lips > teeth > jaw (uptrend).',
  },

  // StochRSI sub-params
  stochrsi_rsi_period: {
    label: 'RSI Period',
    description: 'Lookback period for the RSI calculation inside StochRSI.',
  },
  stochrsi_stoch_period: {
    label: 'Stoch Lookback',
    description: 'Number of bars used to compute the Stochastic applied to the RSI values.',
  },
  stochrsi_oversold: {
    label: 'Oversold Level',
    description:
      'Buy signals only allowed when StochRSI is below this level (price pulling back in an uptrend).',
  },
  stochrsi_overbought: {
    label: 'Overbought Level',
    description:
      'Sell signals only allowed when StochRSI is above this level (price pulling back in a downtrend).',
  },

  // Momentum candle filter (william_fractals)
  momentum_candle_filter: {
    label: 'Momentum Candle Filter',
    description:
      'When enabled, a signal is only taken if the breakout candle qualifies as a momentum candle — strongly directional body with an above-average volume spike.',
  },
  mc_body_ratio_min: {
    label: 'Min Body Ratio',
    description:
      'Minimum fraction of the candle range (high − low) that must be a solid directional body. e.g. 0.6 = 60%. Filters out candles with large wicks.',
  },
  mc_volume_factor: {
    label: 'Volume Spike ×',
    description:
      "The signal candle's tick volume must exceed this multiple of the rolling average. e.g. 1.5 = at least 50% above average.",
  },
  mc_volume_lookback: {
    label: 'Volume Lookback',
    description:
      'Number of prior bars used to compute the rolling average tick volume for the spike threshold.',
  },

  // Momentum Candle
  body_ratio_min: {
    label: 'Min Body Ratio',
    description:
      'Minimum fraction of the candle range (high − low) that must be a solid directional body. 0.7 = 70%. Filters out doji and indecision candles.',
  },
  volume_factor: {
    label: 'Volume Spike ×',
    description:
      "The signal bar's volume must exceed this multiple of the rolling average. e.g. 1.5 = volume must be at least 50% above average to qualify.",
  },
  volume_lookback: {
    label: 'Volume Lookback',
    description:
      'Number of prior bars used to compute the rolling average volume. The signal bar itself is excluded (uses bars N−1 back).',
  },
  retracement_pct: {
    label: 'Entry Retracement',
    description:
      'Limit order is placed at this fraction of the MC range from the candle extreme. 0.5 = enter at the midpoint of the momentum candle.',
  },
  sl_mult: {
    label: 'SL Multiplier',
    description:
      'Stop-loss distance = this × MC range, measured from the candle extreme. 1.0 = full range (SL at the opposite end of the candle). Increase to give the trade more room.',
  },
  tp_mult: {
    label: 'TP Multiplier',
    description:
      'Take-profit distance = this × MC range from the near extreme. 1.0 = full range (TP at the candle extreme in the signal direction). Increase for a wider target.',
  },
  pending_cancel: {
    label: 'Pending Cancel Mode',
    description:
      'Controls when an unfilled stop order is cancelled. "Max bars" expires after N bars. "HL/LH break" cancels if the pullback level is broken (setup invalidated). "Both" cancels on whichever comes first. "None" keeps the order open until filled.',
  },
  max_pending_bars: {
    label: 'Max Pending Bars',
    description:
      'Cancel the pending stop order after this many bars without a fill. Only active when cancel mode is "Max bars" or "Both".',
  },
  sessions: {
    label: 'Session Filter',
    description:
      'Only generate signals when the bar falls inside the selected market sessions (UTC times). Use to focus trading on the most liquid hours.',
  },

  // Support & Resistance
  pivot_n: {
    label: 'Pivot N (each side)',
    description:
      'Number of candles required on each side of the center bar to confirm a pivot high (resistance) or pivot low (support). e.g. N=5 means the center bar must have a higher high (or lower low) than the 5 bars on each side. Higher N = fewer but more significant S/R levels.',
  },
  zone_tolerance: {
    label: 'Zone Tolerance (price)',
    description:
      'How close price must get to a S/R level to count as a "touch". e.g. 0.5 means the candle low only needs to reach within $0.50 of the support level to trigger a bounce signal. Increase if too few signals; decrease for stricter touches.',
  },
  use_ema_filter: {
    label: 'EMA Trend Filter',
    description:
      'When ON, buy signals are only taken when price is above the EMA (uptrend) and sell signals only when below (downtrend). Turn OFF to allow counter-trend S/R trades in both directions.',
  },

  // Grid Trading
  center_period: {
    label: 'Center EMA Period',
    description:
      'EMA period used as the dynamic grid center. Buy zones sit below this line, sell zones above it. Shorter = faster-moving center that tracks price more closely; longer = more stable, slower-reacting center.',
  },
  atr_period: {
    label: 'ATR Period',
    description:
      'Lookback for the Average True Range used to size grid spacing. Longer period = smoother, more stable step size; shorter period = step adapts faster to recent volatility.',
  },
  grid_step_mult: {
    label: 'Grid Step (× ATR)',
    description:
      'Grid spacing = ATR × this multiplier. e.g. 0.5 places levels every half ATR. Smaller = tighter grid with more frequent signals; larger = wider levels, fewer but higher-conviction bounces.',
  },
  grid_levels: {
    label: 'Grid Levels (each side)',
    description:
      'Number of buy/sell zones placed on each side of the center EMA. Level 1 is closest to center (center ± 1 step), level N is furthest. Deeper levels catch larger mean-reversion moves.',
  },

  // Range Breakout
  range_lookback: {
    label: 'Range Lookback',
    description: 'Number of bars used to define the range high and range low. Larger values capture wider, more significant consolidation zones.',
  },
  range_mode: {
    label: 'Range Mode',
    description: 'Rolling: range recalculates fresh on every bar. Fixed: after a breakout signal the range locks in, suppressing new signals for "range lookback" bars to let a new range form.',
  },
  atr_multiplier: {
    label: 'ATR Multiplier (tightness)',
    description: 'Skip signals when the range height exceeds this multiple of ATR. Prevents trading false breakouts from already-wide volatile candles. Set to 0 to disable.',
  },
  breakout_type: {
    label: 'Breakout Type',
    description: 'Close: bar must fully close above/below the range boundary. HL: a high or low touch is enough to trigger — fires earlier but catches more false breakouts.',
  },
  allow_reentry: {
    label: 'Allow Re-entry',
    description: 'ON: fire a signal on every qualifying bar (including consecutive ones). OFF: once a direction breaks out, suppress new signals in that direction until price returns inside the range.',
  },
  sl_buffer: {
    label: 'SL Buffer (price)',
    description: 'Extra distance added beyond the range boundary for the stop-loss. e.g. 0.5 places SL 0.5 below range low (longs) or 0.5 above range high (shorts). Gives the trade more breathing room.',
  },

  // Order Block (SMC)
  structure_period: {
    label: 'Structure Period',
    description:
      'Rolling lookback (bars) used to define the high/low that price must break to trigger a BOS. e.g. 20 = the close must exceed the highest high of the prior 20 bars. Higher values require a larger, more significant breakout to signal.',
  },
  ob_lookback: {
    label: 'OB Lookback',
    description:
      'How many bars before the BOS to search for the Order Block candle. The OB is the last opposing candle (bearish for longs, bullish for shorts) within this window. If no qualifying candle is found, no signal is emitted.',
  },
  require_fvg: {
    label: 'Require Fair Value Gap (FVG)',
    description:
      'A 3-candle imbalance (low[i] > high[i-2] for bullish, high[i] < low[i-2] for bearish) must be present within the OB lookback window. Confirms the impulse was aggressive enough to leave an unfilled price gap.',
  },
  require_ote: {
    label: 'Require OTE (Fibonacci Zone)',
    description:
      'The Order Block entry level must fall within the Optimal Trade Entry zone — a Fibonacci retracement of the BOS impulse leg. Filters for OBs that sit in the 61.8%–78.6% "golden zone" of the move.',
  },
  ote_fib_low: {
    label: 'OTE Lower Fibonacci',
    description:
      'Lower boundary of the OTE zone (e.g. 0.618 = 61.8% retracement of the impulse leg). The OB entry level must be at or above this level.',
  },
  ote_fib_high: {
    label: 'OTE Upper Fibonacci',
    description:
      'Upper boundary of the OTE zone (e.g. 0.786 = 78.6% retracement). The OB entry level must be at or below this level. Entries beyond this are considered over-extended.',
  },
  sl_mode: {
    label: 'Stop-Loss Placement',
    description:
      'Where to place the stop-loss relative to the Order Block. "OB Edge" sets SL just beyond the far edge of the OB candle (full OB height as risk). "OB Midpoint" places SL at the 50% level of the OB for a tighter stop. "Structure" places SL beyond the swing low/high that defined the BOS, giving price more room but widening risk.',
  },
};

// ---------------------------------------------------------------------------
// Parameter groups — defines section headings within Strategy Parameters
// ---------------------------------------------------------------------------

interface ParamGroup {
  title: string;
  params: string[];
}

const PARAM_GROUPS: Record<string, ParamGroup[]> = {
  william_fractals: [
    {
      title: 'Signal Generation',
      params: ['ema_period', 'ema_timeframe', 'fractal_n', 'rr_ratio'],
    },
    {
      title: 'Session Filter',
      params: ['sessions'],
    },
    {
      title: 'Momentum Candle Filter',
      params: [
        'momentum_candle_filter',
        'mc_body_ratio_min', 'mc_volume_factor', 'mc_volume_lookback',
      ],
    },
    {
      title: 'Sideways Filter',
      params: [
        'sideways_filter',
        'adx_period', 'adx_threshold',
        'ema_slope_period', 'ema_slope_min',
        'choppiness_period', 'choppiness_max',
        'alligator_jaw', 'alligator_teeth', 'alligator_lips',
        'stochrsi_rsi_period', 'stochrsi_stoch_period', 'stochrsi_oversold', 'stochrsi_overbought',
      ],
    },
  ],
  support_resistance: [
    {
      title: 'Signal Generation',
      params: ['pivot_n', 'zone_tolerance', 'rr_ratio'],
    },
    {
      title: 'Trend Filter',
      params: ['ema_period', 'use_ema_filter'],
    },
    {
      title: 'Session Filter',
      params: ['sessions'],
    },
    {
      title: 'Sideways Filter',
      params: [
        'sideways_filter',
        'adx_period', 'adx_threshold',
        'ema_slope_period', 'ema_slope_min',
        'choppiness_period', 'choppiness_max',
        'alligator_jaw', 'alligator_teeth', 'alligator_lips',
        'stochrsi_rsi_period', 'stochrsi_stoch_period', 'stochrsi_oversold', 'stochrsi_overbought',
      ],
    },
  ],
  grid: [
    {
      title: 'Grid Configuration',
      params: ['center_period', 'atr_period', 'grid_step_mult', 'grid_levels'],
    },
    {
      title: 'Entry & Exit',
      params: ['rr_ratio'],
    },
    {
      title: 'Session Filter',
      params: ['sessions'],
    },
  ],
  order_block_smc: [
    {
      title: 'Order Block Detection',
      params: ['structure_period', 'ob_lookback'],
    },
    {
      title: 'Entry & Exit',
      params: ['rr_ratio', 'sl_mode', 'sessions'],
    },
    {
      title: 'Confluence Filters',
      params: ['require_fvg', 'require_ote', 'ote_fib_low', 'ote_fib_high'],
    },
  ],
  n_structure: [
    {
      title: 'Signal Generation',
      params: ['ema_period', 'ema_timeframe', 'swing_n', 'rr_ratio', 'sl_mode'],
    },
    {
      title: 'Pending Order',
      params: ['pending_cancel', 'max_pending_bars'],
    },
    {
      title: 'Session Filter',
      params: ['sessions'],
    },
    {
      title: 'Sideways Filter',
      params: [
        'sideways_filter',
        'adx_period', 'adx_threshold',
        'ema_slope_period', 'ema_slope_min',
        'choppiness_period', 'choppiness_max',
        'alligator_jaw', 'alligator_teeth', 'alligator_lips',
        'stochrsi_rsi_period', 'stochrsi_stoch_period', 'stochrsi_oversold', 'stochrsi_overbought',
      ],
    },
  ],
  range_breakout: [
    {
      title: 'Range Definition',
      params: ['range_lookback', 'range_mode', 'atr_period', 'atr_multiplier'],
    },
    {
      title: 'Entry',
      params: ['breakout_type', 'allow_reentry'],
    },
    {
      title: 'Exit',
      params: ['sl_buffer', 'rr_ratio'],
    },
    {
      title: 'Trend Filter',
      params: ['ema_period', 'ema_timeframe'],
    },
    {
      title: 'Session Filter',
      params: ['sessions'],
    },
    {
      title: 'Sideways Filter',
      params: [
        'sideways_filter',
        'adx_period', 'adx_threshold',
        'ema_slope_period', 'ema_slope_min',
        'choppiness_period', 'choppiness_max',
        'alligator_jaw', 'alligator_teeth', 'alligator_lips',
        'stochrsi_rsi_period', 'stochrsi_stoch_period', 'stochrsi_oversold', 'stochrsi_overbought',
      ],
    },
  ],
  momentum_candle: [
    {
      title: 'Signal Generation',
      params: ['ema_period', 'body_ratio_min', 'volume_factor', 'volume_lookback'],
    },
    {
      title: 'Entry & Exit',
      params: ['retracement_pct', 'sl_mult', 'tp_mult', 'max_pending_bars'],
    },
    {
      title: 'Session Filter',
      params: ['sessions'],
    },
    {
      title: 'Sideways Filter',
      params: [
        'sideways_filter',
        'adx_period', 'adx_threshold',
        'ema_slope_period', 'ema_slope_min',
        'choppiness_period', 'choppiness_max',
        'alligator_jaw', 'alligator_teeth', 'alligator_lips',
        'stochrsi_rsi_period', 'stochrsi_stoch_period', 'stochrsi_oversold', 'stochrsi_overbought',
      ],
    },
  ],
};

// Human-readable option labels for select-type params
const OPTION_LABELS: Record<string, Record<string, string>> = {
  ema_timeframe: {
    same:  'Same as running TF',
    M1:    'M1',
    M5:    'M5',
    M15:   'M15',
    H1:    'H1',
    H4:    'H4',
    D1:    'D1 (Daily)',
  },
  range_mode: {
    rolling: 'Rolling — fresh each bar',
    fixed:   'Fixed — locked after breakout',
  },
  breakout_type: {
    close: 'Close — bar must close outside range',
    hl:    'High/Low — touch is enough',
  },
  sideways_filter: {
    none:        'None — no filter',
    adx:         'ADX — trend strength',
    ema_slope:   'EMA Slope — trend angle',
    choppiness:  'Choppiness Index',
    alligator:   'Alligator — line alignment',
    stochrsi:    'Stoch RSI — pullback zone',
  },
  pending_cancel: {
    none:     'None — stay open until filled',
    max_bars: 'Max bars — expire after N bars',
    hl_break: 'HL/LH break — cancel if structure invalidated',
    both:     'Both — whichever comes first',
  },
  sessions: {
    all:                 'All sessions (no filter)',
    asia:                'Asia  (00:00–08:59 UTC)',
    london:              'London  (08:00–16:59 UTC)',
    newyork:             'New York  (13:00–21:59 UTC)',
    asia_london:         'Asia + London',
    london_newyork:      'London + New York',
    asia_newyork:        'Asia + New York',
    asia_london_newyork: 'Asia + London + New York',
  },
};

// ---------------------------------------------------------------------------
// Tooltip component
// ---------------------------------------------------------------------------

function InfoTooltip({ text }: { text: string }) {
  return (
    <span className="group relative inline-block ml-1 align-middle">
      <svg
        className="w-3.5 h-3.5 text-slate-500 hover:text-slate-300 cursor-help inline"
        fill="currentColor"
        viewBox="0 0 20 20"
      >
        <path
          fillRule="evenodd"
          d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z"
          clipRule="evenodd"
        />
      </svg>
      <span
        className="pointer-events-none absolute left-5 top-0 z-50 hidden group-hover:block
                   w-60 bg-slate-900 border border-slate-600 text-slate-300 text-xs
                   rounded-lg px-3 py-2 shadow-xl leading-relaxed"
      >
        {text}
      </span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Main form
// ---------------------------------------------------------------------------

export function BacktestForm({ onResult, initialParams }: Props) {
  const { data: strategies = [] } = useQuery({
    queryKey: ['strategies'],
    queryFn: api.getStrategies,
  });
  const { data: dataAvail } = useQuery({
    queryKey: ['data-available'],
    queryFn: api.getDataAvailable,
  });

  const [strategy, setStrategy] = useState('william_fractals');
  const [selectedSymbol, setSelectedSymbol] = useState('XAUUSD');
  const [selectedYears, setSelectedYears] = useState<number[]>([2025, 2026]);
  const [timeframe, setTimeframe] = useState('H1');
  const [capital, setCapital] = useState(10000);
  const [riskPct, setRiskPct] = useState(2);
  const [riskRecovery, setRiskRecovery] = useState(0);
  const [compound, setCompound] = useState(false);
  const [breakevenOn, setBreakevenOn] = useState(false);
  const [breakevenR, setBreakevenR] = useState(1.0);
  const [breakevenSlR, setBreakevenSlR] = useState(0.0);
  const [slLimitOn, setSlLimitOn] = useState(false);
  const [slLimitMax, setSlLimitMax] = useState(2);
  const [slLimitPeriod, setSlLimitPeriod] = useState<'day' | 'week' | 'month'>('day');
  const [maxPositions, setMaxPositions] = useState(1);
  const [commissionPerLot, setCommissionPerLot] = useState(3.5);
  const [stratParams, setStratParams] = useState<Record<string, number | string | boolean>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const appliedParamsRef = useRef<string>('');

  const stratMeta: StrategyMeta | undefined = strategies.find((s) => s.name === strategy);

  // Apply initialParams whenever they change (handles fresh mount and re-navigation)
  useEffect(() => {
    if (!initialParams) return;
    const key = JSON.stringify(initialParams);
    if (key === appliedParamsRef.current) return;
    appliedParamsRef.current = key;

    if (initialParams.strategy) setStrategy(initialParams.strategy as string);
    if (initialParams.symbol) setSelectedSymbol(initialParams.symbol as string);
    if (initialParams.years) setSelectedYears(initialParams.years as number[]);
    if (initialParams.timeframe) setTimeframe(initialParams.timeframe as string);
    if (initialParams.initial_capital != null) setCapital(initialParams.initial_capital as number);
    if (initialParams.risk_pct != null) {
      setRiskPct(Math.round((initialParams.risk_pct as number) * 1000) / 10);
    }
    if (initialParams.compound != null) setCompound(initialParams.compound as boolean);
    setBreakevenOn(initialParams.breakeven_r != null);
    if (initialParams.breakeven_r != null) setBreakevenR(initialParams.breakeven_r as number);
    if (initialParams.breakeven_sl_r != null) setBreakevenSlR(initialParams.breakeven_sl_r as number);
    setSlLimitOn(initialParams.max_sl_per_period != null);
    if (initialParams.max_sl_per_period != null) setSlLimitMax(initialParams.max_sl_per_period as number);
    const sp = initialParams.sl_period as string | undefined;
    if (sp === 'day' || sp === 'week' || sp === 'month') setSlLimitPeriod(sp);
    if (initialParams.commission_per_lot != null) setCommissionPerLot(initialParams.commission_per_lot as number);
    if (initialParams.max_positions != null) setMaxPositions(initialParams.max_positions as number);
  }, [initialParams]);

  // Init strategy params from defaults, overlaying initialParams
  useEffect(() => {
    if (!stratMeta) return;
    const defaults: Record<string, number | string | boolean> = {};
    for (const p of stratMeta.parameters) {
      defaults[p.name] = p.default as number | string | boolean;
    }
    // Overlay saved params (present when loading from a saved result)
    if (initialParams && appliedParamsRef.current) {
      for (const p of stratMeta.parameters) {
        if (p.name in initialParams) {
          defaults[p.name] = initialParams[p.name] as number | string | boolean;
        }
      }
    }
    setStratParams(defaults);
  }, [strategy, stratMeta, initialParams]);

  const toggleYear = (year: number) => {
    setSelectedYears((prev) =>
      prev.includes(year) ? prev.filter((y) => y !== year) : [...prev, year].sort()
    );
  };

  const poll = useCallback(
    async (jobId: string) => {
      while (true) {
        await new Promise((r) => setTimeout(r, 1000));
        const status = await api.getJobStatus(jobId);
        if (status.status === 'done' && status.result_id) {
          const result = await api.getUnsavedResult(status.result_id);
          setLoading(false);
          onResult(result);
          return;
        }
        if (status.status === 'error') {
          setLoading(false);
          setError(status.error ?? 'Backtest failed');
          return;
        }
      }
    },
    [onResult]
  );

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (selectedYears.length === 0) {
      setError('Select at least one year');
      return;
    }
    if (breakevenOn && breakevenSlR >= breakevenR) {
      setError(`Lock SL (${breakevenSlR}R) must be less than Trigger (${breakevenR}R) — you can't lock in more profit than the trigger level.`);
      return;
    }
    setError(null);
    setLoading(true);

    try {
      const req: BacktestRequest = {
        strategy,
        years: selectedYears,
        timeframe,
        symbol: selectedSymbol,
        initial_capital: capital,
        risk_pct: riskPct / 100,
        risk_recovery: riskRecovery / 100,
        compound,
        breakeven_r:    breakevenOn ? breakevenR : null,
        breakeven_sl_r: breakevenOn ? breakevenSlR : 0.0,
        commission_per_lot: commissionPerLot,
        max_sl_per_period: slLimitOn ? slLimitMax : null,
        sl_period: slLimitOn ? slLimitPeriod : 'none',
        max_positions: maxPositions,
        params: stratParams,
      };
      const job = await api.runBacktest(req);
      poll(job.job_id);
    } catch (err: unknown) {
      setLoading(false);
      const msg = err instanceof Error ? err.message : 'Request failed';
      setError(msg);
    }
  };

  const availableSymbols = dataAvail ? Object.keys(dataAvail.symbols) : ['XAUUSD'];
  const symbolData = dataAvail?.symbols[selectedSymbol];
  const availableYears = symbolData?.years ?? [2021, 2022, 2023, 2024, 2025, 2026];
  const availableTimeframes = symbolData?.timeframes ?? ['M1', 'M5', 'M15', 'H1'];

  return (
    <form onSubmit={handleSubmit} className="space-y-4">

      {/* ── Backtest Setup ─────────────────────────────────────────────── */}
      <div className="bg-slate-800/50 border border-slate-700/60 rounded-lg p-3 space-y-3">
        <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Backtest Setup</p>

        {/* Strategy */}
        <div>
          <label className="block text-xs text-slate-400 mb-1">Strategy</label>
          <select
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
            className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-slate-100 text-sm focus:outline-none focus:border-blue-500"
          >
            {strategies.map((s) => (
              <option key={s.name} value={s.name}>
                {s.display_name}
              </option>
            ))}
          </select>
        </div>

        {/* Symbol */}
        <div>
          <label className="block text-xs text-slate-400 mb-1">Symbol</label>
          <Select
            options={availableSymbols.map((sym) => ({ value: sym, label: sym }))}
            value={{ value: selectedSymbol, label: selectedSymbol }}
            onChange={(opt) => opt && setSelectedSymbol(opt.value)}
            isSearchable
            styles={{
              control: (base, state) => ({
                ...base,
                backgroundColor: '#1e293b',
                borderColor: state.isFocused ? '#3b82f6' : '#475569',
                borderRadius: '0.5rem',
                boxShadow: 'none',
                '&:hover': { borderColor: '#64748b' },
              }),
              menu: (base) => ({
                ...base,
                backgroundColor: '#1e293b',
                border: '1px solid #475569',
                borderRadius: '0.5rem',
                overflow: 'hidden',
              }),
              option: (base, state) => ({
                ...base,
                backgroundColor: state.isSelected
                  ? '#2563eb'
                  : state.isFocused
                  ? '#334155'
                  : 'transparent',
                color: state.isSelected ? '#fff' : '#cbd5e1',
                fontSize: '0.875rem',
                cursor: 'pointer',
              }),
              singleValue: (base) => ({ ...base, color: '#f1f5f9', fontSize: '0.875rem' }),
              input: (base) => ({ ...base, color: '#f1f5f9', fontSize: '0.875rem' }),
              indicatorSeparator: () => ({ display: 'none' }),
              dropdownIndicator: (base) => ({ ...base, color: '#64748b', padding: '0 6px' }),
            }}
          />
        </div>

        {/* Years */}
        <div>
          <label className="block text-xs text-slate-400 mb-1">Years</label>
          <div className="flex flex-wrap gap-2">
            {availableYears.map((year) => (
              <button
                key={year}
                type="button"
                onClick={() => toggleYear(year)}
                className={`px-3 py-1 rounded-md text-sm font-medium transition-colors ${
                  selectedYears.includes(year)
                    ? 'bg-blue-600 text-white'
                    : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                }`}
              >
                {year}
              </button>
            ))}
          </div>
        </div>

        {/* Timeframe */}
        <div>
          <label className="block text-xs text-slate-400 mb-1">Timeframe</label>
          <div className="flex gap-2">
            {availableTimeframes.map((tf) => (
              <button
                key={tf}
                type="button"
                onClick={() => setTimeframe(tf)}
                className={`px-3 py-1 rounded-md text-sm font-medium transition-colors ${
                  timeframe === tf
                    ? 'bg-blue-600 text-white'
                    : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                }`}
              >
                {tf}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* ── Capital & Risk ─────────────────────────────────────────────── */}
      <div className="bg-slate-800/50 border border-slate-700/60 rounded-lg p-3 space-y-3">
        <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Capital &amp; Risk</p>

        {/* Initial Capital */}
        <div>
          <label className="block text-xs text-slate-400 mb-1">
            Initial Capital (USD)
            <InfoTooltip text="Starting account balance for the simulation. Used to calculate lot sizes and track equity growth." />
          </label>
          <input
            type="number"
            value={capital}
            min={100}
            step={1}
            onChange={(e) => setCapital(Number(e.target.value))}
            className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-slate-100 text-sm focus:outline-none focus:border-blue-500"
          />
        </div>

        {/* Risk % */}
        <div>
          <label className="block text-xs text-slate-400 mb-1">
            Risk per Trade:{' '}
            <span className="text-blue-400 font-bold">{riskPct}%</span>
            <InfoTooltip text="Fraction of capital risked per trade. Lot size is calculated so that a full SL hit costs exactly this % of the account (or of initial capital if compounding is off)." />
          </label>
          <input
            type="range"
            min={0.5}
            max={5}
            step={0.5}
            value={riskPct}
            onChange={(e) => setRiskPct(Number(e.target.value))}
            className="w-full accent-blue-500"
          />
          <div className="flex justify-between text-xs text-slate-500 mt-0.5">
            <span>0.5%</span>
            <span>5%</span>
          </div>
        </div>

        {/* Recovery Risk */}
        <div>
          <label className="block text-xs text-slate-400 mb-1">
            Recovery Risk:{' '}
            <span className="text-blue-400 font-bold">{riskRecovery}%</span>
            <InfoTooltip text="Reduced risk % applied when current capital falls below the initial capital. Set to 0 to disable." />
          </label>
          <input
            type="range"
            min={0}
            max={5}
            step={0.5}
            value={riskRecovery}
            onChange={(e) => setRiskRecovery(Number(e.target.value))}
            className="w-full accent-blue-500"
          />
          <div className="flex justify-between text-xs text-slate-500 mt-0.5">
            <span>Disabled (0%)</span>
            <span>5%</span>
          </div>
        </div>

        {/* Commission */}
        <div>
          <label className="block text-xs text-slate-400 mb-1">
            Commission per Lot (USD)
            <InfoTooltip text="Round-trip commission charged per standard lot (entry + exit combined). Deducted from each trade's profit." />
          </label>
          <input
            type="number"
            value={commissionPerLot}
            min={0}
            step={0.5}
            onChange={(e) => setCommissionPerLot(parseFloat(e.target.value) || 0)}
            className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-slate-100 text-sm focus:outline-none focus:border-blue-500"
          />
        </div>

        {/* Compounding */}
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => setCompound((c) => !c)}
            className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 ${
              compound ? 'bg-blue-600' : 'bg-slate-600'
            }`}
          >
            <span
              className={`absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform ${
                compound ? 'translate-x-5' : ''
              }`}
            />
          </button>
          <span className="text-xs text-slate-400">
            Compounding{' '}
            {compound ? <span className="text-blue-400">ON</span> : <span className="text-slate-500">OFF</span>}
          </span>
          <InfoTooltip text="ON: lot size recalculated from current balance each trade (risk compounds). OFF: lot size uses the fixed initial capital throughout." />
        </div>
      </div>

      {/* ── Trade Management ───────────────────────────────────────────── */}
      <div className="bg-slate-800/50 border border-slate-700/60 rounded-lg p-3 space-y-3">
        <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Trade Management</p>

        {/* Max Positions */}
        <div className="flex items-center gap-3">
          <span className="text-xs text-slate-400">
            Max Positions
            <InfoTooltip text="Maximum number of trades that can be open simultaneously. When set to 1 (default) the engine behaves as before — one trade at a time. Higher values allow concurrent positions from different signals." />
          </span>
          <input
            type="number"
            value={maxPositions}
            min={1}
            max={10}
            step={1}
            onChange={(e) => setMaxPositions(Math.max(1, parseInt(e.target.value) || 1))}
            className="w-16 bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm text-center text-slate-100"
          />
        </div>

        {/* Move SL (Breakeven) */}
        <div className="flex items-start gap-3 flex-wrap">
          <button
            type="button"
            onClick={() => setBreakevenOn((b) => !b)}
            className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 mt-0.5 ${
              breakevenOn ? 'bg-amber-500' : 'bg-slate-600'
            }`}
          >
            <span
              className={`absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform ${
                breakevenOn ? 'translate-x-5' : ''
              }`}
            />
          </button>
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-400 flex-shrink-0">Move SL after trigger</span>
              <InfoTooltip text="Once price reaches the trigger R, the stop-loss is moved to the locked R level. E.g. trigger=1R, lock=0.5R locks in 0.5R profit. Lock=0 is classic break-even. Lock can be negative to cut losses early." />
            </div>
            {breakevenOn && (
              <>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-slate-500 w-14">Trigger</span>
                  <input
                    type="number"
                    value={breakevenR}
                    min={0.1}
                    max={10}
                    step={0.1}
                    onChange={(e) => setBreakevenR(parseFloat(e.target.value) || 1.0)}
                    className="w-20 bg-slate-800 border border-amber-500 rounded px-2 py-1 text-sm text-center text-amber-300"
                  />
                  <span className="text-xs text-slate-500">R</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-slate-500 w-14">Lock SL at</span>
                  <input
                    type="number"
                    value={breakevenSlR}
                    min={-2}
                    max={5}
                    step={0.1}
                    onChange={(e) => setBreakevenSlR(parseFloat(e.target.value) ?? 0.0)}
                    className="w-20 bg-slate-800 border border-amber-500 rounded px-2 py-1 text-sm text-center text-amber-300"
                  />
                  <span className="text-xs text-slate-500">R <span className="text-slate-600">(0 = entry, 0.5 = lock profit)</span></span>
                </div>
                {breakevenSlR >= breakevenR && (
                  <p className="text-xs text-red-400">Lock ({breakevenSlR}R) must be &lt; Trigger ({breakevenR}R)</p>
                )}
              </>
            )}
          </div>
        </div>

        {/* Period SL Limit */}
        <div className="flex items-start gap-3 flex-wrap">
          <button
            type="button"
            onClick={() => setSlLimitOn((v) => !v)}
            className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 mt-0.5 ${
              slLimitOn ? 'bg-amber-500' : 'bg-slate-600'
            }`}
          >
            <span
              className={`absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform ${
                slLimitOn ? 'translate-x-5' : ''
              }`}
            />
          </button>
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-400 flex-shrink-0">Period SL Limit</span>
              <InfoTooltip text="Stop taking new trades for the rest of the period (day / week / month) once this many stop-losses have been hit. Resets automatically at the start of each new period." />
            </div>
            {slLimitOn && (
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs text-slate-500">Max</span>
                <input
                  type="number"
                  value={slLimitMax}
                  min={1}
                  max={20}
                  step={1}
                  onChange={(e) => setSlLimitMax(Math.max(1, parseInt(e.target.value) || 1))}
                  className="w-16 bg-slate-800 border border-amber-500 rounded px-2 py-1 text-sm text-center text-amber-300"
                />
                <span className="text-xs text-slate-500">SLs per</span>
                {(['day', 'week', 'month'] as const).map((p) => (
                  <button
                    key={p}
                    type="button"
                    onClick={() => setSlLimitPeriod(p)}
                    className={`px-2 py-0.5 rounded text-xs font-medium transition-colors ${
                      slLimitPeriod === p
                        ? 'bg-amber-500 text-white'
                        : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
                    }`}
                  >
                    {p}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Strategy-specific params — grouped by purpose */}
      {stratMeta && stratMeta.parameters.length > 0 &&
        (() => {
          const currentFilter = (stratParams['sideways_filter'] ?? 'none') as string;
          const mcFilterOn = (stratParams['momentum_candle_filter'] ?? false) as boolean;

          const requireOte = (stratParams['require_ote'] ?? false) as boolean;

          const currentPendingCancel = (stratParams['pending_cancel'] ?? 'max_bars') as string;

          const isVisible = (name: string): boolean => {
            if (name.startsWith('adx_'))        return currentFilter === 'adx';
            if (name.startsWith('ema_slope_'))  return currentFilter === 'ema_slope';
            if (name.startsWith('choppiness_')) return currentFilter === 'choppiness';
            if (name.startsWith('alligator_'))  return currentFilter === 'alligator';
            if (name.startsWith('stochrsi_'))   return currentFilter === 'stochrsi';
            if (name.startsWith('mc_'))         return mcFilterOn;
            if (name.startsWith('ote_'))        return requireOte;
            if (name === 'max_pending_bars')    return currentPendingCancel === 'max_bars' || currentPendingCancel === 'both';
            return true;
          };

          // Build a lookup from param name → spec for quick access
          const paramByName = Object.fromEntries(stratMeta.parameters.map((p) => [p.name, p]));

          const renderParam = (name: string) => {
            const p = paramByName[name];
            if (!p || !isVisible(name)) return null;

            const meta = PARAM_META[p.name];
            const label = meta?.label ?? p.name.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
            const optionLabels = OPTION_LABELS[p.name] ?? {};

            return (
              <div key={p.name}>
                {p.type === 'bool' ? (
                  <div className="flex items-center gap-3">
                    <button
                      type="button"
                      onClick={() =>
                        setStratParams((prev) => ({ ...prev, [p.name]: !prev[p.name] }))
                      }
                      className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 ${
                        stratParams[p.name] ? 'bg-blue-600' : 'bg-slate-600'
                      }`}
                    >
                      <span
                        className={`absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform ${
                          stratParams[p.name] ? 'translate-x-5' : ''
                        }`}
                      />
                    </button>
                    <span className="text-xs text-slate-400">
                      {label}{' '}
                      {stratParams[p.name] ? (
                        <span className="text-blue-400">ON</span>
                      ) : (
                        <span className="text-slate-500">OFF</span>
                      )}
                      {meta?.description && <InfoTooltip text={meta.description} />}
                    </span>
                  </div>
                ) : p.type === 'str' ? (
                  <>
                    <label className="block text-xs text-slate-400 mb-1">
                      {label}
                      {meta?.description && <InfoTooltip text={meta.description} />}
                    </label>
                    <select
                      value={(stratParams[p.name] ?? p.default) as string}
                      onChange={(e) =>
                        setStratParams((prev) => ({ ...prev, [p.name]: e.target.value }))
                      }
                      className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-slate-100 text-sm focus:outline-none focus:border-blue-500"
                    >
                      {p.options?.map((opt) => (
                        <option key={opt} value={opt}>
                          {optionLabels[opt] ?? opt}
                        </option>
                      ))}
                    </select>
                  </>
                ) : (
                  <>
                    <label className="block text-xs text-slate-400 mb-1">
                      {label}:{' '}
                      <span className="text-blue-400">
                        {stratParams[p.name] ?? p.default}
                      </span>
                      {meta?.description && <InfoTooltip text={meta.description} />}
                    </label>
                    <input
                      type="number"
                      step={p.step ?? (p.type === 'float' ? 0.1 : 1)}
                      min={p.min}
                      max={p.max}
                      value={(stratParams[p.name] ?? p.default) as number}
                      onChange={(e) =>
                        setStratParams((prev) => ({
                          ...prev,
                          [p.name]:
                            p.type === 'float'
                              ? parseFloat(e.target.value)
                              : parseInt(e.target.value),
                        }))
                      }
                      className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-slate-100 text-sm focus:outline-none focus:border-blue-500"
                    />
                  </>
                )}
              </div>
            );
          };

          const groups = PARAM_GROUPS[stratMeta.name];

          // Fallback: render all params flat if no group definition exists
          if (!groups) {
            return (
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-3">
                  Strategy Parameters
                </label>
                <div className="space-y-3">
                  {stratMeta.parameters.filter((p) => isVisible(p.name)).map((p) => renderParam(p.name))}
                </div>
              </div>
            );
          }

          return (
            <div className="space-y-4">
              <label className="block text-sm font-medium text-slate-300">
                Strategy Parameters
              </label>
              {groups.map((group) => {
                const visibleInGroup = group.params.filter((name) => paramByName[name] && isVisible(name));
                if (visibleInGroup.length === 0) return null;
                return (
                  <div key={group.title} className="bg-slate-800/50 border border-slate-700/60 rounded-lg p-3 space-y-3">
                    <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider">
                      {group.title}
                    </p>
                    {visibleInGroup.map((name) => renderParam(name))}
                  </div>
                );
              })}
            </div>
          );
        })()}

      {error && (
        <div className="bg-red-900/40 border border-red-500/50 text-red-300 rounded-lg px-4 py-3 text-sm">
          {error}
        </div>
      )}

      <button
        type="submit"
        disabled={loading}
        className="w-full bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 disabled:text-slate-500 text-white font-semibold py-3 rounded-lg transition-colors flex items-center justify-center gap-2"
      >
        {loading ? (
          <>
            <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
              />
            </svg>
            Running backtest…
          </>
        ) : (
          'Run Backtest'
        )}
      </button>
    </form>
  );
}
