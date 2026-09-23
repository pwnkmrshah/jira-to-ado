#!/usr/bin/env python3
"""
api_server.py — HTTP bridge between the Forge app (Atlassian cloud) and the
local migration scripts. Expose this server publicly via ngrok so the Forge
backend function can reach it.

Usage:
    export MIGRATION_API_KEY=your-secret-key
    python3 api_server.py

Then in a separate terminal:
    ngrok http 5001

Pass the ngrok HTTPS URL to the Forge app via environment or manifest egress.
"""

import base64
import json
import logging
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__, static_folder=None)

# Note: Frontend is now served separately via Nginx (Dockerfile.frontend)
# This service is API-only. CORS headers handle cross-origin requests.

# Standalone web-ui calls this API directly from the browser (unlike Forge,
# whose resolvers call it server-to-server), so CORS headers are required.
# Restrict via CORS_ALLOWED_ORIGINS="https://foo.com,https://bar.com" in prod;
# defaults to reflecting the request Origin (safe here since auth is via the
# X-API-Key header, not cookies, so it isn't forgeable cross-site).
_CORS_ALLOWED_ORIGINS = os.environ.get('CORS_ALLOWED_ORIGINS', '*')


@app.after_request
def _add_cors_headers(response):
    if _CORS_ALLOWED_ORIGINS == '*':
        # Allow all origins in development/demo mode
        origin = request.headers.get('Origin', '*')
        response.headers['Access-Control-Allow-Origin'] = origin if origin else '*'
        if request.headers.get('Origin'):
            response.headers['Vary'] = 'Origin'
    else:
        # Restrict to specific origins in production
        origin = request.headers.get('Origin', '')
        if origin and origin in _CORS_ALLOWED_ORIGINS.split(','):
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Vary'] = 'Origin'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-API-Key'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)

REPO_ROOT = Path(__file__).parent
SCRIPTS_DIR = REPO_ROOT / 'jira_ado_copy' / 'scripts'
REPORTS_DIR = REPO_ROOT / 'reports'
REPORTS_DIR.mkdir(exist_ok=True)

# All jobs keyed by UUID: { status, command, output, error, return_code, started_at, finished_at }
_jobs: dict = {}
_jobs_lock = threading.Lock()

# Set MIGRATION_API_KEY in env before starting; Forge app sends it as X-API-Key header
API_KEY = os.environ.get('MIGRATION_API_KEY', 'demo-key-change-me')


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        provided_key = request.headers.get('X-API-Key', '')
        if provided_key != API_KEY:
            logging.warning(f'API key check failed: provided="{provided_key[:10]}..." expected="{API_KEY[:10]}..."')
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

# Timestamp prefix written by Python logging (e.g. "2026-08-10 07:15:53,295 - INFO - ")
_TS_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ - \w+ - ')

# Patterns for extracting live migration progress from log lines
_PROG_PATTERNS = {
    # Matches both filter mode "[main] Found 5 issues" and JQL mode "[main] JQL returned 5 issues"
    'total':         re.compile(r'\[main\] (?:Found|JQL returned) (\d+) issues'),
    'copying':       re.compile(r'\[main\] Copying ([\w-]+)'),
    'finished':      re.compile(r'\[main\] Finished work item: (\d+)'),
    'failed_proc':   re.compile(r'\[main\] Failed to process ([\w-]+): (.+)'),
    'created_full':  re.compile(r'\[CREATE\] Created ADO item (\d+) for Jira ([\w-]+)'),
    'updated_full':  re.compile(r'\[UPDATE\] Updating existing ADO item (\d+) for Jira ([\w-]+)'),
    'dup_full':      re.compile(r'\[DUPLICATE PREVENTED\].*for ([\w-]+) -> (\d+)'),
    'assign_fail':   re.compile(r'Could not assign .* in ADO for ([\w-]+)'),
    'reporter_fail': re.compile(r'Could not set reporter .* in ADO for ([\w-]+)'),
    # group(1)=total processed, group(2)=succeeded — use total for progress bar
    'summary':       re.compile(r'(\d+) processed: (\d+) succeeded'),
}

# Which line prefixes to include in the live_log shown in the UI
_LIVE_LOG_PREFIXES = (
    '[main]', '[CREATE]', '[UPDATE]', '[DUPLICATE', '[bootstrap]',
    '[board-detect]', '[board-setup]', '[summary]', '[gaps]', '[verify]',
    'Copying ', '\u2705', '\u26a0\ufe0f', '\u274c', '==========',
)


def _categorize_error(rc: int, stdout_lines: list, error_lines: list) -> str:
    """Return a short, user-facing message based on what actually went wrong."""
    combined = ' '.join(error_lines + stdout_lines[-10:]).lower()
    all_stdout = ' '.join(stdout_lines).lower()

    if 'filter' in combined and ('not found' in combined or '404' in combined or 'not accessible' in combined):
        import re as _re
        m = _re.search(r"filter[^'\"]*['\"]?(\d+)['\"]?", combined)
        fid = m.group(1) if m else ''
        return (f'Filter ID {fid} was not found.' if fid else 'Filter not found.') + \
               ' Please double-check the number and make sure the filter is shared with your account in Jira.'

    # Did ANY item actually get created/updated/found in ADO this run? If so, the
    # credentials are clearly valid — an isolated 401/403 on one optional/best-effort
    # operation (e.g. dynamic custom-field creation needing elevated PAT scope) must not
    # be reported as "your token may be expired", which is misleading once real work
    # has already succeeded.
    had_success = bool(re.search(r'\[(create|update|duplicate prevented)\]', all_stdout)) or \
                  bool(re.search(r'\d+ processed: [1-9]\d* succeeded', all_stdout))

    if not had_success and any(x in combined for x in ('401', '403', 'unauthorized', 'forbidden', 'invalid token', 'authentication')):
        return 'Access denied — your Jira API token or ADO token may be expired or incorrect. Please check your credentials.'

    if not had_success and any(x in combined for x in ('connection refused', 'timed out', 'could not connect', 'name or service not known')):
        return 'Could not reach Jira or Azure DevOps. Please check your internet connection and try again.'

    if rc == 0 and error_lines:
        import re as _re
        processed = next((_re.search(r'(\d+) processed', l) for l in stdout_lines if 'processed' in l), None)
        if processed:
            return f'Migration completed with some issues — {processed.group(0)}. Download the CSV report for a full breakdown.'
        return 'Migration completed but some cards had issues. Download the CSV report for details.'

    return 'The operation could not be started. Please verify the filter ID / project key / board, check your credentials, and try again.'


# Keep old name as alias so existing callers still work
_summarise_errors = lambda lines: _categorize_error(0, [], lines)



def _drain_pipe(pipe, buf: list, job_id: str, stream: str):
    """Read pipe line-by-line, logging each line, updating rolling snapshot and progress."""
    log_fn = logging.info if stream in ('OUT', 'LOG') else logging.warning
    for raw in pipe:
        line = raw.rstrip('\n')
        log_fn(f"[{job_id}] {stream}: {line}")
        buf.append(line)

        # Skip timestamped versions to avoid double-processing (each line appears twice:
        # once via Python logging with timestamp, once as a plain print without timestamp)
        clean = _TS_RE.sub('', line).strip()
        is_clean = bool(clean) and not _TS_RE.match(line)

        with _jobs_lock:
            key = 'output' if stream == 'OUT' else 'error'
            _jobs[job_id][key] = '\n'.join(buf[-500:])

            if not is_clean:
                continue

            prog = _jobs[job_id].setdefault('progress', {
                'total': 0, 'done': 0,
                'current_card': '', 'current_ado': '', 'current_action': '',
            })
            card_rep = _jobs[job_id].setdefault('card_report', {})

            m = _PROG_PATTERNS['total'].search(clean)
            if m:
                prog['total'] = int(m.group(1))

            m = _PROG_PATTERNS['copying'].search(clean)
            if m:
                jira_key = m.group(1)
                prog['current_card'] = jira_key
                prog['current_action'] = 'processing'
                card_rep.setdefault(jira_key, {
                    'ado_id': '', 'action': '', 'field_issues': [], 'status': 'in_progress', 'error': ''
                })

            m = _PROG_PATTERNS['created_full'].search(clean)
            if m:
                ado_id, jira_key = m.group(1), m.group(2)
                prog['current_ado'] = ado_id
                prog['current_action'] = 'created'
                card_rep.setdefault(jira_key, {'ado_id': '', 'action': '', 'field_issues': [], 'status': 'in_progress', 'error': ''})
                card_rep[jira_key].update({'ado_id': ado_id, 'action': 'created'})

            m = _PROG_PATTERNS['updated_full'].search(clean)
            if m:
                ado_id, jira_key = m.group(1), m.group(2)
                prog['current_ado'] = ado_id
                prog['current_action'] = 'updated'
                card_rep.setdefault(jira_key, {'ado_id': '', 'action': '', 'field_issues': [], 'status': 'in_progress', 'error': ''})
                card_rep[jira_key].update({'ado_id': ado_id, 'action': 'updated'})

            m = _PROG_PATTERNS['dup_full'].search(clean)
            if m:
                jira_key, ado_id = m.group(1), m.group(2)
                prog['current_ado'] = ado_id
                prog['current_action'] = 'already in ADO'
                card_rep.setdefault(jira_key, {'ado_id': '', 'action': '', 'field_issues': [], 'status': 'in_progress', 'error': ''})
                card_rep[jira_key].update({'ado_id': ado_id, 'action': 'existing'})

            m = _PROG_PATTERNS['assign_fail'].search(clean)
            if m:
                jira_key = m.group(1)
                card = card_rep.setdefault(jira_key, {'ado_id': '', 'action': '', 'field_issues': [], 'status': 'in_progress', 'error': ''})
                if 'Assigned To' not in card['field_issues']:
                    card['field_issues'].append('Assigned To')

            m = _PROG_PATTERNS['reporter_fail'].search(clean)
            if m:
                jira_key = m.group(1)
                card = card_rep.setdefault(jira_key, {'ado_id': '', 'action': '', 'field_issues': [], 'status': 'in_progress', 'error': ''})
                if 'Reporter' not in card['field_issues']:
                    card['field_issues'].append('Reporter')

            m = _PROG_PATTERNS['failed_proc'].search(clean)
            if m:
                jira_key, err = m.group(1), m.group(2)
                card = card_rep.setdefault(jira_key, {'ado_id': '', 'action': '', 'field_issues': [], 'status': 'in_progress', 'error': ''})
                # If the card already has an ADO id it was created/updated before the
                # post-processing crash — treat it as warning, not a full failure.
                new_status = 'warning' if card.get('ado_id') else 'failed'
                card.update({'status': new_status, 'error': err})
                prog['done'] = prog.get('done', 0) + 1

            m = _PROG_PATTERNS['finished'].search(clean)
            if m:
                fin_ado_id = m.group(1)
                for k, v in card_rep.items():
                    if v.get('ado_id') == fin_ado_id and v.get('status') == 'in_progress':
                        v['status'] = 'warning' if v.get('field_issues') else 'success'
                        break
                prog['done'] = prog.get('done', 0) + 1

            m = _PROG_PATTERNS['summary'].search(clean)
            if m:
                prog['done'] = int(m.group(1))  # total processed (success + failed)

            if any(clean.startswith(p) for p in _LIVE_LOG_PREFIXES):
                llog = _jobs[job_id].setdefault('live_log', [])
                llog.append(clean)
                _jobs[job_id]['live_log'] = llog[-20:]


def _run_job(job_id: str, cmd: list, cwd: str, env_overrides: dict = None):
    """Run a script subprocess in a background thread with real-time line logging."""
    with _jobs_lock:
        _jobs[job_id].update({
            'status': 'running', 'output': '', 'error': '',
            'started_at': datetime.utcnow().isoformat() + 'Z',
            'progress': {'total': 0, 'done': 0, 'current_card': '', 'current_ado': '', 'current_action': ''},
            'live_log': [],
            'card_report': {},
            'card_csv': '',
        })

    logging.info(f"[{job_id}] START  cmd={' '.join(cmd)}")
    logging.info(f"[{job_id}]        cwd={cwd}")

    stdout_buf, stderr_buf = [], []
    try:
        proc_env = os.environ.copy()
        if env_overrides:
            proc_env.update(env_overrides)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, env=proc_env,
        )
        with _jobs_lock:
            _jobs[job_id]['_proc'] = proc  # stored so /cancel can terminate it
        # Drain both pipes concurrently to avoid buffer deadlocks
        t_out = threading.Thread(target=_drain_pipe, args=(proc.stdout, stdout_buf, job_id, 'OUT'), daemon=True)
        t_err = threading.Thread(target=_drain_pipe, args=(proc.stderr, stderr_buf, job_id, 'LOG'), daemon=True)
        t_out.start()
        t_err.start()
        proc.wait()
        t_out.join()
        t_err.join()

        rc = proc.returncode
        # Worker scripts exit 0 even on application errors — detect logged ERROR lines
        error_lines = [
            l for l in stderr_buf
            if ' - ERROR - ' in l or l.startswith('Error:') or l.startswith('Failed')
        ]
        has_app_errors = bool(error_lines)

        # Three-way outcome: clean, warnings (exit-0 + ERROR lines), or failure
        if rc != 0:
            final_status = 'failed'
        elif has_app_errors:
            final_status = 'warning'   # item migrated but some fields couldn't be set
        else:
            final_status = 'completed'

        if has_app_errors and rc == 0:
            logging.warning(
                f"[{job_id}] Marking as WARNING — script exited 0 but logged {len(error_lines)} ERROR line(s) in stderr"
            )

        # Run verify_migration.py on the migrated keys to get ground-truth CSV
        import tempfile as _tempfile, subprocess as _sub
        with _jobs_lock:
            job_meta = _jobs[job_id]
            card_report = job_meta.get('card_report', {})
            jira_inst = job_meta.get('jira_instance', 'healthfinch')
            ado_proj = job_meta.get('ado_project', 'Embedded Refills Engineering')
            jira_filt = job_meta.get('jira_filter', '')
            jira_ks = job_meta.get('jira_keys', '')

        # Verify only keys that actually reached ADO (have an ado_id)
        verify_keys = ','.join(k for k, v in card_report.items() if v.get('ado_id'))
        card_csv_str = ''
        if verify_keys:
            try:
                with _tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as _tmp:
                    csv_path = _tmp.name
                verify_cmd = [
                    'python3', str(SCRIPTS_DIR / 'verify_migration.py'),
                    '--jira-instance', jira_inst,
                    '--ado-project', ado_proj,
                    '--jira-keys', verify_keys,
                    '--csv-output', csv_path,
                ]
                logging.info(f"[{job_id}] Running post-migration verify: {' '.join(verify_cmd)}")
                vp = _sub.run(verify_cmd, capture_output=True, text=True, timeout=300,
                              cwd=str(SCRIPTS_DIR.parent.parent), env=proc_env)
                if vp.returncode not in (0, 1):
                    logging.warning(f"[{job_id}] verify exited {vp.returncode}: {vp.stderr[-500:]}")
                import os as _os
                if _os.path.exists(csv_path):
                    with open(csv_path) as _f:
                        card_csv_str = _f.read()
                    _os.unlink(csv_path)
                    logging.info(f"[{job_id}] Verify CSV ready ({len(card_csv_str)} bytes)")
            except Exception as _e:
                logging.warning(f"[{job_id}] Post-migration verify failed: {_e}")

        # Build a fallback CSV for any items that never reached ADO (failed before creation)
        import csv as _csv_mod, io as _io_mod
        failed_items = {k: v for k, v in card_report.items() if not v.get('ado_id')}
        if failed_items:
            _buf = _io_mod.StringIO()
            _w = _csv_mod.writer(_buf)
            if not card_csv_str:
                _w.writerow(['jira_key', 'ado_id', 'migration_status', 'failed_checks', 'details'])
            for jira_key, info in failed_items.items():
                _w.writerow([jira_key, '', 'failed', 'creation', info.get('error', 'Item was not created in ADO')])
            extra = _buf.getvalue()
            card_csv_str = (card_csv_str.rstrip('\n') + '\n' + extra.strip('\n')).strip() if card_csv_str else extra
            logging.info(f"[{job_id}] Appended {len(failed_items)} failed item(s) to CSV")

        # Trust the verify CSV as ground truth: only keep 'warning' if CSV shows real failures.
        # Harmless fallbacks (assignee/reporter stored in description) don't count as failures.
        if card_csv_str and final_status == 'warning':
            import csv as _csv, io as _io
            try:
                _reader = _csv.DictReader(_io.StringIO(card_csv_str))
                _has_real_failures = any(
                    row.get('migration_status', '') in ('failed', 'warnings')
                    for row in _reader
                )
                if not _has_real_failures:
                    final_status = 'completed'
                    logging.info(f"[{job_id}] Verify CSV shows all items verified — downgrading WARNING → completed")
            except Exception:
                pass  # keep original status if CSV parsing fails

        # For verify jobs, also read the CSV file if it exists
        verify_csv_path = _jobs[job_id].get('csv_path', '')
        verify_csv_str = ''
        if verify_csv_path:
            import os as _os_verify
            if _os_verify.path.exists(verify_csv_path):
                try:
                    with open(verify_csv_path) as _f:
                        verify_csv_str = _f.read()
                    _os_verify.unlink(verify_csv_path)
                    logging.info(f"[{job_id}] Verify CSV read ({len(verify_csv_str)} bytes)")
                except Exception as _e:
                    logging.warning(f"[{job_id}] Failed to read verify CSV: {_e}")

        # Once the CSV downgrade proves everything actually verified, the error_summary
        # computed from raw log text would be stale/misleading — e.g. "Access denied"
        # from one incidental 401/403 on a best-effort operation. Don't surface it once
        # ground truth (the verify CSV) says the run is actually clean.
        error_summary = '' if final_status == 'completed' else _categorize_error(rc, stdout_buf, error_lines)

        with _jobs_lock:
            _jobs[job_id].update({
                'status': final_status,
                'output': '\n'.join(stdout_buf)[-8000:],
                # Store only the actionable error lines so the frontend can show them cleanly
                'error': '\n'.join(error_lines) if error_lines else '\n'.join(stderr_buf)[-3000:],
                # Strip timestamps and deduplicate for a clean one-liner shown in the UI
                'error_summary': error_summary,
                'return_code': rc,
                'finished_at': datetime.utcnow().isoformat() + 'Z',
                'card_csv': card_csv_str or verify_csv_str,  # For verify jobs, use verify CSV
            })
        logging.info(
            f"[{job_id}] DONE   exit={rc}  status={final_status}  "
            f"stdout_lines={len(stdout_buf)}  stderr_lines={len(stderr_buf)}  "
            f"error_lines={len(error_lines)}"
        )
        if error_lines:
            logging.warning(f"[{job_id}] ERRORS: {' | '.join(error_lines[-5:])}")
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id].update({
                'status': 'failed', 'error': str(exc),
                'error_summary': _categorize_error(1, [], [str(exc)]),
                'finished_at': datetime.utcnow().isoformat() + 'Z',
            })
        logging.error(f"[{job_id}] Exception: {exc}")


def _spawn(cmd: list, extra_fields: dict = None, env_overrides: dict = None) -> str:
    """Register a job, start its thread, return the job_id."""
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {'status': 'queued', 'command': ' '.join(cmd), **(extra_fields or {})}
    threading.Thread(
        target=_run_job,
        args=(job_id, cmd, str(REPO_ROOT), env_overrides),
        daemon=True,
    ).start()
    return job_id


def _jira_env_from_body(body: dict) -> dict:
    """Build JIRA_URL/JIRA_EMAIL/JIRA_TOKEN env overrides from Forge-forwarded credentials.

    Without this, subprocesses fall back to config/jira_config.json, which is
    gitignored and doesn't exist on Render — causing load_jira_config() to return None.
    """
    jira_url = (body.get('jira_url') or '').strip()
    jira_email = (body.get('jira_email') or '').strip()
    jira_token = (body.get('jira_token') or '').strip()
    if jira_url and jira_email and jira_token:
        return {'JIRA_URL': jira_url, 'JIRA_EMAIL': jira_email, 'JIRA_TOKEN': jira_token}
    return {}


def _ado_env_from_body(body: dict) -> dict:
    """Build ADO_ORG/ADO_PAT env overrides from UI-forwarded credentials.

    Without this, subprocesses fall back to config/ado_config.json, which is
    gitignored and doesn't exist on Render — causing load_ado_config() to return None.
    """
    ado_org = (body.get('ado_org') or '').strip()
    ado_pat = (body.get('ado_pat') or '').strip()
    if ado_org and ado_pat:
        return {'ADO_ORG': ado_org, 'ADO_PAT': ado_pat}
    return {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/health', methods=['GET'])
def health():
    """No auth needed — lets Forge verify the server is reachable."""
    return jsonify({
        'status': 'ok',
        'scripts_dir': str(SCRIPTS_DIR),
        'reports_dir': str(REPORTS_DIR),
        'jobs_tracked': len(_jobs),
    })


@app.route('/validate-jira-creds', methods=['GET'])
@require_api_key
def validate_jira_creds():
    """
    STRICT validation for Jira credentials — NEVER falls back to config/env.
    Requires ALL three params explicitly. Used by frontend for credential testing.
    Query params (ALL REQUIRED):
      - jira_url: Full Jira instance URL (e.g., https://company.atlassian.net)
      - jira_email: Jira account email
      - jira_token: Jira API token
    Returns: {'valid': true} or {'error': 'reason'}
    """
    import requests as req

    jira_url = request.args.get('jira_url', '').strip()
    jira_email = request.args.get('jira_email', '').strip()
    jira_token = request.args.get('jira_token', '').strip()

    # Strict validation — ALL three required, NO fallback to config/env
    if not jira_url:
        return jsonify({'error': 'jira_url is required'}), 400
    if not jira_email:
        return jsonify({'error': 'jira_email is required'}), 400
    if not jira_token:
        return jsonify({'error': 'jira_token is required'}), 400

    auth = (jira_email, jira_token)
    try:
        # Use /myself endpoint which ALWAYS requires valid authentication
        test_url = f'{jira_url.rstrip("/")}/rest/api/3/myself'
        logging.info(f'[JIRA VALIDATION] Testing: {test_url}')
        logging.info(f'[JIRA VALIDATION] Auth: email={jira_email}')
        resp = req.get(
            test_url,
            auth=auth,
            timeout=10
        )
        logging.info(f'[JIRA VALIDATION] Response status: {resp.status_code}')
        logging.info(f'[JIRA VALIDATION] Response body: {resp.text[:200]}')
        
        if resp.status_code == 401:
            logging.warning(f'Jira 401: {resp.text}')
            return jsonify({'error': 'Jira authentication failed. Check your email and API token.'}), 401
        if resp.status_code == 403:
            logging.warning(f'Jira 403: {resp.text}')
            return jsonify({'error': 'Jira access denied. Check your account permissions.'}), 403
        if resp.status_code == 404:
            logging.warning(f'Jira 404: {resp.text}')
            return jsonify({'error': 'Jira URL not found. Check your Jira instance URL.'}), 404
        
        # STRICT: Only accept 200 OK, reject all other status codes
        if resp.status_code != 200:
            logging.warning(f'[JIRA VALIDATION] Unexpected status {resp.status_code}: {resp.text[:100]}')
            return jsonify({'error': f'Jira returned unexpected status {resp.status_code}. Check your credentials.'}), 401
        
        # Validate response is valid JSON with expected structure (must have accountId)
        try:
            data = resp.json()
            if not isinstance(data, dict) or 'accountId' not in data:
                logging.warning(f'[JIRA VALIDATION] Invalid response structure: {data}')
                return jsonify({'error': 'Jira returned invalid response. Authentication may have failed.'}), 401
        except Exception as e:
            logging.warning(f'[JIRA VALIDATION] Failed to parse response: {e}')
            return jsonify({'error': 'Jira returned non-JSON response. Authentication may have failed.'}), 401
        
        logging.info(f'[JIRA VALIDATION] ✅ SUCCESS')
        return jsonify({'valid': True, 'message': 'Jira credentials validated successfully'})
    except Exception as exc:
        logging.error(f'Jira validation failed: {exc}')
        error_str = str(exc).lower()
        if 'connection' in error_str or 'timeout' in error_str:
            return jsonify({'error': f'Could not reach Jira at {jira_url}. Check the URL and your internet connection.'}), 500
        return jsonify({'error': f'Jira validation failed: {str(exc)}'}), 500


@app.route('/validate-ado-creds', methods=['GET'])
@require_api_key
def validate_ado_creds():
    """
    STRICT validation for ADO credentials — NEVER falls back to config/env.
    Requires BOTH params explicitly. Used by frontend for credential testing.
    Query params (ALL REQUIRED):
      - ado_org: ADO organization name
      - ado_pat: ADO personal access token
    Returns: {'valid': true} or {'error': 'reason'}
    """
    import requests as req

    ado_org = request.args.get('ado_org', '').strip()
    ado_pat = request.args.get('ado_pat', '').strip()

    # Strict validation — BOTH required, NO fallback to config/env
    if not ado_org:
        return jsonify({'error': 'ado_org is required'}), 400
    if not ado_pat:
        return jsonify({'error': 'ado_pat is required'}), 400

    credentials = base64.b64encode(f':{ado_pat}'.encode()).decode()
    headers = {
        'Authorization': f'Basic {credentials}',
        'Content-Type': 'application/json',
    }

    url = f'https://dev.azure.com/{ado_org}/_apis/projects?api-version=7.0&$top=1'
    try:
        logging.info(f'[ADO VALIDATION] Testing: {url}')
        logging.info(f'[ADO VALIDATION] Org: {ado_org}')
        resp = req.get(url, headers=headers, timeout=10)
        logging.info(f'[ADO VALIDATION] Response status: {resp.status_code}')
        logging.info(f'[ADO VALIDATION] Response body: {resp.text[:200]}')
        
        if resp.status_code == 401:
            logging.warning(f'[ADO VALIDATION] 401 - Auth failed: {resp.text}')
            return jsonify({'error': 'ADO authentication failed. Check your PAT.'}), 401
        if resp.status_code == 403:
            logging.warning(f'[ADO VALIDATION] 403 - Access denied: {resp.text}')
            return jsonify({'error': 'ADO access denied. Check your permissions.'}), 403
        if resp.status_code == 404:
            logging.warning(f'[ADO VALIDATION] 404 - Not found: {resp.text}')
            return jsonify({'error': 'ADO organization not found. Check your organization name.'}), 404
        
        # STRICT: Only accept 200 OK, reject all other status codes
        if resp.status_code != 200:
            logging.warning(f'[ADO VALIDATION] Unexpected status {resp.status_code}: {resp.text[:100]}')
            return jsonify({'error': f'ADO returned unexpected status {resp.status_code}. Check your credentials.'}), 401
        
        # Validate response is valid JSON with expected structure
        try:
            data = resp.json()
            if not isinstance(data, dict) or 'value' not in data:
                logging.warning(f'[ADO VALIDATION] Invalid response structure: {data}')
                return jsonify({'error': 'ADO returned invalid response. Authentication may have failed.'}), 401
        except Exception as e:
            logging.warning(f'[ADO VALIDATION] Failed to parse response: {e}')
            return jsonify({'error': 'ADO returned non-JSON response. Authentication may have failed.'}), 401
        
        logging.info(f'[ADO VALIDATION] ✅ SUCCESS')
        return jsonify({'valid': True, 'message': 'ADO credentials validated successfully'})
    except Exception as exc:
        logging.error(f'ADO validation failed: {exc}')
        error_str = str(exc).lower()
        if 'connection' in error_str or 'timeout' in error_str:
            return jsonify({'error': 'Could not reach Azure DevOps. Check your internet connection.'}), 500
        return jsonify({'error': f'ADO validation failed: {str(exc)}'}), 500


@app.route('/ado-boards', methods=['GET'])
@require_api_key
def ado_boards():
    """
    Return boards for a given ADO project.
    Query param: ?project=<project name or id>
    """
    import requests as req

    project = request.args.get('project', '').strip()
    if not project:
        return jsonify({'error': 'project query parameter is required'}), 400

    ado_org = os.environ.get('ADO_ORG', '')
    ado_pat = os.environ.get('ADO_PAT', '')

    if not ado_org or not ado_pat:
        config_path = REPO_ROOT / 'config' / 'ado_config.json'
        if config_path.exists():
            config = json.loads(config_path.read_text())
            if not ado_org:
                ado_org = config.get('organization') or config.get('organization_url', '').rstrip('/').split('/')[-1]
            if not ado_pat:
                ado_pat = config.get('access_token', '')

    if not ado_org or not ado_pat:
        return jsonify({'error': 'ADO_ORG and ADO_PAT must be set'}), 500

    credentials = base64.b64encode(f':{ado_pat}'.encode()).decode()
    headers = {
        'Authorization': f'Basic {credentials}',
        'Content-Type': 'application/json',
    }

    # Fetch teams — each team in a project owns a Kanban board in ADO ("All team boards")
    url = f'https://dev.azure.com/{ado_org}/_apis/projects/{project}/teams?api-version=7.0&$top=200&mine=false'
    try:
        resp = req.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        boards = [
            {'id': t['id'], 'name': t['name']}
            for t in data.get('value', [])
        ]
        return jsonify({'boards': boards})
    except Exception as exc:
        logging.error(f'ADO boards fetch failed for project "{project}": {exc}')
        return jsonify({'error': str(exc)}), 500


@app.route('/ado-projects', methods=['GET'])
@require_api_key
def ado_projects():
    """
    Return all Azure DevOps projects for the given organisation and PAT.
    IMPORTANT: Credentials come ONLY from UI (query params), NEVER from env vars or config files.
    
    Query params (REQUIRED):
      - ado_org: ADO organization name
      - ado_pat: ADO personal access token
    """
    import requests as req

    ado_org = request.args.get('ado_org', '').strip()
    ado_pat = request.args.get('ado_pat', '').strip()

    # STRICT: NO fallback to env vars or config files
    # Credentials MUST come from UI only
    if not ado_org:
        return jsonify({'error': 'ado_org is required (must be provided from UI)'}), 400
    if not ado_pat:
        return jsonify({'error': 'ado_pat is required (must be provided from UI)'}), 400

    credentials = base64.b64encode(f':{ado_pat}'.encode()).decode()
    headers = {
        'Authorization': f'Basic {credentials}',
        'Content-Type': 'application/json',
    }

    url = f'https://dev.azure.com/{ado_org}/_apis/projects?api-version=7.0&$top=200'
    try:
        resp = req.get(url, headers=headers, timeout=10)
        if resp.status_code == 401:
            return jsonify({'error': 'ADO authentication failed. Check your organization and PAT.'}), 401
        if resp.status_code == 403:
            return jsonify({'error': 'ADO access denied. Check your permissions.'}), 403
        resp.raise_for_status()
        data = resp.json()
        projects = [
            {'id': p['id'], 'name': p['name']}
            for p in data.get('value', [])
        ]
        return jsonify({'projects': projects})
    except Exception as exc:
        logging.error(f'ADO projects fetch failed for org "{ado_org}": {exc}')
        return jsonify({'error': f'Could not fetch ADO projects: {str(exc)}'}), 500


@app.route('/jira-projects', methods=['GET'])
@require_api_key
def jira_projects():
    """
    Return all Jira projects accessible to the authenticated user.
    IMPORTANT: Credentials come ONLY from UI (query params), NEVER from env vars or config files.
    
    Query params (REQUIRED):
      - jira_url: Full Jira instance URL
      - jira_email: Jira account email
      - jira_token: Jira API token
    """
    import requests as req

    jira_url = request.args.get('jira_url', '').strip()
    jira_email = request.args.get('jira_email', '').strip()
    jira_token = request.args.get('jira_token', '').strip()

    # STRICT: NO fallback to config files or env vars
    # Credentials MUST come from UI only
    if not jira_url:
        return jsonify({'error': 'jira_url is required (must be provided from UI)'}), 400
    if not jira_email:
        return jsonify({'error': 'jira_email is required (must be provided from UI)'}), 400
    if not jira_token:
        return jsonify({'error': 'jira_token is required (must be provided from UI)'}), 400

    auth = (jira_email, jira_token)
    try:
        # Fetch all projects from Jira
        resp = req.get(
            f'{jira_url.rstrip("/")}/rest/api/3/project/search?maxResults=100',
            auth=auth,
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        projects = [
            {'key': p['key'], 'name': p['name'], 'id': p['id']}
            for p in data.get('values', [])
        ]
        return jsonify({'projects': projects})
    except Exception as exc:
        logging.error(f'Jira projects fetch failed: {exc}')
        return jsonify({'error': str(exc)}), 500


@app.route('/jira-filters', methods=['GET'])
@require_api_key
def jira_filters():
    """
    Return Jira filters accessible to the authenticated user.
    IMPORTANT: Credentials come ONLY from UI (query params), NEVER from env vars or config files.
    
    Query params (REQUIRED):
      - jira_url: Full Jira instance URL
      - jira_email: Jira account email
      - jira_token: Jira API token
    
    Query params (OPTIONAL):
      - project_key: If provided, return only filters related to this project
    """
    import requests as req

    jira_url = request.args.get('jira_url', '').strip()
    jira_email = request.args.get('jira_email', '').strip()
    jira_token = request.args.get('jira_token', '').strip()
    project_key = request.args.get('project_key', '').strip()

    # STRICT: NO fallback to config files or env vars
    # Credentials MUST come from UI only
    if not jira_url:
        return jsonify({'error': 'jira_url is required (must be provided from UI)'}), 400
    if not jira_email:
        return jsonify({'error': 'jira_email is required (must be provided from UI)'}), 400
    if not jira_token:
        return jsonify({'error': 'jira_token is required (must be provided from UI)'}), 400

    auth = (jira_email, jira_token)
    try:
        # Fetch user's filters from Jira
        resp = req.get(
            f'{jira_url.rstrip("/")}/rest/api/3/filter/search?maxResults=100',
            auth=auth,
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        filters = [
            {'id': f['id'], 'name': f['name'], 'jql': f.get('jql', '')}
            for f in data.get('values', [])
        ]
        
        # If project_key is specified, filter to show only filters related to that project
        if project_key:
            filters = [
                f for f in filters 
                if f'project = {project_key}' in f['jql'] or f'project in ({project_key})' in f['jql']
            ]
            logging.info(f'[JIRA FILTERS] Filtered {len(filters)} filters for project {project_key}')
        
        # Remove JQL from response (client doesn't need it)
        filters = [{'id': f['id'], 'name': f['name']} for f in filters]
        
        return jsonify({'filters': filters})
    except Exception as exc:
        logging.error(f'Jira filters fetch failed: {exc}')
        return jsonify({'error': str(exc)}), 500


@app.route('/preflight', methods=['GET'])
@require_api_key
def preflight():
    """Check ADO write access and type-config compatibility before migration starts."""
    import requests as req
    from urllib.parse import quote
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from utilities.utils_ado import check_type_config_compatibility

    ado_project = request.args.get('ado_project', '').strip()
    if not ado_project:
        return jsonify({'ok': False, 'errors': ['ado_project parameter is required.']}), 400

    ado_org = os.environ.get('ADO_ORG', '')
    ado_pat = os.environ.get('ADO_PAT', '')
    if not ado_org or not ado_pat:
        config_path = REPO_ROOT / 'config' / 'ado_config.json'
        if config_path.exists():
            cfg = json.loads(config_path.read_text())
            if not ado_org:
                ado_org = cfg.get('organization') or cfg.get('organization_url', '').rstrip('/').split('/')[-1]
            if not ado_pat:
                ado_pat = cfg.get('access_token', '')

    if not ado_org or not ado_pat:
        return jsonify({'ok': False, 'errors': ['ADO credentials not configured.']}), 500

    auth = ('', ado_pat)
    base = f'https://dev.azure.com/{ado_org}'

    # Step 1: project exists?
    try:
        r = req.get(
            f'{base}/_apis/projects/{quote(ado_project)}?api-version=7.0',
            auth=auth, timeout=10
        )
        if r.status_code == 404:
            return jsonify({'ok': False, 'errors': [f"ADO project '{ado_project}' was not found. Check the project name."]})
        if r.status_code in (401, 403):
            return jsonify({'ok': False, 'errors': ['ADO credentials are invalid or expired. Check your PAT.']})
        r.raise_for_status()
    except Exception as exc:
        return jsonify({'ok': False, 'errors': [f'Could not reach Azure DevOps: {exc}']})

    # Step 2: get available work item types
    try:
        r = req.get(
            f'{base}/{quote(ado_project, safe="")}/_apis/wit/workitemtypes?api-version=7.0',
            auth=auth, timeout=10
        )
        r.raise_for_status()
        types = [t['name'] for t in r.json().get('value', [])]
    except Exception as exc:
        return jsonify({'ok': False, 'errors': [f"Could not read work item types for '{ado_project}': {exc}"]})

    if not types:
        return jsonify({'ok': False, 'errors': [f"No work item types found in '{ado_project}'."], 'ado_work_item_types': []})

    # Step 3: test write permission with validateOnly=true (no side effects)
    test_type = types[0]
    try:
        r = req.post(
            f'{base}/{quote(ado_project, safe="")}/_apis/wit/workitems/${test_type}?api-version=7.0&validateOnly=true',
            auth=auth,
            headers={'Content-Type': 'application/json-patch+json'},
            json=[{"op": "add", "path": "/fields/System.Title", "value": "__preflight__"}],
            timeout=10
        )
        if r.status_code in (401, 403):
            return jsonify({
                'ok': False,
                'errors': [f"You don't have permission to create work items in '{ado_project}'. Ask your ADO admin for Contributor access."],
                'ado_work_item_types': types,
            })
        # 200 = valid; 400 = field validation errors = write access confirmed
    except Exception as exc:
        logging.warning(f'[preflight] validateOnly check failed (non-blocking): {exc}')

    # Step 4: warn about any type-config mismatches (informational — migration still works via fallback)
    type_config_path = REPO_ROOT / 'config' / 'type_config.json'
    type_warnings = []
    if type_config_path.exists():
        try:
            type_config = json.loads(type_config_path.read_text())
            type_warnings = check_type_config_compatibility(type_config, types)
        except Exception:
            pass

    return jsonify({
        'ok': True,
        'errors': [],
        'ado_work_item_types': types,
        'type_warnings': type_warnings,  # [{jira_type, configured, will_use}]
    })


@app.route('/explain-error', methods=['POST'])
@require_api_key
def explain_error():
    """Return a plain-English explanation for a raw ADO error.

    Checks ado_error_kb.json first. If no KB match and GPT_API_KEY is set,
    falls back to an LLM for unknown errors. Degrades gracefully if neither matches.
    """
    data = request.json or {}
    raw_error = data.get('error', '').strip()
    context = data.get('context', {})  # {ado_project, jira_type, jira_key, available_types}

    if not raw_error:
        return jsonify({'explanation': '', 'action': '', 'who': '', 'source': 'none'}), 400

    # Check knowledge base
    kb_path = REPO_ROOT / 'config' / 'ado_error_kb.json'
    kb = {}
    if kb_path.exists():
        try:
            kb = json.loads(kb_path.read_text())
        except Exception:
            pass

    for code, entry in kb.items():
        if code in raw_error:
            short = entry.get('short', raw_error)
            action = entry.get('action', '')
            who = entry.get('who', '')
            # Interpolate {type} if present in the message
            type_match = re.search(r"work item type '([^']+)'", raw_error, re.IGNORECASE)
            type_name = type_match.group(1) if type_match else context.get('jira_type', 'unknown')
            short = short.format(type=type_name) if '{type}' in short else short
            action = action.format(type=type_name) if '{type}' in action else action
            return jsonify({'explanation': short, 'action': action, 'who': who, 'source': 'kb', 'code': code})

    # LLM fallback — only if GPT_API_KEY is configured
    openai_key = os.environ.get('GPT_API_KEY', '')
    if openai_key:
        try:
            import requests as req
            prompt = (
                f"You are an Azure DevOps migration assistant. Explain this error in plain English "
                f"and give one actionable fix. Be concise (2-3 sentences max).\n\n"
                f"Error: {raw_error}\n"
                f"Context: migrating Jira type '{context.get('jira_type', '?')}' to ADO project "
                f"'{context.get('ado_project', '?')}'. Available ADO types: {context.get('available_types', [])}."
            )
            resp = req.post(
                'https://crvdev-cus-dev-foundry.services.ai.azure.com/openai/v1/responses',
                headers={'Authorization': f'Bearer {openai_key}', 'Content-Type': 'application/json'},
                json={'model': 'gpt-4o-mini', 'input': prompt, 'max_output_tokens': 150},
                timeout=10
            )
            resp.raise_for_status()
            body = resp.json()
            # Responses API: prefer the convenience field when present, else walk output[].content[]
            ai_text = body.get('output_text', '').strip()
            if not ai_text:
                for item in body.get('output', []):
                    if item.get('type') != 'message':
                        continue
                    for c in item.get('content', []):
                        if c.get('type') == 'output_text' and c.get('text'):
                            ai_text = c['text'].strip()
                            break
                    if ai_text:
                        break
            if not ai_text:
                raise ValueError(f'No output_text in Responses API result: {body}')
            return jsonify({'explanation': ai_text, 'action': '', 'who': '', 'source': 'ai'})
        except Exception as exc:
            logging.warning(f'[explain-error] LLM call failed: {exc}')

    # No match — return the raw error cleaned up
    return jsonify({'explanation': raw_error, 'action': '', 'who': '', 'source': 'raw'})


@app.route('/ping', methods=['GET'])
@require_api_key
def ping():
    """
    Connection check called by the Forge backend resolver when the user clicks
    "Migrate to ADO". Returns a confirmation that the Python engine is reachable
    and authenticated. The Forge UI shows this message in the modal.
    """
    return jsonify({
        'status': 'ok',
        'message': 'Connected to Python engine',
        'scripts_available': [
            'worker_jira_to_ado_copy.py',
            'migration_gap_analysis.py',
            'verify_migration.py',
        ],
    })


@app.route('/migrate', methods=['POST'])
@require_api_key
def migrate():
    """
    Trigger a Jira → ADO migration job.

    Request body (JSON):
        jira_instance     str   Jira subdomain, e.g. "healthfinch"
        ado_project       str   ADO project name
        jira_filter       str   Jira filter ID  (provide this OR jira_keys OR jql)
        jira_keys         str   Comma-separated Jira keys  (provide this OR jira_filter OR jql)
        jql               str   JQL query passed as --jira-jql to worker (AI migrate flow, scalable)
        skip_attachments  bool  Skip attachment upload (default: false)

    Response 202:
        { job_id, status: "queued" }
    """
    body = request.get_json(force=True) or {}

    jira_instance = body.get('jira_instance', 'healthfinch')
    ado_project = body.get('ado_project', '').strip() or 'Embedded Refills Engineering'
    jira_filter = body.get('jira_filter', '').strip()
    
    # Handle jira_keys: can be a list (from frontend) or string (from API)
    jira_keys_raw = body.get('jira_keys', [])
    if isinstance(jira_keys_raw, list):
        jira_keys = ','.join(str(k).strip() for k in jira_keys_raw if k)
    else:
        jira_keys = (jira_keys_raw or '').strip()
    
    jql = body.get('jql', '').strip()
    field_filter = [f.strip() for f in (body.get('field_filter') or []) if f.strip()]
    ado_team_name = body.get('ado_team_name', '').strip()
    # Credentials from UI — override the worker's local config files
    jira_url   = body.get('jira_url', '').strip()
    jira_email = body.get('jira_email', '').strip()
    jira_token = body.get('jira_token', '').strip()
    ado_org    = body.get('ado_org', '').strip()
    ado_pat    = body.get('ado_pat', '').strip()
    # Derive the instance label from the actual URL so the command log is accurate
    if jira_url:
        from urllib.parse import urlparse as _urlparse
        _host = _urlparse(jira_url).hostname or ''
        if _host:
            jira_instance = _host.split('.')[0]
    skip_attachments = body.get('skip_attachments', False)

    logging.info(f"MIGRATE request: jira_instance={jira_instance!r}  ado_project={ado_project!r}  "
                 f"jira_filter={jira_filter!r}  jira_keys={jira_keys!r}  jql={jql!r}  "
                 f"skip_attachments={skip_attachments}  field_filter={field_filter}")
    if jira_url:
        logging.info(f"MIGRATE jira_url from Forge KVS: {jira_url}  (overrides --jira-instance {jira_instance!r})")

    if not jira_filter and not jira_keys and not jql:
        logging.warning("MIGRATE rejected: no jira_filter, jira_keys, or jql in request")
        return jsonify({'error': 'Provide jira_filter, jira_keys, or jql'}), 400

    cmd = [
        'python3', str(SCRIPTS_DIR / 'worker_jira_to_ado_copy.py'),
        '--jira-instance', jira_instance,
        '--ado-project', ado_project,
    ]
    if jira_filter:
        cmd += ['--jira-filter', jira_filter]
    if jira_keys:
        cmd += ['--jira-keys', jira_keys]
    if jql:
        # Pass JQL directly — worker paginates internally, no ARG_MAX risk
        cmd += ['--jira-jql', jql]
    if skip_attachments:
        cmd.append('--skip-attachments')
    if field_filter:
        cmd += ['--field-filter', ','.join(field_filter)]
    if ado_team_name:
        cmd += ['--ado-team-name', ado_team_name]

    logging.info(f"MIGRATE cmd: {' '.join(cmd)}")
    # Pass credentials as env vars so worker uses them instead of config files
    env_overrides = {}
    if jira_url and jira_email and jira_token:
        env_overrides.update({'JIRA_URL': jira_url, 'JIRA_EMAIL': jira_email, 'JIRA_TOKEN': jira_token})
    if ado_org and ado_pat:
        env_overrides.update({'ADO_ORG': ado_org, 'ADO_PAT': ado_pat})
    
    if env_overrides:
        logging.info(f"MIGRATE passing credentials via env: jira={'yes' if 'JIRA_URL' in env_overrides else 'no'}, ado={'yes' if 'ADO_ORG' in env_overrides else 'no'}")

    job_id = _spawn(cmd, extra_fields={
        'jira_instance': jira_instance,
        'ado_project': ado_project,
        'jira_filter': jira_filter,
        'jira_keys': jira_keys,
        'jql': jql,
    }, env_overrides=env_overrides)
    logging.info(f"MIGRATE queued as job_id={job_id}  jira_creds_overridden={bool('JIRA_URL' in env_overrides)}  ado_creds_overridden={bool('ADO_ORG' in env_overrides)}")
    return jsonify({'job_id': job_id, 'status': 'queued'}), 202


@app.route('/status/<job_id>', methods=['GET'])
@require_api_key
def job_status(job_id):
    """
    Poll a running or finished job.

    Response fields:
        status      "queued" | "running" | "completed" | "failed"
        output      Last 8 000 chars of stdout
        error       Last 3 000 chars of stderr
        return_code int (present when finished)
        started_at  ISO-8601
        finished_at ISO-8601 (present when finished)
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    # Strip internal non-serializable fields before returning
    safe = {k: v for k, v in job.items() if not k.startswith('_')}
    # Ensure required fields are always present with defaults
    safe.setdefault('status', 'queued')
    safe.setdefault('output', '')
    safe.setdefault('error', '')
    safe.setdefault('command', '')
    safe.setdefault('progress', {'total': 0, 'done': 0, 'current_card': '', 'current_ado': '', 'current_action': ''})
    safe['has_csv'] = bool(safe.get('card_csv', ''))
    return jsonify(safe)


@app.route('/cancel/<job_id>', methods=['POST'])
@require_api_key
def cancel_job(job_id):
    """Terminate a running migration job immediately."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    status = job.get('status')
    if status not in ('queued', 'running'):
        return jsonify({'message': 'Job already finished', 'status': status}), 200

    proc = job.get('_proc')
    if proc:
        try:
            proc.terminate()
            logging.info(f"[{job_id}] CANCELLED by user — sent SIGTERM to subprocess")
        except Exception as e:
            logging.warning(f"[{job_id}] terminate() failed: {e}")

    with _jobs_lock:
        _jobs[job_id].update({
            'status': 'cancelled',
            'error_summary': 'Migration was cancelled by the user.',
            'finished_at': datetime.utcnow().isoformat() + 'Z',
        })

    return jsonify({'message': 'Migration cancelled', 'status': 'cancelled'}), 200


@app.route('/csv/<job_id>', methods=['GET'])
def job_csv(job_id):
    """Return the per-card migration report CSV for a finished job.

    Accepts auth via X-API-Key header OR ?key= query param (needed for browser download links).
    """
    key = request.headers.get('X-API-Key') or request.args.get('key', '')
    if key != API_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    csv_content = job.get('card_csv', '')
    if not csv_content:
        return jsonify({'error': 'No CSV report available yet'}), 404
    return csv_content, 200, {
        'Content-Type': 'text/csv',
        'Content-Disposition': f'attachment; filename="migration-report-{job_id[:8]}.csv"',
    }


@app.route('/gaps', methods=['POST'])
@require_api_key
def gaps():
    """
    Run gap analysis between a Jira filter and an ADO board.

    Request body (JSON):
        jira_instance  str
        jira_filter    str   (required)
        ado_board      str
        ado_project    str

    Response 202:
        { job_id, status: "queued", report: "/absolute/path/to/gaps_<ts>.csv" }
    """
    body = request.get_json(force=True) or {}

    jira_instance = body.get('jira_instance', 'healthfinch')
    ado_project = body.get('ado_project', 'Embedded Refills Engineering')
    jira_filter = body.get('jira_filter', '').strip()
    ado_board = body.get('ado_board', '').strip()
    jira_env = _jira_env_from_body(body)
    ado_env = _ado_env_from_body(body)

    if not jira_filter:
        return jsonify({'error': 'jira_filter is required'}), 400

    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    report_path = str(REPORTS_DIR / f'gaps_{ts}.csv')

    cmd = [
        'python3', str(SCRIPTS_DIR / 'migration_gap_analysis.py'),
        '--jira-instance', jira_instance,
        '--jira-filter', jira_filter,
        '--ado-project', ado_project,
        '--csv', report_path,
    ]
    if ado_board:
        cmd += ['--ado-board', ado_board]

    # Merge Jira and ADO credential env vars
    env_overrides = {**jira_env, **ado_env}
    job_id = _spawn(cmd, extra_fields={'report': report_path}, env_overrides=env_overrides)
    return jsonify({'job_id': job_id, 'status': 'queued', 'report': report_path}), 202


@app.route('/gaps-board', methods=['POST'])
@require_api_key
def gaps_board():
    """
    Run gap analysis for an ENTIRE Jira board (project key) vs an ADO board —
    no Jira filter ID required. Uses migration_gap_analysis_board.py, a separate
    copy of migration_gap_analysis.py so the existing filter-based flow is untouched.

    Request body (JSON):
        jira_instance    str
        jira_board_key   str   (required) Jira project key, e.g. "OP"
        ado_board        str
        ado_project      str

    Response 202:
        { job_id, status: "queued", report: "/absolute/path/to/gaps_board_<ts>.csv" }
    """
    body = request.get_json(force=True) or {}

    jira_instance = body.get('jira_instance', 'healthfinch')
    ado_project = body.get('ado_project', 'Embedded Refills Engineering')
    jira_board_key = body.get('jira_board_key', '').strip()
    ado_board = body.get('ado_board', '').strip()
    jira_env = _jira_env_from_body(body)
    ado_env = _ado_env_from_body(body)

    if not jira_board_key:
        return jsonify({'error': 'jira_board_key is required'}), 400

    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    report_path = str(REPORTS_DIR / f'gaps_board_{ts}.csv')

    cmd = [
        'python3', str(SCRIPTS_DIR / 'migration_gap_analysis_board.py'),
        '--jira-instance', jira_instance,
        '--jira-board-key', jira_board_key,
        '--ado-project', ado_project,
        '--csv', report_path,
    ]
    if ado_board:
        cmd += ['--ado-board', ado_board]

    # Merge Jira and ADO credential env vars
    env_overrides = {**jira_env, **ado_env}
    job_id = _spawn(cmd, extra_fields={'report': report_path}, env_overrides=env_overrides)
    return jsonify({'job_id': job_id, 'status': 'queued', 'report': report_path}), 202


@app.route('/verify', methods=['POST'])
@require_api_key
def verify():
    """
    Verify field-by-field accuracy of migrated cards.

    Request body (JSON):
        jira_instance  str
        ado_project    str
        project_key    str   Verify all cards in project  (provide this OR jira_keys)
        jira_keys      str   Comma-separated keys to verify

    Response 202:
        { job_id, status: "queued", report: "/absolute/path/to/verify_<ts>.html" }
    """
    body = request.get_json(force=True) or {}

    jira_instance = body.get('jira_instance', 'healthfinch')
    ado_project = body.get('ado_project', 'Embedded Refills Engineering')
    project_key = body.get('project_key', '').strip()
    
    # Handle jira_keys: can be a list (from frontend) or string (from API)
    jira_keys_raw = body.get('jira_keys', [])
    if isinstance(jira_keys_raw, list):
        jira_keys = ','.join(str(k).strip() for k in jira_keys_raw if k)
    else:
        jira_keys = (jira_keys_raw or '').strip()
    
    jira_filter = body.get('jira_filter', '').strip()
    jira_env = _jira_env_from_body(body)
    ado_env = _ado_env_from_body(body)

    if not project_key and not jira_keys and not jira_filter:
        return jsonify({'error': 'Provide project_key, jira_keys, or jira_filter'}), 400

    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    report_path = str(REPORTS_DIR / f'verify_{ts}.html')
    csv_path = str(REPORTS_DIR / f'verify_{ts}.csv')

    cmd = [
        'python3', str(SCRIPTS_DIR / 'verify_migration.py'),
        '--jira-instance', jira_instance,
        '--ado-project', ado_project,
        '--output', report_path,
        '--csv-output', csv_path,
    ]
    if project_key:
        cmd += ['--project-key', project_key]
    if jira_keys:
        cmd += ['--jira-keys', jira_keys]
    if jira_filter:
        cmd += ['--jira-filter', jira_filter]

    # Merge Jira and ADO credential env vars
    env_overrides = {**jira_env, **ado_env}
    job_id = _spawn(cmd, extra_fields={'report': report_path, 'csv_path': csv_path}, env_overrides=env_overrides)
    return jsonify({'job_id': job_id, 'status': 'queued', 'report': report_path}), 202


# ---------------------------------------------------------------------------
# Analysis  (Phase 1 — deterministic, no LLM)
# ---------------------------------------------------------------------------

@app.route('/analyze', methods=['POST'])
@require_api_key
def analyze():
    """
    POST /analyze
    Body (JSON):
        jira_project_key — Jira project key (e.g. "SUST")
        jira_filter_id   — (OPTIONAL) Jira filter ID to analyze specific filter
        jira_keys        — (OPTIONAL) List of specific issue keys to analyze
        
        ado_project      — ADO project name (e.g. "Embedded Refills Engineering")
        ado_org          — ADO organisation slug
        
        jira_url         — Full Jira instance URL
        jira_email       — Jira account email
        jira_token       — Jira API token
        ado_pat          — ADO personal access token

    Response 200:
        {total_issues, by_type, by_status, ado_available_types,
         type_gaps, user_gaps, attachment_count, comment_count}
    Response 400: missing required fields
    Response 422: analysis error (e.g. filter not found, no issues)
    """
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from api.analysis_engine import run_analysis  # lazy import keeps startup fast

    body = request.get_json(force=True) or {}

    # Required fields
    ado_project      = (body.get('ado_project') or '').strip()
    jira_project_key = (body.get('jira_project_key') or '').strip()
    jira_url         = (body.get('jira_url') or '').strip()
    jira_email       = (body.get('jira_email') or '').strip()
    jira_token       = (body.get('jira_token') or '').strip()
    ado_org          = (body.get('ado_org') or '').strip()
    ado_pat          = (body.get('ado_pat') or '').strip()
    
    # Optional scope parameters
    jira_filter_id   = (body.get('jira_filter_id') or '').strip()  # Optional
    jira_keys        = body.get('jira_keys') or []  # Optional list
    
    # Optional filters
    status_filter    = body.get('status_filter') or []   # list of status name strings
    field_filter     = body.get('field_filter') or []    # list of field name strings

    # Check required fields
    missing = [f for f, v in {
        'ado_project': ado_project,
        'jira_project_key': jira_project_key,
        'jira_url': jira_url, 'jira_email': jira_email, 'jira_token': jira_token,
        'ado_org': ado_org, 'ado_pat': ado_pat,
    }.items() if not v]

    if missing:
        return jsonify({'error': f"Missing required fields: {', '.join(missing)}"}), 400

    # Log which scope is being analyzed
    scope_desc = f"project '{jira_project_key}'"
    if jira_filter_id:
        scope_desc = f"filter '{jira_filter_id}'"
    elif jira_keys:
        scope_desc = f"keys {jira_keys}"
    
    logging.info(
        f"ANALYZE scope={scope_desc}  ado_project={ado_project!r}  "
        f"ado_org={ado_org!r}  status_filter={status_filter}  field_filter={field_filter}"
    )

    result = run_analysis(
        ado_project=ado_project,
        jira_url=jira_url,
        jira_email=jira_email,
        jira_token=jira_token,
        ado_org=ado_org,
        ado_pat=ado_pat,
        jira_project_key=jira_project_key,
        jira_filter_id=jira_filter_id,  # NEW: pass filter ID if provided
        jira_keys=jira_keys,             # NEW: pass specific keys if provided
        status_filter=status_filter,
        field_filter=field_filter,
    )

    if 'error' in result:
        return jsonify(result), 422

    return jsonify(result)


# Note: Frontend is now served separately via Nginx container
# This backend is API-only


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    logging.info(f"Migration API server on :{port}")
    logging.info(f"Scripts: {SCRIPTS_DIR}")
    logging.info(f"API key env var MIGRATION_API_KEY={'set' if 'MIGRATION_API_KEY' in os.environ else 'NOT SET (using default)'}")
    app.run(host='0.0.0.0', port=port, debug=False)
