import type { ResultSummary } from '../api/types';

interface Props {
  results: ResultSummary[];
  onRemove: (id: string) => void;
}

const METRICS: { key: keyof ResultSummary; label: string; higherIsBetter: boolean; format: (v: unknown) => string }[] =
  [
    {
      key: 'total_return_pct',
      label: 'Total Return',
      higherIsBetter: true,
      format: (v) => `${(v as number) >= 0 ? '+' : ''}${(v as number).toFixed(2)}%`,
    },
    {
      key: 'win_rate_pct',
      label: 'Win Rate',
      higherIsBetter: true,
      format: (v) => `${(v as number).toFixed(1)}%`,
    },
    {
      key: 'profit_factor',
      label: 'Profit Factor',
      higherIsBetter: true,
      format: (v) => (v as number).toFixed(3),
    },
    {
      key: 'max_drawdown_pct',
      label: 'Max Drawdown',
      higherIsBetter: false,
      format: (v) => `-${(v as number).toFixed(2)}%`,
    },
    {
      key: 'total_trades',
      label: 'Total Trades',
      higherIsBetter: true,
      format: (v) => String(v),
    },
  ];

// Sub-params that belong to each sideways filter
const FILTER_SUBPARAMS: Record<string, { key: string; label: string }[]> = {
  adx: [
    { key: 'adx_period', label: 'ADX Period' },
    { key: 'adx_threshold', label: 'ADX Threshold' },
  ],
  ema_slope: [
    { key: 'ema_slope_period', label: 'EMA Slope Period' },
    { key: 'ema_slope_min', label: 'EMA Slope Min' },
  ],
  choppiness: [
    { key: 'choppiness_period', label: 'Choppiness Period' },
    { key: 'choppiness_max', label: 'Choppiness Max' },
  ],
  alligator: [
    { key: 'alligator_jaw', label: 'Jaw Period' },
    { key: 'alligator_teeth', label: 'Teeth Period' },
    { key: 'alligator_lips', label: 'Lips Period' },
  ],
  stochrsi: [
    { key: 'stochrsi_rsi_period', label: 'RSI Period' },
    { key: 'stochrsi_stoch_period', label: 'Stoch Period' },
    { key: 'stochrsi_oversold', label: 'Oversold' },
    { key: 'stochrsi_overbought', label: 'Overbought' },
  ],
};

const FILTER_LABELS: Record<string, string> = {
  none: 'None',
  adx: 'ADX',
  ema_slope: 'EMA Slope',
  choppiness: 'Choppiness',
  alligator: 'Alligator',
  stochrsi: 'Stoch RSI',
};

function formatParam(key: string, val: unknown): string {
  if (val === null || val === undefined) return '—';
  if (typeof val === 'boolean') return val ? 'Yes' : 'No';
  if (key === 'risk_pct') return `${((val as number) * 100).toFixed(1)}%`;
  if (key === 'initial_capital') return `$${(val as number).toLocaleString()}`;
  if (key === 'commission_per_lot') return `$${(val as number).toFixed(2)}`;
  if (typeof val === 'number') return String(val);
  return String(val);
}

export function ComparePanel({ results, onRemove }: Props) {
  if (results.length === 0) return null;

  const best: Record<string, string | number> = {};
  for (const m of METRICS) {
    const vals = results.map((r) => r[m.key] as number);
    best[m.key] = m.higherIsBetter ? Math.max(...vals) : Math.min(...vals);
  }

  // Collect all active sideways filter sub-param rows across compared results
  const activeFilters = new Set(
    results.map((r) => (r.parameters.sideways_filter as string | undefined) ?? 'none')
  );
  const filterSubRows: { key: string; label: string }[] = [];
  for (const filter of activeFilters) {
    for (const sp of FILTER_SUBPARAMS[filter] ?? []) {
      if (!filterSubRows.find((x) => x.key === sp.key)) filterSubRows.push(sp);
    }
  }

  // Strategy param rows (always shown)
  const stratParamRows: { key: string; label: string }[] = [
    { key: 'ema_period', label: 'EMA Period' },
    { key: 'fractal_n', label: 'Fractal N' },
    { key: 'rr_ratio', label: 'R/R Ratio' },
  ];

  // Config rows
  const configRows: { key: string; label: string }[] = [
    { key: 'initial_capital', label: 'Capital' },
    { key: 'risk_pct', label: 'Risk %' },
    { key: 'compound', label: 'Compound' },
    { key: 'breakeven_r', label: 'Break-even R' },
    { key: 'commission_per_lot', label: 'Commission/Lot' },
  ];

  // A value is "different" if not all results share the same value for that param
  const isDiff = (key: string) => {
    const vals = results.map((r) => String(r.parameters[key] ?? ''));
    return new Set(vals).size > 1;
  };

  const SectionRow = ({ label }: { label: string }) => (
    <tr className="bg-slate-800/60">
      <td colSpan={results.length + 1} className="px-6 py-2 text-xs font-semibold text-slate-400 uppercase tracking-wider">
        {label}
      </td>
    </tr>
  );

  const ParamRow = ({ rowKey, label }: { rowKey: string; label: string }) => {
    const diff = isDiff(rowKey);
    return (
      <tr className="border-b border-slate-800/50">
        <td className="px-6 py-2.5 text-slate-400 text-xs">{label}</td>
        {results.map((r) => {
          const val = r.parameters[rowKey];
          return (
            <td key={r.id} className={`px-4 py-2.5 text-xs font-medium ${diff ? 'text-amber-300' : 'text-slate-300'}`}>
              {formatParam(rowKey, val)}
            </td>
          );
        })}
      </tr>
    );
  };

  const FilterRow = () => {
    const diff = isDiff('sideways_filter');
    return (
      <tr className="border-b border-slate-800/50">
        <td className="px-6 py-2.5 text-slate-400 text-xs">Filter</td>
        {results.map((r) => {
          const f = (r.parameters.sideways_filter as string | undefined) ?? 'none';
          return (
            <td key={r.id} className={`px-4 py-2.5 text-xs font-medium ${diff ? 'text-amber-300' : 'text-slate-300'}`}>
              {FILTER_LABELS[f] ?? f}
            </td>
          );
        })}
      </tr>
    );
  };

  return (
    <div className="bg-slate-900 border border-slate-700 rounded-2xl overflow-hidden">
      <div className="px-6 py-4 border-b border-slate-700 flex items-center justify-between">
        <h3 className="text-lg font-semibold text-slate-100">Compare Results</h3>
        <span className="text-xs text-slate-500">
          <span className="inline-block w-2 h-2 rounded-full bg-amber-400 mr-1" />
          amber = values differ
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-800">
              <th className="text-left px-6 py-3 text-slate-500 font-medium w-40">Metric / Param</th>
              {results.map((r) => (
                <th key={r.id} className="px-4 py-3 text-left">
                  <div className="flex items-start gap-2">
                    <div>
                      <div className="text-slate-200 font-medium text-xs">{r.strategy.replace(/_/g, ' ')}</div>
                      <div className="text-slate-500 text-xs">
                        {r.timeframe} · {r.years.join(', ')}
                      </div>
                    </div>
                    <button
                      onClick={() => onRemove(r.id)}
                      className="text-slate-600 hover:text-red-400 mt-0.5 text-xs"
                    >
                      ✕
                    </button>
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {/* Performance metrics */}
            <SectionRow label="Performance" />
            {METRICS.map((m) => (
              <tr key={m.key} className="border-b border-slate-800/50">
                <td className="px-6 py-2.5 text-slate-400 text-xs">{m.label}</td>
                {results.map((r) => {
                  const val = r[m.key] as number;
                  const isBest = val === best[m.key];
                  return (
                    <td key={r.id} className={`px-4 py-2.5 font-semibold text-xs ${isBest ? 'text-emerald-400' : 'text-slate-300'}`}>
                      {m.format(val)}
                      {isBest && <span className="ml-1">★</span>}
                    </td>
                  );
                })}
              </tr>
            ))}

            {/* Strategy params */}
            <SectionRow label="Strategy" />
            {stratParamRows.map((p) => <ParamRow key={p.key} rowKey={p.key} label={p.label} />)}

            {/* Sideways filter */}
            <SectionRow label="Sideways Filter" />
            <FilterRow />
            {filterSubRows.map((p) => <ParamRow key={p.key} rowKey={p.key} label={p.label} />)}

            {/* Config */}
            <SectionRow label="Configuration" />
            {configRows.map((p) => <ParamRow key={p.key} rowKey={p.key} label={p.label} />)}
          </tbody>
        </table>
      </div>
    </div>
  );
}
