import { useState } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../api/client';
import type { BacktestResult, EAPlatform } from '../api/types';

interface Props {
  result: BacktestResult;
  onClose: () => void;
}

export function EAModal({ result, onClose }: Props) {
  const [platform, setPlatform] = useState<EAPlatform>('MT5');
  const [code, setCode] = useState('');
  const [prompt, setPrompt] = useState('');
  const [filename, setFilename] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const [activeTab, setActiveTab] = useState<'code' | 'prompt'>('code');

  const generate = async () => {
    setLoading(true);
    setError('');
    setCode('');
    try {
      const res = await api.generateEA({ result_id: result.id, platform });
      setCode(res.code);
      setPrompt(res.prompt);
      setFilename(res.filename);
      setActiveTab('code');
    } catch (e: unknown) {
      const msg =
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Generation failed. Check that ANTHROPIC_API_KEY is set in .env.';
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = async () => {
    await navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleDownload = () => {
    const blob = new Blob([code], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  const tf = result.parameters.timeframe as string;
  const years = (result.parameters.years as number[]).join(', ');

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-sm">
      <div className="bg-slate-900 border border-slate-700 rounded-2xl w-full max-w-4xl max-h-[90vh] flex flex-col shadow-2xl">

        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-800 flex-shrink-0">
          <div>
            <h2 className="text-base font-semibold text-slate-100">Generate Expert Advisor</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              {result.strategy.replace(/_/g, ' ')} · {tf} · {years}
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-slate-500 hover:text-slate-300 transition-colors p-1"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Controls */}
        <div className="px-6 py-4 border-b border-slate-800 flex-shrink-0">
          <div className="flex items-center gap-4">
            {/* Platform toggle */}
            <div>
              <span className="text-xs text-slate-500 uppercase tracking-wide block mb-2">Platform</span>
              <div className="flex bg-slate-800 rounded-lg p-1 gap-1">
                {(['MT4', 'MT5'] as EAPlatform[]).map((p) => (
                  <button
                    key={p}
                    onClick={() => { setPlatform(p); setCode(''); setPrompt(''); setError(''); }}
                    disabled={loading}
                    className={`px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${
                      platform === p
                        ? 'bg-blue-600 text-white'
                        : 'text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>

            {/* Generate button */}
            <div className="flex-1 flex items-end justify-start pb-0.5 mt-auto">
              <button
                onClick={generate}
                disabled={loading}
                className="px-5 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition-colors flex items-center gap-2"
              >
                {loading ? (
                  <>
                    <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z" />
                    </svg>
                    Generating…
                  </>
                ) : code ? (
                  'Regenerate'
                ) : (
                  'Generate EA'
                )}
              </button>
            </div>

            {/* Action buttons — shown only after code is ready */}
            {code && (
              <div className="flex items-end gap-2 pb-0.5 mt-auto ml-auto">
                <button
                  onClick={handleCopy}
                  className="px-3 py-2 bg-slate-700 hover:bg-slate-600 text-slate-200 text-sm rounded-lg transition-colors"
                >
                  {copied ? 'Copied!' : 'Copy'}
                </button>
                <button
                  onClick={handleDownload}
                  className="px-3 py-2 bg-slate-700 hover:bg-slate-600 text-slate-200 text-sm rounded-lg transition-colors"
                >
                  Download {platform === 'MT4' ? '.mq4' : '.mq5'}
                </button>
              </div>
            )}
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-hidden flex flex-col">
          {!code && !loading && !error && (
            <div className="flex-1 flex flex-col items-center justify-center gap-3 text-slate-600 p-8">
              <svg className="w-12 h-12 text-slate-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                  d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
              </svg>
              <div className="text-center">
                <p className="text-slate-500 font-medium">Ready to generate</p>
                <p className="text-sm mt-1">
                  Select a platform and click <span className="text-slate-400">Generate EA</span> to create {platform} code.
                </p>
              </div>
            </div>
          )}

          {loading && (
            <div className="flex-1 flex flex-col items-center justify-center gap-4 text-slate-400 p-8">
              <svg className="w-10 h-10 animate-spin text-blue-500" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z" />
              </svg>
              <div className="text-center">
                <p className="text-slate-300 font-medium">Generating {platform} Expert Advisor…</p>
                <p className="text-sm text-slate-500 mt-1">Claude AI is writing your EA. This may take 30–60 seconds.</p>
              </div>
            </div>
          )}

          {error && !loading && (
            <div className="flex-1 flex items-center justify-center p-8">
              <div className="bg-red-950/40 border border-red-800/50 rounded-xl p-6 max-w-lg text-center">
                <p className="text-red-400 font-medium mb-2">Generation failed</p>
                <p className="text-red-300/70 text-sm">{error}</p>
              </div>
            </div>
          )}

          {code && !loading && (
            <>
              {/* Tabs */}
              <div className="flex gap-1 px-6 pt-4 pb-0 flex-shrink-0">
                {(['code', 'prompt'] as const).map((tab) => (
                  <button
                    key={tab}
                    onClick={() => setActiveTab(tab)}
                    className={`px-3 py-1.5 rounded-t-md text-xs font-medium transition-colors capitalize border-b-2 ${
                      activeTab === tab
                        ? 'text-blue-400 border-blue-500'
                        : 'text-slate-500 border-transparent hover:text-slate-300'
                    }`}
                  >
                    {tab === 'code' ? 'EA Code' : 'Prompt'}
                  </button>
                ))}
              </div>
              <div className="flex-1 overflow-auto border-t border-slate-800">
                <pre className="p-6 text-xs font-mono leading-relaxed whitespace-pre min-w-max text-slate-300">
                  {activeTab === 'code' ? code : prompt}
                </pre>
              </div>
            </>
          )}
        </div>

        {/* Footer — file info */}
        {filename && code && (
          <div className="px-6 py-3 border-t border-slate-800 flex-shrink-0">
            <p className="text-xs text-slate-600">
              File: <span className="text-slate-500 font-mono">{filename}</span>
              {' · '}Place in your MetaTrader {platform === 'MT4' ? '4' : '5'}{' '}
              <span className="font-mono text-slate-500">MQL{platform === 'MT4' ? '4' : '5'}/Experts/</span> folder and compile in MetaEditor.
            </p>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
