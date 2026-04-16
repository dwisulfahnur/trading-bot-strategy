import { useState, useEffect, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { BacktestRequest, BacktestResult, StrategyMeta } from '../api/types';

interface Props {
  onResult: (result: BacktestResult) => void;
}


export function BacktestForm({ onResult }: Props) {
  const { data: strategies = [] } = useQuery({
    queryKey: ['strategies'],
    queryFn: api.getStrategies,
  });
  const { data: dataAvail } = useQuery({
    queryKey: ['data-available'],
    queryFn: api.getDataAvailable,
  });

  const [strategy, setStrategy] = useState('william_fractals');
  const [selectedYears, setSelectedYears] = useState<number[]>([2025, 2026]);
  const [timeframe, setTimeframe] = useState('H1');
  const [capital, setCapital] = useState(10000);
  const [riskPct, setRiskPct] = useState(2);
  const [compound, setCompound] = useState(false);
  const [breakevenOn, setBreakevenOn] = useState(false);
  const [breakevenR, setBreakevenR] = useState(1.0);
  const [stratParams, setStratParams] = useState<Record<string, number | string>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const stratMeta: StrategyMeta | undefined = strategies.find((s) => s.name === strategy);

  // Init strategy params from defaults
  useEffect(() => {
    if (!stratMeta) return;
    const defaults: Record<string, number | string> = {};
    for (const p of stratMeta.parameters) {
      defaults[p.name] = p.default as number | string;
    }
    setStratParams(defaults);
  }, [strategy, stratMeta]);

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
          const result = await api.getResult(status.result_id);
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
    setError(null);
    setLoading(true);

    try {
      const req: BacktestRequest = {
        strategy,
        years: selectedYears,
        timeframe,
        initial_capital: capital,
        risk_pct: riskPct / 100,
        compound,
        breakeven_r: breakevenOn ? breakevenR : null,
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

  const availableYears = dataAvail?.years ?? [2021, 2022, 2023, 2024, 2025, 2026];

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      {/* Strategy selector */}
      <div>
        <label className="block text-sm font-medium text-slate-300 mb-1">Strategy</label>
        <select
          value={strategy}
          onChange={(e) => setStrategy(e.target.value)}
          className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-slate-100 focus:outline-none focus:border-blue-500"
        >
          {strategies.map((s) => (
            <option key={s.name} value={s.name}>
              {s.display_name}
            </option>
          ))}
        </select>
      </div>

      {/* Year selection */}
      <div>
        <label className="block text-sm font-medium text-slate-300 mb-2">Years</label>
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
        <label className="block text-sm font-medium text-slate-300 mb-2">Timeframe</label>
        <div className="flex gap-2">
          {(dataAvail?.timeframes ?? ['M1', 'M5', 'M15', 'H1']).map((tf) => (
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

      {/* Capital */}
      <div>
        <label className="block text-sm font-medium text-slate-300 mb-1">
          Initial Capital (USD)
        </label>
        <input
          type="number"
          value={capital}
          min={100}
          step={1}
          onChange={(e) => setCapital(Number(e.target.value))}
          className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-slate-100 focus:outline-none focus:border-blue-500"
        />
      </div>

      {/* Risk % */}
      <div>
        <label className="block text-sm font-medium text-slate-300 mb-1">
          Risk per Trade: <span className="text-blue-400 font-bold">{riskPct}%</span>
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
        <div className="flex justify-between text-xs text-slate-500 mt-1">
          <span>0.5%</span>
          <span>5%</span>
        </div>
      </div>

      {/* Toggles */}
      <div className="space-y-2">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => setCompound((c) => !c)}
            className={`relative w-11 h-6 rounded-full transition-colors ${
              compound ? 'bg-blue-600' : 'bg-slate-600'
            }`}
          >
            <span
              className={`absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform ${
                compound ? 'translate-x-5' : ''
              }`}
            />
          </button>
          <span className="text-sm text-slate-300">
            Compounding {compound ? <span className="text-blue-400">ON</span> : <span className="text-slate-500">OFF</span>}
          </span>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <button
            type="button"
            onClick={() => setBreakevenOn((b) => !b)}
            className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 ${
              breakevenOn ? 'bg-amber-500' : 'bg-slate-600'
            }`}
          >
            <span
              className={`absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform ${
                breakevenOn ? 'translate-x-5' : ''
              }`}
            />
          </button>
          <span className="text-sm text-slate-300 flex-shrink-0">Move SL to BE at</span>
          <input
            type="number"
            value={breakevenR}
            min={0.1}
            max={10}
            step={0.1}
            disabled={!breakevenOn}
            onChange={(e) => setBreakevenR(parseFloat(e.target.value) || 1.0)}
            className={`w-20 bg-slate-800 border rounded px-2 py-1 text-sm text-center transition-colors ${
              breakevenOn
                ? 'border-amber-500 text-amber-300'
                : 'border-slate-700 text-slate-600 cursor-not-allowed'
            }`}
          />
          <span className="text-sm text-slate-500">R</span>
        </div>
      </div>

      {/* Strategy-specific params */}
      {stratMeta && stratMeta.parameters.length > 0 && (() => {
        const currentFilter = (stratParams['sideways_filter'] ?? 'none') as string;

        // Only show sub-params that belong to the active filter
        const isVisible = (name: string): boolean => {
          if (name.startsWith('adx_'))        return currentFilter === 'adx';
          if (name.startsWith('ema_slope_'))  return currentFilter === 'ema_slope';
          if (name.startsWith('choppiness_')) return currentFilter === 'choppiness';
          if (name.startsWith('alligator_'))  return currentFilter === 'alligator';
          if (name.startsWith('stochrsi_'))   return currentFilter === 'stochrsi';
          return true; // base params + sideways_filter selector always visible
        };

        const visibleParams = stratMeta.parameters.filter((p) => isVisible(p.name));

        return (
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-3">Strategy Parameters</label>
            <div className="space-y-3">
              {visibleParams.map((p) => (
                <div key={p.name}>
                  {p.type === 'str' ? (
                    <>
                      <label className="block text-xs text-slate-400 mb-1">
                        {p.name.replace(/_/g, ' ')}
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
                            {opt === 'none'        ? 'None (disabled)'
                             : opt === 'adx'        ? 'ADX — trend strength'
                             : opt === 'ema_slope'  ? 'EMA Slope — trend angle'
                             : opt === 'choppiness' ? 'Choppiness Index'
                             : opt === 'alligator'  ? 'Alligator — line order'
                             : opt === 'stochrsi'   ? 'Stoch RSI — pullback zone'
                             : opt}
                          </option>
                        ))}
                      </select>
                    </>
                  ) : (
                    <>
                      <label className="block text-xs text-slate-400 mb-1">
                        {p.name.replace(/_/g, ' ')}:{' '}
                        <span className="text-blue-400">{stratParams[p.name] ?? p.default}</span>
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
                            [p.name]: p.type === 'float' ? parseFloat(e.target.value) : parseInt(e.target.value),
                          }))
                        }
                        className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-slate-100 text-sm focus:outline-none focus:border-blue-500"
                      />
                    </>
                  )}
                </div>
              ))}
            </div>
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
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
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
