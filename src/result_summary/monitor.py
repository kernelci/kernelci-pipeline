# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo Navarro <ricardo.canuelo@collabora.com>
#
# monitor-mode-specifc code for result-summary.

from datetime import datetime, timezone
import os
import re

import result_summary
import result_summary.utils as utils


def setup(service, args, context):
    base_filter = context['preset_params'][0]
    sub_id = service._api_helper.subscribe_filters({
        'kind': base_filter['kind'],
        'state': base_filter['state'],
    })
    if not sub_id:
        raise Exception("Error subscribing to event")
    return {'sub_id': sub_id}


def stop(service, context):
    if context and context.get('sub_id'):
        service._api_helper.unsubscribe_filters(context['sub_id'])


def get_item(dict, item, default=None):
    """General form of dict.get() that supports the retrieval of
    dot-separated fields in nested dicts.
    """
    if not dict:
        return default
    items = item.split('.')
    if len(items) == 1:
        return dict.get(items[0], default)
    return get_item(dict.get(items[0], default), '.'.join(items[1:]), default)


def filter_node(node, params):
    """Returns True if <node> matches the constraints defined in
    the <params> dict, where each param is defined like:

        node_field : value

    with an optional operator (ne, gt, lt, re):

        node_field__op : value

    The value matching is done differently depending on the
    operator (equal, not equal, greater than, lesser than,
    regex)

    If the node doesn't match the full set of parameter
    constraints, it returns False.
    """
    match = True
    for param_name, value in params.items():
        if value == 'null':
            value = None
        field, _, cmd = param_name.partition('__')
        node_value = get_item(node, field)
        if cmd == 'ne':
            if node_value == value:
                match = False
                break
        elif cmd == 'gt':
            if node_value <= value:
                match = False
                break
        elif cmd == 'lt':
            if node_value >= value:
                match = False
                break
        elif cmd == 're' and node_value:
            if not re.search(value, node_value):
                match = False
                break
        else:
            if node_value != value:
                match = False
                break
    if not match:
        return False, f"<{field} = {node_value}> doesn't match constraint '{param_name}: {value}'"
    return True, "Ok"


def send_email_report(service, context, report_text):
    if not service._email_sender:
        return
    if 'title' in context['metadata']:
        title = context['metadata']['title']
    else:
        title = "KernelCI report"
    service._email_sender.create_and_send_email(title, report_text)


def run(service, context):
    while True:
        node, _ = service._api_helper.receive_event_node(context['sub_id'])
        service.log.debug(f"Node event received: {node['id']}")
        preset_params = context['preset_params']
        for param_set in context['preset_params']:
            service.log.debug(f"Match check. param_set: {param_set}")
            match, msg = filter_node(node, {**param_set, **context['extra_query_params']})
            if match:
                service.log.info(f"Result received: {node['id']}")
                template_params = {
                    'metadata': context['metadata'],
                    'node': node,
                }
                # Post-process node
                utils.post_process_node(node, service._api)

                output_text = context['template'].render(template_params)
                # Setup output dir from base path and user-specified
                # parameter (in preset metadata or cmdline)
                output_dir = result_summary.BASE_OUTPUT_DIR
                if context.get('output_dir'):
                    output_dir = os.path.join(output_dir, context['output_dir'])
                elif 'output_dir' in context['metadata']:
                    output_dir = os.path.join(output_dir, context['metadata']['output_dir'])
                os.makedirs(output_dir, exist_ok=True)
                # Generate and dump output
                # output_file specified in cmdline:
                output_file = context['output_file']
                if not output_file:
                    # Check if output_file is specified as a preset
                    # parameter. Since we expect many reports to be
                    # generated, prepend them with a timestamp
                    if 'output_file' in context['metadata']:
                        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                        output_file = now_str + '__' + context['metadata']['output_file']
                if output_file:
                    output_file = os.path.join(output_dir, output_file)
                    with open(output_file, 'w') as outfile:
                        outfile.write(output_text)
                    service.log.info(f"Report generated in {output_file}\n")
                else:
                    result_summary.logger.info(output_text)
                send_email_report(service, context, output_text)
            else:
                service.log.debug(f"Result received but filtered: {node['id']}. {msg}\n")
    return True
