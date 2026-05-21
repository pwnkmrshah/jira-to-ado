"""
verify_migration.py — Post-migration verification: Jira → ADO.

Checks per ticket:
  2   Work item type mapping
  3   State / workflow mapping
  4a  Title present (includes Jira key)
  4b  Description present
  4c  Priority mapped
  4d  Reporter in description
  4e  Assignee: if Jira has assignee → must appear in ADO AssignedTo and/or description;
               if Jira has NO assignee → ADO AssignedTo must be null
  5   Comments count (ADO >= Jira)
  6   Labels / tags
  7a  Start Date field removed — ADO value must be empty
  7b  Actual Completion Date matches Jira resolutiondate (not migration date)
  7c  Actual Start Date matches Jira created date
  7d  Target Date matches Jira due date (if set)
  8   Attachments count matches Jira
  8b  No duplicate attachment relations
  8c  No broken Jira image URLs in comments
  9   Hierarchy / parent link

Generates migration_verification_report.html (convert to PDF via weasyprint).

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

PRIO_MAP = {'Highest': '1-Critical', 'High': '2-High', 'Medium': '3-Medium', 'Low': '4-Low', 'Lowest': '4-Low'}
TYPE_MAP  = {}
STATE_MAP = {}


def load_config(filename):
    path = Path(__file__).parent.parent.parent / "config" / filename
    return json.loads(path.read_text()) if path.exists() else {}


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_ado_item(ado_client, ado_id):
    try:
        return ado_client.get_work_item_full(ado_id)
    except Exception as e:
        logging.warning(f"Could not fetch ADO item {ado_id}: {e}")
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

def verify_ticket(jira_ticket, ado_item, jira_comments, ado_comments):
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
    ado_start_date      = (_field(ado_item, 'Microsoft.VSTS.Scheduling.StartDate') or '')[:10]
    ado_target_date     = (_field(ado_item, 'Microsoft.VSTS.Scheduling.TargetDate') or '')[:10]
    ado_actual_complete = (_field(ado_item, 'Custom.ActualCompletionDate') or '')[:10]
    ado_actual_start    = (_field(ado_item, 'Custom.ActualStartDate') or '')[:10]
    # AssignedTo is either a plain string email or a dict with 'uniqueName'
    if isinstance(ado_assigned, dict):
        ado_assigned = ado_assigned.get('uniqueName', '') or ado_assigned.get('displayName', '')

    def chk(ok, jira_val, ado_val, note=''):
        return {'pass': bool(ok), 'jira': jira_val, 'ado': ado_val, 'note': note if not ok else ''}

    checks = {}

    checks["2 - Work Item Type Mapping"] = chk(
        ado_item and ado_type.lower() == expected_type.lower(),
        f"{jira_type} → expected {expected_type}", ado_type,
        f"Expected '{expected_type}', got '{ado_type}'")

    checks["3 - State / Workflow Mapping"] = chk(
        ado_item and ado_state.lower() == expected_state.lower(),
        f"{jira_state} → expected {expected_state}", ado_state,
        f"Expected '{expected_state}', got '{ado_state}'")

    checks["4a - Title Present"] = chk(
        ado_item and bool(ado_title) and jira_key in ado_title,
        jira_title[:80], ado_title[:80],
        "Title missing or Jira key not in ADO title")

    checks["4b - Description Present"] = chk(
        ado_item and bool(ado_desc.strip()),
        "Has description" if fields.get('description') else "No description",
        f"{len(ado_desc)} chars" if ado_desc else "EMPTY",
        "ADO description is empty")

    checks["4c - Priority Mapped"] = chk(
        ado_item and ado_prio.lower() == expected_prio.lower(),
        f"{jira_prio} → expected {expected_prio}", ado_prio,
        f"Expected '{expected_prio}', got '{ado_prio}'")

    checks["4d - Reporter in Description"] = chk(
        ado_item and '<b>Reporter:</b>' in ado_desc,
        jira_reporter,
        "Reporter line found" if (ado_item and '<b>Reporter:</b>' in ado_desc) else "MISSING",
        "Reporter line not found in ADO description")

    # 4e — Assignee
    # Rule 1: No Jira assignee → ADO System.AssignedTo must also be null/empty
    # Rule 2: Jira has assignee → description MUST contain '<b>Assignee:</b> name'
    # Rule 3: If ADO System.AssignedTo is set → must match the Jira assignee email
    #         If ADO System.AssignedTo is empty → description-only fallback is acceptable
    if not jira_assignee:
        if ado_assigned:
            checks["4e - Assignee"] = chk(
                False, "(unassigned)", ado_assigned,
                f"Jira has no assignee but ADO AssignedTo is '{ado_assigned}'")
        else:
            checks["4e - Assignee"] = chk(True, "(unassigned)", "(null) ✓")
    else:
        desc_has_assignee = ado_item and jira_assignee in ado_desc
        ado_has_field     = bool(ado_assigned)
        if ado_has_field:
            # Assignee exists in ADO — verify it matches and is also in description
            email_match = jira_assignee_email.lower() in ado_assigned.lower() if jira_assignee_email else True
            if not email_match:
                note = f"ADO AssignedTo '{ado_assigned}' does not match Jira assignee '{jira_assignee} ({jira_assignee_email})'"
                checks["4e - Assignee"] = chk(False, f"{jira_assignee} ({jira_assignee_email})", ado_assigned, note)
            elif not desc_has_assignee:
                note = f"ADO AssignedTo is set correctly but assignee name missing from description"
                checks["4e - Assignee"] = chk(False, jira_assignee, "Not in description", note)
            else:
                checks["4e - Assignee"] = chk(True, f"{jira_assignee} ({jira_assignee_email})", f"AssignedTo={ado_assigned}")
        else:
            # ADO has no AssignedTo — description fallback must be present
            if desc_has_assignee:
                checks["4e - Assignee"] = chk(True, jira_assignee, "Description fallback (user not in ADO)")
            else:
                note = (f"Jira assignee '{jira_assignee}' not in ADO AssignedTo field "
                        f"and not found in description")
                checks["4e - Assignee"] = chk(False, jira_assignee, "(missing)", note)

    jira_c, ado_c = len(jira_comments), len(ado_comments)
    checks["5 - Comments Count (ADO >= Jira)"] = chk(
        ado_item and ado_c >= jira_c,
        str(jira_c), str(ado_c),
        f"ADO has {ado_c} comments, Jira has {jira_c}")

    if jira_labels and ado_item:
        ado_tag_set = {t.strip() for t in ado_tags.split(';') if t.strip()}
        missing_labels = jira_labels - ado_tag_set
        checks["6 - Labels / Tags"] = chk(
            not missing_labels,
            ', '.join(sorted(jira_labels)), ado_tags[:100] or "(none)",
            f"Missing: {', '.join(sorted(missing_labels))}")
    else:
        checks["6 - Labels / Tags"] = chk(True, "(none)", "(none)")

    # 7a — Start Date: field was removed from ADO; verify it is now empty
    exp_start = _jira_to_date(jira_created)
    checks["7a - Start Date (Removed)"] = chk(
        not ado_item or not ado_start_date,
        "(empty — field removed)", ado_start_date or '(empty)',
        f"StartDate field should be empty after removal, but got '{ado_start_date}'")

    # 7b — Actual Completion Date: Jira resolutiondate → Custom.ActualCompletionDate
    MIGRATION_DATE = '2026-05-16'
    if jira_resolved:
        exp_comp = _jira_to_date(jira_resolved)
        is_migration_date = ado_actual_complete == MIGRATION_DATE
        checks["7b - Actual Completion Date"] = chk(
            not ado_item or ado_actual_complete == exp_comp,
            exp_comp, ado_actual_complete or '(empty)',
            f"Got '{ado_actual_complete or '(empty)'}'"
            + (" — still set to migration date!" if is_migration_date else ""))
    else:
        checks["7b - Actual Completion Date"] = chk(True, "(unresolved)", "(N/A)")

    # 7c — Actual Start Date: Jira created → Custom.ActualStartDate
    checks["7c - Actual Start Date"] = chk(
        not ado_item or not exp_start or ado_actual_start == exp_start,
        exp_start, ado_actual_start or '(empty)',
        f"Expected {exp_start}, got '{ado_actual_start or '(empty)'}'")

    # 7d — Target Date: Jira duedate → Microsoft.VSTS.Scheduling.TargetDate
    if jira_duedate:
        checks["7d - Target Date (Due Date)"] = chk(
            not ado_item or ado_target_date == jira_duedate,
            jira_duedate, ado_target_date or '(empty)',
            f"Expected {jira_duedate}, got '{ado_target_date or '(empty)'}'")
    else:
        checks["7d - Target Date (Due Date)"] = chk(True, "(no due date)", "(N/A)")

    if jira_attach == 0:
        checks["8 - Attachments"] = chk(True, "0", "0")
    else:
        ado_attach = _attachment_count(ado_item)
        exact_match = ado_item and ado_attach == jira_attach
        enough      = ado_item and ado_attach >= jira_attach
        if exact_match:
            note = ''
        elif ado_attach > jira_attach:
            note = f"ADO has {ado_attach - jira_attach} extra (duplicate) attachment(s) — Jira: {jira_attach}, ADO: {ado_attach}"
        else:
            note = f"ADO is missing attachments — Jira: {jira_attach}, ADO: {ado_attach}"
        checks["8 - Attachments"] = chk(
            exact_match,
            str(jira_attach), str(ado_attach),
            note)

    # 8b — Duplicate Attachments: raw relation count vs deduplicated
    raw_attach_urls    = [r['url'] for r in (ado_item or {}).get('relations', []) if r.get('rel') == 'AttachedFile']
    unique_attach_urls = {u.split('?')[0] for u in raw_attach_urls}
    dupe_count = len(raw_attach_urls) - len(unique_attach_urls)
    checks["8b - No Duplicate Attachments"] = chk(
        not ado_item or dupe_count == 0,
        f"{jira_attach} unique",
        f"{len(raw_attach_urls)} total / {len(unique_attach_urls)} unique",
        f"{dupe_count} duplicate relation(s) detected")

    # 8c — Broken Image URLs: any ADO comment still points to atlassian.net attachment URLs
    broken_img_comments = _has_broken_images(ado_comments)
    checks["8c - No Broken Images in Comments"] = chk(
        not broken_img_comments,
        "No broken images",
        f"{len(broken_img_comments)} comment(s) with broken img URLs" if broken_img_comments else "OK",
        f"Comment ID(s) with Jira img src: {', '.join(broken_img_comments)}")

    if 'parent' in fields:
        parent_key  = fields['parent']['key']
        has_link     = _has_relation(ado_item, 'Hierarchy-Reverse')
        has_fallback = not has_link and any(
            r.get('rel') == 'Hyperlink' and parent_key in r.get('url', '')
            for r in (ado_item or {}).get('relations', []))
        parent_ok = has_link or has_fallback
        checks["9 - Hierarchy / Parent Link"] = chk(
            parent_ok, parent_key,
            "Jira hyperlink (parent not migrated)" if has_fallback else ("Parent link found" if has_link else "(none)"),
            "Parent (Hierarchy-Reverse) relation missing in ADO")
    else:
        checks["9 - Hierarchy / Parent Link"] = chk(True, "(none)", "(none)")

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
    parser.add_argument('--jira-filter',   help='Jira filter ID')
    parser.add_argument('--jira-keys',     help='Comma-separated Jira keys')
    parser.add_argument('--project-key',   help='Board prefix (e.g. HIVE) or ALL — reads keys from migration_mapping.json')
    parser.add_argument('--ado-project',   required=True)
    parser.add_argument('--output',        default='migration_verification_report.html')
    args = parser.parse_args()

    if not args.jira_filter and not args.jira_keys and not args.project_key:
        parser.error("Provide --jira-filter, --jira-keys, or --project-key")

    global TYPE_MAP, STATE_MAP
    TYPE_MAP  = load_config('type_config.json')
    STATE_MAP = load_config('state_config.json')

    ado_client  = AzureDevOpsClient(load_ado_config(), args.ado_project)
    jira_client = JiraClient(load_jira_config(args.jira_instance))
    mapping     = load_issue_mapping()
    logging.info(f"Loaded mapping with {len(mapping)} entries")

    # --project-key: derive key list from migration_mapping.json
    if args.project_key:
        prefix = args.project_key.upper()
        if prefix == 'ALL':
            project_keys = sorted(mapping.keys())
        else:
            project_keys = sorted(k for k in mapping.keys() if k.startswith(prefix + '-'))
        if not project_keys:
            logging.error(f"No keys found for project '{args.project_key}' in migration_mapping.json")
            sys.exit(1)
        args.jira_keys = ','.join(project_keys)
        logging.info(f"--project-key {args.project_key}: resolved {len(project_keys)} key(s)")

    jira_tickets = []
    if args.jira_keys:
        keys = [k.strip() for k in args.jira_keys.split(',') if k.strip()]
        logging.info(f"Fetching {len(keys)} Jira ticket(s) by key ...")
        for key in keys:
            t = jira_client.get_jira_issue(key)
            if t:
                jira_tickets.append(t)
            else:
                logging.warning(f"Could not fetch {key}")
    else:
        logging.info(f"Fetching Jira filter {args.jira_filter} ...")
        resp = jira_client.get_filter_items(args.jira_filter)
        if not resp or 'issues' not in resp:
            logging.error("No issues returned from Jira filter")
            sys.exit(1)
        for key in [i['key'] for i in resp['issues']]:
            t = jira_client.get_jira_issue(key)
            if t:
                jira_tickets.append(t)

    logging.info(f"Verifying {len(jira_tickets)} tickets ...")

    all_keys = [t['key'] for t in jira_tickets]
    resolved = {k: mapping[k] for k in all_keys if k in mapping}
    unknown  = [k for k in all_keys if k not in mapping]
    if unknown:
        logging.info(f"Resolving {len(unknown)} uncached ADO IDs via WIQL ...")
        resolved.update(ado_client.bulk_fetch_jira_key_mapping(unknown))
    else:
        logging.info(f"All {len(all_keys)} ADO IDs resolved from local mapping cache.")

    all_results = []
    for idx, ticket in enumerate(jira_tickets, 1):
        key    = ticket['key']
        ado_id = resolved.get(key)
        logging.info(f"[{idx}/{len(jira_tickets)}] Verifying {key} → ADO {ado_id} ...")

        ado_item      = fetch_ado_item(ado_client, ado_id) if ado_id else None
        jira_comments = fetch_jira_comments(jira_client, ticket['id'])
        ado_comments  = fetch_ado_comments(ado_client, ado_id) if ado_id else []

        result = verify_ticket(ticket, ado_item, jira_comments, ado_comments)
        all_results.append(result)

        icon = "✅" if result['all_pass'] else ("⚠️  NOT FOUND" if not result['found_in_ado'] else "❌")
        print(f"  {icon}  {key:<12} → ADO {ado_id or 'N/A'}")

    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if args.project_key:
        scope_str = f"project-key={args.project_key}"
    elif args.jira_filter:
        scope_str = f"filter={args.jira_filter}"
    else:
        scope_str = f"keys={args.jira_keys}"
    args_summary = f"jira-instance={args.jira_instance}  {scope_str}  ado-project={args.ado_project}"

    Path(args.output).write_text(build_report(all_results, generated_at, args_summary), encoding='utf-8')
    logging.info(f"\nReport written to: {Path(args.output).resolve()}")

    total   = len(all_results)
    passing = sum(1 for r in all_results if r['all_pass'])
    missing = sum(1 for r in all_results if not r['found_in_ado'])
    print(f"\n{'='*60}")
    print(f"VERIFICATION SUMMARY — {generated_at}")
    print(f"{'='*60}")
    print(f"  Total tickets : {total}")
    print(f"  Found in ADO  : {total - missing}")
    print(f"  Missing       : {missing}")
    print(f"  All checks OK : {passing}")
    print(f"  Has failures  : {total - passing}")
    print(f"  Pass rate     : {round(100*passing/total) if total else 0}%")
    print(f"  Report        : {Path(args.output).resolve()}")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()
