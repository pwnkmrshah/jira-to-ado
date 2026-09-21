import React, { useState, useEffect } from 'react';
import { api, loadCreds } from '../lib/api.js';
import { useJob } from '../hooks/useJob.js';
import JobStatusPanel from '../components/JobStatusPanel.jsx';

const MODES = [
  { id: 'filter', label: 'Jira Filter' },
  { id: 'keys', label: 'Specific Jira Keys (comma-separated)' },
];

export default function MigrateTab() {
  const creds = loadCreds();
  const [mode, setMode] = useState('filter');
  
  // Jira projects
  const [jiraProjects, setJiraProjects] = useState([]);
  const [selectedJiraProject, setSelectedJiraProject] = useState('');
  const [loadingJiraProjects, setLoadingJiraProjects] = useState(false);
  
  // Jira filters
  const [jiraFilters, setJiraFilters] = useState([]);
  const [selectedFilterId, setSelectedFilterId] = useState('');
  const [loadingFilters, setLoadingFilters] = useState(false);
  
  // Jira keys (manual)
  const [keys, setKeys] = useState('');
  
  // ADO projects
  const [adoProjects, setAdoProjects] = useState([]);
  const [selectedAdoProject, setSelectedAdoProject] = useState(creds.adoProject || '');
  const [loadingAdoProjects, setLoadingAdoProjects] = useState(false);
  
  const [skipAttachments, setSkipAttachments] = useState(false);
  const { job, jobId, startError, isRunning, start } = useJob();

  // Fetch Jira projects on mount
  useEffect(() => {
    const fetchProjects = async () => {
      setLoadingJiraProjects(true);
      try {
        const result = await api.jiraProjectsWithCreds(creds.jiraUrl, creds.jiraEmail, creds.jiraToken);
        setJiraProjects(result.projects || []);
      } catch (err) {
        console.error('Failed to fetch Jira projects:', err);
      } finally {
        setLoadingJiraProjects(false);
      }
    };
    fetchProjects();
  }, []);

  // Fetch Jira filters when project is selected
  useEffect(() => {
    if (!selectedJiraProject || mode !== 'filter') {
      setJiraFilters([]);
      setSelectedFilterId('');
      return;
    }

    const fetchFilters = async () => {
      setLoadingFilters(true);
      try {
        const result = await api.jiraFiltersWithCreds(
          creds.jiraUrl, 
          creds.jiraEmail, 
          creds.jiraToken,
          selectedJiraProject
        );
        setJiraFilters(result.filters || []);
      } catch (err) {
        console.error('Failed to fetch Jira filters:', err);
      } finally {
        setLoadingFilters(false);
      }
    };
    fetchFilters();
  }, [selectedJiraProject, mode, creds.jiraUrl, creds.jiraEmail, creds.jiraToken]);

  // Fetch ADO projects on mount
  useEffect(() => {
    const fetchAdoProjects = async () => {
      setLoadingAdoProjects(true);
      try {
        const result = await api.adoProjectsWithCreds(creds.adoOrg, creds.adoPat);
        setAdoProjects(result.projects || []);
      } catch (err) {
        console.error('Failed to fetch ADO projects:', err);
      } finally {
        setLoadingAdoProjects(false);
      }
    };
    fetchAdoProjects();
  }, []);

  const hasInput = mode === 'filter' ? selectedFilterId : keys.trim();
  const canRun = !isRunning && selectedAdoProject && selectedJiraProject && hasInput && !loadingFilters && !loadingAdoProjects && !loadingJiraProjects;

  const handleRun = () => {
    start(() => api.migrate({
      ado_project: selectedAdoProject,
      jira_filter: mode === 'filter' ? selectedFilterId : '',
      jira_keys: mode === 'keys' ? keys.trim() : '',
      skip_attachments: skipAttachments,
    }));
  };

  return (
    <section className="tab-panel">
      <h2>📋 Manual Migration</h2>
      <p className="hint">Migrate specific Jira issues using a saved filter or individual keys.</p>
      
      <div className="field-row">
        <label htmlFor="migrate-mode">Source</label>
        <select id="migrate-mode" value={mode} onChange={(e) => setMode(e.target.value)}>
          {MODES.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
        </select>
      </div>
      
      <div className="field-row">
        <label htmlFor="migrate-jira-project">Jira Project *</label>
        {loadingJiraProjects ? (
          <select disabled>
            <option>Loading Jira projects...</option>
          </select>
        ) : jiraProjects.length > 0 ? (
          <select 
            id="migrate-jira-project" 
            value={selectedJiraProject} 
            onChange={(e) => {
              setSelectedJiraProject(e.target.value);
              setSelectedFilterId('');
            }}
          >
            <option value="">Select a Jira project...</option>
            {jiraProjects.map((p) => (
              <option key={p.key} value={p.key}>{p.name} ({p.key})</option>
            ))}
          </select>
        ) : (
          <select disabled>
            <option>No Jira projects available</option>
          </select>
        )}
      </div>
      
      {mode === 'filter' ? (
        <div className="field-row">
          <label htmlFor="migrate-filter">Jira Filter *</label>
          {loadingFilters ? (
            <select disabled>
              <option>Loading Jira filters...</option>
            </select>
          ) : jiraFilters.length > 0 ? (
            <select 
              id="migrate-filter" 
              value={selectedFilterId} 
              onChange={(e) => setSelectedFilterId(e.target.value)}
            >
              <option value="">Select a filter...</option>
              {jiraFilters.map((f) => (
                <option key={f.id} value={f.id}>{f.name}</option>
              ))}
            </select>
          ) : (
            <select disabled>
              <option>No Jira filters available</option>
            </select>
          )}
        </div>
      ) : (
        <div className="field-row">
          <label htmlFor="migrate-keys">Jira Keys *</label>
          <input 
            id="migrate-keys" 
            value={keys} 
            onChange={(e) => setKeys(e.target.value)} 
            placeholder="TM-1, TM-2, TM-3" 
          />
          <small>Comma-separated list of Jira issue keys</small>
        </div>
      )}
      
      <div className="field-row">
        <label htmlFor="migrate-ado-project">ADO Project *</label>
        {loadingAdoProjects ? (
          <select disabled>
            <option>Loading ADO projects...</option>
          </select>
        ) : adoProjects.length > 0 ? (
          <select 
            id="migrate-ado-project" 
            value={selectedAdoProject} 
            onChange={(e) => setSelectedAdoProject(e.target.value)}
          >
            <option value="">Select ADO project...</option>
            {adoProjects.map((p) => (
              <option key={p.id} value={p.name}>{p.name}</option>
            ))}
          </select>
        ) : (
          <select disabled>
            <option>No ADO projects available</option>
          </select>
        )}
      </div>
      
      <label className="checkbox-row">
        <input 
          type="checkbox" 
          checked={skipAttachments} 
          onChange={(e) => setSkipAttachments(e.target.checked)} 
        />
        Skip attachments
      </label>
      
      <button 
        type="button" 
        className="primary" 
        disabled={!canRun} 
        onClick={handleRun}
      >
        {isRunning ? '⏳ Migrating…' : '🚀 Start Migration'}
      </button>
      
      {startError && <p className="status-fail">❌ {startError}</p>}
      <JobStatusPanel job={job} jobId={jobId} />
    </section>
  );
}
