import React, { useState, useEffect } from 'react';
import { api, loadCreds } from '../lib/api.js';

export default function AIMigrationTab() {
  const creds = loadCreds();
  const [adoProjects, setAdoProjects] = useState([]);
  const [selectedAdoProject, setSelectedAdoProject] = useState(creds.adoProject || '');
  const [adoOrg, setAdoOrg] = useState('');
  const [loadingProjects, setLoadingProjects] = useState(false);
  
  const [jiraProjectKey, setJiraProjectKey] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [fieldFilter, setFieldFilter] = useState('');
  
  const [analysisResult, setAnalysisResult] = useState(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [analysisError, setAnalysisError] = useState('');

  // Fetch ADO projects on mount
  useEffect(() => {
    const fetchAdoProjects = async () => {
      setLoadingProjects(true);
      try {
        const result = await api.adoProjects();
        setAdoProjects(result.projects || []);
      } catch (err) {
        console.error('Failed to fetch ADO projects:', err);
        setAnalysisError('Could not fetch ADO projects. Check your connection settings.');
      } finally {
        setLoadingProjects(false);
      }
    };
    fetchAdoProjects();
  }, []);

  const canAnalyze = !isAnalyzing && jiraProjectKey.trim() && selectedAdoProject.trim();

  const handleAnalyze = async () => {
    setIsAnalyzing(true);
    setAnalysisError('');
    setAnalysisResult(null);
    try {
      const result = await api.analyze({
        jira_project_key: jiraProjectKey.trim(),
        ado_project: selectedAdoProject.trim(),
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
      <h2>🚀 Start AI Migration</h2>
      <p className="hint">Choose your source, scope, and destination. We'll analyze the migration before anything is changed.</p>

      {/* SOURCE SECTION */}
      <div className="form-section">
        <h3>SOURCE</h3>
        <div className="field-row">
          <label htmlFor="ai-jira-project">Jira Board *</label>
          <input 
            id="ai-jira-project" 
            type="text"
            value={jiraProjectKey} 
            onChange={(e) => setJiraProjectKey(e.target.value)} 
            placeholder="Enter Jira project key (e.g., TM, PROJ)" 
          />
          <small>Enter the Jira project key to analyze</small>
        </div>
      </div>

      {/* SCOPE SECTION */}
      <div className="form-section">
        <h3>SCOPE</h3>
        <div className="field-row">
          <label>Migration Scope *</label>
          <select defaultValue="entire-board" disabled>
            <option value="entire-board">Entire board</option>
          </select>
          <small>All issues from the selected Jira project</small>
        </div>
      </div>

      {/* TARGET SECTION */}
      <div className="form-section">
        <h3>TARGET</h3>
        <div className="field-row">
          <label htmlFor="ai-ado-project">Azure DevOps Project *</label>
          {loadingProjects ? (
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
        </div>
      </div>

      {/* OPTIONAL FILTERS */}
      <div className="form-section">
        <h3>OPTIONAL FILTERS</h3>
        <div className="field-row">
          <label htmlFor="ai-status-filter">Status Filter (comma-separated)</label>
          <input 
            id="ai-status-filter" 
            value={statusFilter} 
            onChange={(e) => setStatusFilter(e.target.value)} 
            placeholder="e.g. Open, In Progress, Done" 
          />
          <small>Leave empty to include all statuses</small>
        </div>

        <div className="field-row">
          <label htmlFor="ai-field-filter">Field Filter (comma-separated)</label>
          <input 
            id="ai-field-filter" 
            value={fieldFilter} 
            onChange={(e) => setFieldFilter(e.target.value)} 
            placeholder="e.g. labels, components" 
          />
          <small>Leave empty to include all fields</small>
        </div>
      </div>

      {/* INFO BOX */}
      <div className="info-box">
        <span className="info-icon">ℹ️</span>
        <div>
          <strong>What AI will analyze</strong>
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
        disabled={!canAnalyze || loadingProjects} 
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
