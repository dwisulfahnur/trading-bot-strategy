import { useState } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ResultCard } from '../components/ResultCard';
import { EquityChart } from '../components/EquityChart';
import { PriceChart } from '../components/PriceChart';
import { TradeTable } from '../components/TradeTable';
import { PerMonthTable } from '../components/PerMonthTable';
import { TradeStatsTable } from '../components/TradeStatsTable';
import { PipCurve } from '../components/PipCurve';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import type { OhlcvBar } from '../api/types';

const FILTER_LABELS: Record<string, string> = {
  none: 'None',
  adx: 'ADX',
  ema_slope: 'EMA Slope',
  choppiness: 'Choppiness',
  alligator: 'Alligator',
  stochrsi: 'Stoch RSI',
};

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

function formatParamValue(key: string, val: unknown): string {
  if (val === null || val === undefined) return '—';
  if (typeof val === 'boolean') return val ? 'Yes' : 'No';
  if (key === 'risk_pct') return `${((val as number) * 100).toFixed(1)}%`;
  if (key === 'initial_capital') return `$${(val as number).toLocaleString()}`;
  if (key === 'commission_per_lot') return `$${(val as number).toFixed(2)}`;
  if (Array.isArray(val)) return val.join(', ');
  return String(val);
}

function ParamField({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-slate-500 uppercase tracking-wide">{label}</span>
      <span className="text-sm text-slate-200 font-medium">{value}</span>
    </div>
  );
}

function SectionHeader({ title }: { title: string }) {
  return (
    <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">{title}</h3>
  );
}

export function ResultDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { user, logout } = useAuth();

  const [highlightedTrade, setHighlightedTrade] = useState<number | null>(null);
  const [activeTab, setActiveTab] = useState<'equity' | 'price' | 'trades' | 'stats'>('equity');

  function handleLogout() {
    logout();
    navigate('/login');
  }

  const { data: result, isLoading, isError } = useQuery({
    queryKey: ['result', id],
    queryFn: () => api.getResult(id!),
    enabled: !!id,
  });

  const { data: bars = [] } = useQuery<OhlcvBar[]>({
    queryKey: ['ohlcv', result?.parameters?.timeframe, result?.parameters?.years, result?.parameters?.symbol],
    queryFn: () => {
      if (!result) return Promise.resolve([]);
      const tf = result.parameters.timeframe as string;
      const years = result.parameters.years as number[];
      const symbol = (result.parameters.symbol as string) ?? 'XAUUSD';
      const trades = result.results.trades;
      const dateFrom = trades[0]?.entry_time?.slice(0, 10);
      const dateTo = trades[trades.length - 1]?.exit_time?.slice(0, 10);
      return api.getOhlcv(tf, years, symbol, dateFrom, dateTo);
    },
    enabled: !!result,
  });

  const handleTradeClick = (index: number) => {
    setHighlightedTrade(index);
    if (activeTab !== 'price') setActiveTab('price');
  };

  const handleEquityClick = (index: number) => {
    setHighlightedTrade(index);
    setActiveTab('price');
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      {/* Header */}
      <header className="border-b border-slate-800 px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link to="/" className="flex items-center gap-3 hover:opacity-80">
            <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center text-white font-bold text-sm">
              FX
            </div>
            <span className="font-semibold text-lg text-slate-100">Strategy Backtest</span>
          </Link>
          <span className="text-slate-600">/</span>
          <Link to="/results" className="text-slate-400 hover:text-slate-200 transition-colors">
            Saved Results
          </Link>
          {result && (
            <>
              <span className="text-slate-600">/</span>
              <span className="text-slate-400 truncate max-w-xs text-sm">
                {result.name ?? result.strategy.replace(/_/g, ' ')}
              </span>
            </>
          )}
        </div>
        <div className="flex items-center gap-4">
          <Link to="/results" className="text-sm text-blue-400 hover:text-blue-300 transition-colors">
            ← Back to Results
          </Link>
          <div className="flex items-center gap-2 border-l border-slate-800 pl-4">
            <span className="text-xs text-slate-500 hidden sm:block">{user?.email}</span>
            <button
              onClick={handleLogout}
              className="text-xs text-slate-400 hover:text-red-400 transition-colors"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <div className="max-w-6xl mx-auto px-6 py-8 space-y-8">
        {isLoading && (
          <div className="flex items-center justify-center py-24 text-slate-500">Loading result…</div>
        )}

        {isError && (
          <div className="flex flex-col items-center justify-center py-24 text-slate-500 gap-3">
            <p>Failed to load result.</p>
            <Link to="/results" className="text-blue-400 hover:text-blue-300 text-sm">← Back to Results</Link>
          </div>
        )}

        {result && (
          <>
            {/* Title */}
            <div className="flex items-start justify-between gap-4">
              <div className="space-y-1 min-w-0">
                <h1 className="text-2xl font-bold text-slate-100">
                  {result.name && <span>{result.name}</span>}
                  {result.name && <span className="ml-2 text-slate-500 font-normal text-lg">·</span>}
                  <span className={result.name ? 'ml-2 text-slate-400 font-normal text-lg' : ''}>
                    {result.strategy.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())}
                  </span>
                </h1>
                <div className="flex flex-wrap items-center gap-2 text-sm text-slate-400">
                  <span className="bg-slate-800 px-2 py-0.5 rounded text-xs font-medium">
                    {(result.parameters.symbol as string) ?? 'XAUUSD'}
                  </span>
                  <span className="bg-slate-800 px-2 py-0.5 rounded text-xs font-medium">
                    {result.parameters.timeframe as string}
                  </span>
                  <span className="bg-slate-800 px-2 py-0.5 rounded text-xs font-medium">
                    {(result.parameters.years as number[]).join(', ')}
                  </span>
                  <span className="text-slate-600 text-xs">
                    Saved {new Date(result.created_at).toLocaleString()}
                  </span>
                </div>
                <p className="text-xs text-slate-600 font-mono truncate">{result.id}</p>
              </div>
              <button
                onClick={() => navigate(`/?load=${result.id}`)}
                className="flex-shrink-0 flex items-center gap-1.5 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-semibold rounded-lg transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M11 15l-3-3m0 0l3-3m-3 3h8M3 12a9 9 0 1118 0A9 9 0 013 12z" />
                </svg>
                Open in Backtest
              </button>
            </div>

            {/* Parameters overview */}
            <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-6">
              <h2 className="font-semibold text-slate-200">Backtest Configuration</h2>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-6">
                {/* Strategy params */}
                <div className="space-y-3">
                  <SectionHeader title="Strategy Parameters" />
                  <div className="space-y-3">
                    <ParamField label="EMA Period" value={formatParamValue('ema_period', result.parameters.ema_period)} />
                    <ParamField label="Fractal N" value={formatParamValue('fractal_n', result.parameters.fractal_n)} />
                    <ParamField label="R/R Ratio" value={formatParamValue('rr_ratio', result.parameters.rr_ratio)} />
                  </div>
                </div>

                {/* Sideways filter */}
                <div className="space-y-3">
                  <SectionHeader title="Sideways Filter" />
                  <div className="space-y-3">
                    <ParamField
                      label="Filter"
                      value={FILTER_LABELS[(result.parameters.sideways_filter as string) ?? 'none'] ?? String(result.parameters.sideways_filter ?? 'None')}
                    />
                    {(() => {
                      const filter = (result.parameters.sideways_filter as string) ?? 'none';
                      const subParams = FILTER_SUBPARAMS[filter] ?? [];
                      return subParams.map(({ key, label }) => (
                        <ParamField
                          key={key}
                          label={label}
                          value={formatParamValue(key, result.parameters[key])}
                        />
                      ));
                    })()}
                  </div>
                </div>

                {/* Run config */}
                <div className="space-y-3">
                  <SectionHeader title="Run Configuration" />
                  <div className="space-y-3">
                    <ParamField label="Initial Capital" value={formatParamValue('initial_capital', result.parameters.initial_capital)} />
                    <ParamField label="Risk per Trade" value={formatParamValue('risk_pct', result.parameters.risk_pct)} />
                    <ParamField label="Compounding" value={formatParamValue('compound', result.parameters.compound)} />
                    <ParamField
                      label="SL Move Trigger R"
                      value={result.parameters.breakeven_r != null ? String(result.parameters.breakeven_r) : 'Disabled'}
                    />
                    {result.parameters.breakeven_r != null && (
                      <ParamField label="SL Lock R" value={formatParamValue('breakeven_sl_r', result.parameters.breakeven_sl_r)} />
                    )}
                    <ParamField label="Commission / Lot" value={formatParamValue('commission_per_lot', result.parameters.commission_per_lot)} />
                    {result.parameters.max_sl_per_period != null && (
                      <ParamField
                        label="Max SL / Period"
                        value={`${result.parameters.max_sl_per_period} per ${result.parameters.sl_period ?? 'week'}`}
                      />
                    )}
                  </div>
                </div>
              </div>
            </div>

            {/* Performance metrics */}
            <div className="space-y-3">
              <h2 className="font-semibold text-slate-200">Performance Metrics</h2>
              <ResultCard results={result.results} stoppedOut={result.results.stopped_out} />
            </div>

            {/* Monthly breakdown */}
            <div className="space-y-3">
              <h2 className="font-semibold text-slate-200">Monthly Performance</h2>
              <PerMonthTable
                trades={result.results.trades}
                initialCapital={result.results.initial_capital}
              />
            </div>

            {/* Charts & data tabs */}
            <div className="space-y-4">
              <div className="flex gap-1 bg-slate-900 rounded-lg p-1 w-fit border border-slate-800">
                {(['equity', 'price', 'trades', 'stats'] as const).map((tab) => (
                  <button
                    key={tab}
                    onClick={() => setActiveTab(tab)}
                    className={`px-4 py-1.5 rounded-md text-sm font-medium transition-colors capitalize ${
                      activeTab === tab
                        ? 'bg-blue-600 text-white'
                        : 'text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {tab === 'equity'
                      ? 'Equity Curve'
                      : tab === 'price'
                      ? 'Price Chart'
                      : tab === 'trades'
                      ? 'Trades'
                      : 'Statistics'}
                  </button>
                ))}
              </div>

              {activeTab === 'equity' && (
                <div className="space-y-4">
                  <div className="bg-slate-900 border border-slate-800 rounded-2xl p-4">
                    <h3 className="text-sm font-semibold text-slate-400 mb-4">Equity Curve</h3>
                    <EquityChart
                      data={result.results.equity_curve}
                      initialCapital={result.results.initial_capital}
                      stoppedOut={result.results.stopped_out}
                      onTradeClick={handleEquityClick}
                      highlightedTrade={
                        highlightedTrade !== null
                          ? result.results.trades[highlightedTrade]?.trade
                          : null
                      }
                    />
                  </div>
                  <div className="bg-slate-900 border border-slate-800 rounded-2xl p-4">
                    <h3 className="text-sm font-semibold text-slate-400 mb-4">Pip Curve</h3>
                    <PipCurve trades={result.results.trades} />
                  </div>
                </div>
              )}

              {activeTab === 'price' && (
                <div className="bg-slate-900 border border-slate-800 rounded-2xl p-4">
                  <h3 className="text-sm font-semibold text-slate-400 mb-4">Price Chart with Trades</h3>
                  {bars.length > 0 ? (
                    <PriceChart
                      bars={bars}
                      trades={result.results.trades}
                      highlightedTrade={highlightedTrade}
                      onTradeClick={(idx) => setHighlightedTrade(idx)}
                      emaPeriod={result.parameters.ema_period as number}
                    />
                  ) : (
                    <div className="flex items-center justify-center h-40 text-slate-600">
                      Loading chart data…
                    </div>
                  )}
                </div>
              )}

              {activeTab === 'trades' && (
                <div className="bg-slate-900 border border-slate-800 rounded-2xl p-4">
                  <h3 className="text-sm font-semibold text-slate-400 mb-4">
                    Trade Log ({result.results.total_trades} trades)
                  </h3>
                  <TradeTable
                    trades={result.results.trades}
                    highlightedTrade={highlightedTrade}
                    onTradeClick={handleTradeClick}
                    pipMult={result.results.pip_mult}
                  />
                </div>
              )}

              {activeTab === 'stats' && (
                <div className="bg-slate-900 border border-slate-800 rounded-2xl p-4">
                  <h3 className="text-sm font-semibold text-slate-400 mb-4">Trade Statistics</h3>
                  <TradeStatsTable
                    trades={result.results.trades}
                    pipMult={result.results.pip_mult}
                  />
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
