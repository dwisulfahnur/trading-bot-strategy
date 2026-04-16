import axios from 'axios';
import type {
  BacktestRequest,
  BacktestResult,
  DataAvailable,
  EARequest,
  EAResponse,
  JobStatus,
  OhlcvBar,
  ResultSummary,
  StrategyMeta,
} from './types';

const http = axios.create({ baseURL: '/api' });

export const api = {
  // Metadata
  getStrategies: () => http.get<StrategyMeta[]>('/strategies').then((r) => r.data),
  getDataAvailable: () => http.get<DataAvailable>('/data/available').then((r) => r.data),

  // Backtest execution
  runBacktest: (req: BacktestRequest) =>
    http.post<JobStatus>('/backtest/run', req).then((r) => r.data),
  getJobStatus: (jobId: string) =>
    http.get<JobStatus>(`/backtest/status/${jobId}`).then((r) => r.data),

  // Results
  listResults: () => http.get<ResultSummary[]>('/results').then((r) => r.data),
  getResult: (id: string) => http.get<BacktestResult>(`/results/${id}`).then((r) => r.data),
  deleteResult: (id: string) => http.delete(`/results/${id}`).then((r) => r.data),
  deleteResults: (ids: string[]) => http.delete('/results', { data: ids }).then((r) => r.data),

  // EA generation
  generateEA: (req: EARequest) =>
    http.post<EAResponse>('/ea/generate', req).then((r) => r.data),

  // OHLCV
  getOhlcv: (timeframe: string, years: number[], dateFrom?: string, dateTo?: string) => {
    const params: Record<string, string> = {
      timeframe,
      years: years.join(','),
    };
    if (dateFrom) params.date_from = dateFrom;
    if (dateTo) params.date_to = dateTo;
    return http.get<OhlcvBar[]>('/ohlcv', { params }).then((r) => r.data);
  },
};
