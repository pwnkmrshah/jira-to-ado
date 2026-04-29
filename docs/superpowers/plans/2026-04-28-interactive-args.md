# Interactive Arguments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the `copy` subcommand from `worker_jira_to_ado_copy.py` and make all three arguments optional CLI flags with interactive `input()` prompts as fallback.

**Architecture:** Flatten the argparse setup from a subcommand structure to a single top-level parser. After parsing, check each of the three arguments; if any is falsy, prompt the user interactively with `input()`. All downstream logic is untouched.

**Tech Stack:** Python 3, `argparse`, `input()`

---

### Task 1: Restructure argument parsing and add interactive fallback

**Files:**
- Modify: `jira_ado_copy/scripts/worker_jira_to_ado_copy.py:37-75`

This task replaces the subcommand-based parser with a flat parser and adds `input()` prompts for any missing arguments.

- [ ] **Step 1: Replace the `main()` parser block**

Replace lines 37–75 (from `def main():` through the end of the `if args.command == 'copy':` / `else:` block) with the following. Everything from line 76 onward (the Jira/ADO logic) stays exactly as-is.

```python
def main():
    parser = argparse.ArgumentParser(description='Jira to ADO Copy')
    parser.add_argument('--jira-instance', help='Jira source instance name (from URL)')
    parser.add_argument('--jira-filter', help='Jira source filter ID')
    parser.add_argument('--ado-project', help='ADO target project name')

    args = parser.parse_args()

    if not args.jira_instance:
        args.jira_instance = input("Enter Jira instance name: ").strip()
    if not args.jira_filter:
        args.jira_filter = input("Enter Jira filter ID: ").strip()
    if not args.ado_project:
        args.ado_project = input("Enter ADO project name: ").strip()

    jira_instance = args.jira_instance
    jira_filter = args.jira_filter
    ado_project = args.ado_project

    try:
        print(f'Copying Jira Work Items from {jira_instance} to {ado_project}')
        logging.info(f'Copying Jira Work Items from {jira_instance} to {ado_project}')

        ado_config = load_ado_config()
        ado_client = AzureDevOpsClient(ado_config, ado_project)

        jira_config = load_jira_config(jira_instance)
        jira_client = JiraClient(jira_config)
```

The full resulting `main()` function should look like this (showing the complete file from `def main():` to end):

```python
def main():
    parser = argparse.ArgumentParser(description='Jira to ADO Copy')
    parser.add_argument('--jira-instance', help='Jira source instance name (from URL)')
    parser.add_argument('--jira-filter', help='Jira source filter ID')
    parser.add_argument('--ado-project', help='ADO target project name')

    args = parser.parse_args()

    if not args.jira_instance:
        args.jira_instance = input("Enter Jira instance name: ").strip()
    if not args.jira_filter:
        args.jira_filter = input("Enter Jira filter ID: ").strip()
    if not args.ado_project:
        args.ado_project = input("Enter ADO project name: ").strip()

    jira_instance = args.jira_instance
    jira_filter = args.jira_filter
    ado_project = args.ado_project

    try:
        print(f'Copying Jira Work Items from {jira_instance} to {ado_project}')
        logging.info(f'Copying Jira Work Items from {jira_instance} to {ado_project}')

        ado_config = load_ado_config()
        ado_client = AzureDevOpsClient(ado_config, ado_project)

        jira_config = load_jira_config(jira_instance)
        jira_client = JiraClient(jira_config)

        # Get all tickets from Jira using the provided filter ID
        jira_df = jira_client.get_filter_items(jira_filter)

        # If the filter id is incorrect or the results are empty, return nothing
        if jira_df is None:
            print(f'No items returned from filter {jira_filter}')
            logging.info(f'No items returned from filter {jira_filter}')
            return None

        # Get the issues from the filter result
        jira_issues = jira_df.get('issues')
        logging.info(f"[main] Found {len(jira_issues)} issues in Jira")

        attachment_dict = {}       # Initialize attachment dict

        # For each item in the filter, get its info, then create an ADO work item
        for x in jira_issues:
            logging.debug(f"[main] Processing Jira issue...")
            jira_ticket = jira_client.get_jira_issue(x['id'])

            #Parse ticket response for relevant information
            jira_key = jira_ticket['key']
            jira_id = jira_ticket['id']
            
            # If the ticket has a parent, add it to the title
            if 'parent' in jira_ticket['fields']:
                title = ''.join(('[', jira_ticket['fields']['parent']['key'], '] ', '[', jira_key, '] ', jira_ticket['fields']['summary']))
            else:
                title = ''.join(('[' , jira_key, '] ', jira_ticket['fields']['summary']))
            
            # If the ticket has an assignee, add it to the ticket
            if hasattr(jira_ticket['fields']['assignee'], 'emailAddress'):
                assignee = jira_ticket['fields']['assignee']['emailAddress']
            else:
                assignee = None
            
            state = jira_ticket['fields']['status']['name']
            jira_type = jira_ticket['fields']['issuetype']['name']
            description = jira_ticket['renderedFields']['description']

            # Add customers to description
            customers = jira_ticket['fields']['customfield_10907']

            if customers:
                logging.debug(f"[main] Adding customers to description...")
                description = ''.join((description, '<p><b>Customers:</b></p>'))
                customers = jira_ticket['fields']['customfield_10907']['content'][0]['content']
                
                for customer in customers:
                    description = ''.join((description, customer['text'], '<br />'))

            # Translate Jira issue type to ADO work item type using config file
            type_config = load_config('type_config.json')
            if (ado_type := type_config.get(jira_type)) is None:
                ado_type = type_config.get('Default')

            # Translate Jira state to ADO state using config file
            state_config = load_config('state_config.json')
            if (ado_state := state_config.get(state)) is None:
                ado_state = state_config.get('Default')
            
            logging.info(f'[main] Copying {jira_key} ...')

            # Create the ADO work item
            work_item = ado_client.create_item(ado_type, None, title, ado_state, assignee, description)

            ado_id = work_item['id']

            # Set Requested By field if the reporter is active and has an email address
            if jira_ticket['fields']['reporter']['active'] and hasattr(jira_ticket['fields']['reporter'], 'emailAddress'):
                response = ado_client.update_field(ado_id, '/fields/Custom.RequestedBy', jira_ticket['fields']['reporter']['emailAddress'])

            # Otherwise, append requestor name to the end of the Description
            else:
                reporter = ''.join(('<b>Reporter:</b> ', jira_ticket['fields']['reporter']['displayName']))
                response = ado_client.append_description(ado_id, reporter)

            # Define custom fields to migrate
            custom_fields = load_config('custom_fields_config.json')

            logging.info(f"[main] Updating custom fields for: {ado_id}")

            # For each custom field
            for y in custom_fields:
                if hasattr(jira_ticket['fields'],y['jira_field']):
                    logging.debug(f"[main] Updating custom field: {y['jira_field']}")
                    field = jira_ticket['fields'][y['jira_field']]

                    if type(field) in (list, dict, tuple):
                        for z in field:
                            if y['jira_field'] == 'customfield_10845':
                                match z[y['jira_field_name']]:
                                    case 'Staging': environment = 'STAGING'
                                    case 'Customer Test': environment = 'CERT/TEST'
                                    case 'Production': environment = 'PROD'

                                logging.debug(f"[main] Updating {y['jira_field']} for {ado_id}")
                                response = ado_client.update_field(ado_id, y['ado_field'], environment)

                            elif y['jira_field'] == 'customfield_10003':
                                if z[y['jira_field_name']] is not None:
                                    logging.debug(f"[main] Updating customfield_10003 for {ado_id}")
                                    response = ado_client.update_field(ado_id, y['ado_field'], 'Yes')

                            else:
                                logging.debug(f"[main] Updating other custom field {y['jira_field']} for {ado_id}")
                                response = ado_client.update_field(ado_id, y['ado_field'], z[y['jira_field_name']])
                    else:
                        logging.debug(f"[main] Final else updating {y['jira_field']} for {ado_id}")
                        response = ado_client.update_field(ado_id, y['ado_field'], field)

            logging.info(f"[main] Finished updating custom fields for: {ado_id}")

            # Set priority (non-custom field in Jira) if it exists
            if field := jira_ticket['fields']['priority']['name']:
                match field:
                    case 'Highest': priority = '1-Critical'
                    case 'High': priority = '2-High'
                    case 'Medium': priority = '3-Medium'
                    case 'Low': priority = '4-Low'
                    case 'Lowest': priority = '4-Low'
                response = ado_client.update_field(ado_id, '/fields/Custom.PriorityLevel', priority)

            # Set due date (non-custom field in Jira) if it exists
            if field := jira_ticket['fields']['duedate']:
                response = ado_client.update_field(ado_id, '/fields/Microsoft.VSTS.Scheduling.TargetDate', field)

            # Set Labels (non-custom field in Jira) if it exists
            if field := jira_ticket['fields']['labels']:
                for z in field:
                    response = ado_client.update_field(ado_id, '/fields/System.Tags', z)

            attachment_dict.clear()

            logging.debug(f"[main] Copying attachments for: {ado_id}")

            # Copy attachments
            for i in jira_ticket['fields']['attachment']:
                attachment = jira_client.get_attachment(i['content'])
                response = ado_client.create_attachment(i['filename'], attachment)
                attachment_dict[i['filename']] = response['url']

                if hasattr(i['author'], 'emailAddress'):
                    response = ado_client.add_attachment(ado_id, response['url'], i['author']['emailAddress'], i['created'])
                else:
                    response = ado_client.add_attachment(ado_id, response['url'], None, i['created'])
            
            logging.debug(f"[main] Adding attachment links to: {ado_id}")

            # Update the work item description if it contains any image links
            if '<img src=' in description.lower():
                img_links = re.findall('<img src="([^"]+)" alt="([^"]+)"', description)
            
                for a in img_links:
                    description = description.replace(a[0], attachment_dict[a[1]])

                response = ado_client.update_field(ado_id, '/fields/System.Description', description)

            # Add comments to the work item from the Jira ticket
            response = jira_client.get_comments(jira_ticket['id'])
            ado_client.add_comments(ado_id, attachment_dict, response['comments'])

            # Get pull request info
            response = jira_client.get_pull_request(jira_id)

            if response['detail']:
                if response['detail'][0]['pullRequests']:
                    response = ado_client.add_hyperlink(ado_id, response['detail'][0]['pullRequests'][0]['url']
                                                        ,response['detail'][0]['pullRequests'][0]['author']['name']
                                                        ,response['detail'][0]['pullRequests'][0]['lastUpdate'])
                elif response['detail'][0]['branches']:
                    response = ado_client.add_hyperlink(ado_id, response['detail'][0]['branches'][0]['url']
                                                        ,response['detail'][0]['branches'][0]['lastCommit']['author']['name']
                                                        ,response['detail'][0]['branches'][0]['lastCommit']['authorTimestamp'])
            
            if hasattr(jira_ticket['fields'], 'issuelinks'):
                logging.debug(f"[main] Adding linked issues to work item: {ado_id}")
                for a in jira_ticket['fields']['issuelinks']:
                    if 'inwardIssue' in a:
                        related_links = f'https://{jira_instance}.atlassian.net/browse/{a["inwardIssue"]["key"]}'
                        response = ado_client.add_hyperlink(ado_id, related_links)
                    if 'outwardIssue' in a:
                        related_links = f'https://{jira_instance}.atlassian.net/browse/{a["outwardIssue"]["key"]}'
                        response = ado_client.add_hyperlink(ado_id, related_links)

            logging.info(f"[main] Finished work item: {ado_id}")
            logging.info(f"**********************************************************************************")

    except Exception as e:
        logging.error(f"Error: {str(e)}")
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            logging.info(f"Response: {e.response.text}")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Manually verify the file looks correct**

Open `jira_ado_copy/scripts/worker_jira_to_ado_copy.py` and confirm:
- No `subparsers`, `add_subparsers`, or `copy` subparser references remain
- The three `parser.add_argument(...)` calls are on the top-level parser
- Three `if not args.<arg>: args.<arg> = input(...).strip()` blocks are present
- No `if args.command == 'copy':` or `else: parser.print_help()` blocks remain
- All downstream logic (Jira/ADO copy loop) is intact

- [ ] **Step 3: Smoke-test argument parsing (no network needed)**

Run with `--help` to confirm the flat argument structure:

```bash
cd /Users/chao.gan/Code/HealthCatalyst/jira-to-ado
python jira_ado_copy/scripts/worker_jira_to_ado_copy.py --help
```

Expected output (approximately):
```
usage: worker_jira_to_ado_copy.py [-h] [--jira-instance JIRA_INSTANCE] [--jira-filter JIRA_FILTER] [--ado-project ADO_PROJECT]

Jira to ADO Copy

options:
  -h, --help            show this help message and exit
  --jira-instance JIRA_INSTANCE
                        Jira source instance name (from URL)
  --jira-filter JIRA_FILTER
                        Jira source filter ID
  --ado-project ADO_PROJECT
                        ADO target project name
```

No `{copy}` subcommand should appear.

- [ ] **Step 4: Commit**

```bash
git add jira_ado_copy/scripts/worker_jira_to_ado_copy.py
git commit -m "refactor: remove copy subcommand, add interactive input() fallback for args"
```
