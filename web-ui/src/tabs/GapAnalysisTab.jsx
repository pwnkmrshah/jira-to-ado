import React, { useState, useEffect } from 'react';
import { api, loadCreds } from '../lib/api.js';
import { useJob } from '../hooks/useJob.js';
import JobStatusPanel from '../components/JobStatusPanel.jsx';

const TERMINAL = new Set(['completed', 'warning', 'failed']);

/** Parses migration_gap_analysis[_board].py's printed report, e.g.:
 *   ✅  Correctly migrated:                     130  (85%)
 *   ❌  Not found anywhere in ADO:               12  (8%)
 *   ⚠   Wrong area path:                          3  (2%)
 */
function parseGapCounts(output) {
  if (!output) return null;
  const grab = (substring) => {
    const line = output.split('\n').find((l) => l.includes(substring));
    if (!line) return null;
    const m = line.match(/(\d+)/);
    return m ? parseInt(m[1], 10) : null;
  };
  return {
    correct: grab('Correctly migrated'),
    missed: grab('Not found anywhere'),
    wrongArea: grab('Wrong area'),
  };
}

export default function GapAnalysisTab({ onJobStart, onJobEnd }) {
  const creds = loadCreds();
  const [mode, setMode] = useState('filter'); // filter | board
  const [filterId, setFilterId] = useState('');
  const [boardKey, setBoardKey] = useState('');
  const [adoProject, setAdoProject] = useState(creds.adoProject || '');
  const [adoBoard, setAdoBoard] = useState('');
  const { job, jobId, startError, isRunning, start } = useJob();

  // Notify parent when job status changes
  useEffect(() => {
    if (isRunning) {
      onJobStart?.('gaps');
    } else if (jobId && job && TERMINAL.has(job.status)) {
      onJobEnd?.();
    }
  }, [isRunning, jobId, job, onJobStart, onJobEnd]);

  const hasInput = mode === 'filter' ? filterId.trim() : boardKey.trim();
  const canRun = !isRunning && adoProject.trim() && hasInput && adoBoard.trim();

  // Extract jira_instance from jira_url (e.g., 'https://healthfinch.atlassian.net' → 'healthfinch')
  const extractJiraInstance = (url) => {
    if (!url) return 'healthfinch'; // fallback
    const match = url.match(/https?:\/\/([^.]+)\.atlassian\.net/);
    return match ? match[1] : 'healthfinch';
  };

  const handleRun = () => {
    if (!adoBoard.trim()) {
      alert('ADO Team/Board is required');
      return;
    }
    const jiraInstance = extractJiraInstance(creds.jiraUrl);
    start(() => (mode === 'filter'
      ? api.gaps({ jira_instance: jiraInstance, ado_project: adoProject.trim(), jira_filter: filterId.trim(), ado_board: adoBoard.trim() })
      : api.gapsBoard({ jira_instance: jiraInstance, ado_project: adoProject.trim(), jira_board_key: boardKey.trim(), ado_board: adoBoard.trim() })
    ));
  };

  const summary = job && TERMINAL.has(job.status) ? parseGapCounts(job.output) : null;

  return (
    <section className="tab-panel">
      <h2>Gap Analysis</h2>
      <div className="field-row">
        <label htmlFor="gap-mode">Source</label>
        <select id="gap-mode" value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="filter">Jira Filter ID</option>
          <option value="board">Entire Jira Board</option>
        </select>
      </div>
      {mode === 'filter' ? (
        <div className="field-row">
          <label htmlFor="gap-filter">Jira Filter ID</label>
          <input id="gap-filter" value={filterId} onChange={(e) => setFilterId(e.target.value)} placeholder="e.g. 12345" />
        </div>
      ) : (
        <div className="field-row">
          <label htmlFor="gap-board-key">Jira Project Key</label>
          <input id="gap-board-key" value={boardKey} onChange={(e) => setBoardKey(e.target.value)} placeholder="e.g. SUST" />
        </div>
      )}
      <div className="field-row">
        <label htmlFor="gap-ado-project">ADO Project</label>
        <input id="gap-ado-project" value={adoProject} onChange={(e) => setAdoProject(e.target.value)} placeholder="Embedded Refills Engineering" />
      </div>
      <div className="field-row">
        <label htmlFor="gap-ado-board">ADO Team/Board</label>
        <input id="gap-ado-board" value={adoBoard} onChange={(e) => setAdoBoard(e.target.value)} placeholder="Team name" required />
      </div>
      <button type="button" className="primary" disabled={!canRun} onClick={handleRun}>
        {isRunning ? 'Analyzing…' : 'Run Gap Analysis'}
      </button>
      {startError && <p className="status-fail">❌ {startError}</p>}
      <JobStatusPanel
        job={job}
        jobId={jobId}
        extraSummary={summary && (
          <ul className="summary-list">
            {summary.correct != null && <li>✅ Correctly migrated: {summary.correct}</li>}
            {summary.missed != null && <li>❌ Missing in ADO: {summary.missed}</li>}
            {summary.wrongArea != null && <li>⚠️ Wrong area path: {summary.wrongArea}</li>}
          </ul>
        )}
      />
    </section>
  );
}
