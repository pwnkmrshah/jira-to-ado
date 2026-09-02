#!/usr/bin/env python3
"""fix_parent_child_links.py — Add parent/child links after all items have been created.

When migrating large sets of items with parent-child relationships, the main worker
uses a two-pass approach:
  1. First pass (worker_jira_to_ado_copy.py): Create all work items, even if parents
     don't yet exist. Parent relationships are added as Jira URL hyperlinks as fallback.
  2. Second pass (this script): Now that all items exist in ADO, create proper parent/child
     work item links to replace the hyperlinks.

This script:
  - Loads the migration mapping
  - For each Jira item with a parent, checks if both parent and child exist in ADO
  - Creates the System.LinkTypes.Hierarchy-Reverse link (child → parent)
  - Logs success/failure for each link

Usage:
  python3 fix_parent_child_links.py --jira-instance healthfinch \
    --ado-project "Embedded Refills Engineering" \
    --project-key OP
"""

import sys
import json
import argparse
import logging
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent / "utilities"))
from utils_ado import AzureDevOpsClient, load_ado_config
from utils_jira import JiraClient, load_jira_config
from utils_mapping import load_issue_mapping

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description='Add parent/child links after initial migration'
    )
    parser.add_argument('--jira-instance', required=True, help='Jira instance name (e.g., healthfinch)')
    parser.add_argument('--ado-project', required=True, help='ADO project name')
    parser.add_argument('--project-key', help='Jira project key filter (e.g., OP). If provided, only links within this project are fixed.')
    parser.add_argument('--jira-keys', help='Comma-separated Jira keys to fix links for')
    parser.add_argument('--force', action='store_true', help='Overwrite existing links')

    args = parser.parse_args()

    # Load configs
    jira_config = load_jira_config(args.jira_instance)
    jira_client = JiraClient(jira_config)
    ado_config = load_ado_config()
    ado_client = AzureDevOpsClient(ado_config, args.ado_project)
    mapping = load_issue_mapping()

    logger.info(f"Loaded mapping with {len(mapping)} entries")
    logger.info(f"ADO project: {args.ado_project}")
    logger.info(f"Jira instance: {args.jira_instance}")

    # Determine which keys to process
    if args.jira_keys:
        keys = [k.strip().upper() for k in args.jira_keys.split(',') if k.strip()]
        logger.info(f"Processing {len(keys)} explicit keys")
    elif args.project_key:
        prefix = args.project_key.upper()
        keys = sorted([k for k in mapping.keys() if k.startswith(prefix + '-')])
        logger.info(f"Processing {len(keys)} keys for project '{prefix}'")
    else:
        keys = sorted(mapping.keys())
        logger.info(f"Processing all {len(keys)} keys in mapping")

    # Track results
    fixed = []
    already_linked = []
    missing_parent = []
    missing_child = []
    errors = []

    # Process each key
    for idx, jira_key in enumerate(keys, 1):
        if idx % 50 == 0:
            logger.info(f"Progress: {idx}/{len(keys)}")

        # Get Jira ticket
        try:
            jira_ticket = jira_client.get_jira_issue(jira_key)
        except Exception as e:
            logger.warning(f"[{jira_key}] Could not fetch from Jira: {e}")
            errors.append((jira_key, f"Jira fetch failed: {e}"))
            continue

        if not jira_ticket:
            logger.warning(f"[{jira_key}] Not found in Jira")
            errors.append((jira_key, "Not in Jira"))
            continue

        # Check if this item has a parent
        parent_key = jira_ticket.get('fields', {}).get('parent', {}).get('key')
        if not parent_key:
            # No parent — skip
            continue

        # Get ADO IDs
        child_ado_id = mapping.get(jira_key)
        parent_ado_id = mapping.get(parent_key)

        if not child_ado_id:
            logger.warning(f"[{jira_key}] Child not in ADO mapping")
            missing_child.append(jira_key)
            continue

        if not parent_ado_id:
            logger.warning(f"[{jira_key}] Parent {parent_key} not in ADO mapping")
            missing_parent.append((jira_key, parent_key))
            continue

        # Check if link already exists
        existing_urls = ado_client.get_work_item_relation_urls(child_ado_id)
        parent_target_url = f'{ado_client.organization_url}/_apis/wit/workitems/{parent_ado_id}'
        
        if parent_target_url in existing_urls and not args.force:
            logger.debug(f"[{jira_key}] Parent link already exists")
            already_linked.append(jira_key)
            continue

        # Create the link
        try:
            ado_client.add_work_item_link(
                child_ado_id,
                parent_ado_id,
                'System.LinkTypes.Hierarchy-Reverse'  # child → parent
            )
            logger.info(f"[{jira_key}] ✓ Linked {child_ado_id} → {parent_ado_id} ({parent_key})")
            fixed.append((jira_key, parent_key))
        except Exception as e:
            logger.error(f"[{jira_key}] Failed to link to {parent_key}: {e}")
            errors.append((jira_key, f"Link creation failed: {e}"))

    # Summary
    print("\n" + "=" * 80)
    print("PARENT/CHILD LINK FIX-UP SUMMARY")
    print("=" * 80)
    print(f"Processed:        {len(keys)}")
    print(f"  Fixed:          {len(fixed)}")
    print(f"  Already linked: {len(already_linked)}")
    print(f"  Parent missing: {len(missing_parent)}")
    print(f"  Child missing:  {len(missing_child)}")
    print(f"  Errors:         {len(errors)}")
    print("=" * 80)

    if fixed:
        print(f"\n✓ {len(fixed)} links fixed:")
        for jira_key, parent_key in fixed[:20]:
            print(f"  - {jira_key} → {parent_key}")
        if len(fixed) > 20:
            print(f"  ... and {len(fixed) - 20} more")

    if missing_parent:
        print(f"\n⚠ {len(missing_parent)} items have missing parents:")
        for jira_key, parent_key in missing_parent[:20]:
            print(f"  - {jira_key} (parent: {parent_key})")
        if len(missing_parent) > 20:
            print(f"  ... and {len(missing_parent) - 20} more")
        print("  → Run worker script to migrate parent items first")

    if errors:
        print(f"\n❌ {len(errors)} errors occurred:")
        for jira_key, error in errors[:10]:
            print(f"  - {jira_key}: {error}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")

    # Return exit code
    if missing_parent or errors:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == '__main__':
    main()
