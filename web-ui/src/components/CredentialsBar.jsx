import React, { useState, useEffect } from 'react';
import { api, loadCreds, saveCreds, clearCreds } from '../lib/api.js';
import { getApiKey } from '../lib/apiKeyManager.js';

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
  const [testPassed, setTestPassed] = useState(!!initial.apiKey); // Track if validation succeeded

  // Auto-load API key based on backend URL environment
  useEffect(() => {
    if (!apiBaseUrl) return;
    
    const loadApiKey = async () => {
      try {
        const key = await getApiKey(apiBaseUrl);
        setApiKey(key);
        // Don't reset test status on key load - just set the key
      } catch (err) {
        console.error('Error loading API key:', err.message);
        // For production URL, ask user to enter key manually
        if (!apiBaseUrl.includes('localhost')) {
          setApiKey(''); // Clear field to prompt user
        }
      }
    };
    
    loadApiKey();
  }, [apiBaseUrl]);

  const canSave = apiKey.trim() && jiraUrl.trim() && jiraEmail.trim() && jiraToken.trim();
  const canTest = canSave && testStatus !== 'testing';
  const canClickSave = testPassed && canSave; // Only allow save AFTER test passes

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

  // Reset validation state when any credential changes
  const handleCredChange = (setter) => (e) => {
    setter(e.target.value);
    setTestPassed(false);
    setTestStatus('idle');
  };

  const handleTest = async () => {
    setTestStatus('testing');
    setTestPassed(false);
    try {
      // Normalize API base URL (remove trailing slash)
      const baseUrl = apiBaseUrl.trim().replace(/\/$/, '');

      // Test Jira credentials using strict validation endpoint
      const jiraResp = await fetch(
        `${baseUrl}/validate-jira-creds?jira_url=${encodeURIComponent(jiraUrl.trim())}&jira_email=${encodeURIComponent(jiraEmail.trim())}&jira_token=${encodeURIComponent(jiraToken.trim())}`,
        {
          headers: { 'X-API-Key': apiKey.trim() },
        }
      );
      const jiraTest = await jiraResp.json();
      
      if (!jiraResp.ok || jiraTest.error) {
        throw new Error(`Jira: ${jiraTest.error || 'Validation failed'}`);
      }

      // Test ADO credentials using strict validation endpoint
      const adoResp = await fetch(
        `${baseUrl}/validate-ado-creds?ado_org=${encodeURIComponent(adoOrg.trim())}&ado_pat=${encodeURIComponent(adoPat.trim())}`,
        {
          headers: { 'X-API-Key': apiKey.trim() },
        }
      );
      const adoTest = await adoResp.json();
      
      if (!adoResp.ok || adoTest.error) {
        throw new Error(`ADO: ${adoTest.error || 'Validation failed'}`);
      }

      // Both tests passed!
      setTestStatus('ok');
      setTestPassed(true);
      setTestMsg('✅ Jira & Azure DevOps credentials validated successfully.');
    } catch (err) {
      setTestStatus('fail');
      setTestPassed(false);
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
          <input value={apiBaseUrl} onChange={handleCredChange(setApiBaseUrl)} placeholder="https://jira-to-ado.onrender.com" />
        </label>
        <label>
          Backend API key
          <input type="password" value={apiKey} onChange={handleCredChange(setApiKey)} placeholder="X-API-Key" />
          <small style={{ display: 'block', marginTop: '4px', opacity: 0.8 }}>
            {apiBaseUrl.includes('localhost') ? '🔓 Local dev key' : '🔐 Fetched from Azure Key Vault'}
          </small>
        </label>
        <label>
          Jira URL
          <input value={jiraUrl} onChange={handleCredChange(setJiraUrl)} placeholder="https://yourcompany.atlassian.net" />
        </label>
        <label>
          Jira email
          <input value={jiraEmail} onChange={handleCredChange(setJiraEmail)} placeholder="you@company.com" />
        </label>
        <label>
          Jira API token
          <input type="password" value={jiraToken} onChange={handleCredChange(setJiraToken)} placeholder="ATATT3x..." />
        </label>
        <label>
          Azure DevOps Organization
          <input value={adoOrg} onChange={handleCredChange(setAdoOrg)} placeholder="your-org-name" />
        </label>
        <label>
          Azure DevOps PAT
          <input type="password" value={adoPat} onChange={handleCredChange(setAdoPat)} placeholder="Personal Access Token" />
        </label>
        <label>
          Default ADO project
          <input value={adoProject} onChange={(e) => setAdoProject(e.target.value)} placeholder="Embedded Refills Engineering" />
        </label>
      </div>
      <div className="creds-actions">
        <button type="button" onClick={handleTest} disabled={!canTest}>
          {testStatus === 'testing' ? 'Testing…' : 'Test connection'}
        </button>
        <button type="button" className="primary" onClick={handleSave} disabled={!canClickSave} title={!testPassed ? '⚠️ Test connection first' : ''}>Save</button>
        <button type="button" className="subtle" onClick={handleClear}>Clear</button>
      </div>
      {testStatus === 'ok' && <p className="status-ok">✅ {testMsg}</p>}
      {testStatus === 'fail' && <p className="status-fail">❌ {testMsg}</p>}
      {testStatus === 'idle' && testPassed && <p className="status-ok">✅ Credentials validated. Ready to save.</p>}
    </div>
  );
}
