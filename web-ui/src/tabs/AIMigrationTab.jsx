import React, { useState, useEffect } from 'react';
import { api, loadCreds } from '../lib/api.js';

export default function AIMigrationTab() {
  const creds = loadCreds();
  
  // Jira projects state
  const [jiraProjects, setJiraProjects] = useState([]);
  const [selectedJiraProject, setSelectedJiraProject] = useState('');
  const [loadingJiraProjects, setLoadingJiraProjects] = useState(false);
  
  // Jira filter ID (manual text input, not dropdown)
  const [filterId, setFilterId] = useState('');
  
  // ADO projects state
  const [adoProjects, setAdoProjects] = useState([]);
  const [selectedAdoProject, setSelectedAdoProject] = useState(creds.adoProject || '');
  const [loadingAdoProjects, setLoadingAdoProjects] = useState(false);
  
  // Scope and optional inputs
  const [scopeType, setScopeType] = useState('entire-board'); // entire-board, filter, specific-issues
  const [issueKeys, setIssueKeys] = useState(''); // For specific issues
  
  // Analysis state
  const [analysisResult, setAnalysisResult] = useState(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [analysisError, setAnalysisError] = useState('');

  // Fetch Jira projects on mount
  useEffect(() => {
    const fetchJiraProjects = async () => {
      setLoadingJiraProjects(true);
      try {
        const result = await api.jiraProjects();
        setJiraProjects(result.projects || []);
      } catch (err) {
        console.error('Failed to fetch Jira projects:', err);
        setAnalysisError('Could not fetch Jira projects. Check your connection settings.');
      } finally {
        setLoadingJiraProjects(false);
      }
    };
    fetchJiraProjects();
  }, []);

  // Fetch ADO projects on mount
  useEffect(() => {
    const fetchAdoProjects = async () => {
      setLoadingAdoProjects(true);
      try {
        const result = await api.adoProjectsWithCreds(creds.adoOrg, creds.adoPat);
        setAdoProjects(result.projects || []);
      } catch (err) {
        console.error('Failed to fetch ADO projects:', err);
        setAnalysisError('Could not fetch ADO projects. Check your connection settings.');
      } finally {
        setLoadingAdoProjects(false);
      }
    };
    fetchAdoProjects();
  }, []);

  const canAnalyze = () => {
    if (isAnalyzing || !selectedJiraProject || !selectedAdoProject || loadingJiraProjects || loadingAdoProjects) {
      return false;
    }
    // Check based on scope type
    if (scopeType === 'filter') {
      return filterId.trim() !== '';
    }
    if (scopeType === 'specific-issues') {
      return issueKeys.trim() !== '';
    }
    return true; // entire-board doesn't need additional selection
  };

  const handleAnalyze = async () => {
    setIsAnalyzing(true);
    setAnalysisError('');
    setAnalysisResult(null);
    try {
      // Build analysis payload based on scope type
      const payload = {
        jira_project_key: selectedJiraProject, // ALWAYS required
        ado_project: selectedAdoProject,
        ado_org: creds.adoOrg,
      };

      // Add optional scope parameters
      if (scopeType === 'filter') {
        payload.jira_filter_id = filterId.trim();
      } else if (scopeType === 'specific-issues') {
        payload.jira_keys = issueKeys.split(',').map(k => k.trim());
      }

      const result = await api.analyze(payload);
      setAnalysisResult(result);
    } catch (err) {
      setAnalysisError(err.message);
    } finally {
      setIsAnalyzing(false);
    }
  };

  return (
    <section className="tab-panel">
      <h2>🚀 Start AI Migration</h2>
      <p className="hint">Choose your source, scope, and destination. We'll analyze the migration before anything is changed.</p>

      {/* SOURCE SECTION */}
      <div className="form-section">
        <h3>SOURCE</h3>
        <div className="field-row">
          <label htmlFor="ai-jira-project">Jira Board *</label>
          {loadingJiraProjects ? (
            <select disabled>
              <option>Loading Jira projects...</option>
            </select>
          ) : jiraProjects.length > 0 ? (
            <select 
              id="ai-jira-project" 
              value={selectedJiraProject} 
              onChange={(e) => setSelectedJiraProject(e.target.value)}
            >
              <option value="">Select a Jira project...</option>
              {jiraProjects.map(project => (
                <option key={project.id} value={project.key}>
                  {project.name} ({project.key})
                </option>
              ))}
            </select>
          ) : (
            <select disabled>
              <option>No Jira projects available</option>
            </select>
          )}
          <small>Select the Jira project to migrate from</small>
        </div>
      </div>

      {/* SCOPE SECTION */}
      <div className="form-section">
        <h3>SCOPE</h3>
        <div className="field-row">
          <label htmlFor="ai-scope">Migration Scope *</label>
          <select 
            id="ai-scope" 
            value={scopeType} 
            onChange={(e) => {
              setScopeType(e.target.value);
              setFilterId('');
              setIssueKeys('');
            }}
            disabled={!selectedJiraProject}
          >
            <option value="entire-board">Entire board</option>
            <option value="filter">Jira saved filter</option>
            <option value="specific-issues">Specific issues</option>
          </select>
          <small>Choose how many issues to include in the analysis</small>
        </div>

        {/* Filter selector when "filter" scope is selected */}
        {scopeType === 'filter' && (
          <div className="field-row">
            <label htmlFor="ai-jira-filter-id">Jira Filter ID *</label>
            <input 
              id="ai-jira-filter-id" 
              value={filterId} 
              onChange={(e) => setFilterId(e.target.value)} 
              placeholder="e.g. 10000" 
            />
            <small>Enter the Jira filter ID (found in filter URL: .../browse?jql=...&filterId=XXXXX)</small>
          </div>
        )}

        {/* Issue keys input when "specific-issues" scope is selected */}
        {scopeType === 'specific-issues' && (
          <div className="field-row">
            <label htmlFor="ai-issue-keys">🔥 Issue Keys * (comma-separated)</label>
            <input 
              id="ai-issue-keys" 
              value={issueKeys} 
              onChange={(e) => setIssueKeys(e.target.value)} 
              placeholder="e.g. PROJ-101, PROJ-102, PROJ-103" 
            />
            <small>Enter specific issue keys to analyze</small>
          </div>
        )}
      </div>

      {/* TARGET SECTION */}
      <div className="form-section">
        <h3>TARGET</h3>
        <div className="field-row">
          <label htmlFor="ai-ado-project">Azure DevOps Project *</label>
          {loadingAdoProjects ? (
            <select disabled>
              <option>Loading ADO projects...</option>
            </select>
          ) : adoProjects.length > 0 ? (
            <select 
              id="ai-ado-project" 
              value={selectedAdoProject} 
              onChange={(e) => setSelectedAdoProject(e.target.value)}
            >
              <option value="">Select ADO project...</option>
              {adoProjects.map(project => (
                <option key={project.id} value={project.name}>{project.name}</option>
              ))}
            </select>
          ) : (
            <select disabled>
              <option>No ADO projects available</option>
            </select>
          )}
          <small>Select the target Azure DevOps project</small>
        </div>
      </div>

      {/* INFO BOX */}
      <div className="info-box">
        <span className="info-icon">ℹ️</span>
        <div>
          <strong>✨ What AI will analyze</strong>
          <ul>
            <li>Issue types and field mappings</li>
            <li>User and status compatibility</li>
            <li>Custom fields and attachments</li>
            <li>Migration gaps and risks</li>
          </ul>
        </div>
      </div>

      {/* ACTION BUTTON */}
      <button 
        type="button" 
        className="primary" 
        disabled={!canAnalyze()} 
        onClick={handleAnalyze}
      >
        {isAnalyzing ? '🔄 Analyzing…' : '✨ Analyze & Plan Migration'}
      </button>

      {analysisError && <p className="status-fail">❌ {analysisError}</p>}

      {/* RESULTS */}
      {analysisResult && (
        <div className="analysis-results">
          <h3>📊 Analysis Results</h3>
          <div className="result-grid">
            <div className="result-item">
              <label>Total Issues</label>
              <span className="big-number">{analysisResult.total_issues}</span>
            </div>
            {analysisResult.by_type && Object.entries(analysisResult.by_type).map(([type, count]) => (
              <div key={type} className="result-item">
                <label>{type}</label>
                <span>{count}</span>
              </div>
            ))}
          </div>

          {analysisResult.attachment_count !== undefined && (
            <p>📎 Attachments: <strong>{analysisResult.attachment_count}</strong></p>
          )}

          {analysisResult.comment_count !== undefined && (
            <p>💬 Comments: <strong>{analysisResult.comment_count}</strong></p>
          )}

          {analysisResult.type_gaps && Object.keys(analysisResult.type_gaps).length > 0 && (
            <div className="gaps-section warning">
              <h4>⚠️ Type Gaps</h4>
              <p>Jira issue types that need mapping to ADO:</p>
              <ul>
                {Object.entries(analysisResult.type_gaps).map(([jiraType, adoMapping]) => (
                  <li key={jiraType}><strong>{jiraType}</strong> → {adoMapping || '❌ No mapping'}</li>
                ))}
              </ul>
            </div>
          )}

          {analysisResult.user_gaps && Object.keys(analysisResult.user_gaps).length > 0 && (
            <div className="gaps-section warning">
              <h4>⚠️ User Gaps</h4>
              <p>Jira users that need to be added to ADO:</p>
              <ul>
                {Object.entries(analysisResult.user_gaps).slice(0, 10).map(([jiraUser, adoMapping]) => (
                  <li key={jiraUser}><strong>{jiraUser}</strong> → {adoMapping || '❌ Not in ADO'}</li>
                ))}
                {Object.keys(analysisResult.user_gaps).length > 10 && (
                  <li>... and {Object.keys(analysisResult.user_gaps).length - 10} more users</li>
                )}
              </ul>
            </div>
          )}

          <p className="hint">✅ Review the analysis above. Once gaps are resolved, you can proceed with migration.</p>
        </div>
      )}
    </section>
  );
}
