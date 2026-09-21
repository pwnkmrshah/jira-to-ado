# Jira → ADO Migration — Standalone Web UI

A plain React SPA that talks directly to the existing Flask backend
(`api_server.py`, deployed on Render). No Jira/Atlassian install required —
this is for teams whose Jira admins won't approve installing the Forge app.

It calls the exact same endpoints the Forge app uses (`/migrate`, `/gaps`,
`/gaps-board`, `/verify`, `/ado-projects`, `/ado-boards`), so no backend
logic is duplicated.

## Run locally

```bash
cd web-ui
npm install
cp .env.example .env   # adjust VITE_API_BASE_URL if needed
npm run dev
```

Open the printed localhost URL, then fill in the connection settings panel:
- **Backend API key** — the `MIGRATION_API_KEY` configured on the Render service
- **Jira URL / email / API token** — forwarded per-request to the backend,
  same as the Forge app forwards its stored credentials
- **Default ADO project** — optional, just pre-fills the project field

Credentials are kept in `sessionStorage` only (cleared when the tab closes),
never written to disk or synced anywhere.

## Build for deployment

```bash
npm run build
```

Outputs static files to `dist/` — deploy as a static site (Render Static
Site, Netlify, S3+CloudFront, etc.) pointed at this backend's URL.

## Security notes

- The backend's `MIGRATION_API_KEY` is sent as a browser-visible header —
  treat this UI as trusted-network-only (VPN/internal), not public internet,
  until a real auth layer is added.
- `CORS_ALLOWED_ORIGINS` on the Flask backend defaults to `*`; set it to this
  site's exact origin(s) in Render's env vars once deployed.
