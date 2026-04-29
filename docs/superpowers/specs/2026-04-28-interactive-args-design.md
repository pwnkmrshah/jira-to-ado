# Design: Interactive Arguments for worker_jira_to_ado_copy.py

## Summary

Update `worker_jira_to_ado_copy.py` to accept three arguments either as CLI flags or interactively via `input()` prompts when flags are omitted. Remove the unused `copy` subcommand structure.

## Changes

### Argument Parsing

Remove `subparsers` and the `copy` subparser. Move all three arguments directly onto the top-level `ArgumentParser` as optional flags:

- `--jira-instance` — Jira source instance name (subdomain from URL)
- `--jira-filter` — Jira filter ID
- `--ado-project` — ADO target project name

### Interactive Fallback

After `parse_args()`, check each argument. If `None`, prompt the user:

```python
if not args.jira_instance:
    args.jira_instance = input("Enter Jira instance name: ").strip()
if not args.jira_filter:
    args.jira_filter = input("Enter Jira filter ID: ").strip()
if not args.ado_project:
    args.ado_project = input("Enter ADO project name: ").strip()
```

### Main Body

- Remove `if args.command == 'copy':` block and `else: parser.print_help()` branch
- The existing validation (`if not args.jira_instance: raise ValueError(...)`) is also removed since interactive prompting guarantees values are collected before proceeding
- All downstream logic (fetching Jira issues, creating ADO work items) is unchanged

## Usage

```bash
# Fully scripted (no prompts)
python worker_jira_to_ado_copy.py --jira-instance healthfinch --jira-filter 12345 --ado-project "My Project"

# Partially scripted (prompts for missing args)
python worker_jira_to_ado_copy.py --jira-instance healthfinch

# Fully interactive (prompts for all three)
python worker_jira_to_ado_copy.py
```

## Out of Scope

- No changes to config loading, Jira/ADO client logic, or any other behavior
- No new validation beyond what already exists in the downstream code
