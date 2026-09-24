/**
 * API Key Management
 * 
 * Handles API key retrieval based on environment:
 * - LOCAL (localhost): Uses simple development key from .env or default
 * - PRODUCTION (Azure/Render): Fetches from Azure Key Vault
 */

/**
 * Detect if running in local development mode
 * IMPORTANT: Must be port 10000 (backend), not 3000 (frontend)
 */
function isLocalEnvironment(apiBaseUrl) {
  if (!apiBaseUrl) return true; // Default to local
  // Check for localhost/127.0.0.1 on port 10000 specifically
  return (apiBaseUrl.includes('localhost:10000') || 
          apiBaseUrl.includes('127.0.0.1:10000') ||
          apiBaseUrl === 'http://localhost:10000' ||
          apiBaseUrl === 'http://127.0.0.1:10000');
}

/**
 * Get API key for local development
 * Stores it in localStorage for persistence across sessions
 */
function getLocalApiKey() {
  const storageKey = 'jira-ado-local-api-key';
  let storedKey = localStorage.getItem(storageKey);
  
  if (!storedKey) {
    // Default development key - same as in .env
    storedKey = 'dev-local-api-key-12345';
    // Optionally store it so user can override
    localStorage.setItem(storageKey, storedKey);
  }
  
  return storedKey;
}

/**
 * Fetch API key from Azure Key Vault (Production)
 * TODO: Implement in backend first
 * For now, just returns a message
 */
async function getAzureKeyVaultApiKey(apiBaseUrl) {
  try {
    // TODO: Implement backend endpoint /api/get-secret
    // For now, throw error to indicate not yet implemented
    throw new Error('Azure Key Vault integration not yet implemented. Ask user for API key.');
  } catch (error) {
    console.error('Error fetching API key from Azure Key Vault:', error);
    throw new Error('Could not retrieve API key from production secret manager');
  }
}

/**
 * Get the appropriate API key based on environment
 */
export async function getApiKey(apiBaseUrl) {
  if (isLocalEnvironment(apiBaseUrl)) {
    // Local dev: return immediately
    return getLocalApiKey();
  }
  
  // Production: fetch from Azure Key Vault (not yet implemented)
  return await getAzureKeyVaultApiKey(apiBaseUrl);
}

/**
 * Override local API key (for testing/development)
 */
export function setLocalApiKey(newKey) {
  const storageKey = 'jira-ado-local-api-key';
  localStorage.setItem(storageKey, newKey);
  return newKey;
}

/**
 * Clear stored API key
 */
export function clearLocalApiKey() {
  localStorage.removeItem('jira-ado-local-api-key');
}

/**
 * Check if API key is available in current environment
 */
export async function hasApiKey(apiBaseUrl) {
  try {
    const key = await getApiKey(apiBaseUrl);
    return !!key;
  } catch {
    return false;
  }
}
