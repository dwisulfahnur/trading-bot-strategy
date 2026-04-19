import { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ComparePanel } from '../components/ComparePanel';
import { api } from '../api/client';
import type { ResultSummary } from '../api/types';

type SortKey = 'created_at' | 'total_return_pct' | 'win_rate_pct' | 'max_drawdown_pct' | 'profit_factor' | 'total_trades';

const TIMEFRAMES = ['M1', 'M5', 'M15', 'H1', 'H4'];

const COLUMN_TIPS: Partial<Record<string, string>> = {
  TF: 'Timeframe — the candle interval used for this backtest (M1 = 1 min, M5 = 5 min, H1 = 1 hour, H4 = 4 hours)',
  PF: 'Profit Factor — Gross Profit ÷ Gross Loss.\n> 1.0 profitable  |  > 1.5 good  |  > 2.0 excellent',
};

export function Results() {
  const queryClient = useQueryClient();
  const [sortKey, setSortKey] = useState<SortKey>('created_at');
  const [sortAsc, setSortAsc] = useState(false);
  const [compareIds, setCompareIds] = useState<Set<string>>(new Set());
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Filters
  const [filterTf, setFilterTf] = useState<string[]>([]);
  const [filterYears, setFilterYears] = useState<number[]>([]);
  const [filterStrategies, setFilterStrategies] = useState<string[]>([]);
  const [ddMin, setDdMin] = useState('');
  const [ddMax, setDdMax] = useState('');
  const [wrMin, setWrMin] = useState('');
  const [wrMax, setWrMax] = useState('');

  const { data: results = [], isLoading } = useQuery({
    queryKey: ['results'],
    queryFn: api.listResults,
  });

  const deleteMutation = useMutation({
    mutationFn: api.deleteResult,
    onSuccess: (_, id) => {
      queryClient.invalidateQueries({ queryKey: ['results'] });
      setCompareIds((prev) => { const s = new Set(prev); s.delete(id); return s; });
      if (selectedId === id) setSelectedId(null);
    },
  });

  const deleteManyMutation = useMutation({
    mutationFn: api.deleteResults,
    onSuccess: (_, ids) => {
      queryClient.invalidateQueries({ queryKey: ['results'] });
      setCompareIds((prev) => { const s = new Set(prev); ids.forEach((id) => s.delete(id)); return s; });
      if (selectedId && ids.includes(selectedId)) setSelectedId(null);
    },
  });

  const handleSort = (key: SortKey) => {
    if (sortKey === key) setSortAsc((a) => !a);
    else { setSortKey(key); setSortAsc(key === 'max_drawdown_pct'); }
  };

  // Derive available years and strategies from loaded results
  const availableYears = [...new Set(results.flatMap((r) => r.years))].sort();
  const availableStrategies = [...new Set(results.map((r) => r.strategy))].sort();

  // Apply filters then sort
  const filtered = results.filter((r) => {
    if (filterTf.length > 0 && !filterTf.includes(r.timeframe)) return false;
    if (filterYears.length > 0 && !filterYears.some((y) => r.years.includes(y))) return false;
    if (filterStrategies.length > 0 && !filterStrategies.includes(r.strategy)) return false;
    if (ddMin !== '' && r.max_drawdown_pct < parseFloat(ddMin)) return false;
    if (ddMax !== '' && r.max_drawdown_pct > parseFloat(ddMax)) return false;
    if (wrMin !== '' && r.win_rate_pct < parseFloat(wrMin)) return false;
    if (wrMax !== '' && r.win_rate_pct > parseFloat(wrMax)) return false;
    return true;
  });

  const sorted = [...filtered].sort((a, b) => {
    const av = a[sortKey] as string | number;
    const bv = b[sortKey] as string | number;
    if (typeof av === 'number' && typeof bv === 'number') return sortAsc ? av - bv : bv - av;
    return sortAsc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
  });

  const toggleCompare = (id: string) => {
    setCompareIds((prev) => {
      const s = new Set(prev);
      if (s.has(id)) s.delete(id); else s.add(id);
      return s;
    });
  };

  const allSelected = sorted.length > 0 && sorted.every((r) => compareIds.has(r.id));
  const someSelected = !allSelected && sorted.some((r) => compareIds.has(r.id));

  const toggleSelectAll = () => {
    if (allSelected) {
      setCompareIds(new Set());
    } else {
      setCompareIds(new Set(sorted.map((r) => r.id)));
    }
  };

  const selectAllRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someSelected;
    }
  }, [someSelected]);

  const compareResults = results.filter((r) => compareIds.has(r.id));

  const hasActiveFilters =
    filterTf.length > 0 || filterYears.length > 0 || filterStrategies.length > 0 ||
    ddMin !== '' || ddMax !== '' || wrMin !== '' || wrMax !== '';

  const SortIcon = ({ k }: { k: SortKey }) =>
    sortKey === k ? (
      <span className="ml-1 text-blue-400">{sortAsc ? '↑' : '↓'}</span>
    ) : (
      <span className="ml-1 text-slate-600">↕</span>
    );

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      {/* Header */}
      <header className="border-b border-slate-800 px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <a href="/" className="flex items-center gap-3 hover:opacity-80">
            <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center text-white font-bold text-sm">
              FX
            </div>
            <span className="font-semibold text-lg text-slate-100">Strategy Backtest</span>
          </a>
          <span className="text-slate-600">/</span>
          <span className="text-slate-400">Saved Results</span>
        </div>
        <a href="/" className="text-sm text-blue-400 hover:text-blue-300 transition-colors">
          ← New Backtest
        </a>
      </header>

      <div className="max-w-7xl mx-auto px-6 py-8 space-y-6">
        {/* Compare panel */}
        {compareResults.length >= 2 && (
          <ComparePanel
            results={compareResults}
            onRemove={(id) => toggleCompare(id)}
          />
        )}

        {/* Filters */}
        {results.length > 0 && (
          <div className="bg-slate-900 border border-slate-800 rounded-2xl px-6 py-4 space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-300">Filters</span>
              {hasActiveFilters && (
                <button
                  onClick={() => { setFilterTf([]); setFilterYears([]); setFilterStrategies([]); setDdMin(''); setDdMax(''); setWrMin(''); setWrMax(''); }}
                  className="text-xs text-slate-500 hover:text-slate-300 transition-colors"
                >
                  Clear all
                </button>
              )}
            </div>

            <div className="flex flex-wrap gap-6">
              {/* Timeframe */}
              <div className="space-y-1.5">
                <span className="text-xs text-slate-500 uppercase tracking-wide">Timeframe</span>
                <div className="flex gap-1.5">
                  {TIMEFRAMES.map((tf) => (
                    <button
                      key={tf}
                      onClick={() => setFilterTf((prev) =>
                        prev.includes(tf) ? prev.filter((x) => x !== tf) : [...prev, tf]
                      )}
                      className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                        filterTf.includes(tf)
                          ? 'bg-blue-600 text-white'
                          : 'bg-slate-800 text-slate-400 hover:bg-slate-700'
                      }`}
                    >
                      {tf}
                    </button>
                  ))}
                </div>
              </div>

              {/* Year */}
              {availableYears.length > 0 && (
                <div className="space-y-1.5">
                  <span className="text-xs text-slate-500 uppercase tracking-wide">Year</span>
                  <div className="flex gap-1.5 flex-wrap">
                    {availableYears.map((yr) => (
                      <button
                        key={yr}
                        onClick={() => setFilterYears((prev) =>
                          prev.includes(yr) ? prev.filter((x) => x !== yr) : [...prev, yr]
                        )}
                        className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                          filterYears.includes(yr)
                            ? 'bg-blue-600 text-white'
                            : 'bg-slate-800 text-slate-400 hover:bg-slate-700'
                        }`}
                      >
                        {yr}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* Strategy */}
              {availableStrategies.length > 1 && (
                <div className="space-y-1.5">
                  <span className="text-xs text-slate-500 uppercase tracking-wide">Strategy</span>
                  <div className="flex gap-1.5 flex-wrap">
                    {availableStrategies.map((s) => (
                      <button
                        key={s}
                        onClick={() => setFilterStrategies((prev) =>
                          prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]
                        )}
                        className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                          filterStrategies.includes(s)
                            ? 'bg-blue-600 text-white'
                            : 'bg-slate-800 text-slate-400 hover:bg-slate-700'
                        }`}
                      >
                        {s.replace(/_/g, ' ')}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* Drawdown range */}
              <div className="space-y-1.5">
                <span className="text-xs text-slate-500 uppercase tracking-wide">Max Drawdown (%)</span>
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    placeholder="Min"
                    value={ddMin}
                    min={0}
                    max={100}
                    step={0.1}
                    onChange={(e) => setDdMin(e.target.value)}
                    className="w-20 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200 focus:outline-none focus:border-blue-500"
                  />
                  <span className="text-slate-600 text-xs">—</span>
                  <input
                    type="number"
                    placeholder="Max"
                    value={ddMax}
                    min={0}
                    max={100}
                    step={0.1}
                    onChange={(e) => setDdMax(e.target.value)}
                    className="w-20 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200 focus:outline-none focus:border-blue-500"
                  />
                </div>
              </div>

              {/* Win rate range */}
              <div className="space-y-1.5">
                <span className="text-xs text-slate-500 uppercase tracking-wide">Win Rate (%)</span>
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    placeholder="Min"
                    value={wrMin}
                    min={0}
                    max={100}
                    step={0.1}
                    onChange={(e) => setWrMin(e.target.value)}
                    className="w-20 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200 focus:outline-none focus:border-blue-500"
                  />
                  <span className="text-slate-600 text-xs">—</span>
                  <input
                    type="number"
                    placeholder="Max"
                    value={wrMax}
                    min={0}
                    max={100}
                    step={0.1}
                    onChange={(e) => setWrMax(e.target.value)}
                    className="w-20 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200 focus:outline-none focus:border-blue-500"
                  />
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Results table */}
        <div className="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden">
          <div className="px-6 py-4 border-b border-slate-800 flex items-center justify-between">
            <h2 className="font-semibold text-slate-100">
              All Results
              <span className="ml-2 text-slate-500 font-normal text-sm">
                {hasActiveFilters ? `${sorted.length} of ${results.length}` : `(${results.length})`}
              </span>
            </h2>
            <div className="flex items-center gap-4">
              {compareIds.size > 0 && (
                <span className="text-xs text-slate-400">
                  {compareIds.size} selected for compare {compareIds.size < 2 && '(select ≥2)'}
                </span>
              )}
              {results.length > 0 && (
                <button
                  onClick={() => {
                    if (confirm(`Delete all ${results.length} results? This cannot be undone.`))
                      deleteManyMutation.mutate(results.map((r) => r.id));
                  }}
                  disabled={deleteManyMutation.isPending}
                  className="text-xs text-slate-500 hover:text-red-400 transition-colors px-2 py-1 rounded hover:bg-red-900/30 disabled:opacity-40"
                >
                  Delete All
                </button>
              )}
            </div>
          </div>

          {isLoading ? (
            <div className="flex items-center justify-center py-16 text-slate-500">Loading…</div>
          ) : results.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-slate-600 gap-3">
              <div className="text-4xl">📭</div>
              <p>No saved results yet. Run a backtest first.</p>
              <a href="/" className="text-blue-400 hover:text-blue-300 text-sm">← Run a backtest</a>
            </div>
          ) : sorted.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-slate-600 gap-2">
              <p className="text-sm">No results match the current filters.</p>
              <button
                onClick={() => { setFilterTf([]); setFilterYears([]); setFilterStrategies([]); setDdMin(''); setDdMax(''); setWrMin(''); setWrMax(''); }}
                className="text-blue-400 hover:text-blue-300 text-xs"
              >
                Clear filters
              </button>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm text-left">
                <thead className="bg-slate-800/50 text-slate-400 uppercase text-xs tracking-wide">
                  <tr>
                    <th className="px-4 py-3">
                      <input
                        ref={selectAllRef}
                        type="checkbox"
                        checked={allSelected}
                        onChange={toggleSelectAll}
                        className="w-4 h-4 accent-blue-500"
                        title={allSelected ? 'Deselect all' : 'Select all'}
                      />
                    </th>
                    <th className="px-4 py-3">Strategy</th>
                    <ColHeader label="TF" tip={COLUMN_TIPS['TF']} />
                    <th className="px-4 py-3">Years</th>
                    {(
                      [
                        ['total_return_pct', 'Return', undefined],
                        ['win_rate_pct', 'Win Rate', undefined],
                        ['profit_factor', 'PF', COLUMN_TIPS['PF']],
                        ['max_drawdown_pct', 'Max DD', undefined],
                        ['total_trades', 'Trades', undefined],
                        ['created_at', 'Date', undefined],
                      ] as [SortKey, string, string | undefined][]
                    ).map(([key, label, tip]) => (
                      <th
                        key={key}
                        onClick={() => handleSort(key)}
                        className="px-4 py-3 cursor-pointer hover:text-slate-200 whitespace-nowrap"
                      >
                        <span className="inline-flex items-center gap-1">
                          {tip ? (
                            <Tooltip text={tip}>
                              <span className="border-b border-dashed border-slate-600 cursor-help">{label}</span>
                            </Tooltip>
                          ) : label}
                          <SortIcon k={key} />
                        </span>
                      </th>
                    ))}
                    <th className="px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((r: ResultSummary) => {
                    const isSelected = selectedId === r.id;
                    const inCompare = compareIds.has(r.id);
                    return (
                      <tr
                        key={r.id}
                        onClick={() => setSelectedId(isSelected ? null : r.id)}
                        className={`border-t border-slate-800 cursor-pointer transition-colors ${
                          isSelected ? 'bg-blue-900/20' : 'hover:bg-slate-800/40'
                        }`}
                      >
                        <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={inCompare}
                            onChange={() => toggleCompare(r.id)}
                            className="w-4 h-4 accent-blue-500"
                          />
                        </td>
                        <td className="px-4 py-3 text-slate-200 font-medium">
                          {r.strategy.replace(/_/g, ' ')}
                        </td>
                        <td className="px-4 py-3 text-slate-400">{r.timeframe}</td>
                        <td className="px-4 py-3 text-slate-400">{r.years.join(', ')}</td>
                        <td className={`px-4 py-3 font-semibold ${r.total_return_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {r.total_return_pct >= 0 ? '+' : ''}{r.total_return_pct.toFixed(2)}%
                        </td>
                        <td className="px-4 py-3 text-slate-300">{r.win_rate_pct.toFixed(1)}%</td>
                        <td className={`px-4 py-3 font-semibold ${r.profit_factor >= 1 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {r.profit_factor.toFixed(3)}
                        </td>
                        <td className="px-4 py-3 text-red-400">-{r.max_drawdown_pct.toFixed(2)}%</td>
                        <td className="px-4 py-3 text-slate-400">{r.total_trades}</td>
                        <td className="px-4 py-3 text-slate-500 text-xs whitespace-nowrap">
                          {new Date(r.created_at).toLocaleDateString()}
                        </td>
                        <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                          <button
                            onClick={() => {
                              if (confirm(`Delete "${r.id}"?`)) deleteMutation.mutate(r.id);
                            }}
                            className="text-slate-600 hover:text-red-400 transition-colors text-xs px-2 py-1 rounded hover:bg-red-900/30"
                          >
                            Delete
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Expanded detail panel */}
        {selectedId && (() => {
          const r = results.find((x) => x.id === selectedId);
          if (!r) return null;
          return (
            <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-3">
              <h3 className="font-semibold text-slate-200 text-sm">{r.id}</h3>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
                <Stat label="Return" value={`${r.total_return_pct >= 0 ? '+' : ''}${r.total_return_pct.toFixed(2)}%`} />
                <Stat label="Win Rate" value={`${r.win_rate_pct.toFixed(1)}%`} />
                <Stat label="Profit Factor" value={r.profit_factor.toFixed(3)} />
                <Stat label="Max Drawdown" value={`-${r.max_drawdown_pct.toFixed(2)}%`} />
              </div>
              <a
                href="/"
                onClick={() => sessionStorage.setItem('loadResult', selectedId)}
                className="inline-block text-xs text-blue-400 hover:text-blue-300"
              >
                Open in backtest view →
              </a>
            </div>
          );
        })()}
      </div>
    </div>
  );
}

function Tooltip({ text, children }: { text: string; children: React.ReactNode }) {
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);

  return (
    <span
      onMouseEnter={(e) => setPos({ x: e.clientX, y: e.clientY })}
      onMouseMove={(e) => setPos({ x: e.clientX, y: e.clientY })}
      onMouseLeave={() => setPos(null)}
    >
      {children}
      {pos && createPortal(
        <div
          className="pointer-events-none fixed z-50 w-56 rounded-lg bg-slate-700 border border-slate-600 px-3 py-2 text-xs text-slate-200 shadow-xl whitespace-pre-line normal-case tracking-normal font-normal"
          style={{ left: pos.x, top: pos.y - 8, transform: 'translate(-50%, -100%)' }}
        >
          {text}
        </div>,
        document.body
      )}
    </span>
  );
}

function ColHeader({ label, tip }: { label: string; tip?: string }) {
  return (
    <th className="px-4 py-3 whitespace-nowrap">
      {tip ? (
        <Tooltip text={tip}>
          <span className="border-b border-dashed border-slate-600 cursor-help">{label}</span>
        </Tooltip>
      ) : label}
    </th>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-slate-800 rounded-lg p-3">
      <div className="text-xs text-slate-500 mb-1">{label}</div>
      <div className="text-slate-100 font-semibold">{value}</div>
    </div>
  );
}
