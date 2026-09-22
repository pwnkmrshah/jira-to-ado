import React, { useState, useEffect } from 'react';
import { api, loadCreds } from '../lib/api.js';
import { useJob } from '../hooks/useJob.js';
import JobStatusPanel from '../components/JobStatusPanel.jsx';

const TERMINAL = new Set(['completed', 'warning', 'failed']);

const MODES = [
  { id: 'project', label: 'By Project Key' },
  { id: 'board', label: 'Entire Jira Board' },
  { id: 'keys', label: 'By Specific Jira Keys' },
  { id: 'filter', label: 'By Jira Filter ID' },
];

/** Parses verify_migration.py's VERIFICATION SUMMARY block, e.g.:
 *   Total tickets : 7
 *   Found in ADO  : 5
 *   Missing       : 2
 *   Pass rate     : 90%
 */
function parseSummary(output) {
  if (!output) return null;
  const grab = (substring) => {
    const line = output.split('\n').find((l) => l.includes(substring));
    if (!line) return null;
    const m = line.match(/(\d+)/);
    return m ? parseInt(m[1], 10) : null;
  };
  const total = grab('Total tickets');
  const found = grab('Found in ADO');
  const missing = grab('Missing');
  const failures = grab('Has failures');
  const passRateMatch = output.match(/Pass rate\s*:\s*(\d+)%/);
  const passRate = passRateMatch ? parseInt(passRateMatch[1], 10) : null;
  if (total === null && found === null) return null;
  return { total, found, missing, failures, passRate };
}

export default function VerifyTab({ onJobStart, onJobEnd }) {
  const creds = loadCreds();
  const [mode, setMode] = useState('project');
  const [projectKey, setProjectKey] = useState('');
  const [boardKey, setBoardKey] = useState('');
  const [jiraKeys, setJiraKeys] = useState('');
  const [filterId, setFilterId] = useState('');
  const [adoProject, setAdoProject] = useState(creds.adoProject || '');
  const { job, jobId, startError, isRunning, start } = useJob();

  // Notify parent when job status changes
  useEffect(() => {
    if (isRunning) {
      onJobStart?.('verify');
    } else if (jobId && job && TERMINAL.has(job.status)) {
      onJobEnd?.();
    }
  }, [isRunning, jobId, job, onJobStart, onJobEnd]);

  const sourceValue = { project: projectKey, board: boardKey, keys: jiraKeys, filter: filterId }[mode];
  const canRun = !isRunning && adoProject.trim() && sourceValue.trim();

  // Extract jira_instance from jira_url (e.g., 'https://healthfinch.atlassian.net' → 'healthfinch')
  const extractJiraInstance = (url) => {
    if (!url) return 'healthfinch'; // fallback
    const match = url.match(/https?:\/\/([^.]+)\.atlassian\.net/);
    return match ? match[1] : 'healthfinch';
  };

  const handleRun = () => {
    const jiraInstance = extractJiraInstance(creds.jiraUrl);
    start(() => api.verify({
      jira_instance: jiraInstance,
      ado_project: adoProject.trim(),
      // "Entire Jira Board" reuses the existing --project-key backend path
      project_key: mode === 'project' || mode === 'board' ? (mode === 'project' ? projectKey.trim() : boardKey.trim()) : '',
      jira_keys: mode === 'keys' ? jiraKeys.trim() : '',
      jira_filter: mode === 'filter' ? filterId.trim() : '',
    }));
  };

  const summary = job && TERMINAL.has(job.status) ? parseSummary(job.output) : null;

  return (
    <section className="tab-panel">
      <h2>Verify Migration</h2>
      <div className="field-row">
        <label htmlFor="verify-mode">Source</label>
        <select id="verify-mode" value={mode} onChange={(e) => setMode(e.target.value)}>
          {MODES.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
        </select>
      </div>
      {mode === 'project' && (
        <div className="field-row">
          <label htmlFor="verify-project">Jira Project Key</label>
          <input id="verify-project" value={projectKey} onChange={(e) => setProjectKey(e.target.value)} placeholder="e.g. SUST" />
        </div>
      )}
      {mode === 'board' && (
        <div className="field-row">
          <label htmlFor="verify-board">Jira Project Key (board)</label>
          <input id="verify-board" value={boardKey} onChange={(e) => setBoardKey(e.target.value)} placeholder="e.g. TM" />
        </div>
      )}
      {mode === 'keys' && (
        <div className="field-row">
          <label htmlFor="verify-keys">Jira Keys</label>
          <input id="verify-keys" value={jiraKeys} onChange={(e) => setJiraKeys(e.target.value)} placeholder="TM-1, TM-2" />
        </div>
      )}
      {mode === 'filter' && (
        <div className="field-row">
          <label htmlFor="verify-filter">Jira Filter ID</label>
          <input id="verify-filter" value={filterId} onChange={(e) => setFilterId(e.target.value)} placeholder="e.g. 12345" />
        </div>
      )}
      <div className="field-row">
        <label htmlFor="verify-ado-project">ADO Project</label>
        <input id="verify-ado-project" value={adoProject} onChange={(e) => setAdoProject(e.target.value)} placeholder="Embedded Refills Engineering" />
      </div>
      <button type="button" className="primary" disabled={!canRun} onClick={handleRun}>
        {isRunning ? 'Verifying…' : 'Run Verification'}
      </button>
      {startError && <p className="status-fail">❌ {startError}</p>}
      <JobStatusPanel
        job={job}
        jobId={jobId}
        extraSummary={summary && (
          <ul className="summary-list">
            {summary.total != null && <li>Total tickets checked: {summary.total}</li>}
            {summary.found != null && <li>Found in ADO: {summary.found}</li>}
            {!!summary.missing && <li>Missing (not yet migrated): {summary.missing}</li>}
            {summary.failures != null && <li>Field failures: {summary.failures}</li>}
            {summary.passRate != null && <li>Pass rate: {summary.passRate}%</li>}
          </ul>
        )}
      />
    </section>
  );
}
