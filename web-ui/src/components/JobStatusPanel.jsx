import React from 'react';
import { api } from '../lib/api.js';

export default function JobStatusPanel({ job, jobId, extraSummary }) {
  if (!job) return null;
  const { status, progress, output, error_summary: errorSummary, has_csv: hasCsv } = job;

  const recentLines = output
    ? output.trim().split('\n').filter((l) => l.trim()).slice(-8)
    : [];

  return (
    <div className="job-panel">
      <span className={`status-badge status-${status}`}>{status}</span>

      {progress?.total > 0 && (
        <div className="progress-bar">
          <div
            className="progress-fill"
            style={{ width: `${Math.min(100, Math.round((progress.done / progress.total) * 100))}%` }}
          />
          <span className="progress-label">{progress.done} / {progress.total}</span>
        </div>
      )}

      {extraSummary}

      {errorSummary && <p className="error-summary">⚠️ {errorSummary}</p>}

      {!!recentLines.length && <pre className="log-tail">{recentLines.join('\n')}</pre>}

      {hasCsv && jobId && (
        <a className="csv-link" href={api.csvDownloadUrl(jobId)} target="_blank" rel="noreferrer">
          Download CSV report
        </a>
      )}
    </div>
  );
}
