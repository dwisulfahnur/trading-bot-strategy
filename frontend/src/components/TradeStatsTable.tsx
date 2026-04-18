import { useState } from 'react';
import {
  ComposedChart,
  Bar,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import type { TradeRecord } from '../api/types';

type GroupBy = 'session' | 'hour' | 'day' | 'month';
type Metric  = 'r' | 'pips';

// ── Pip calculation ────────────────────────────────────────────────────────
// 1 pip = $0.10 for XAUUSD → multiply raw price diff by 10
function tradePips(t: TradeRecord): number {
  const diff = t.exit_price - t.entry_price;
  return (t.direction === 'long' ? diff : -diff) * 10;
}

// ── Distribution bar charts ────────────────────────────────────────────────
interface BucketData {
  label: string;
  sublabel?: string;
  trades: number;
  wins: number;
  losses: number;
  avg_r: number;
  net_r: number;
  avg_pips: number;
  net_pips: number;
}

const DAY_NAMES   = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

interface SessionDef { label: string; hours: string; hourStart: number; hourEnd: number }
const SESSIONS: SessionDef[] = [
  { label: 'Asian',     hours: '00–08 UTC', hourStart:  0, hourEnd:  7 },
  { label: 'London',    hours: '08–13 UTC', hourStart:  8, hourEnd: 12 },
  { label: 'Lon / NY',  hours: '13–17 UTC', hourStart: 13, hourEnd: 16 },
  { label: 'New York',  hours: '17–21 UTC', hourStart: 17, hourEnd: 20 },
  { label: 'Off-hours', hours: '21–00 UTC', hourStart: 21, hourEnd: 23 },
];

function getSession(entry_time: string): string {
  const hour = parseInt(entry_time.slice(11, 13), 10);
  return SESSIONS.find((s) => hour >= s.hourStart && hour <= s.hourEnd)?.label ?? 'Off-hours';
}

function getKey(entry_time: string, groupBy: GroupBy): string {
  if (groupBy === 'session') return getSession(entry_time);
  if (groupBy === 'hour')    return entry_time.slice(11, 13);
  if (groupBy === 'day')     return String(new Date(entry_time).getDay());
  return entry_time.slice(5, 7);
}

function toLabel(key: string, groupBy: GroupBy): string {
  if (groupBy === 'session') return key;
  if (groupBy === 'hour')    return `${key}:00`;
  if (groupBy === 'day')     return DAY_NAMES[parseInt(key, 10)];
  return MONTH_NAMES[parseInt(key, 10) - 1];
}

function makeBucket(key: string, ts: TradeRecord[], groupBy: GroupBy): BucketData {
  const wins     = ts.filter((t) => t.exit_reason === 'tp').length;
  const net_r    = ts.reduce((sum, t) => sum + t.pnl_r, 0);
  const net_pips = ts.reduce((sum, t) => sum + tradePips(t), 0);
  return {
    label:    toLabel(key, groupBy),
    sublabel: groupBy === 'session' ? SESSIONS.find((s) => s.label === key)?.hours : undefined,
    trades:   ts.length,
    wins,
    losses:   ts.length - wins,
    avg_r:    net_r    / ts.length,
    net_r,
    avg_pips: net_pips / ts.length,
    net_pips,
  };
}

function computeBuckets(trades: TradeRecord[], groupBy: GroupBy): BucketData[] {
  const map = new Map<string, TradeRecord[]>();
  for (const t of trades) {
    const key = getKey(t.entry_time, groupBy);
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(t);
  }
  if (groupBy === 'session') {
    return SESSIONS.filter((s) => map.has(s.label)).map((s) => makeBucket(s.label, map.get(s.label)!, groupBy));
  }
  return [...map.keys()].sort().map((k) => makeBucket(k, map.get(k)!, groupBy));
}

function fmtMetric(v: number, metric: Metric): string {
  const sign = v >= 0 ? '+' : '';
  return metric === 'r' ? `${sign}${v.toFixed(2)}R` : `${sign}${v.toFixed(1)} pips`;
}

function MetricTooltip({ active, payload, label, metric }: { metric: Metric } & Record<string, unknown>) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  if (!(active as any) || !(payload as any)?.length) return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const d: BucketData = (payload as any)[0].payload;
  const avg = metric === 'r' ? d.avg_r  : d.avg_pips;
  const net = metric === 'r' ? d.net_r  : d.net_pips;
  return (
    <div className="bg-slate-900 border border-slate-700 rounded-xl p-3 text-xs shadow-xl min-w-36">
      <div className="font-semibold text-slate-200">{label as string}</div>
      {d.sublabel && <div className="text-slate-500 mb-2">{d.sublabel}</div>}
      {!d.sublabel && <div className="mb-2" />}
      <div className="space-y-1">
        <Row label="Trades"   value={String(d.trades)} />
        <Row label="Wins"     value={String(d.wins)}   color="text-emerald-400" />
        <Row label="Losses"   value={String(d.losses)} color="text-red-400" />
        <Row label="Win Rate" value={`${d.trades > 0 ? ((d.wins / d.trades) * 100).toFixed(1) : '0'}%`} />
        <Row label={metric === 'r' ? 'Avg R' : 'Avg Pips'} value={fmtMetric(avg, metric)}
             color={avg >= 0 ? 'text-emerald-400' : 'text-red-400'} />
        <Row label={metric === 'r' ? 'Net R' : 'Net Pips'} value={fmtMetric(net, metric)}
             color={net >= 0 ? 'text-emerald-400' : 'text-red-400'} />
      </div>
    </div>
  );
}

function Row({ label, value, color = 'text-slate-200' }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex justify-between gap-4">
      <span className="text-slate-500">{label}</span>
      <span className={`tabular-nums ${color}`}>{value}</span>
    </div>
  );
}

const axisTick = { fill: '#475569', fontSize: 11 };

function StatChart({ buckets, metric }: { buckets: BucketData[]; metric: Metric }) {
  const avgKey  = metric === 'r' ? 'avg_r' : 'avg_pips';
  const allAvg  = buckets.map((b) => (metric === 'r' ? b.avg_r : b.avg_pips));
  const absMax  = Math.max(...allAvg.map(Math.abs), 0.01);
  const yPadded = absMax * 1.3;

  return (
    <ResponsiveContainer width="100%" height={220}>
      <ComposedChart data={buckets} margin={{ top: 4, right: 44, left: 0, bottom: 0 }} barCategoryGap="30%" barGap={2}>
        <CartesianGrid stroke="#1e293b" vertical={false} />
        <XAxis dataKey="label" tick={axisTick} tickLine={false} axisLine={{ stroke: '#1e293b' }} />
        <YAxis yAxisId="left" tick={axisTick} tickLine={false} axisLine={false} allowDecimals={false} width={24} />
        <YAxis
          yAxisId="right"
          orientation="right"
          domain={[-yPadded, yPadded]}
          tick={false}
          tickLine={false}
          axisLine={false}
          width={0}
        />
        <Tooltip content={<MetricTooltip metric={metric} />} cursor={{ fill: '#1e293b' }} />
        <ReferenceLine yAxisId="right" y={0} stroke="#475569" strokeWidth={1} strokeDasharray="4 3" />
        <Bar yAxisId="left" dataKey="wins"   fill="#10b981" fillOpacity={0.85} radius={[3,3,0,0]} isAnimationActive={false} />
        <Bar yAxisId="left" dataKey="losses" fill="#ef4444" fillOpacity={0.85} radius={[3,3,0,0]} isAnimationActive={false} />
        <Line
          yAxisId="right"
          dataKey={avgKey}
          stroke="#818cf8"
          strokeWidth={2}
          strokeDasharray="5 3"
          dot={{ r: 3, fill: '#818cf8', stroke: '#0f172a', strokeWidth: 1 }}
          activeDot={{ r: 5 }}
          isAnimationActive={false}
        />
      </ComposedChart>
    </ResponsiveContainer>
  );
}

// ── Main ───────────────────────────────────────────────────────────────────
const GROUPS: { key: GroupBy; title: string }[] = [
  { key: 'session', title: 'By Session' },
  { key: 'hour',    title: 'By Hour'    },
  { key: 'day',     title: 'By Day'     },
  { key: 'month',   title: 'By Month'   },
];

interface Props { trades: TradeRecord[] }

export function TradeStatsTable({ trades }: Props) {
  const [metric, setMetric] = useState<Metric>('r');

  if (trades.length === 0) return null;

  return (
    <div className="space-y-6">
      {/* R / Pips switch */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-4">
          <LegendDot  color="#10b981" label="Wins" />
          <LegendDot  color="#ef4444" label="Losses" />
          <LegendLine color="#818cf8" dashed label={metric === 'r' ? 'Avg R' : 'Avg Pips'} />
        </div>
        <div className="flex items-center gap-1 bg-slate-800 rounded-lg p-0.5">
          {(['r', 'pips'] as Metric[]).map((m) => (
            <button
              key={m}
              onClick={() => setMetric(m)}
              className={`px-3 py-1 rounded-md text-xs font-semibold transition-colors ${
                metric === m ? 'bg-slate-600 text-slate-100' : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              {m === 'r' ? 'R' : 'Pips'}
            </button>
          ))}
        </div>
      </div>

      {/* 2-per-row distribution grid */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {GROUPS.map(({ key, title }) => {
          const buckets = computeBuckets(trades, key);
          return (
            <div key={key} className="bg-slate-800/40 border border-slate-700/60 rounded-xl p-4">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-1">{title}</p>
              {key === 'session' ? (
                <p className="text-xs text-slate-600 mb-3">
                  Asian 00–08 · London 08–13 · Lon/NY 13–17 · NY 17–21 · Off 21–00 (UTC)
                </p>
              ) : (
                <div className="mb-3" />
              )}
              <StatChart buckets={buckets} metric={metric} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="w-2.5 h-2.5 rounded-sm inline-block" style={{ background: color }} />
      <span className="text-xs text-slate-500">{label}</span>
    </div>
  );
}

function LegendLine({ color, label, dashed }: { color: string; label: string; dashed?: boolean }) {
  return (
    <div className="flex items-center gap-1.5">
      <span
        className="w-4 border-t-2 inline-block"
        style={{ borderColor: color, borderStyle: dashed ? 'dashed' : 'solid' }}
      />
      <span className="text-xs text-slate-500">{label}</span>
    </div>
  );
}
