import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import type { TradeRecord } from '../api/types';

interface PipPoint {
  trade: number;
  exit_time: string;
  pips: number;
  cumulative: number;
}

function tradePips(t: TradeRecord): number {
  const diff = t.exit_price - t.entry_price;
  return (t.direction === 'long' ? diff : -diff) * 10;
}

function buildCurve(trades: TradeRecord[]): PipPoint[] {
  let cum = 0;
  return trades.map((t) => {
    const pips = tradePips(t);
    cum += pips;
    return { trade: t.trade, exit_time: t.exit_time, pips, cumulative: cum };
  });
}

function fmtTick(iso: string): string {
  return new Date(iso).toLocaleString('en-US', {
    month: 'short', day: 'numeric', timeZone: 'UTC',
  });
}

function fmtPips(v: number): string {
  return `${v >= 0 ? '+' : ''}${v.toFixed(1)}`;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function CurveTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const p: PipPoint = payload[0].payload;
  const dateStr = new Date(p.exit_time).toLocaleString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC',
  });
  return (
    <div className="bg-slate-900 border border-slate-700/80 rounded-xl p-3 text-sm shadow-2xl min-w-44">
      <div className="text-slate-500 text-xs mb-0.5">Trade #{p.trade}</div>
      <div className="text-slate-400 text-xs mb-2">{dateStr}</div>
      <div className="flex justify-between gap-6 text-xs mb-1">
        <span className="text-slate-500">This trade</span>
        <span className={`tabular-nums font-semibold ${p.pips >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
          {fmtPips(p.pips)} pips
        </span>
      </div>
      <div className="flex justify-between gap-6 text-xs">
        <span className="text-slate-500">Cumulative</span>
        <span className={`tabular-nums font-bold ${p.cumulative >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
          {fmtPips(p.cumulative)} pips
        </span>
      </div>
    </div>
  );
}

function StatCard({ label, value, valueClass = 'text-slate-100', sub }: {
  label: string; value: string; valueClass?: string; sub?: string;
}) {
  return (
    <div className="bg-slate-800/60 border border-slate-700/50 rounded-xl px-4 py-3 min-w-24">
      <div className="text-xs text-slate-500 mb-1">{label}</div>
      <div className={`text-base font-bold tabular-nums ${valueClass}`}>{value}</div>
      {sub && <div className="text-xs text-slate-600 mt-0.5">{sub}</div>}
    </div>
  );
}

interface Props { trades: TradeRecord[] }

export function PipCurve({ trades }: Props) {
  if (!trades.length) return null;

  const data       = buildCurve(trades);
  const finalPips  = data[data.length - 1].cumulative;
  const isProfit   = finalPips >= 0;
  const lineColor  = isProfit ? '#10b981' : '#ef4444';
  const gradId     = 'pipGrad';
  const allPips    = data.map((d) => d.pips);
  const allCum     = data.map((d) => d.cumulative);

  const minVal = Math.min(...allCum, 0);
  const maxVal = Math.max(...allCum, 0);
  const range  = maxVal - minVal || 1;
  const yMin   = minVal - range * 0.1;
  const yMax   = maxVal + range * 0.1;

  return (
    <div>
      {/* Stats strip */}
      <div className="flex items-stretch gap-3 mb-5 flex-wrap">
        <StatCard
          label="Total Pips"
          value={`${fmtPips(finalPips)} pips`}
          valueClass={isProfit ? 'text-emerald-400' : 'text-red-400'}
        />
        <StatCard
          label="Avg / Trade"
          value={`${fmtPips(finalPips / data.length)} pips`}
          valueClass="text-slate-300"
        />
        <StatCard
          label="Best Trade"
          value={`+${Math.max(...allPips).toFixed(1)} pips`}
          valueClass="text-emerald-400"
        />
        <StatCard
          label="Worst Trade"
          value={`${Math.min(...allPips).toFixed(1)} pips`}
          valueClass="text-red-400"
        />
      </div>

      <ResponsiveContainer width="100%" height={300}>
        <AreaChart data={data} margin={{ top: 8, right: 16, left: 8, bottom: 0 }}>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%"   stopColor={lineColor} stopOpacity={0.3} />
              <stop offset="100%" stopColor={lineColor} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#1e293b" vertical={false} />
          <XAxis
            dataKey="exit_time"
            tick={{ fill: '#475569', fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: '#1e293b' }}
            tickFormatter={fmtTick}
            interval="preserveStartEnd"
            minTickGap={70}
          />
          <YAxis
            domain={[yMin, yMax]}
            tick={{ fill: '#475569', fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v) => `${v > 0 ? '+' : ''}${v.toFixed(0)}`}
            width={56}
          />
          <Tooltip content={<CurveTooltip />} cursor={{ stroke: '#334155', strokeWidth: 1, strokeDasharray: '4 3' }} />
          <ReferenceLine y={0} stroke="#475569" strokeDasharray="4 3" strokeWidth={1} />
          <Area
            type="monotone"
            dataKey="cumulative"
            stroke={lineColor}
            strokeWidth={2}
            fill={`url(#${gradId})`}
            dot={false}
            activeDot={{ r: 4, fill: lineColor, stroke: '#0f172a', strokeWidth: 2 }}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
