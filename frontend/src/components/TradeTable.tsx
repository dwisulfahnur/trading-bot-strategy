import { useState, useEffect, useRef } from 'react';
import type { TradeRecord } from '../api/types';

interface Props {
  trades: TradeRecord[];
  highlightedTrade: number | null;
  onTradeClick: (index: number) => void;
}

const PAGE_SIZE = 20;

const EXIT_COLORS: Record<string, string> = {
  tp: 'bg-emerald-900 text-emerald-300',
  sl: 'bg-red-900 text-red-300',
  be: 'bg-amber-900 text-amber-300',
  end_of_data: 'bg-slate-700 text-slate-400',
};

type SortKey = keyof TradeRecord;
type FilterDirection = 'all' | 'long' | 'short';
type FilterExit = 'all' | 'tp' | 'sl' | 'be' | 'end_of_data';

export function TradeTable({ trades, highlightedTrade, onTradeClick }: Props) {
  const [page, setPage] = useState(1);
  const [sortKey, setSortKey] = useState<SortKey>('trade');
  const [sortAsc, setSortAsc] = useState(true);
  const [filterDir, setFilterDir] = useState<FilterDirection>('all');
  const [filterExit, setFilterExit] = useState<FilterExit>('all');
  const rowRefs = useRef<Map<number, HTMLTableRowElement>>(new Map());

  // Scroll to highlighted row
  useEffect(() => {
    if (highlightedTrade === null) return;
    const el = rowRefs.current.get(highlightedTrade);
    if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [highlightedTrade]);

  // Reset page when filters change
  useEffect(() => {
    setPage(1);
  }, [filterDir, filterExit, sortKey, sortAsc]);

  const filtered = trades
    .filter((t) => filterDir === 'all' || t.direction === filterDir)
    .filter((t) => filterExit === 'all' || t.exit_reason === filterExit);

  const sorted = [...filtered].sort((a, b) => {
    const av = a[sortKey];
    const bv = b[sortKey];
    if (typeof av === 'number' && typeof bv === 'number') return sortAsc ? av - bv : bv - av;
    return sortAsc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
  });

  const totalPages = Math.ceil(sorted.length / PAGE_SIZE);
  const paged = sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) setSortAsc((a) => !a);
    else { setSortKey(key); setSortAsc(true); }
  };

  const SortIcon = ({ k }: { k: SortKey }) =>
    sortKey === k ? (
      <span className="ml-1 text-blue-400">{sortAsc ? '↑' : '↓'}</span>
    ) : (
      <span className="ml-1 text-slate-600">↕</span>
    );

  return (
    <div className="flex flex-col gap-3">
      {/* Filters */}
      <div className="flex flex-wrap gap-2 text-xs">
        <span className="text-slate-500 self-center">Direction:</span>
        {(['all', 'long', 'short'] as FilterDirection[]).map((d) => (
          <button
            key={d}
            onClick={() => setFilterDir(d)}
            className={`px-2 py-1 rounded ${
              filterDir === d ? 'bg-blue-700 text-white' : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
            }`}
          >
            {d}
          </button>
        ))}
        <span className="text-slate-500 self-center ml-2">Exit:</span>
        {(['all', 'tp', 'sl', 'be', 'end_of_data'] as FilterExit[]).map((e) => (
          <button
            key={e}
            onClick={() => setFilterExit(e)}
            className={`px-2 py-1 rounded ${
              filterExit === e ? 'bg-blue-700 text-white' : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
            }`}
          >
            {e}
          </button>
        ))}
        <span className="ml-auto text-slate-500 self-center">{filtered.length} trades</span>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-xl border border-slate-700">
        <table className="w-full text-xs text-left">
          <thead className="bg-slate-800 text-slate-400 uppercase tracking-wide">
            <tr>
              {(
                [
                  ['trade', '#'],
                  ['direction', 'Dir'],
                  ['entry_time', 'Entry Time'],
                  ['entry_price', 'Entry'],
                  ['sl', 'SL'],
                  ['tp', 'TP'],
                  ['exit_time', 'Exit Time'],
                  ['exit_price', 'Exit'],
                  ['exit_reason', 'Reason'],
                  ['lot_size', 'Lot'],
                  ['profit_usd', 'Profit (USD)'],
                  ['capital_after', 'Capital'],
                ] as [SortKey, string][]
              ).map(([key, label]) => (
                <th
                  key={key}
                  onClick={() => handleSort(key)}
                  className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap"
                >
                  {label}<SortIcon k={key} />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {paged.map((t) => {
              const globalIdx = trades.indexOf(t);
              const isHighlighted = globalIdx === highlightedTrade;
              return (
                <tr
                  key={t.trade}
                  ref={(el) => { if (el) rowRefs.current.set(globalIdx, el); }}
                  onClick={() => onTradeClick(globalIdx)}
                  className={`cursor-pointer border-t border-slate-800 transition-colors ${
                    isHighlighted
                      ? 'bg-blue-900/40'
                      : 'hover:bg-slate-800/60'
                  }`}
                >
                  <td className="px-3 py-2 text-slate-500">{t.trade}</td>
                  <td className="px-3 py-2">
                    <span
                      className={`px-1.5 py-0.5 rounded font-medium ${
                        t.direction === 'long'
                          ? 'bg-emerald-900 text-emerald-300'
                          : 'bg-red-900 text-red-300'
                      }`}
                    >
                      {t.direction.toUpperCase()}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-slate-400 whitespace-nowrap">{t.entry_time.slice(0, 16)}</td>
                  <td className="px-3 py-2 text-slate-100">{t.entry_price.toFixed(2)}</td>
                  <td className="px-3 py-2 text-red-400">{t.sl.toFixed(2)}</td>
                  <td className="px-3 py-2 text-emerald-400">{t.tp.toFixed(2)}</td>
                  <td className="px-3 py-2 text-slate-400 whitespace-nowrap">{t.exit_time.slice(0, 16)}</td>
                  <td className="px-3 py-2 text-slate-100">{t.exit_price.toFixed(2)}</td>
                  <td className="px-3 py-2">
                    <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${EXIT_COLORS[t.exit_reason]}`}>
                      {t.exit_reason.toUpperCase()}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-slate-300">{t.lot_size.toFixed(2)}</td>
                  <td className={`px-3 py-2 font-semibold ${t.profit_usd >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {t.profit_usd >= 0 ? '+' : ''}${t.profit_usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </td>
                  <td className="px-3 py-2 text-slate-300">${t.capital_after.toLocaleString()}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex justify-center gap-2 text-sm">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
            className="px-3 py-1 rounded bg-slate-700 text-slate-300 hover:bg-slate-600 disabled:opacity-40"
          >
            ‹ Prev
          </button>
          <span className="self-center text-slate-400">
            {page} / {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page === totalPages}
            className="px-3 py-1 rounded bg-slate-700 text-slate-300 hover:bg-slate-600 disabled:opacity-40"
          >
            Next ›
          </button>
        </div>
      )}
    </div>
  );
}
