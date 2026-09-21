import React, { useState } from 'react';
import { api, loadCreds } from '../lib/api.js';
import { useJob } from '../hooks/useJob.js';
import JobStatusPanel from '../components/JobStatusPanel.jsx';

const MODES = [
  { id: 'filter', label: 'Jira Filter ID' },
  { id: 'keys', label: 'Specific Jira Keys (comma-separated)' },
];

export default function MigrateTab() {
  const creds = loadCreds();
  const [mode, setMode] = useState('filter');
  const [filterId, setFilterId] = useState('');
  const [keys, setKeys] = useState('');
  const [adoProject, setAdoProject] = useState(creds.adoProject || '');
  const [skipAttachments, setSkipAttachments] = useState(false);
  const { job, jobId, startError, isRunning, start } = useJob();

  const hasInput = mode === 'filter' ? filterId.trim() : keys.trim();
  const canRun = !isRunning && adoProject.trim() && hasInput;

  const handleRun = () => {
    start(() => api.migrate({
      ado_project: adoProject.trim(),
      jira_filter: mode === 'filter' ? filterId.trim() : '',
      jira_keys: mode === 'keys' ? keys.trim() : '',
      skip_attachments: skipAttachments,
    }));
  };

  return (
    <section className="tab-panel">
      <h2>Manual Migration</h2>
      <div className="field-row">
        <label htmlFor="migrate-mode">Source</label>
        <select id="migrate-mode" value={mode} onChange={(e) => setMode(e.target.value)}>
          {MODES.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
        </select>
      </div>
      {mode === 'filter' ? (
        <div className="field-row">
          <label htmlFor="migrate-filter">Jira Filter ID</label>
          <input id="migrate-filter" value={filterId} onChange={(e) => setFilterId(e.target.value)} placeholder="e.g. 12345" />
        </div>
      ) : (
        <div className="field-row">
          <label htmlFor="migrate-keys">Jira Keys</label>
          <input id="migrate-keys" value={keys} onChange={(e) => setKeys(e.target.value)} placeholder="TM-1, TM-2, TM-3" />
        </div>
      )}
      <div className="field-row">
        <label htmlFor="migrate-ado-project">ADO Project</label>
        <input id="migrate-ado-project" value={adoProject} onChange={(e) => setAdoProject(e.target.value)} placeholder="Embedded Refills Engineering" />
      </div>
      <label className="checkbox-row">
        <input type="checkbox" checked={skipAttachments} onChange={(e) => setSkipAttachments(e.target.checked)} />
        Skip attachments
      </label>
      <button type="button" className="primary" disabled={!canRun} onClick={handleRun}>
        {isRunning ? 'Migrating…' : 'Start Migration'}
      </button>
      {startError && <p className="status-fail">❌ {startError}</p>}
      <JobStatusPanel job={job} jobId={jobId} />
    </section>
  );
}
