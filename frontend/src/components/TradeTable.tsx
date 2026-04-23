import { useState, useEffect, useRef } from 'react';
import type { TradeRecord } from '../api/types';

interface Props {
  trades: TradeRecord[];
  highlightedTrade: number | null;
  onTradeClick: (index: number) => void;
  pipMult?: number;
}

const PAGE_SIZE = 20;

const EXIT_COLORS: Record<string, string> = {
  tp: 'bg-emerald-900 text-emerald-300',
  sl: 'bg-red-900 text-red-300',
  be: 'bg-amber-900 text-amber-300',
  end_of_data: 'bg-slate-700 text-slate-400',
};

type SortKey = keyof TradeRecord | 'profit_pips' | 'hold_period';
type FilterDirection = 'all' | 'long' | 'short';
type FilterExit = 'all' | 'tp' | 'sl' | 'be' | 'end_of_data';

// ---------------------------------------------------------------------------
// Column definitions
// ---------------------------------------------------------------------------

interface ColDef {
  key: string;
  label: string;
  defaultVisible: boolean;
}

const COLUMNS: ColDef[] = [
  { key: 'trade',        label: '#',             defaultVisible: true  },
  { key: 'direction',    label: 'Dir',           defaultVisible: true  },
  { key: 'entry_time',   label: 'Entry Time',    defaultVisible: true  },
  { key: 'entry_price',  label: 'Entry',         defaultVisible: true  },
  { key: 'sl',           label: 'SL',            defaultVisible: true  },
  { key: 'tp',           label: 'TP',            defaultVisible: true  },
  { key: 'exit_time',    label: 'Exit Time',     defaultVisible: true  },
  { key: 'hold_period',  label: 'Hold Period',   defaultVisible: true  },
  { key: 'exit_price',   label: 'Exit',          defaultVisible: true  },
  { key: 'exit_reason',  label: 'Reason',        defaultVisible: true  },
  { key: 'lot_size',     label: 'Lot',           defaultVisible: true  },
  { key: 'profit_pips',  label: 'Pips',          defaultVisible: true  },
  { key: 'profit_usd',   label: 'Profit (USD)',  defaultVisible: true  },
  { key: 'capital_after',label: 'Capital',       defaultVisible: true  },
];

function tradePips(t: TradeRecord, pipMult: number): number {
  const diff = t.exit_price - t.entry_price;
  return (t.direction === 'long' ? diff : -diff) * pipMult;
}

export function TradeTable({ trades, highlightedTrade, onTradeClick, pipMult = 10.0 }: Props) {
  const [page, setPage] = useState(1);
  const [sortKey, setSortKey] = useState<SortKey>('trade');
  const [sortAsc, setSortAsc] = useState(true);
  const [filterDir, setFilterDir] = useState<FilterDirection>('all');
  const [filterExit, setFilterExit] = useState<FilterExit>('all');
  const [visibleCols, setVisibleCols] = useState<Set<string>>(
    () => new Set(COLUMNS.filter((c) => c.defaultVisible).map((c) => c.key))
  );
  const [colMenuOpen, setColMenuOpen] = useState(false);
  const colMenuRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef<Map<number, HTMLTableRowElement>>(new Map());

  // Close column menu on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (colMenuRef.current && !colMenuRef.current.contains(e.target as Node)) {
        setColMenuOpen(false);
      }
    };
    if (colMenuOpen) document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [colMenuOpen]);

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

  const toggleCol = (key: string) => {
    setVisibleCols((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const filtered = trades
    .filter((t) => filterDir === 'all' || t.direction === filterDir)
    .filter((t) => filterExit === 'all' || t.exit_reason === filterExit);

  const sorted = [...filtered].sort((a, b) => {
    let av: number | string;
    let bv: number | string;
    if (sortKey === 'profit_pips') {
      av = tradePips(a, pipMult);
      bv = tradePips(b, pipMult);
    } else if (sortKey === 'hold_period') {
      av = a.hold_period;
      bv = b.hold_period;
    } else {
      av = a[sortKey as keyof TradeRecord] as number | string;
      bv = b[sortKey as keyof TradeRecord] as number | string;
    }
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

  const show = (key: string) => visibleCols.has(key);

  return (
    <div className="flex flex-col gap-3">
      {/* Filters + column toggle */}
      <div className="flex flex-wrap gap-2 text-xs items-center">
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
        <span className="text-slate-500 self-center">{filtered.length} trades</span>

        {/* Column visibility toggle */}
        <div className="relative ml-auto" ref={colMenuRef}>
          <button
            onClick={() => setColMenuOpen((o) => !o)}
            title="Toggle columns"
            className={`px-2 py-1 rounded flex items-center gap-1 transition-colors ${
              colMenuOpen ? 'bg-slate-600 text-slate-200' : 'bg-slate-700 text-slate-400 hover:bg-slate-600 hover:text-slate-200'
            }`}
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4" />
            </svg>
            Columns
          </button>
          {colMenuOpen && (
            <div className="absolute right-0 top-full mt-1 z-50 bg-slate-800 border border-slate-700 rounded-lg shadow-xl p-2 min-w-[160px]">
              {COLUMNS.map((col) => (
                <label
                  key={col.key}
                  className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-slate-700 cursor-pointer text-slate-300 text-xs"
                >
                  <input
                    type="checkbox"
                    checked={visibleCols.has(col.key)}
                    onChange={() => toggleCol(col.key)}
                    className="accent-blue-500"
                  />
                  {col.label}
                </label>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-xl border border-slate-700">
        <table className="w-full text-xs text-left">
          <thead className="bg-slate-800 text-slate-400 uppercase tracking-wide">
            <tr>
              {show('trade') && (
                <th onClick={() => handleSort('trade')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  #<SortIcon k="trade" />
                </th>
              )}
              {show('direction') && (
                <th onClick={() => handleSort('direction')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  Dir<SortIcon k="direction" />
                </th>
              )}
              {show('entry_time') && (
                <th onClick={() => handleSort('entry_time')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  Entry Time<SortIcon k="entry_time" />
                </th>
              )}
              {show('entry_price') && (
                <th onClick={() => handleSort('entry_price')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  Entry<SortIcon k="entry_price" />
                </th>
              )}
              {show('sl') && (
                <th onClick={() => handleSort('sl')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  SL<SortIcon k="sl" />
                </th>
              )}
              {show('tp') && (
                <th onClick={() => handleSort('tp')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  TP<SortIcon k="tp" />
                </th>
              )}
              {show('exit_time') && (
                <th onClick={() => handleSort('exit_time')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  Exit Time<SortIcon k="exit_time" />
                </th>
              )}
              {show('hold_period') && (
                <th onClick={() => handleSort('hold_period')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  Hold Period<SortIcon k="hold_period" />
                </th>
              )}
              {show('exit_price') && (
                <th onClick={() => handleSort('exit_price')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  Exit<SortIcon k="exit_price" />
                </th>
              )}
              {show('exit_reason') && (
                <th onClick={() => handleSort('exit_reason')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  Reason<SortIcon k="exit_reason" />
                </th>
              )}
              {show('lot_size') && (
                <th onClick={() => handleSort('lot_size')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  Lot<SortIcon k="lot_size" />
                </th>
              )}
              {show('pnl_r') && (
                <th onClick={() => handleSort('pnl_r')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  R<SortIcon k="pnl_r" />
                </th>
              )}
              {show('profit_pips') && (
                <th onClick={() => handleSort('profit_pips')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  Pips<SortIcon k="profit_pips" />
                </th>
              )}
              {show('profit_usd') && (
                <th onClick={() => handleSort('profit_usd')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  Profit (USD)<SortIcon k="profit_usd" />
                </th>
              )}
              {show('capital_after') && (
                <th onClick={() => handleSort('capital_after')} className="px-3 py-2 cursor-pointer select-none hover:text-slate-200 whitespace-nowrap">
                  Capital<SortIcon k="capital_after" />
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {paged.map((t) => {
              const globalIdx = trades.indexOf(t);
              const isHighlighted = globalIdx === highlightedTrade;
              const pips = tradePips(t, pipMult);
              return (
                <tr
                  key={t.trade}
                  ref={(el) => { if (el) rowRefs.current.set(globalIdx, el); }}
                  onClick={() => onTradeClick(globalIdx)}
                  className={`cursor-pointer border-t border-slate-800 transition-colors ${
                    isHighlighted ? 'bg-blue-900/40' : 'hover:bg-slate-800/60'
                  }`}
                >
                  {show('trade') && <td className="px-3 py-2 text-slate-500">{t.trade}</td>}
                  {show('direction') && (
                    <td className="px-3 py-2">
                      <span className={`px-1.5 py-0.5 rounded font-medium ${t.direction === 'long' ? 'bg-emerald-900 text-emerald-300' : 'bg-red-900 text-red-300'}`}>
                        {t.direction.toUpperCase()}
                      </span>
                    </td>
                  )}
                  {show('entry_time') && <td className="px-3 py-2 text-slate-400 whitespace-nowrap">{t.entry_time.slice(0, 16)}</td>}
                  {show('entry_price') && <td className="px-3 py-2 text-slate-100">{t.entry_price.toFixed(2)}</td>}
                  {show('sl') && <td className="px-3 py-2 text-red-400">{t.sl.toFixed(2)}</td>}
                  {show('tp') && <td className="px-3 py-2 text-emerald-400">{t.tp.toFixed(2)}</td>}
                  {show('exit_time') && <td className="px-3 py-2 text-slate-400 whitespace-nowrap">{t.exit_time.slice(0, 16)}</td>}
                  {show('hold_period') && (
                    <td className="px-3 py-2 text-slate-300">
                      {t.hold_period < 60
                        ? `${t.hold_period.toFixed(0)}s`
                        : t.hold_period < 3600
                        ? `${(t.hold_period / 60).toFixed(1)}m`
                        : `${(t.hold_period / 3600).toFixed(1)}h`}
                    </td>
                  )}
                  {show('exit_price') && <td className="px-3 py-2 text-slate-100">{t.exit_price.toFixed(2)}</td>}
                  {show('exit_reason') && (
                    <td className="px-3 py-2">
                      <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${EXIT_COLORS[t.exit_reason]}`}>
                        {t.exit_reason.toUpperCase()}
                      </span>
                    </td>
                  )}
                  {show('lot_size') && <td className="px-3 py-2 text-slate-300">{t.lot_size.toFixed(2)}</td>}
                  {show('pnl_r') && (
                    <td className={`px-3 py-2 font-semibold ${t.pnl_r >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {t.pnl_r >= 0 ? '+' : ''}{t.pnl_r.toFixed(2)}R
                    </td>
                  )}
                  {show('profit_pips') && (
                    <td className={`px-3 py-2 font-semibold ${pips >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {pips >= 0 ? '+' : ''}{pips.toFixed(1)}
                    </td>
                  )}
                  {show('profit_usd') && (
                    <td className={`px-3 py-2 font-semibold ${t.profit_usd >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {t.profit_usd >= 0 ? '+' : ''}${t.profit_usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </td>
                  )}
                  {show('capital_after') && <td className="px-3 py-2 text-slate-300">${t.capital_after.toLocaleString()}</td>}
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
