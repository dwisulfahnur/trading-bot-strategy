import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { BacktestForm } from '../components/BacktestForm';
import { ResultCard } from '../components/ResultCard';
import { EquityChart } from '../components/EquityChart';
import { PriceChart } from '../components/PriceChart';
import { TradeTable } from '../components/TradeTable';
import { PerMonthTable } from '../components/PerMonthTable';
import { TradeStatsTable } from '../components/TradeStatsTable';
import { PipCurve } from '../components/PipCurve';
import { EAModal } from '../components/EAModal';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import type { BacktestResult, OhlcvBar } from '../api/types';

export function Home() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [highlightedTrade, setHighlightedTrade] = useState<number | null>(null);
  const [activeTab, setActiveTab] = useState<'equity' | 'price' | 'trades' | 'stats'>('equity');
  const [showEAModal, setShowEAModal] = useState(false);

  // Save state
  const [saveName, setSaveName] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [savedId, setSavedId] = useState<string | null>(null);

  function handleLogout() {
    logout();
    navigate('/login');
  }

  function handleNewResult(r: BacktestResult) {
    setResult(r);
    setHighlightedTrade(null);
    setActiveTab('equity');
    setSaveName('');
    setSaveError('');
    setSavedId(null);
  }

  async function handleSave() {
    if (!result || !saveName.trim()) return;
    setSaving(true);
    setSaveError('');
    try {
      await api.saveResult(result.id, saveName.trim());
      setSavedId(result.id);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Failed to save';
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  // Fetch OHLCV only when we have a result
  const { data: bars = [] } = useQuery<OhlcvBar[]>({
    queryKey: ['ohlcv', result?.parameters?.timeframe, result?.parameters?.years, result?.parameters?.symbol],
    queryFn: () => {
      if (!result) return Promise.resolve([]);
      const tf = result.parameters.timeframe as string;
      const years = result.parameters.years as number[];
      const symbol = (result.parameters.symbol as string) ?? 'XAUUSD';
      // Filter to date range of trades
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
          <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center text-white font-bold text-sm">
            FX
          </div>
          <span className="font-semibold text-lg text-slate-100">Strategy Backtest</span>
        </div>
        <div className="flex items-center gap-4">
          <a href="/results" className="text-sm text-slate-400 hover:text-slate-200 transition-colors">
            Saved Results →
          </a>
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

      <div className="flex flex-col lg:flex-row min-h-[calc(100vh-65px)]">
        {/* Left panel — form */}
        <aside className="w-full lg:w-80 xl:w-96 border-b lg:border-b-0 lg:border-r border-slate-800 p-6 flex-shrink-0">
          <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wide mb-6">
            Configure Backtest
          </h2>
          <BacktestForm onResult={handleNewResult} />
        </aside>

        {/* Right panel — results */}
        <main className="flex-1 p-6 overflow-auto">
          {!result ? (
            <div className="h-full flex flex-col items-center justify-center text-center text-slate-600 gap-4">
              <div className="text-6xl">📊</div>
              <div>
                <p className="text-lg font-medium text-slate-500">No results yet</p>
                <p className="text-sm mt-1">Configure a backtest on the left and click Run.</p>
              </div>
            </div>
          ) : (
            <div className="space-y-6 max-w-5xl">
              {/* Title */}
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h2 className="text-xl font-semibold text-slate-100">
                    {result.strategy.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())}
                    <span className="ml-2 text-slate-500 font-normal text-base">
                      {(result.parameters.symbol as string) ?? 'XAUUSD'} ·{' '}
                      {(result.parameters.timeframe as string)} ·{' '}
                      {(result.parameters.years as number[]).join(', ')}
                    </span>
                  </h2>
                  <p className="text-xs text-slate-500 mt-1 truncate max-w-xl">{result.id}</p>
                </div>
                <button
                  onClick={() => setShowEAModal(true)}
                  className="flex-shrink-0 flex items-center gap-1.5 px-3 py-1.5 bg-slate-800 hover:bg-slate-700 border border-slate-700 hover:border-slate-600 text-slate-300 text-xs font-medium rounded-lg transition-colors"
                  title="Generate MetaTrader Expert Advisor code for this strategy"
                >
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
                  </svg>
                  Generate EA
                </button>
              </div>

              {/* Save banner */}
              {savedId === result.id ? (
                <div className="flex items-center gap-2 bg-emerald-900/30 border border-emerald-700/50 rounded-xl px-4 py-3">
                  <svg className="w-4 h-4 text-emerald-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                  </svg>
                  <span className="text-sm text-emerald-300">
                    Saved as <span className="font-semibold">"{saveName}"</span>
                  </span>
                  <a href="/results" className="ml-auto text-xs text-emerald-400 hover:text-emerald-300 transition-colors">
                    View in Saved Results →
                  </a>
                </div>
              ) : (
                <div className="flex items-center gap-3 bg-amber-950/30 border border-amber-700/40 rounded-xl px-4 py-3">
                  <svg className="w-4 h-4 text-amber-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M12 9v2m0 4h.01M12 3a9 9 0 100 18A9 9 0 0012 3z" />
                  </svg>
                  <span className="text-xs text-amber-400/80 hidden sm:block">Unsaved</span>
                  <input
                    type="text"
                    value={saveName}
                    onChange={(e) => setSaveName(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && handleSave()}
                    placeholder="Enter a name to save this result…"
                    className="flex-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-amber-500 transition-colors"
                  />
                  {saveError && (
                    <span className="text-xs text-red-400 flex-shrink-0">{saveError}</span>
                  )}
                  <button
                    onClick={handleSave}
                    disabled={saving || !saveName.trim()}
                    className="flex-shrink-0 px-4 py-1.5 bg-amber-500 hover:bg-amber-400 disabled:opacity-40 disabled:cursor-not-allowed text-slate-950 font-semibold text-xs rounded-lg transition-colors"
                  >
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                </div>
              )}

              {/* Metrics */}
              <ResultCard results={result.results} stoppedOut={result.results.stopped_out} />

              {/* Per-month breakdown */}
              {result.results.per_year && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-400 uppercase tracking-wide mb-3">
                    Monthly Performance
                  </h3>
                  <PerMonthTable
                    trades={result.results.trades}
                    initialCapital={result.results.initial_capital}
                  />
                </div>
              )}

              {/* Chart tabs */}
              <div>
                <div className="flex gap-1 mb-4 bg-slate-900 rounded-lg p-1 w-fit border border-slate-800">
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
                      {tab === 'equity' ? 'Equity Curve' : tab === 'price' ? 'Price Chart' : tab === 'trades' ? 'Trades' : 'Statistics'}
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
                        highlightedTrade={highlightedTrade !== null ? result.results.trades[highlightedTrade]?.trade : null}
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
                    <h3 className="text-sm font-semibold text-slate-400 mb-4">
                      Trade Statistics
                    </h3>
                    <TradeStatsTable trades={result.results.trades} pipMult={result.results.pip_mult} />
                  </div>
                )}
              </div>
            </div>
          )}
        </main>
      </div>

      {showEAModal && result && (
        <EAModal result={result} onClose={() => setShowEAModal(false)} />
      )}
    </div>
  );
}
