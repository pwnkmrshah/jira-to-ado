import React, { useState } from 'react';
import { api, loadCreds, saveCreds, clearCreds } from '../lib/api.js';

export default function CredentialsBar({ onSaved }) {
  const initial = loadCreds();
  const [apiBaseUrl, setApiBaseUrl] = useState(initial.apiBaseUrl || '');
  const [apiKey, setApiKey] = useState(initial.apiKey || '');
  const [jiraUrl, setJiraUrl] = useState(initial.jiraUrl || '');
  const [jiraEmail, setJiraEmail] = useState(initial.jiraEmail || '');
  const [jiraToken, setJiraToken] = useState(initial.jiraToken || '');
  const [adoOrg, setAdoOrg] = useState(initial.adoOrg || '');
  const [adoPat, setAdoPat] = useState(initial.adoPat || '');
  const [adoProject, setAdoProject] = useState(initial.adoProject || '');
  const [testStatus, setTestStatus] = useState('idle'); // idle | testing | ok | fail
  const [testMsg, setTestMsg] = useState('');
  const [expanded, setExpanded] = useState(!initial.apiKey);

  const canSave = apiKey.trim() && jiraUrl.trim() && jiraEmail.trim() && jiraToken.trim();

  const currentCreds = () => ({
    apiBaseUrl: apiBaseUrl.trim(),
    apiKey: apiKey.trim(),
    jiraUrl: jiraUrl.trim(),
    jiraEmail: jiraEmail.trim(),
    jiraToken: jiraToken.trim(),
    adoOrg: adoOrg.trim(),
    adoPat: adoPat.trim(),
    adoProject: adoProject.trim(),
  });

  const handleSave = () => {
    saveCreds(currentCreds());
    setExpanded(false);
    onSaved?.();
  };

  const handleTest = async () => {
    saveCreds(currentCreds());
    setTestStatus('testing');
    try {
      // Test Jira credentials
      const jiraTest = await fetch(`${apiBaseUrl.trim()}/jira-projects?jira_url=${encodeURIComponent(jiraUrl.trim())}&jira_email=${encodeURIComponent(jiraEmail.trim())}&jira_token=${encodeURIComponent(jiraToken.trim())}`, {
        headers: { 'X-API-Key': apiKey.trim() },
      }).then(r => r.json());
      
      if (jiraTest.error) {
        throw new Error(`Jira: ${jiraTest.error}`);
      }

      // Test ADO credentials
      const adoTest = await api.adoProjectsWithCreds(adoOrg.trim(), adoPat.trim());
      if (adoTest.error) {
        throw new Error(`ADO: ${adoTest.error}`);
      }

      setTestStatus('ok');
      setTestMsg('✅ Backend reachable and all credentials accepted (Jira & ADO).');
    } catch (err) {
      setTestStatus('fail');
      setTestMsg(err.message || 'Connection test failed');
    }
  };

  const handleClear = () => {
    clearCreds();
    setApiBaseUrl('');
    setApiKey('');
    setJiraUrl('');
    setJiraEmail('');
    setJiraToken('');
    setAdoOrg('');
    setAdoPat('');
    setAdoProject('');
    setTestStatus('idle');
    setExpanded(true);
    onSaved?.();
  };

  if (!expanded) {
    return (
      <div className="creds-bar collapsed">
        <span>🔵 Jira: {jiraEmail}</span>
        <span>🔑 API key set</span>
        <button type="button" onClick={() => setExpanded(true)}>Edit connection</button>
      </div>
    );
  }

  return (
    <div className="creds-bar">
      <h2>Connection settings</h2>
      <p className="hint">
        Credentials are kept only in this browser tab&apos;s session storage —
        never written to disk, and cleared when the tab closes.
      </p>
      <div className="creds-grid">
        <label>
          Backend API base URL
          <input value={apiBaseUrl} onChange={(e) => setApiBaseUrl(e.target.value)} placeholder="https://jira-to-ado.onrender.com" />
        </label>
        <label>
          Backend API key
          <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="X-API-Key" />
        </label>
        <label>
          Jira URL
          <input value={jiraUrl} onChange={(e) => setJiraUrl(e.target.value)} placeholder="https://yourcompany.atlassian.net" />
        </label>
        <label>
          Jira email
          <input value={jiraEmail} onChange={(e) => setJiraEmail(e.target.value)} placeholder="you@company.com" />
        </label>
        <label>
          Jira API token
          <input type="password" value={jiraToken} onChange={(e) => setJiraToken(e.target.value)} placeholder="ATATT3x..." />
        </label>
        <label>
          Azure DevOps Organization
          <input value={adoOrg} onChange={(e) => setAdoOrg(e.target.value)} placeholder="your-org-name" />
        </label>
        <label>
          Azure DevOps PAT
          <input type="password" value={adoPat} onChange={(e) => setAdoPat(e.target.value)} placeholder="Personal Access Token" />
        </label>
        <label>
          Default ADO project
          <input value={adoProject} onChange={(e) => setAdoProject(e.target.value)} placeholder="Embedded Refills Engineering" />
        </label>
      </div>
      <div className="creds-actions">
        <button type="button" onClick={handleTest} disabled={!canSave || testStatus === 'testing'}>
          {testStatus === 'testing' ? 'Testing…' : 'Test connection'}
        </button>
        <button type="button" className="primary" onClick={handleSave} disabled={!canSave}>Save</button>
        <button type="button" className="subtle" onClick={handleClear}>Clear</button>
      </div>
      {testStatus === 'ok' && <p className="status-ok">✅ {testMsg}</p>}
      {testStatus === 'fail' && <p className="status-fail">❌ {testMsg}</p>}
    </div>
  );
}
