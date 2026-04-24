import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
} from 'recharts';
import type { EquityPoint } from '../api/types';

interface Props {
  data: EquityPoint[];
  initialCapital: number;
  stoppedOut?: boolean;
  onTradeClick?: (tradeIndex: number) => void;
  highlightedTrade?: number | null;
}

const EXIT_COLORS: Record<string, string> = {
  tp:          '#10b981',
  sl:          '#ef4444',
  be:          '#f59e0b',
  end_of_data: '#64748b',
};

const EXIT_LABELS: Record<string, string> = {
  tp:          'TP',
  sl:          'SL',
  be:          'BE',
  end_of_data: 'EOD',
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function TradeDot(props: any) {
  const { cx, cy, payload, highlightedTrade } = props;
  if (cx == null || cy == null) return null;

  const isHighlighted = payload.trade === highlightedTrade;
  const color = EXIT_COLORS[payload.exit_reason] ?? '#64748b';

  if (isHighlighted) {
    return (
      <g>
        <circle cx={cx} cy={cy} r={9} fill={color} opacity={0.25} />
        <circle cx={cx} cy={cy} r={5} fill={color} stroke="#0f172a" strokeWidth={2} />
      </g>
    );
  }

  return (
    <circle cx={cx} cy={cy} r={3} fill={color} stroke="#0f172a" strokeWidth={1} opacity={0.9} />
  );
}

// Stop-out terminal marker
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function StopOutDot(props: any) {
  const { cx, cy, index, dataLength } = props;
  if (cx == null || cy == null || index !== dataLength - 1) return null;
  const s = 7;
  return (
    <g>
      <circle cx={cx} cy={cy} r={14} fill="#ef444420" />
      <circle cx={cx} cy={cy} r={8} fill="#ef4444" opacity={0.3} />
      <line x1={cx - s} y1={cy - s} x2={cx + s} y2={cy + s} stroke="#ef4444" strokeWidth={2.5} strokeLinecap="round" />
      <line x1={cx + s} y1={cy - s} x2={cx - s} y2={cy + s} stroke="#ef4444" strokeWidth={2.5} strokeLinecap="round" />
    </g>
  );
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false,
    timeZone: 'UTC',
  });
}

function formatTick(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

function formatCapital(v: number): string {
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000)     return `$${(v / 1_000).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function CustomTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const p: EquityPoint = payload[0].payload;
  const exitColor = EXIT_COLORS[p.exit_reason] ?? '#64748b';
  const exitLabel = EXIT_LABELS[p.exit_reason] ?? p.exit_reason.toUpperCase();
  return (
    <div className="bg-slate-900 border border-slate-700/80 rounded-xl p-3 text-sm shadow-2xl min-w-47.5">
      <div className="text-slate-500 text-xs mb-1.5">Trade #{p.trade}</div>
      <div className="text-slate-400 text-xs mb-2">{formatDate(p.exit_time)}</div>
      <div className="text-slate-100 font-bold text-lg mb-2 tabular-nums">{formatCapital(p.capital)}</div>
      <div className="flex items-center gap-2 flex-wrap">
        <span
          className={`px-2 py-0.5 rounded-md text-xs font-semibold ${
            p.direction === 'long' ? 'bg-emerald-900/60 text-emerald-300' : 'bg-red-900/60 text-red-300'
          }`}
        >
          {p.direction.toUpperCase()}
        </span>
        <span
          style={{ background: exitColor + '22', color: exitColor, border: `1px solid ${exitColor}44` }}
          className="px-2 py-0.5 rounded-md text-xs font-semibold"
        >
          {exitLabel}
        </span>
        <span className={`text-xs font-semibold tabular-nums ${p.pnl_r >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
          {p.pnl_r >= 0 ? '+' : ''}{p.pnl_r.toFixed(2)}R
        </span>
      </div>
    </div>
  );
}

interface StatCardProps {
  label: string;
  value: string;
  valueClass?: string;
  sub?: string;
}

function StatCard({ label, value, valueClass = 'text-slate-100', sub }: StatCardProps) {
  return (
    <div className="bg-slate-800/60 border border-slate-700/50 rounded-xl px-4 py-3 min-w-25">
      <div className="text-xs text-slate-500 mb-1">{label}</div>
      <div className={`text-base font-bold tabular-nums ${valueClass}`}>{value}</div>
      {sub && <div className="text-xs text-slate-600 mt-0.5 tabular-nums">{sub}</div>}
    </div>
  );
}

export function EquityChart({ data, initialCapital, stoppedOut, onTradeClick, highlightedTrade }: Props) {
  if (!data?.length) return null;

  const lastCapital  = stoppedOut ? 0 : data[data.length - 1].capital;
  const isProfit     = lastCapital >= initialCapital;
  const lineColor    = (stoppedOut || !isProfit) ? '#ef4444' : '#10b981';
  const gradientId   = 'equityGrad';

  const returnPct    = stoppedOut ? -100 : ((lastCapital - initialCapital) / initialCapital) * 100;

  // Compute max drawdown the same way as backtest.py: track running peak, not global peak vs global trough
  let runningPeak = initialCapital;
  let maxDD = 0;
  for (const pt of data) {
    if (pt.capital > runningPeak) runningPeak = pt.capital;
    const dd = runningPeak > 0 ? (runningPeak - pt.capital) / runningPeak * 100 : 0;
    if (dd > maxDD) maxDD = dd;
  }

  const wins   = data.filter((d) => d.pnl_r > 0).length;
  const losses = data.filter((d) => d.pnl_r <= 0).length;

  const minVal = Math.min(...data.map((d) => d.capital), initialCapital, stoppedOut ? 0 : Infinity);
  const maxVal = Math.max(...data.map((d) => d.capital), initialCapital);
  const range  = maxVal - minVal || 1;
  const yMin   = Math.max(0, minVal - range * 0.1);
  const yMax   = maxVal + range * 0.1;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const handleClick = (d: any) => {
    if (d?.activePayload?.[0] && onTradeClick) {
      onTradeClick((d.activePayload[0].payload as EquityPoint).trade - 1);
    }
  };

  const renderDot = (props: any) => { // eslint-disable-line @typescript-eslint/no-explicit-any
    if (stoppedOut && props.index === data.length - 1) {
      return <StopOutDot {...props} dataLength={data.length} />;
    }
    return <TradeDot {...props} highlightedTrade={highlightedTrade} />;
  };

  return (
    <div>
      {/* Stats strip */}
      <div className="flex items-stretch gap-3 mb-5 flex-wrap">
        <StatCard
          label="Final Capital"
          value={stoppedOut ? '$0' : formatCapital(lastCapital)}
          valueClass={stoppedOut || !isProfit ? 'text-red-400' : 'text-emerald-400'}
          sub={stoppedOut ? undefined : `from ${formatCapital(initialCapital)}`}
        />
        <StatCard
          label="Total Return"
          value={`${returnPct >= 0 ? '+' : ''}${returnPct.toFixed(1)}%`}
          valueClass={returnPct >= 0 ? 'text-emerald-400' : 'text-red-400'}
        />
        <StatCard
          label="Max Drawdown"
          value={`-${maxDD.toFixed(1)}%`}
          valueClass="text-amber-400"
        />
        <StatCard
          label="Trades"
          value={String(data.length)}
          sub={`${wins}W / ${losses}L`}
        />

        {stoppedOut && (
          <div className="ml-auto self-center flex items-center gap-2 px-4 py-2.5 bg-red-950/70 border border-red-700/50 rounded-xl">
            <span className="relative flex h-2.5 w-2.5">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-500 opacity-60" />
              <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-red-500" />
            </span>
            <span className="text-red-400 text-sm font-semibold tracking-wide">STOP OUT</span>
            <span className="text-red-600 text-xs">— account blown</span>
          </div>
        )}
      </div>

      {/* Legend */}
      <div className="flex items-center gap-4 mb-3 px-1">
        {Object.entries(EXIT_COLORS).map(([key, color]) => (
          <div key={key} className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ background: color }} />
            <span className="text-xs text-slate-500 capitalize">{EXIT_LABELS[key]}</span>
          </div>
        ))}
        <div className="flex items-center gap-1.5 ml-1">
          <span className="w-4 border-t border-dashed border-slate-500 inline-block" style={{ borderColor: '#475569' }} />
          <span className="text-xs text-slate-500">Initial</span>
        </div>
      </div>

      {/* Chart */}
      <ResponsiveContainer width="100%" height={340}>
        <AreaChart data={data} margin={{ top: 8, right: 16, left: 8, bottom: 0 }} onClick={handleClick} style={{ cursor: 'pointer' }}>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%"   stopColor={lineColor} stopOpacity={0.3} />
              <stop offset="100%" stopColor={lineColor} stopOpacity={0.02} />
            </linearGradient>
          </defs>

          <CartesianGrid stroke="#1e293b" vertical={false} strokeDasharray="0" />

          <XAxis
            dataKey="exit_time"
            tick={{ fill: '#475569', fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: '#1e293b' }}
            tickFormatter={formatTick}
            interval="preserveStartEnd"
            minTickGap={70}
          />
          <YAxis
            domain={[yMin, yMax]}
            tick={{ fill: '#475569', fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={formatCapital}
            width={62}
          />

          <Tooltip content={<CustomTooltip />} cursor={{ stroke: '#334155', strokeWidth: 1, strokeDasharray: '4 3' }} />

          {/* Initial capital reference */}
          <ReferenceLine
            y={initialCapital}
            stroke="#475569"
            strokeDasharray="5 4"
            strokeWidth={1}
            label={{ value: formatCapital(initialCapital), fill: '#475569', fontSize: 10, position: 'insideTopRight', offset: 6 }}
          />

          {/* Zero / stop-out line */}
          {(stoppedOut || minVal < initialCapital * 0.15) && (
            <ReferenceLine
              y={0}
              stroke="#7f1d1d"
              strokeDasharray="3 3"
              strokeWidth={1}
              label={stoppedOut ? { value: 'STOP OUT', fill: '#ef4444', fontSize: 10, position: 'insideBottomRight' } : undefined}
            />
          )}

          <Area
            type="monotone"
            dataKey="capital"
            stroke={lineColor}
            strokeWidth={2}
            fill={`url(#${gradientId})`}
            dot={renderDot}
            activeDot={{ r: 5, fill: lineColor, stroke: '#0f172a', strokeWidth: 2 }}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
