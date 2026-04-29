import os
import sys
import json
import argparse
import re
import logging

from pathlib import Path

# Add the utilities directory to the path to import utils_ado
sys.path.append(str(Path(__file__).parent.parent.parent / "utilities"))
from utils_ado import AzureDevOpsClient, load_ado_config
from utils_jira import JiraClient, load_jira_config

logging.basicConfig(filename='worker_jira_to_ado_copy.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_config(filename: str):  #-> dict[str, str]:
    """Load configuration json file"""
    #config_path = "C:\\Users\\matt.baker\\CascadeProjects\\ADO-Automation\\Config\\jira_config.json" #os.path.expanduser('~\ado_config.json')
    config_path = Path(__file__).parent.parent.parent / "config" / filename

    # Try to load from config file
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)

                if config_data:
                    return config_data
                else:
                    raise ValueError("Configuration file not loaded.")
                
        except Exception as e:
            logging.error(f"[load_config] Could not read config file: {e}")

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

        # Get list of Jira users for the specified project
        #jira_users = jira_client.get_users()

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
            #creator = jira_ticket['fields']['reporter']['displayName']
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
            
            # Minor debugging output
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
            custom_fields = load_config('custom_fields_config.json')    # Custom fields defined in config file

            logging.info(f"[main] Updating custom fields for: {ado_id}")

            # For each custom field
            for y in custom_fields:
                # If the field exists in Jira, loop through each value, adding it to ADO
                if hasattr(jira_ticket['fields'],y['jira_field']):
                    logging.debug(f"[main] Updating custom field: {y['jira_field']}")
                    field = jira_ticket['fields'][y['jira_field']]

                    if type(field) in (list, dict, tuple):
                        for z in field:             # If field is a list, loop through each value
                            if y['jira_field'] == 'customfield_10845':  # Issue Environment, translate to ADO values
                                match z[y['jira_field_name']]:
                                    case 'Staging': environment = 'STAGING'
                                    case 'Customer Test': environment = 'CERT/TEST'
                                    case 'Production': environment = 'PROD'

                                logging.debug(f"[main] Updating {y['jira_field']} for {ado_id}")
                                response = ado_client.update_field(ado_id, y['ado_field'], environment)

                            elif y['jira_field'] == 'customfield_10003':  # Impediment (Blocked)
                                if z[y['jira_field_name']] is not None:
                                    logging.debug(f"[main] Updating customfield_10003 for {ado_id}")
                                    response = ado_client.update_field(ado_id, y['ado_field'], 'Yes')

                            else:
                                logging.debug(f"[main] Updating other custom field {y['jira_field']} for {ado_id}")
                                response = ado_client.update_field(ado_id, y['ado_field'], z[y['jira_field_name']])
                    else:                           # If field is not a list, add the value
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

            # Clear the attachment dictionary between each issue copy
            attachment_dict.clear()

            logging.debug(f"[main] Copying attachments for: {ado_id}")

            # Copy attachments
            for i in jira_ticket['fields']['attachment']:
                # For each attachment, get content from Jira
                attachment = jira_client.get_attachment(i['content'])
                
                # Copy attachment to ADO
                response = ado_client.create_attachment(i['filename'], attachment)

                # Write filename and attachment URL to dict
                attachment_dict[i['filename']] = response['url']

                # Link to the current work item
                if hasattr(i['author'], 'emailAddress'):
                    response = ado_client.add_attachment(ado_id, response['url'], i['author']['emailAddress'], i['created'])
                else:   # If no author email address, set to None
                    response = ado_client.add_attachment(ado_id, response['url'], None, i['created'])
            
            logging.debug(f"[main] Adding attachment links to: {ado_id}")

            # Update the work item description if it contains any image links
            if '<img src=' in description.lower():
                # Find all image HTML references
                img_links = re.findall('<img src="([^"]+)" alt="([^"]+)"', description)
            
                # For each image reference in the description
                for a in img_links:
                    # Replace the image source link with the new ADO attachment link
                    description = description.replace(a[0], attachment_dict[a[1]])

                # Update the description on the work item
                response = ado_client.update_field(ado_id, '/fields/System.Description', description)

            # Add comments to the work item from the Jira ticket
            response = jira_client.get_comments(jira_ticket['id'])
            ado_client.add_comments(ado_id, attachment_dict, response['comments'])

            # Get pull request info
            response = jira_client.get_pull_request(jira_id)

            # If there is valid repo data
            if response['detail']:
                if response['detail'][0]['pullRequests']:
                    # Add pull request to hyperlink section
                    response = ado_client.add_hyperlink(ado_id, response['detail'][0]['pullRequests'][0]['url']
                                                        ,response['detail'][0]['pullRequests'][0]['author']['name']
                                                        ,response['detail'][0]['pullRequests'][0]['lastUpdate'])
                elif response['detail'][0]['branches']:
                    # Add branch to hyperlink section
                    response = ado_client.add_hyperlink(ado_id, response['detail'][0]['branches'][0]['url']
                                                        ,response['detail'][0]['branches'][0]['lastCommit']['author']['name']
                                                        ,response['detail'][0]['branches'][0]['lastCommit']['authorTimestamp'])
            
            # If there are any linked issues
            if hasattr(jira_ticket['fields'], 'issuelinks'):
                logging.debug(f"[main] Adding linked issues to work item: {ado_id}")
                # Loop through and add each to the links section of the work item
                for a in jira_ticket['fields']['issuelinks']:
                    # If there are internal issues, add them
                    if 'inwardIssue' in a:
                        related_links = f'https://{jira_instance}.atlassian.net/browse/{a['inwardIssue']['key']}'
                        response = ado_client.add_hyperlink(ado_id, related_links)
                    # If there are external issues, add them
                    if 'outwardIssue' in a:
                        related_links = f'https://{jira_instance}.atlassian.net/browse/{a['outwardIssue']['key']}'
                        response = ado_client.add_hyperlink(ado_id, related_links)

            logging.info(f"[main] Finished work item: {ado_id}")
            logging.info(f"**********************************************************************************")  
        # None values are Area Path and State respectively, setting the default
        #work_item = ado_client.create_item(ado_project, 'user story', None, 'Test Issue Creation', None, 'Matt Baker', 'Test Description')
            
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            logging.info(f"Response: {e.response.text}")

if __name__ == "__main__":
    main()
