import React, { useState } from 'react';
import CredentialsBar from './components/CredentialsBar.jsx';
import MigrateTab from './tabs/MigrateTab.jsx';
import GapAnalysisTab from './tabs/GapAnalysisTab.jsx';
import VerifyTab from './tabs/VerifyTab.jsx';
import { loadCreds } from './lib/api.js';

const TABS = [
  { id: 'migrate', label: 'Migrate' },
  { id: 'gaps', label: 'Gap Analysis' },
  { id: 'verify', label: 'Verify Migration' },
];

export default function App() {
  const [activeTab, setActiveTab] = useState('migrate');
  // Bumping this forces a re-render so `isReady` re-reads session storage.
  const [credsVersion, setCredsVersion] = useState(0);
  const creds = loadCreds();
  const isReady = !!(creds.apiKey && creds.jiraUrl && creds.jiraEmail && creds.jiraToken);

  return (
    <div className="app-shell">
      <header>
        <h1>Jira → Azure DevOps Migration</h1>
      </header>

      <CredentialsBar onSaved={() => setCredsVersion((v) => v + 1)} />

      {isReady ? (
        <>
          <nav className="tab-bar">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                className={activeTab === t.id ? 'active' : ''}
                onClick={() => setActiveTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </nav>
          <main>
            {activeTab === 'migrate' && <MigrateTab key={credsVersion} />}
            {activeTab === 'gaps' && <GapAnalysisTab key={credsVersion} />}
            {activeTab === 'verify' && <VerifyTab key={credsVersion} />}
          </main>
        </>
      ) : (
        <p className="hint">Fill in the connection settings above to get started.</p>
      )}
    </div>
  );
}
