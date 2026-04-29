# Jira to ADO Copy

Copies work items from Jira to Azure DevOps, including fields, attachments, comments, pull request links, and linked issues.

## How to obtain Access Tokens for each environment
- Create Jira API token: https://id.atlassian.com/manage-profile/security/api-tokens
- ADO API Token: https://dev.azure.com/healthcatalyst/_usersSettings/tokens

## Running the Script

All three arguments can be passed as flags or omitted to be prompted interactively.

```bash
python jira_ado_copy/scripts/worker_jira_to_ado_copy.py
```

Or pass any combination of arguments directly:

```bash
python jira_ado_copy/scripts/worker_jira_to_ado_copy.py \
  --jira-instance <instance_name> \
  --jira-filter <filter_id> \
  --ado-project <project_name>
```

Any argument not provided on the command line will be prompted for interactively.

### Arguments

| Argument | Description |
|---|---|
| `--jira-instance` | Jira instance name (the subdomain from `https://<instance>.atlassian.net`) |
| `--jira-filter` | Jira filter ID that returns the issues to copy |
| `--ado-project` | Target Azure DevOps project name |

### Examples

**Fully interactive** (prompts for all three):

```bash
python jira_ado_copy/scripts/worker_jira_to_ado_copy.py
```

**Fully scripted** (no prompts):

```bash
python jira_ado_copy/scripts/worker_jira_to_ado_copy.py \
  --jira-instance healthfinch \
  --jira-filter 12345 \
  --ado-project "My Project"
```

**Partially scripted** (prompts for missing args):

```bash
python jira_ado_copy/scripts/worker_jira_to_ado_copy.py --jira-instance healthfinch
```

### What Gets Copied

For each issue returned by the Jira filter, the script copies:

- Title (prefixed with Jira key and parent key if applicable)
- Description (HTML)
- Assignee and Reporter
- State (mapped via `state_config.json`)
- Work item type (mapped via `type_config.json`)
- Custom fields (mapped via `custom_fields_config.json`)
- Priority
- Due date
- Labels (as tags)
- Attachments (with image references updated in descriptions)
- Comments (with image references updated)
- Pull request / branch hyperlinks
- Linked issues (as hyperlinks back to Jira)

### Logging

Output is written to `worker_jira_to_ado_copy.log` in the working directory.

## Configuration Files

All configuration files live in the `config/` directory at the project root.

### ado_config.json

Connection settings for Azure DevOps. See `config/example-ado_config.json`.

```json
{
  "organization_url": "https://dev.azure.com/your-org",
  "username": "your.name@example.com",
  "access_token": "your ADO personal access token",
  "project": "Your Project Name"
}
```

### jira_config.json

Connection settings for Jira. See `config/example-jira_config.json`.

```json
{
  "server": "https://your-instance.atlassian.net",
  "email": "your.name@example.com",
  "access_token": "your Jira API token"
}
```

> Note: When `--jira-instance` is provided, the server URL is built automatically as `https://<instance>.atlassian.net`, overriding the `server` value in the config file. The `email` and `access_token` are still read from the config.

### type_config.json

Maps Jira issue types to ADO work item types. The `Default` key is used when no match is found.

```json
{
  "Default": "Issue",
  "Task": "Task",
  "Bug": "Bug",
  "Story": "User Story",
  "Epic": "Epic"
}
```

### state_config.json

Maps Jira statuses to ADO states. The `Default` key is used when no match is found.

```json
{
  "Default": "New",
  "In Progress": "Active",
  "Done": "Completed"
}
```

### custom_fields_config.json

Defines custom field mappings between Jira and ADO. Each entry specifies the ADO field path, the Jira custom field ID, and the sub-field name to extract from the Jira value.

```json
[
  {
    "ado_field": "/fields/System.Tags",
    "jira_field": "customfield_10007",
    "jira_field_name": "name"
  }
]
```

## Utility Reference: AzureDevOpsClient

Located in `utilities/utils_ado.py`. Initialize with a config and project name:

```python
from utils_ado import AzureDevOpsClient, load_ado_config

config = load_ado_config()
client = AzureDevOpsClient(config, "My Project")
```

### Methods

#### `get_item_info(item_id)`
Retrieves all fields for a work item. Returns a normalized `pandas` DataFrame.

```python
df = client.get_item_info(12345)
```

#### `create_item(type, area_path=None, title=None, state=None, assigned_to=None, description=None)`
Creates a new work item of the given type. Returns the API response with the new item's `id`.

```python
response = client.create_item("User Story", title="My Story", state="New", description="Details here")
item_id = response["id"]
```

#### `update_field(item_id, field, text)`
Updates a single field on a work item using a JSON patch operation.

```python
client.update_field(12345, "/fields/System.State", "Active")
client.update_field(12345, "/fields/Custom.PriorityLevel", "2-High")
```

#### `append_description(item_id, add_text)`
Appends HTML text to the end of a work item's existing description.

```python
client.append_description(12345, "<b>Reporter:</b> John Smith")
```

#### `create_attachment(filename, attachment)`
Uploads an attachment to ADO. Returns the response containing the attachment `url`.

```python
response = client.create_attachment("screenshot.png", file_content)
attachment_url = response["url"]
```

#### `add_attachment(item_id, attachment_url, author, created_date)`
Links an uploaded attachment to a work item with author and date metadata.

```python
client.add_attachment(12345, attachment_url, "user@example.com", "2025-01-15T10:30:00.000+0000")
```

#### `add_comments(item_id, attachment_dict, comments)`
Adds a list of Jira comments to a work item. Updates any embedded image references using the provided `attachment_dict` (a mapping of `filename -> ADO attachment URL`).

```python
client.add_comments(12345, {"image.png": "https://dev.azure.com/..."}, comments_list)
```

#### `add_hyperlink(item_id, hyperlink, author=None, last_update=None)`
Adds a hyperlink relation to a work item. Optionally includes author and timestamp metadata.

```python
client.add_hyperlink(12345, "https://github.com/org/repo/pull/1", "dev@example.com", "2025-01-15T10:30:00.000+0000")
```

#### `format_date(date_string)`
Converts a Jira timestamp string (ISO 8601 with timezone) to `YYYY-MM-DD HH:MM UTC-6` format.

```python
formatted = client.format_date("2025-01-15T10:30:00.000+0000")
# Returns: "2025-01-15 10:30 UTC-6"
```

## Utility Reference: JiraClient

Located in `utilities/utils_jira.py`. Initialize with a config:

```python
from utils_jira import JiraClient, load_jira_config

config = load_jira_config("healthfinch")
client = JiraClient(config)
```

When `load_jira_config` is called with an instance name, it builds the server URL as `https://<instance>.atlassian.net` and uses the `email` and `access_token` from the config file. When called without an argument, it uses the `server` value from the config file directly.

### Methods

#### `get_jira_issue(issue_key)`
Retrieves a single Jira issue by key or ID, including rendered fields (HTML descriptions).

```python
issue = client.get_jira_issue("PROJ-123")
title = issue["fields"]["summary"]
description = issue["renderedFields"]["description"]
```

#### `get_filter_items(filter_id)`
Executes a saved Jira filter and returns matching issues (up to 1000).

```python
results = client.get_filter_items("54321")
issues = results["issues"]
```

#### `get_comments(issue_key)`
Retrieves all comments on an issue with rendered HTML bodies.

```python
response = client.get_comments("PROJ-123")
comments = response["comments"]
```

#### `get_attachment(url)`
Downloads attachment content from a Jira attachment URL.

```python
content = client.get_attachment("https://your-instance.atlassian.net/rest/api/3/attachment/content/12345")
```

#### `get_pull_request(issue_key)`
Retrieves GitHub pull request and branch data linked to a Jira issue via the dev-status API.

```python
response = client.get_pull_request("10001")
pr_url = response["detail"][0]["pullRequests"][0]["url"]
```

> Note: This uses Jira's internal dev-status API which is undocumented and unsupported by Atlassian.

#### `get_users()`
Retrieves a list of Jira users (up to 1000).

```python
users = client.get_users()
```
