#!/usr/bin/env python3
"""Debug script to inspect Jira issue fields and find story points field ID."""

import os
import sys
import json
import requests

# Credentials from environment
jira_host = os.environ.get('JIRA_INSTANCE', 'carevive')
jira_email = os.environ.get('JIRA_EMAIL', '')
jira_token = os.environ.get('JIRA_TOKEN', '')

if not jira_email or not jira_token:
    print("Error: JIRA_EMAIL and JIRA_TOKEN environment variables required")
    sys.exit(1)

# Query CAR-22002
url = f"https://{jira_host}.atlassian.net/rest/api/3/issues/CAR-22002"
headers = {
    'Accept': 'application/json'
}
auth = (jira_email, jira_token)

try:
    response = requests.get(url, auth=auth, headers=headers, timeout=10)
    response.raise_for_status()
    
    issue = response.json()
    fields = issue.get('fields', {})
    
    print(f"Issue Key: {issue.get('key')}")
    print(f"Issue Type: {fields.get('issuetype', {}).get('name')}")
    print(f"Title: {fields.get('summary')}")
    print(f"\nAll custom fields:")
    print("=" * 60)
    
    for key, value in sorted(fields.items()):
        if key.startswith('customfield_'):
            print(f"{key}: {value}")
            if 'story' in str(key).lower() or 'point' in str(value).lower():
                print(f"  ^^^ POSSIBLE STORY POINTS FIELD ^^^")
    
    print("\n\nStory Points field IDs to try:")
    print("- customfield_10006 (standard Jira)")
    print("- customfield_10002 (some instances)")
    print("- customfield_10046 (some instances)")
    
    # Check specifically for story points variations
    story_fields = {k: v for k, v in fields.items() 
                    if k.startswith('customfield_') and v is not None 
                    and ('point' in str(v).lower() or isinstance(v, (int, float)))}
    
    if story_fields:
        print(f"\nNumeric custom fields that might be story points:")
        for k, v in story_fields.items():
            print(f"  {k}: {v} (type: {type(v).__name__})")
    
except requests.RequestException as e:
    print(f"Error fetching issue: {e}")
    sys.exit(1)
