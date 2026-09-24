const STORAGE_KEY = 'jira-ado-web-ui:creds';

export function loadCreds() {
  try {
    return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '{}');
  } catch {
    return {};
  }
}

export function saveCreds(creds) {
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(creds));
}

export function clearCreds() {
  sessionStorage.removeItem(STORAGE_KEY);
}

function baseUrl() {
  return loadCreds().apiBaseUrl || import.meta.env.VITE_API_BASE_URL || 'https://jira-to-ado.onrender.com';
}

async function request(path, { method = 'GET', body, query } = {}) {
  const creds = loadCreds();
  const url = new URL(path, baseUrl());
  if (query) {
    Object.entries(query).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
    });
  }

  const headers = { 'X-API-Key': creds.apiKey || '' };
  if (body) headers['Content-Type'] = 'application/json';

  const res = await fetch(url.toString(), {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (HTTP ${res.status})`);
  return data;
}

// Jira and ADO credentials are forwarded per-request (mirrors how Forge forwards them
// from its KVS storage) — the backend has no per-user session of its own.
function withCreds(payload) {
  const creds = loadCreds();
  return {
    ...payload,
    jira_url: creds.jiraUrl || '',
    jira_email: creds.jiraEmail || '',
    jira_token: creds.jiraToken || '',
    ado_org: creds.adoOrg || '',
    ado_pat: creds.adoPat || '',
  };
}

export const api = {
  health: () => request('/health'),
  adoProjects: () => request('/ado-projects'),
  adoProjectsWithCreds: (adoOrg, adoPat) => request('/ado-projects', { query: { ado_org: adoOrg, ado_pat: adoPat } }),
  adoBoards: (project) => request('/ado-boards', { query: { project } }),
  adoBoardsWithCreds: (adoOrg, adoPat, project) => request('/ado-boards', { query: { project, ado_org: adoOrg, ado_pat: adoPat } }),
  jiraProjects: () => {
    const creds = loadCreds();
    return request('/jira-projects', {
      query: {
        jira_url: creds.jiraUrl,
        jira_email: creds.jiraEmail,
        jira_token: creds.jiraToken,
      }
    });
  },
  jiraProjectsWithCreds: (jiraUrl, jiraEmail, jiraToken) => request('/jira-projects', {
    query: {
      jira_url: jiraUrl,
      jira_email: jiraEmail,
      jira_token: jiraToken,
    }
  }),
  jiraFilters: () => {
    const creds = loadCreds();
    return request('/jira-filters', {
      query: {
        jira_url: creds.jiraUrl,
        jira_email: creds.jiraEmail,
        jira_token: creds.jiraToken,
      }
    });
  },
  jiraFiltersWithCreds: (jiraUrl, jiraEmail, jiraToken, projectKey) => request('/jira-filters', {
    query: {
      jira_url: jiraUrl,
      jira_email: jiraEmail,
      jira_token: jiraToken,
      project_key: projectKey,
    }
  }),
  analyze: (payload) => request('/analyze', { method: 'POST', body: withCreds(payload) }),
  migrate: (payload) => request('/migrate', { method: 'POST', body: withCreds(payload) }),
  gaps: (payload) => request('/gaps', { method: 'POST', body: withCreds(payload) }),
  gapsBoard: (payload) => request('/gaps-board', { method: 'POST', body: withCreds(payload) }),
  verify: (payload) => request('/verify', { method: 'POST', body: withCreds(payload) }),
  status: (jobId) => request(`/status/${jobId}`),
  cancel: (jobId) => request(`/cancel/${jobId}`, { method: 'POST' }),
  csvDownloadUrl: (jobId) => {
    const creds = loadCreds();
    const url = new URL(`/csv/${jobId}`, baseUrl());
    url.searchParams.set('key', creds.apiKey || '');
    return url.toString();
  },
};
