#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>
# Author: Helen Mae Koike Fornazier <helen.koike@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

# Automatically generates KCIDB issues and incidents from logspec error
# specifications

from copy import deepcopy
import gzip
import hashlib
import json
import requests
import kcidb

from .logspec import main


# Configuration tables per object type
object_types = {
    'build': {
        # logspec parser to use
        'parser': 'kbuild',
        # Object id field to match in the incidents table
        'incident_id_field': 'build_id',
        # Additional incident parameters
        'build_valid': False,
    },
    'test': {
        'parser': 'generic_linux_boot',
        'incident_id_field': 'test_id',
        # Additional incident parameters
        'test_status': 'FAIL',
    },
}


def get_log(url, snippet_lines=0):
    """Fetches a text log given its url.
    Returns:
      If the log file couldn't be retrieved by any reason: None
      Otherwise:
        If snippet_lines == 0: the full log
        If snippet_lines > 0: the first snippet_lines log lines
        If snippet_lines < 0: the last snippet_lines log lines
    """
    response = requests.get(url)
    if not len(response.content):
        return None
    try:
        raw_bytes = gzip.decompress(response.content)
        text = raw_bytes.decode('utf-8')
    except gzip.BadGzipFile:
        text = response.text
    if snippet_lines > 0:
        lines = text.splitlines()
        return '\n'.join(lines[:snippet_lines])
    elif snippet_lines < 0:
        lines = text.splitlines()
        return '\n'.join(lines[snippet_lines:])
    return text


def get_logspec_errors(parsed_data, parser):
    """From a logspec output dict, extracts the relevant fields for a
    KCIDB issue definition (only the error definitions without "hidden"
    fields (fields that start with an underscore)) and returns the list
    of errors.
    """
    errors_list = []
    logspec_version = main.logspec_version()
    base_dict = {
        'version': logspec_version,
        'parser': parser,
    }
    errors = parsed_data.pop('errors')
    for error in errors:
        logspec_dict = {}
        logspec_dict.update(base_dict)
        logspec_dict['error'] = {k: v for k, v in vars(error).items()
                                 if v and not k.startswith('_')}
        logspec_dict['error']['signature'] = error._signature
        logspec_dict['error']['log_excerpt'] = error._report
        logspec_dict['error']['signature_fields'] = {
            field:getattr(error, field)
            for field in error._signature_fields}
        errors_list.append(logspec_dict)
    return errors_list


def new_issue(logspec_error, object_type):
    """Generates a new KCIDB issue object from a logspec error for a
    specific object type.
    Returns the issue as a dict.
    """
    error_copy = deepcopy(logspec_error)
    signature = error_copy['error'].pop('signature')
    comment = f"[logspec:{object_types[object_type]['parser']}] {error_copy['error']['error_type']}"
    if 'error_summary' in error_copy['error']:
        comment += f" {error_copy['error']['error_summary']}"
    if 'target' in error_copy['error']:
        comment += f" in {error_copy['error']['target']}"
        if 'src_file' in error_copy['error']:
            comment += f" ({error_copy['error']['src_file']})"
        elif 'script' in error_copy['error']:
            comment += f" ({error_copy['error']['script']})"
    issue = {
        'origin': 'maestro',
        'id': f'maestro:{signature}',
        'version': 0,
        'comment': comment,
        'misc': {
            'logspec': error_copy
        }
    }
    if 'build_valid' in object_types[object_type]:
        issue['build_valid'] = object_types[object_type]['build_valid']
    if 'test_status' in object_types[object_type]:
        issue['test_status'] = object_types[object_type]['test_status']
    return issue


def new_incident(result_id, issue_id, object_type, issue_version):
    """Generates a new KCIDB incident object for a specific object type
    from an issue id.
    Returns the incident as a dict.
    """
    id_components = json.dumps([result_id, issue_id, issue_version],
                               sort_keys=True, ensure_ascii=False)
    incident_id = hashlib.sha1(id_components.encode('utf-8')).hexdigest()
    incident = {
        'id': f"maestro:{incident_id}",
        'issue_id': issue_id,
        'issue_version': issue_version,
        object_types[object_type]['incident_id_field']: result_id,
        'comment': "test incident, automatically generated",
        'origin': 'maestro',
        'present': True,
    }
    return incident


def generate_output_dict(issues=None, incidents=None):
    """Returns a dict suitable for KCIDB submission containing a list of
    issues and a list of incidents.
    Returns None if no issues or incidents are provided.
    """
    if not issues and not incidents:
        return None
    output_dict = {}
    if issues:
        output_dict['issues'] = issues
    if incidents:
        output_dict['incidents'] = incidents
    return output_dict


def process_log(log_url, parser, start_state):
    """Processes a test log using logspec. The log is first downloaded
    with get_log() and then parsed with logspec.
    """
    log = get_log(log_url)
    if not log:
        return
    parsed_data = main.parse_log(log, start_state)
    # return processed data
    return get_logspec_errors(parsed_data, parser)


def get_issue_from_db(oo_client, signature):
    data = oo_client.query(kcidb.orm.query.Pattern.parse(f">issue[maestro:{signature}]#"))
    if len(data['issue']):
        issue = data['issue'][0]
        return issue
    return None


def generate_issues_and_incidents(result_id, log_url, object_type, oo_client):
    # Load logspec parser
    start_state = main.load_parser(object_types[object_type]['parser'])
    parser = object_types[object_type]['parser']
    error_list = process_log(log_url, parser, start_state)
    issues = []
    incidents = []
    for error in error_list:
        if error and error['error'].get('signature'):
            issue = get_issue_from_db(oo_client, error['error']['signature'])
            if issue:
                issue_id = issue.id
                issue_version = issue.version_num
            else:
                issue = new_issue(error, object_type)
                issues.append(issue)
                issue_id = issue["id"]
                issue_version = issue["version"]
            incidents.append(new_incident(result_id, issue_id, object_type, issue_version))
    # Return the new issues and incidents as a formatted dict
    if issues or incidents:
        return generate_output_dict(issues, incidents)
