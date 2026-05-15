"""
Persistent mapping of Jira issue keys to ADO work item IDs.

Stored as config/migration_mapping.json so it survives script restarts.
"""
import json
import logging
from pathlib import Path

MAPPING_PATH = Path(__file__).parent.parent / "config" / "migration_mapping.json"


def load_issue_mapping() -> dict:
    """Load the full Jira key -> ADO ID mapping from disk.

    Returns an empty dict when the file does not yet exist.
    """
    if MAPPING_PATH.exists():
        try:
            with open(MAPPING_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"[load_issue_mapping] Could not read mapping file: {e}")
    return {}


def save_issue_mapping(jira_key: str, ado_id: int) -> None:
    """Persist a single Jira key -> ADO ID entry without touching other entries."""
    mapping = load_issue_mapping()
    mapping[jira_key] = ado_id
    try:
        MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MAPPING_PATH, "w") as f:
            json.dump(mapping, f, indent=2)
        logging.debug(f"[save_issue_mapping] Saved: {jira_key} -> {ado_id}")
    except Exception as e:
        logging.error(f"[save_issue_mapping] Could not save mapping: {e}")
