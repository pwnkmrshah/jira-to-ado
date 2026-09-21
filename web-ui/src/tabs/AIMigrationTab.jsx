import React, { useState } from 'react';
import { api, loadCreds } from '../lib/api.js';

export default function AIMigrationTab() {
  const creds = loadCreds();
  const [jiraProjectKey, setJiraProjectKey] = useState('');
  const [adoOrg, setAdoOrg] = useState('');
  const [adoProject, setAdoProject] = useState(creds.adoProject || '');
  const [statusFilter, setStatusFilter] = useState('');
  const [fieldFilter, setFieldFilter] = useState('');
  const [analysisResult, setAnalysisResult] = useState(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [analysisError, setAnalysisError] = useState('');

  const canAnalyze = !isAnalyzing && jiraProjectKey.trim() && adoOrg.trim() && adoProject.trim();

  const handleAnalyze = async () => {
    setIsAnalyzing(true);
    setAnalysisError('');
    setAnalysisResult(null);
    try {
      const result = await api.analyze({
        jira_project_key: jiraProjectKey.trim(),
        ado_org: adoOrg.trim(),
        ado_project: adoProject.trim(),
        status_filter: statusFilter.trim() ? statusFilter.split(',').map(s => s.trim()) : [],
        field_filter: fieldFilter.trim() ? fieldFilter.split(',').map(s => s.trim()) : [],
      });
      setAnalysisResult(result);
    } catch (err) {
      setAnalysisError(err.message);
    } finally {
      setIsAnalyzing(false);
    }
  };

  return (
    <section className="tab-panel">
      <h2>AI Migration Analysis</h2>
      <p className="hint">Analyze your Jira project to identify migration gaps and opportunities.</p>

      <div className="field-row">
        <label htmlFor="ai-jira-project">Jira Project Key</label>
        <input 
          id="ai-jira-project" 
          value={jiraProjectKey} 
          onChange={(e) => setJiraProjectKey(e.target.value)} 
          placeholder="e.g. TM, PROJ" 
        />
      </div>

      <div className="field-row">
        <label htmlFor="ai-ado-org">ADO Organization</label>
        <input 
          id="ai-ado-org" 
          value={adoOrg} 
          onChange={(e) => setAdoOrg(e.target.value)} 
          placeholder="e.g. your-org" 
        />
      </div>

      <div className="field-row">
        <label htmlFor="ai-ado-project">ADO Project</label>
        <input 
          id="ai-ado-project" 
          value={adoProject} 
          onChange={(e) => setAdoProject(e.target.value)} 
          placeholder="Embedded Refills Engineering" 
        />
      </div>

      <div className="field-row">
        <label htmlFor="ai-status-filter">Status Filter (comma-separated, optional)</label>
        <input 
          id="ai-status-filter" 
          value={statusFilter} 
          onChange={(e) => setStatusFilter(e.target.value)} 
          placeholder="e.g. Open, In Progress, Done" 
        />
      </div>

      <div className="field-row">
        <label htmlFor="ai-field-filter">Field Filter (comma-separated, optional)</label>
        <input 
          id="ai-field-filter" 
          value={fieldFilter} 
          onChange={(e) => setFieldFilter(e.target.value)} 
          placeholder="e.g. labels, components, customfield_" 
        />
      </div>

      <button 
        type="button" 
        className="primary" 
        disabled={!canAnalyze} 
        onClick={handleAnalyze}
      >
        {isAnalyzing ? 'Analyzing…' : 'Analyze Project'}
      </button>

      {analysisError && <p className="status-fail">❌ {analysisError}</p>}

      {analysisResult && (
        <div className="analysis-results">
          <h3>Analysis Results</h3>
          <div className="result-grid">
            <div className="result-item">
              <label>Total Issues</label>
              <span>{analysisResult.total_issues}</span>
            </div>
            {analysisResult.by_type && Object.entries(analysisResult.by_type).map(([type, count]) => (
              <div key={type} className="result-item">
                <label>{type}</label>
                <span>{count}</span>
              </div>
            ))}
          </div>

          {analysisResult.attachment_count !== undefined && (
            <p>📎 Attachments: {analysisResult.attachment_count}</p>
          )}

          {analysisResult.comment_count !== undefined && (
            <p>💬 Comments: {analysisResult.comment_count}</p>
          )}

          {analysisResult.type_gaps && Object.keys(analysisResult.type_gaps).length > 0 && (
            <div className="gaps-section">
              <h4>⚠️ Type Gaps (Jira types not in ADO)</h4>
              <ul>
                {Object.entries(analysisResult.type_gaps).map(([jiraType, adoMapping]) => (
                  <li key={jiraType}>{jiraType} → {adoMapping || 'No mapping'}</li>
                ))}
              </ul>
            </div>
          )}

          {analysisResult.user_gaps && Object.keys(analysisResult.user_gaps).length > 0 && (
            <div className="gaps-section">
              <h4>⚠️ User Gaps (Jira users not in ADO)</h4>
              <ul>
                {Object.entries(analysisResult.user_gaps).map(([jiraUser, adoMapping]) => (
                  <li key={jiraUser}>{jiraUser} → {adoMapping || 'No mapping'}</li>
                ))}
              </ul>
            </div>
          )}

          <p className="hint">Review the analysis results above before proceeding with migration.</p>
        </div>
      )}
    </section>
  );
}
