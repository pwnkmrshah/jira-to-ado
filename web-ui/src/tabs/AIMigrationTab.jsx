import React, { useState, useEffect } from 'react';
import { api, loadCreds } from '../lib/api.js';
import { useJob } from '../hooks/useJob.js';
import JobStatusPanel from '../components/JobStatusPanel.jsx';

export default function AIMigrationTab({ onJobStart, onJobEnd }) {
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
  
  // Mapping selections - user can change these
  const [typeMappingSelections, setTypeMappingSelections] = useState({});
  const [stateMappingSelections, setStateMappingSelections] = useState({});
  
  // Migration state - use useJob hook for proper status tracking
  const { job: migrationJob, jobId: migrationJobId, startError: migrationError, isRunning: isMigrating, start: startMigration } = useJob();

  // Notify parent when analysis starts/ends
  useEffect(() => {
    if (isAnalyzing) {
      onJobStart?.('ai-migrate');
    } else if (analysisResult) {
      onJobEnd?.();
    }
  }, [isAnalyzing, analysisResult, onJobStart, onJobEnd]);

  // Notify parent when migration starts/ends
  useEffect(() => {
    if (isMigrating) {
      onJobStart?.('ai-migrate');
    } else if (migrationJobId && migrationJob && ['completed', 'failed', 'warning'].includes(migrationJob.status)) {
      onJobEnd?.();
    }
  }, [isMigrating, migrationJobId, migrationJob, onJobStart, onJobEnd]);

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

      console.log('[ANALYZE] Sending payload:', payload);
      const result = await api.analyze(payload);
      console.log('[ANALYZE] Response received:', result);
      
      if (!result) {
        throw new Error('Empty response from analysis');
      }
      
      // Initialize mapping selections from analysis result
      const typeSelections = {};
      if (Array.isArray(result.type_mappings)) {
        result.type_mappings.forEach((m) => {
          typeSelections[m.jira] = m.ado;
        });
      }
      
      const stateSelections = {};
      if (Array.isArray(result.state_mappings)) {
        result.state_mappings.forEach((m) => {
          stateSelections[m.jira] = m.ado;
        });
      }
      
      setTypeMappingSelections(typeSelections);
      setStateMappingSelections(stateSelections);
      setAnalysisResult(result);
    } catch (err) {
      console.error('[ANALYZE] Error:', err);
      setAnalysisError(err.message || 'Analysis failed');
    } finally {
      setIsAnalyzing(false);
    }
  };

  const handleProceedToMigration = () => {
    console.log('[MIGRATE] Starting migration with mappings:', {
      types: typeMappingSelections,
      states: stateMappingSelections
    });

    // Build migration payload
    const payload = {
      jira_project_key: selectedJiraProject,
      ado_project: selectedAdoProject,
      type_mappings: typeMappingSelections,
      state_mappings: stateMappingSelections,
      // Include credentials for worker
      jira_url: creds.jiraUrl,
      jira_email: creds.jiraEmail,
      jira_token: creds.jiraToken,
      ado_org: creds.adoOrg,
      ado_pat: creds.adoPat,
    };

    // Add scope parameters (backend requires at least one: jira_filter, jira_keys, or jql)
    if (scopeType === 'filter' && filterId.trim()) {
      payload.jira_filter = filterId.trim();
      console.log('[MIGRATE] Using filter:', filterId);
    } else if (scopeType === 'specific-issues' && issueKeys.trim()) {
      payload.jira_keys = issueKeys.split(',').map(k => k.trim()).filter(k => k);
      console.log('[MIGRATE] Using specific issues:', payload.jira_keys);
    } else if (scopeType === 'entire-board' && analysisResult?.jql_used) {
      // For entire board, use the JQL from analysis
      payload.jql = analysisResult.jql_used;
      console.log('[MIGRATE] Using entire board JQL:', payload.jql);
    } else {
      console.error('[MIGRATE] No migration scope provided');
      alert('No migration scope provided. Please select a filter, specific issues, or analyze the entire board.');
      return;
    }

    console.log('[MIGRATE] Sending payload:', payload);
    startMigration(() => api.migrate(payload));
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

          {/* SUMMARY CARDS */}
          <div className="result-grid">
            <div className="result-item">
              <label>Total Issues</label>
              <span className="big-number">{analysisResult.total_issues}</span>
            </div>
            {analysisResult.attachment_count !== undefined && (
              <div className="result-item">
                <label>Attachments</label>
                <span className="big-number">{analysisResult.attachment_count}</span>
              </div>
            )}
            {analysisResult.comment_count !== undefined && (
              <div className="result-item">
                <label>Comments</label>
                <span className="big-number">{analysisResult.comment_count}</span>
              </div>
            )}
          </div>

          {/* BY TYPE BREAKDOWN */}
          {Array.isArray(analysisResult.by_type) && analysisResult.by_type.length > 0 && (
            <div className="section">
              <h4>📋 Issue Types</h4>
              <table className="result-table">
                <thead>
                  <tr><th>Type</th><th>Count</th></tr>
                </thead>
                <tbody>
                  {analysisResult.by_type.map((item, idx) => (
                    <tr key={`type-${idx}`}>
                      <td>{item.name}</td>
                      <td>{item.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* BY STATUS BREAKDOWN */}
          {Array.isArray(analysisResult.by_status) && analysisResult.by_status.length > 0 && (
            <div className="section">
              <h4>🔄 Issue Statuses</h4>
              <table className="result-table">
                <thead>
                  <tr><th>Status</th><th>Count</th></tr>
                </thead>
                <tbody>
                  {analysisResult.by_status.map((item, idx) => (
                    <tr key={`status-${idx}`}>
                      <td>{item.name}</td>
                      <td>{item.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* TYPE MAPPINGS TABLE - EDITABLE */}
          {Array.isArray(analysisResult.type_mappings) && analysisResult.type_mappings.length > 0 && (
            <div className="section">
              <h4>🔗 Type Mappings (Jira → Azure DevOps)</h4>
              <p className="hint">Click to change how each Jira issue type will be migrated:</p>
              <table className="mapping-table">
                <thead>
                  <tr>
                    <th>Jira Type</th>
                    <th>Count</th>
                    <th>ADO Type (Editable)</th>
                    <th>Confidence</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {analysisResult.type_mappings.map((m, idx) => {
                    const allAdoTypes = analysisResult.ado_available_types || ['Bug', 'Task', 'Epic', 'Feature', 'Issue', 'User Story'];
                    return (
                      <tr key={`tm-${idx}`}>
                        <td><strong>{m.jira}</strong></td>
                        <td>{m.count}</td>
                        <td>
                          <select 
                            value={typeMappingSelections[m.jira] || m.ado || ''}
                            onChange={(e) => setTypeMappingSelections({
                              ...typeMappingSelections,
                              [m.jira]: e.target.value
                            })}
                            style={{padding: '4px', borderRadius: '4px'}}
                          >
                            <option value="">(select type)</option>
                            {allAdoTypes.map(t => (
                              <option key={t} value={t}>{t}</option>
                            ))}
                          </select>
                        </td>
                        <td><span className="confidence-badge" style={{backgroundColor: m.confidence >= 90 ? '#4CAF50' : m.confidence >= 70 ? '#FFC107' : '#F44336'}}>{m.confidence}%</span></td>
                        <td className="reason-text">{m.reason}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* STATE MAPPINGS TABLE - EDITABLE */}
          {Array.isArray(analysisResult.state_mappings) && analysisResult.state_mappings.length > 0 && (
            <div className="section">
              <h4>🔗 State Mappings (Jira → Azure DevOps)</h4>
              <p className="hint">Click to change how each Jira status will be migrated:</p>
              <table className="mapping-table">
                <thead>
                  <tr>
                    <th>Jira Status</th>
                    <th>Count</th>
                    <th>ADO State (Editable)</th>
                    <th>Confidence</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {analysisResult.state_mappings.map((m, idx) => {
                    const allAdoStates = analysisResult.ado_available_states || ['New', 'Active', 'Resolved', 'Completed', 'Removed', 'Code Review', 'QA Review'];
                    return (
                      <tr key={`sm-${idx}`}>
                        <td><strong>{m.jira}</strong></td>
                        <td>{m.count}</td>
                        <td>
                          <select 
                            value={stateMappingSelections[m.jira] || m.ado || ''}
                            onChange={(e) => setStateMappingSelections({
                              ...stateMappingSelections,
                              [m.jira]: e.target.value
                            })}
                            style={{padding: '4px', borderRadius: '4px'}}
                          >
                            <option value="">(select state)</option>
                            {allAdoStates.map(s => (
                              <option key={s} value={s}>{s}</option>
                            ))}
                          </select>
                        </td>
                        <td><span className="confidence-badge" style={{backgroundColor: m.confidence >= 90 ? '#4CAF50' : m.confidence >= 70 ? '#FFC107' : '#F44336'}}>{m.confidence}%</span></td>
                        <td className="reason-text">{m.reason}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* TYPE GAPS - WARNING */}
          {Array.isArray(analysisResult.type_gaps) && analysisResult.type_gaps.length > 0 && (
            <div className="section warning">
              <h4>⚠️ Type Gaps - REQUIRES REVIEW</h4>
              <p>These Jira types have no ADO equivalent:</p>
              <ul>
                {analysisResult.type_gaps.map((g, idx) => (
                  <li key={`tgap-${idx}`}><strong>{g.jira_type}</strong></li>
                ))}
              </ul>
            </div>
          )}

          {/* USER GAPS - WARNING */}
          {Array.isArray(analysisResult.user_gaps) && analysisResult.user_gaps.length > 0 && (
            <div className="section warning">
              <h4>👥 User Gaps - ACTION NEEDED</h4>
              <p>These users are not in ADO:</p>
              <ul>
                {analysisResult.user_gaps.slice(0, 20).map((u, idx) => (
                  <li key={`ugap-${idx}`}>{u.jira_user}</li>
                ))}
                {analysisResult.user_gaps.length > 20 && <li>... and {analysisResult.user_gaps.length - 20} more</li>}
              </ul>
            </div>
          )}

          {analysisResult.jql_used && (
            <div className="section info">
              <h4>🔍 JQL Query</h4>
              <code className="jql-code">{analysisResult.jql_used}</code>
            </div>
          )}

          <p className="hint">✅ Review mappings and gaps above before proceeding with migration.</p>
          
          <button 
            type="button" 
            className="primary" 
            disabled={!analysisResult || isMigrating}
            onClick={handleProceedToMigration}
          >
            {isMigrating ? '🔄 Migrating...' : '🚀 Proceed to Migration'}
          </button>
        </div>
      )}

      {migrationJobId && (
        <JobStatusPanel
          job={migrationJob}
          jobId={migrationJobId}
        />
      )}

      {migrationError && (
        <p className="status-fail">❌ {migrationError}</p>
      )}
    </section>
  );
}
