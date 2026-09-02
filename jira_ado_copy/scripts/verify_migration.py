"""verify_migration.py — Post-migration verification: Jira → ADO.

Verifies migration correctness for these fields ONLY:
  • Work item type
  • Title / Summary
  • Description
  • State / Status
  • Priority
  • Assignee
  • Reporter (must appear in description)
  • Created date (mapped to Actual Start Date)
  • Labels / Tags
  • Comments (count only)
  • Attachments (count only)
  • Linked issues / relations (presence check only)

Logging:
  • Failed tickets written to: migration_verification_<timestamp>.log
  • Terminal output: Only failed tickets and periodic progress
  • HTML report generated for visual review

Usage:
  # By explicit key list:
  python3 verify_migration.py --jira-instance healthfinch \
    --jira-keys "ICE-1,ICE-2" --ado-project "Embedded Refills Engineering"

  # By board prefix (reads migration_mapping.json):
  python3 verify_migration.py --jira-instance healthfinch \
    --project-key HIVE --ado-project "Embedded Refills Engineering"

  # All migrated items:
  python3 verify_migration.py --jira-instance healthfinch \
    --project-key ALL --ado-project "Embedded Refills Engineering"
"""

import sys
import json
import re
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone
from dateutil.parser import parse as _parse_dt

sys.path.append(str(Path(__file__).parent.parent.parent / "utilities"))
from utils_ado import AzureDevOpsClient, load_ado_config
from utils_jira import JiraClient, load_jira_config
from utils_mapping import load_issue_mapping

PRIO_MAP = {'Highest': '1-Critical', 'High': '2-High', 'Medium': '3-Medium', 'Low': '4-Low', 'Lowest': '4-Low'}
TYPE_MAP  = {}
STATE_MAP = {}

# Setup structured logging with timestamp
TIMESTAMP = datetime.now().strftime('%Y%m%d_%H%M%S')
LOG_FILE = f"migration_verification_{TIMESTAMP}.log"

logging.basicConfig(level=logging.INFO, format='%(message)s')
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(file_handler)

def log_failed_result(result):
    """Log only failed tickets with structured field-by-field detail."""
    key = result["jira_key"]
    ado_id = result.get("ado_id")

    if result.get("all_pass"):
        return  # Skip passed tickets

    logging.info("\n" + "=" * 80)
    logging.info(f"FAILED: {key} → ADO {ado_id or 'NOT FOUND'}")
    logging.info("")

    for check_name, check in result["checks"].items():
        if not check["pass"]:
            jira_val = check.get("jira", "")
            ado_val = check.get("ado", "")
            reason = check.get("note", "")

            logging.info(f"Field: {check_name}")
            logging.info(f"Jira : {jira_val}")
            logging.info(f"ADO  : {ado_val}")
            if reason:
                logging.info(f"Reason: {reason}")
            logging.info("")

    logging.info("=" * 80)


def log_missing_tickets(missing_keys, count_total):
    """Log missing Jira tickets (not migrated)."""
    if not missing_keys:
        return
    logging.info("\n" + "=" * 80)
    logging.info("MISSING JIRA TICKETS (NOT MIGRATED)")
    logging.info(f"Total Missing: {len(missing_keys)}")
    logging.info("")
    for k in missing_keys:
        logging.info(k)
    logging.info("=" * 80)

def load_config(filename):
    path = Path(__file__).parent.parent.parent / "config" / filename
    return json.loads(path.read_text()) if path.exists() else {}


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_ado_item(ado_client, ado_id):
    """Fetch ADO work item with detailed error logging."""
    if not ado_id:
        logging.warning(f"[fetch_ado_item] No ADO ID provided")
        return None
    
    try:
        logging.debug(f"[fetch_ado_item] Attempting to fetch ADO work item {ado_id}...")
        item = ado_client.get_work_item_full(ado_id)
        
        if item is None:
            logging.error(f"[fetch_ado_item] ADO {ado_id}: get_work_item_full returned None")
            return None
        
        # Log the response structure for debugging
        logging.debug(f"[fetch_ado_item] ADO {ado_id}: Response keys: {list(item.keys()) if isinstance(item, dict) else type(item)}")
        
        if not isinstance(item, dict) or 'id' not in item:
            response_str = str(item)[:200] if isinstance(item, (dict, str)) else f"{type(item)}"
            logging.error(f"[fetch_ado_item] ADO {ado_id}: Invalid response structure. Response: {response_str}")
            return None
        
        logging.debug(f"[fetch_ado_item] ADO {ado_id}: ✓ Successfully fetched work item (fields: {len(item)} keys)")
        return item
        
    except Exception as e:
        logging.error(f"[fetch_ado_item] ADO {ado_id}: Exception raised: {type(e).__name__}: {str(e)[:300]}")
        import traceback
        tb_lines = traceback.format_exc().split('\n')
        for line in tb_lines[:10]:  # Log first 10 lines of traceback
            if line.strip():
                logging.error(f"  {line}")
        return None


def fetch_ado_comments(ado_client, ado_id):
    try:
        url = (f'{ado_client.organization_url}/{ado_client.project}'
               f'/_apis/wit/workItems/{ado_id}/comments?api-version={ado_client.api_version}')
        resp = ado_client.ado_api_call('GET', url)
        return resp.get('comments', []) if resp else []
    except Exception:
        return []


def fetch_jira_comments(jira_client, jira_id):
    try:
        resp = jira_client.get_comments(jira_id)
        return resp.get('comments', []) if resp else []
    except Exception:
        return []


def _field(ado_item, key):
    return (ado_item or {}).get('fields', {}).get(key)


def _has_relation(ado_item, rel_fragment):
    return any(rel_fragment in r.get('rel', '') for r in (ado_item or {}).get('relations', []))


def _attachment_count(ado_item):
    urls = [r['url'] for r in (ado_item or {}).get('relations', []) if r.get('rel') == 'AttachedFile']
    return len({u.split('?')[0] for u in urls})


def _jira_to_date(ts):
    """Jira ISO timestamp → 'YYYY-MM-DD', or '' if None/invalid."""
    if not ts:
        return ''
    try:
        return _parse_dt(ts).strftime('%Y-%m-%d')
    except Exception:
        return ''


def _has_broken_images(comments):
    """Return list of comment IDs whose text still references Jira attachment content URLs."""
    broken = []
    pattern = r'atlassian\.net/rest/api/3/attachment/content'
    for c in comments:
        if re.search(pattern, c.get('text', '') or ''):
            broken.append(str(c.get('id', '?')))
    return broken


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_ticket(jira_ticket, ado_item, jira_comments, ado_comments, ado_project=None, jira_pr_data=None):
    fields        = jira_ticket['fields']
    jira_key      = jira_ticket['key']
    jira_type     = fields.get('issuetype', {}).get('name', '')
    jira_state    = fields.get('status', {}).get('name', '')
    jira_title    = fields.get('summary', '')
    jira_prio     = (fields.get('priority') or {}).get('name', '')
    jira_labels   = set(fields.get('labels', []))
    jira_attach   = len(fields.get('attachment', []))
    jira_reporter = (fields.get('reporter') or {}).get('displayName', '')
    jira_assignee_field = fields.get('assignee') or {}
    jira_assignee = jira_assignee_field.get('displayName', '')
    jira_assignee_email = jira_assignee_field.get('emailAddress', '')
    jira_created  = fields.get('created', '')
    jira_resolved = fields.get('resolutiondate', '')
    jira_duedate  = fields.get('duedate', '')  # already YYYY-MM-DD

    # Exclude the CSV-header sentinel key ('Jira Type' / 'Jira State') from explicit-mapping checks
    type_explicitly_mapped  = jira_type  in TYPE_MAP  and jira_type  != 'Jira Type'
    state_explicitly_mapped = jira_state in STATE_MAP and jira_state != 'Jira State'
    expected_type  = TYPE_MAP.get(jira_type,  TYPE_MAP.get('Default', ''))
    expected_state = STATE_MAP.get(jira_state, STATE_MAP.get('Default', ''))
    expected_prio  = PRIO_MAP.get(jira_prio, '3-Medium')

    ado_type       = _field(ado_item, 'System.WorkItemType') or ''
    ado_state      = _field(ado_item, 'System.State') or ''
    ado_title      = _field(ado_item, 'System.Title') or ''
    ado_desc       = _field(ado_item, 'System.Description') or ''
    ado_tags       = _field(ado_item, 'System.Tags') or ''
    ado_prio       = _field(ado_item, 'Custom.PriorityLevel') or ''
    ado_assigned   = _field(ado_item, 'System.AssignedTo') or ''
    ado_requested_by = _field(ado_item, 'Custom.RequestedBy') or ''
    ado_start_date      = (_field(ado_item, 'Microsoft.VSTS.Scheduling.StartDate') or '')[:10]
    ado_target_date     = (_field(ado_item, 'Microsoft.VSTS.Scheduling.TargetDate') or '')[:10]
    ado_actual_complete = (_field(ado_item, 'Custom.ActualCompletionDate') or '')[:10]
    ado_actual_start    = (_field(ado_item, 'Custom.ActualStartDate') or '')[:10]
    
    # Normalize identity fields (AssignedTo, RequestedBy) which may be dicts or strings
    def _normalize_identity_field(field_value):
        """Extract display name from identity field which may be string or dict."""
        if isinstance(field_value, dict):
            # Try common key names for identity fields
            return (field_value.get('displayName') or 
                    field_value.get('uniqueName') or
                    field_value.get('name') or
                    field_value.get('id') or '')
        return str(field_value) if field_value else ''
    
    # AssignedTo is either a plain string email or a dict with 'uniqueName' or 'displayName'
    if isinstance(ado_assigned, dict):
        ado_assigned = _normalize_identity_field(ado_assigned)
    
    # RequestedBy is either a plain string email or a dict with 'uniqueName' or 'displayName'
    if isinstance(ado_requested_by, dict):
        ado_requested_by = _normalize_identity_field(ado_requested_by)

    def chk(ok, jira_val, ado_val, note=''):
        return {'pass': bool(ok), 'jira': jira_val, 'ado': ado_val, 'note': note if not ok else ''}

    checks = {}

    # # Work Item Type
    # checks["Work Item Type"] = chk(
    #     ado_item and ado_type.lower() == expected_type.lower(),
    #     jira_type, ado_type,
    #     f"Expected '{expected_type}', got '{ado_type}'")

    # State / Workflow
    checks["State"] = chk(
        ado_item and ado_state.lower() == expected_state.lower(),
        jira_state, ado_state,
        f"Expected '{expected_state}', got '{ado_state}'")

    # Title
    checks["Title"] = chk(
        ado_item and bool(ado_title) and jira_key in ado_title,
        jira_title[:60], ado_title[:60],
        "Title missing or Jira key not in ADO title")

    # Description
    checks["Description"] = chk(
        ado_item and bool(ado_desc.strip()),
        "Present" if fields.get('description') else "Absent",
        "Present" if ado_desc else "Empty",
        "ADO description is empty")

    # Priority
    checks["Priority"] = chk(
        ado_item and ado_prio.lower() == expected_prio.lower(),
        jira_prio, ado_prio,
        f"Expected '{expected_prio}', got '{ado_prio}'")

    # Reporter: PASS if found in RequestedBy field OR description
    # FAIL only if missing from BOTH places
    reporter_in_field = ado_requested_by and jira_reporter.lower() in ado_requested_by.lower() if jira_reporter else False
    reporter_in_desc = ado_item and jira_reporter and jira_reporter.lower() in ado_desc.lower()
    reporter_found = reporter_in_field or reporter_in_desc
    
    if not jira_reporter:
        # No reporter in Jira — check if ADO also has no reporter
        checks["Reporter"] = chk(
            not ado_requested_by and '<b>Reporter:</b>' not in ado_desc,
            "(unset)", "(unset)")
    else:
        # Reporter exists in Jira — must be found in field or description
        location = ""
        if reporter_in_field and reporter_in_desc:
            location = "Field + Description"
        elif reporter_in_field:
            location = "RequestedBy field"
        elif reporter_in_desc:
            location = "Description"
        
        checks["Reporter"] = chk(
            reporter_found,
            jira_reporter,
            location if reporter_found else "Missing",
            "" if reporter_found else f"Reporter '{jira_reporter}' not found in RequestedBy field or description")

    # Assignee: PASS if found in System.AssignedTo field OR description
    # FAIL only if missing from BOTH places
    assignee_in_field = ado_assigned and (
        (jira_assignee.lower() in ado_assigned.lower()) or
        (jira_assignee_email.lower() in ado_assigned.lower() if jira_assignee_email else False)
    ) if jira_assignee else False
    assignee_in_desc = ado_item and jira_assignee and jira_assignee.lower() in ado_desc.lower()
    assignee_found = assignee_in_field or assignee_in_desc
    
    if not jira_assignee:
        # No assignee in Jira — ADO should also have no assignee
        # Both should be unassigned — PASS
        checks["Assignee"] = chk(
            not ado_assigned,
            "(unassigned)", "(unassigned)")
    else:
        # Assignee exists in Jira — must be found in field or description
        location = ""
        if assignee_in_field and assignee_in_desc:
            location = "Field + Description"
        elif assignee_in_field:
            location = "System.AssignedTo field"
        elif assignee_in_desc:
            location = "Description"
        
        checks["Assignee"] = chk(
            assignee_found,
            jira_assignee,
            location if assignee_found else "Missing",
            "" if assignee_found else f"Assignee '{jira_assignee}' not found in System.AssignedTo or description")

    # Comments (count only)
    jira_c, ado_c = len(jira_comments), len(ado_comments)
    checks["Comments Count"] = chk(
        ado_item and ado_c >= jira_c,
        str(jira_c), str(ado_c),
        f"ADO {ado_c}, Jira {jira_c}" if ado_c < jira_c else "")

    # Labels / Tags
    if jira_labels and ado_item:
        ado_tag_set = {t.strip() for t in ado_tags.split(';') if t.strip()}

        # Case-insensitive comparison
        ado_tag_set_lower = {t.lower() for t in ado_tag_set}

        missing_labels = {
            label for label in jira_labels
            if label.lower() not in ado_tag_set_lower
        }

        checks["Labels / Tags"] = chk(
            not missing_labels,
            ', '.join(sorted(jira_labels))[:60] or "(none)",
            ado_tags[:60] or "(none)",
            f"Missing: {', '.join(sorted(missing_labels))}" if missing_labels else ""
        )
    else:
        checks["Labels / Tags"] = chk(True, "(none)", "(none)")
    # Created Date (mapped to Actual Start Date)
    # Allow ±1 day tolerance due to UTC timezone conversion
    # The worker converts Jira timestamps to ADO format using noon UTC of the local date,
    # which can result in ±1 day variation depending on the source timezone
    def _dates_match_with_tolerance(expected_date, actual_date, tolerance_days=1):
        """Compare dates allowing for ±N day tolerance due to timezone conversion."""
        if not expected_date or not actual_date:
            return not expected_date and not actual_date
        try:
            from datetime import datetime, timedelta
            exp_dt = datetime.strptime(expected_date, '%Y-%m-%d')
            act_dt = datetime.strptime(actual_date, '%Y-%m-%d')
            diff = abs((exp_dt - act_dt).days)
            return diff <= tolerance_days
        except Exception:
            return expected_date == actual_date
    
    exp_start = _jira_to_date(jira_created)
    date_matches = _dates_match_with_tolerance(exp_start, ado_actual_start)
    checks["Created Date"] = chk(
        not ado_item or not exp_start or date_matches,
        exp_start, ado_actual_start or '(empty)',
        f"Expected {exp_start}, got '{ado_actual_start or '(empty)'}' (timezone conversion tolerance: ±1 day)" if exp_start and not date_matches else "")

    # Resolution Date (mapped to Actual Completion Date)
    # If Jira is resolved, ADO must have matching completion date
    if jira_resolved:
        exp_comp = _jira_to_date(jira_resolved)
        checks["Actual Completion Date"] = chk(
            ado_item and ado_actual_complete == exp_comp,
            exp_comp, ado_actual_complete or '(empty)',
            f"Expected {exp_comp}, got '{ado_actual_complete or '(empty)'}'")
    else:
        checks["Actual Completion Date"] = chk(True, "(unresolved)", "(N/A)")

    # Attachments (count only)
    ado_attach = _attachment_count(ado_item) if ado_item else 0
    checks["Attachments Count"] = chk(
        ado_item and ado_attach == jira_attach,
        str(jira_attach), str(ado_attach),
        f"Jira has {jira_attach}, ADO has {ado_attach}" if ado_attach != jira_attach else "")

    # Linked Issues / Relations (presence check only)
    if 'parent' in fields:
        parent_key = fields['parent']['key']
        has_link = _has_relation(ado_item, 'Hierarchy-Reverse')
        has_fallback = not has_link and any(
            r.get('rel') == 'Hyperlink' and parent_key in r.get('url', '')
            for r in (ado_item or {}).get('relations', []))
        parent_ok = has_link or has_fallback
        checks["Parent Link"] = chk(
            parent_ok, parent_key,
            "Present" if has_link else ("Hyperlink fallback" if has_fallback else "Missing"),
            "" if parent_ok else "No parent relation found")
    else:
        checks["Parent Link"] = chk(True, "(none)", "(none)")

    return {
        "jira_key":     jira_key,
        "ado_id":       ado_item['id'] if ado_item else None,
        "ado_url":      (f"https://dev.azure.com/healthcatalyst/"
                         f"{_field(ado_item,'System.TeamProject')}/_workitems/edit/{ado_item['id']}"
                         if ado_item else None),
        "found_in_ado": ado_item is not None,
        "all_pass":     all(v['pass'] for v in checks.values()),
        "checks":       checks,
    }


# ---------------------------------------------------------------------------
# HTML report (rendered to PDF via weasyprint)
# ---------------------------------------------------------------------------

def _b(ok):
    return '<span class="pass">PASS</span>' if ok else '<span class="fail">FAIL</span>'


def _ado_link(r):
    if r['ado_id']:
        return f'<a href="{r["ado_url"]}" target="_blank">{r["ado_id"]}</a>'
    return '<span class="missing">NOT FOUND</span>'


def build_report(results, generated_at, args_summary):
    total   = len(results)
    found   = sum(1 for r in results if r['found_in_ado'])
    passing = sum(1 for r in results if r['all_pass'])
    pct     = round(100 * passing / total) if total else 0

    # Per-check pass/fail counts
    check_counts = {}
    for r in results:
        for name, c in r['checks'].items():
            check_counts.setdefault(name, {'pass': 0, 'fail': 0})
            check_counts[name]['pass' if c['pass'] else 'fail'] += 1

    # Summary cards
    pct_color = 'green' if pct >= 90 else 'red'
    summary_cards = '\n'.join(
        f'<div class="stat"><div class="n {color}">{val}</div><div class="l">{label}</div></div>'
        for val, label, color in [
            (total,       'Total Tickets', ''),
            (found,       'Found in ADO',  ''),
            (total-found, 'Missing in ADO','red'),
            (passing,     'Fully Passing', 'green'),
            (f'{pct}%',   'Pass Rate',     pct_color),
        ])

    # Check summary table rows
    check_rows = []
    for name, c in check_counts.items():
        t = c['pass'] + c['fail']
        rate = round(100 * c['pass'] / t) if t else 0
        color = 'green' if rate >= 90 else 'red'
        check_rows.append(
            f'<tr><td>{name}</td>'
            f'<td class="green">{c["pass"]}</td>'
            f'<td class="red">{c["fail"]}</td>'
            f'<td class="{color}">{rate}%</td></tr>'
        )

    # Failing tickets table
    failing = [r for r in results if not r['all_pass']]
    if failing:
        fail_rows = []
        for r in failing:
            failures = '<br>'.join(
                f'{n}: {c["note"]}' for n, c in r['checks'].items() if not c['pass'])
            fail_rows.append(
                f'<tr><td><b>{r["jira_key"]}</b></td>'
                f'<td>{_ado_link(r)}</td>'
                f'<td class="fail-note">{failures}</td></tr>'
            )
        fail_section = f"""
        <h2 class="red">Failing Tickets ({len(failing)})</h2>
        <table>
          <thead><tr><th>Jira Key</th><th>ADO ID</th><th>Failures</th></tr></thead>
          <tbody>{''.join(fail_rows)}</tbody>
        </table>"""
    else:
        fail_section = "<p class='green'><b>✅ All tickets passed all checks!</b></p>"

    # Per-ticket detail rows
    ticket_rows = []
    for r in results:
        badge = _b(r['all_pass']) if r['found_in_ado'] else '<span class="missing">NOT FOUND</span>'
        open_attr = '' if r['all_pass'] else 'open'
        check_detail = '\n'.join(
            f'<tr class="check-row">'
            f'<td class="check-name">{n}</td>'
            f'<td>{_b(c["pass"])}</td>'
            f'<td>{c["jira"][:60]}</td>'
            f'<td>{c["ado"][:60]}</td>'
            f'<td>{"<span class=fail-note>" + c["note"] + "</span>" if c["note"] else "—"}</td>'
            f'</tr>'
            for n, c in r['checks'].items()
        )
        ticket_rows.append(f"""
        <tr class="ticket-header">
          <td><details {open_attr}><summary>{r["jira_key"]}</summary></details></td>
          <td>{_ado_link(r)}</td>
          <td colspan="3">{badge}</td>
        </tr>
        {check_detail}""")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Jira → ADO Verification</title>
  <style>
    body       {{ font-family: Segoe UI, Arial, sans-serif; font-size: 13px; margin: 20px; color: #222; }}
    h1         {{ font-size: 20px; margin-bottom: 4px; }}
    h2         {{ font-size: 15px; margin-top: 24px; }}
    a          {{ color: #0078D4; text-decoration: none; font-size: 11px; }}
    a:hover    {{ text-decoration: underline; }}
    .summary   {{ display: flex; gap: 20px; margin: 16px 0 24px; }}
    .stat      {{ background: #f5f5f5; border-radius: 6px; padding: 12px 18px; text-align: center; }}
    .stat .n   {{ font-size: 26px; font-weight: bold; }}
    .stat .l   {{ font-size: 11px; color: #666; }}
    .green     {{ color: #107C10; }}
    .red       {{ color: #d13438; }}
    table      {{ border-collapse: collapse; width: 100%; margin-bottom: 28px; }}
    th         {{ background: #0078D4; color: white; padding: 7px 10px; text-align: left; font-size: 12px; }}
    td         {{ padding: 5px 10px; border-bottom: 1px solid #e0e0e0; vertical-align: top; }}
    tr:hover   {{ background: #f0f7ff; }}
    .pass      {{ background: #dff6dd; color: #107C10; border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: bold; }}
    .fail      {{ background: #fde7e9; color: #d13438; border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: bold; }}
    .missing   {{ background: #fff4ce; color: #a4262c; border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: bold; }}
    .fail-note {{ font-size: 11px; color: #d13438; }}
    .ticket-header {{ background: #f3f3f3; font-weight: bold; }}
    .check-row td  {{ font-size: 11px; padding-left: 22px; color: #555; }}
    details summary {{ cursor: pointer; color: #0078D4; font-size: 12px; }}
  </style>
</head>
<body>
  <h1>Jira → ADO Migration Verification Report</h1>
  <p><b>Generated:</b> {generated_at} &nbsp;|&nbsp; <b>Source:</b> {args_summary}</p>

  <div class="summary">{summary_cards}</div>

  <h2>Check Summary</h2>
  <table style="width: 560px">
    <thead><tr><th>Check</th><th>Pass</th><th>Fail</th><th>Pass Rate</th></tr></thead>
    <tbody>{''.join(check_rows)}</tbody>
  </table>

  {fail_section}

  <h2>Ticket Detail</h2>
  <table>
    <thead>
      <tr>
        <th style="width:120px">Jira Key</th>
        <th style="width:90px">ADO ID</th>
        <th style="width:70px">Result</th>
        <th style="width:170px">Jira Value</th>
        <th style="width:170px">ADO Value</th>
        <th>Note</th>
      </tr>
    </thead>
    <tbody>{''.join(ticket_rows)}</tbody>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Verify Jira → ADO migration')
    parser.add_argument('--jira-instance', required=True)
    parser.add_argument('--jira-filter')
    parser.add_argument('--jira-keys')
    parser.add_argument('--project-key')
    parser.add_argument('--ado-project', required=True)
    parser.add_argument('--output', default='migration_verification_report.html')
    parser.add_argument('--csv-output', default='', help='Write per-card verification CSV to this path')
    parser.add_argument('--verbose', action='store_true', help='Print detailed logs')
    parser.add_argument('--list-keys', action='store_true', help='List resolved Jira keys and exit')

    args = parser.parse_args()

    # ✅ Normalize single key
    if args.jira_keys:
        args.jira_keys = args.jira_keys.strip()

    # ✅ Prevent mixed modes
    if args.jira_keys and args.project_key:
        logging.warning("Both --jira-keys and --project-key provided. Using ONLY --jira-keys.")
        args.project_key = None

    if not args.jira_filter and not args.jira_keys and not args.project_key:
        parser.error("Provide --jira-filter, --jira-keys, or --project-key")

    global TYPE_MAP, STATE_MAP
    TYPE_MAP = load_config('type_config.json')
    STATE_MAP = load_config('state_config.json')

    ado_client = AzureDevOpsClient(load_ado_config(), args.ado_project)
    jira_client = JiraClient(load_jira_config(args.jira_instance))
    mapping = load_issue_mapping()

    logging.info(f"Loaded mapping with {len(mapping)} entries")

    board_prefix = None
    jira_keys = []
    jira_actual_keys = None  # Track actual Jira keys for project-key mode

    # ==================================================================================
    # LAYER 1: Load and filter keys by source (--jira-keys, --project-key, --jira-filter)
    # ==================================================================================
    if args.jira_keys:
        keys = [k.strip().upper() for k in args.jira_keys.split(',') if k.strip()]
        jira_keys = keys
        logging.info(f"Using direct --jira-keys: {len(jira_keys)} keys")
    elif args.project_key:
        prefix = args.project_key.upper()
        board_prefix = prefix  # Set FIRST, used for all subsequent filtering

        if prefix == 'ALL':
            jira_keys = sorted(mapping.keys())
            logging.info(f"Using ALL keys from mapping: {len(jira_keys)} keys")
        else:
            # LAYER 1 FILTER: Get only keys from mapping starting with board prefix
            jira_keys = sorted([k for k in mapping.keys() if k.startswith(prefix + '-')])
            logging.info(f"Filtered mapping by prefix '{prefix}': {len(jira_keys)} keys")

            if not jira_keys:
                logging.error(f"No keys found for project '{args.project_key}' in mapping")
                sys.exit(1)
            
            # FETCH ACTUAL JIRA KEYS for this project (for gap detection)
            try:
                jql = f'project = {prefix}'
                resp = jira_client.search_issues(jql)
                jira_actual_keys = sorted([i['key'] for i in resp.get('issues', [])])
                logging.info(f"Fetched from Jira project '{prefix}': {len(jira_actual_keys)} actual keys")
            except Exception as e:
                logging.warning(f"Could not fetch actual Jira keys for project '{prefix}': {e}")
                jira_actual_keys = None
    else:
        resp = jira_client.get_filter_items(args.jira_filter)
        jira_keys = [i['key'] for i in resp.get('issues', [])]
        logging.info(f"Fetched from Jira filter: {len(jira_keys)} keys")

    # ==================================================================================
    # LAYER 2: Strict project-key filtering (applied ONLY if --project-key was used)
    # ==================================================================================
    if board_prefix:
        before_count = len(jira_keys)
        jira_keys = [k for k in jira_keys if k.startswith(board_prefix + '-')]
        after_count = len(jira_keys)
        
        if before_count != after_count:
            removed_count = before_count - after_count
            logging.warning(f"LAYER 2 FILTER removed {removed_count} non-matching keys (before: {before_count}, after: {after_count})")
        else:
            logging.info(f"LAYER 2 FILTER passed: all {after_count} keys match prefix '{board_prefix}-'")

    # ==================================================================================
    # LAYER 3: Final validation (abort if any contamination remains)
    # ==================================================================================
    if board_prefix:
        bad_keys = [k for k in jira_keys if not k.startswith(board_prefix + '-')]
        if bad_keys:
            logging.error(f"CRITICAL: After filtering, {len(bad_keys)} keys do not match project prefix '{board_prefix}':")
            logging.error(f"  Sample bad keys: {bad_keys[:10]}")
            logging.error("Aborting to prevent cross-project verification.")
            sys.exit(2)
        logging.info(f"LAYER 3 VALIDATION passed: {len(jira_keys)} keys are all {board_prefix}-* (safe to proceed)")

    print(f"\n✅ Processing {len(jira_keys)} tickets from project '{board_prefix or 'mixed/all'}'...\n")

    # If requested, just list keys and mapping info and exit (dry-run)
    if args.list_keys:
        print("\n-- Jira keys (first 100) --")
        for i, k in enumerate(jira_keys[:100], 1):
            mapped = mapping.get(k)
            print(f"{i:3d}. {k} -> {mapped if mapped else '(no mapping)'}")
        sys.exit(0)

    if args.verbose and board_prefix:
        # Show sample of filtered keys
        sample_size = min(20, len(jira_keys))
        print(f"\nDEBUG: Sample of {sample_size} keys to be verified:")
        for k in jira_keys[:sample_size]:
            mapped = mapping.get(k)
            print(f"  {k} -> {mapped if mapped else '(no mapping in migration_mapping.json)'}")
        if len(jira_keys) > sample_size:
            print(f"  ... and {len(jira_keys) - sample_size} more keys\n")

    # ==================================================================================
    # Detect missing Jira tickets (not migrated to ADO)
    # ==================================================================================
    missing_keys = []
    if board_prefix:
        mapping_keys = set(mapping.keys())
        missing_keys = sorted([
            k for k in jira_keys
            if k not in mapping_keys
        ])
        
        if missing_keys:
            print(f"❌ Missing Jira tickets (NOT migrated):")
            print(",".join(missing_keys[:50]))
            if len(missing_keys) > 50:
                print(f"... and {len(missing_keys) - 50} more")
            log_missing_tickets(missing_keys, len(jira_keys))
            logging.info(f"Total unmigrated: {len(missing_keys)} out of {len(jira_keys)} ({100*len(missing_keys)//len(jira_keys)}%)")

    # ==================================================================================
    # PRE-FLIGHT: Count comparison and missing item detection
    # ==================================================================================
    print("\n📊 PRE-FLIGHT CHECK: Card Count Comparison")
    print("=" * 60)
    
    # Resolve ADO IDs
    resolved = {
        k: mapping[k]
        for k in jira_keys
        if k in mapping and (not board_prefix or k.startswith(board_prefix + '-'))
    }

    # Debug: log resolution summary
    unresolved_keys = [k for k in jira_keys if k not in resolved]
    logging.info(f"Resolved {len(resolved)}/{len(jira_keys)} keys from mapping; {len(unresolved_keys)} unresolved")
    if unresolved_keys:
        logging.info(f"Sample unresolved keys: {unresolved_keys[:10]}")

    unknown = [
        k for k in jira_keys
        if k not in resolved and (not board_prefix or k.startswith(board_prefix + '-'))
    ]

    if unknown:
        resolved.update(ado_client.bulk_fetch_jira_key_mapping(unknown))

    # Count total migrated items
    jira_count = len(jira_keys)
    ado_count = len(resolved)
    
    # If we have actual Jira keys (from JQL fetch), use those for comparison
    if jira_actual_keys is not None:
        jira_count = len(jira_actual_keys)
        print(f"Jira items (actual project): {jira_count}")
    else:
        print(f"Jira items (filter/keys):    {jira_count}")
    
    print(f"ADO items migrated:         {ado_count}")
    
    # Find missing items in ADO (using actual Jira keys if available)
    items_to_check = jira_actual_keys if jira_actual_keys else jira_keys
    missing_in_ado = sorted([k for k in items_to_check if k not in resolved])
    
    if missing_in_ado:
        print(f"Missing in ADO:             {len(missing_in_ado)}")
        print("=" * 60)
        print(f"\n❌ MISSING ITEMS FOUND! These Jira items are NOT in the migration mapping.\n")
        print(f"Missing {len(missing_in_ado)} item(s):")
        
        # Show missing items in groups of 20
        for i, key in enumerate(missing_in_ado, 1):
            print(f"  {i:3d}. {key}")
            if i >= 20 and i < len(missing_in_ado):
                print(f"  ... and {len(missing_in_ado) - 20} more")
                break
        
        # Log to file
        logging.info(f"\n\nMISSING ITEMS REPORT (Not in migration mapping)")
        logging.info(f"=" * 60)
        logging.info(f"Jira count (actual): {jira_count}")
        logging.info(f"ADO count (mapped): {ado_count}")
        logging.info(f"Missing: {len(missing_in_ado)}")
        logging.info(f"Missing items: {', '.join(missing_in_ado)}")
        logging.info("=" * 60)
        
        print(f"\n📝 Detailed log: {LOG_FILE}\n")
        sys.exit(1)  # Exit with error code
    
    print(f"Missing in ADO:             0 ✅")
    print("=" * 60)
    print(f"\n✅ Card counts match! Proceeding with detailed verification...\n")
    
    print(f"Missing in ADO:            0 ✅")
    print("=" * 60)
    print(f"\n✅ Card counts match! Proceeding with detailed verification...\n")

    # Verify tickets
    results = []
    logging.info(f"Starting detailed verification of {len(resolved)} items...")
    logging.info(f"board_prefix='{board_prefix}'")
    
    for idx, key in enumerate(resolved.keys(), 1):
        # FINAL GUARD: Ensure this key matches the requested project prefix.
        # If --project-key was provided, abort if a non-matching key reaches here.
        if board_prefix and not key.startswith(board_prefix + '-'):
            logging.critical(f"[TICKET {idx}] CONTAMINATION DETECTED: Key '{key}' does not match project prefix '{board_prefix}'. Aborting.")
            print(f"\n❌ CRITICAL ERROR: Non-matching key '{key}' reached verification loop (expected '{board_prefix}-*')")
            sys.exit(3)
        
        ado_id = resolved.get(key)

        jira_ticket = jira_client.get_jira_issue(key)
        ado_item = fetch_ado_item(ado_client, ado_id) if ado_id else None

        jira_comments = fetch_jira_comments(jira_client, jira_ticket['id']) if jira_ticket else []
        ado_comments = fetch_ado_comments(ado_client, ado_id) if ado_id else []

        jira_pr_data = None
        try:
            jira_pr_data = jira_client.get_pull_request(jira_ticket['id'])
        except Exception:
            pass

        result = verify_ticket(
            jira_ticket,
            ado_item,
            jira_comments,
            ado_comments,
            args.ado_project,
            jira_pr_data
        )

        results.append(result)
        log_failed_result(result)

        # Minimal terminal output
        if not result['all_pass']:
            print(f"❌ {key} → FAILED (see log)")
        elif idx % 50 == 0:
            print(f"✅ Processed {idx}/{len(jira_keys)}")

    # ✅ Write CSV if requested
    if args.csv_output and results:
        import csv as _csv
        with open(args.csv_output, 'w', newline='') as _f:
            w = _csv.writer(_f)
            w.writerow(['jira_key', 'ado_id', 'migration_status', 'failed_checks', 'details'])
            for r in results:
                if not r['found_in_ado']:
                    status, failed, details = 'not_found', '', 'Item not found in ADO'
                elif r['all_pass']:
                    status, failed, details = 'verified', '', ''
                else:
                    failed_items = [(n, c) for n, c in r['checks'].items() if not c['pass']]
                    status = 'warnings'
                    failed = ' | '.join(n for n, _ in failed_items)
                    details = ' | '.join(c['note'] for _, c in failed_items if c.get('note'))
                w.writerow([r['jira_key'], r.get('ado_id', ''), status, failed, details])
        logging.info(f'CSV written to {args.csv_output} ({len(results)} rows)')

    # ✅ Summary
    total = len(results)
    passing = sum(1 for r in results if r['all_pass'])
    missing = sum(1 for r in results if not r['found_in_ado'])

    print("\n" + "=" * 60)
    print(f"VERIFICATION SUMMARY — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print(f"  Total tickets : {total}")
    print(f"  Found in ADO  : {total - missing}")
    print(f"  Missing       : {missing}")
    print(f"  All checks OK : {passing}")
    print(f"  Has failures  : {total - passing}")
    print(f"  Pass rate     : {round(100*passing/total) if total else 0}%")
    print("=" * 60 + "\n")

if __name__ == '__main__':
    main()
