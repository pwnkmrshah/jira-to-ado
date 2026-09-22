import React, { useState } from 'react';
import CredentialsBar from './components/CredentialsBar.jsx';
import AIMigrationTab from './tabs/AIMigrationTab.jsx';
import GapAnalysisTab from './tabs/GapAnalysisTab.jsx';
import VerifyTab from './tabs/VerifyTab.jsx';
import { loadCreds } from './lib/api.js';

const TABS = [
  { id: 'ai-migrate', label: 'AI Migration' },
  { id: 'gaps', label: 'Gap Analysis' },
  { id: 'verify', label: 'Verify Migration' },
];

export default function App() {
  const [activeTab, setActiveTab] = useState('ai-migrate');
  // Bumping this forces a re-render so `isReady` re-reads session storage.
  const [credsVersion, setCredsVersion] = useState(0);
  const [activeJobTab, setActiveJobTab] = useState(null);  // Track which tab has a running job
  const creds = loadCreds();
  const isReady = !!(creds.apiKey && creds.jiraUrl && creds.jiraEmail && creds.jiraToken);

  const onJobStart = (tabId) => setActiveJobTab(tabId);
  const onJobEnd = () => setActiveJobTab(null);

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
                disabled={activeJobTab && activeJobTab !== t.id}
                title={activeJobTab && activeJobTab !== t.id ? `Wait for ${TABS.find(x => x.id === activeJobTab)?.label} to finish` : ''}
              >
                {t.label}
                {activeJobTab === t.id && ' ⏳'}
              </button>
            ))}
          </nav>
          <main>
            {activeTab === 'ai-migrate' && <AIMigrationTab key={credsVersion} onJobStart={onJobStart} onJobEnd={onJobEnd} />}
            {activeTab === 'gaps' && <GapAnalysisTab key={credsVersion} onJobStart={onJobStart} onJobEnd={onJobEnd} />}
            {activeTab === 'verify' && <VerifyTab key={credsVersion} onJobStart={onJobStart} onJobEnd={onJobEnd} />}
          </main>
        </>
      ) : (
        <p className="hint">Fill in the connection settings above to get started.</p>
      )}
    </div>
  );
}
