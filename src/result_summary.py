#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo Navarro <ricardo.canuelo@collabora.com>

# KernelCI client code to retrieve and summarize job (test, regressions)
# results
#
# How to use this (for now):
#
#        docker-compose run result_summary --preset=<result-preset>
#
# where <result-preset> is defined as a query preset definition in
# config/result-summary.yaml.

# You can specify a date range for the searh using the --date-from
# (default: yesterday) and --date-to (default: now) options, formatted
# as YYYY-MM-DD or YYYY-MM-DDTHH:mm:SS (UTC)
#
# Each preset may define the name and the directory of the output file
# generated (in data/output). This can be overriden with the
# --output-dir and --output-file options. If no output file is defined,
# the output will be printed to stdout.
#
# For current status info, see the development changelog in
# doc/result-summary-CHANGELOG

# TODO:
# - Refactor liberally
# - Send email reports
# - Do we want to focus on regressions only or on any kind of result?
#   If we want test results as well:
#   - Provide logs for test leaf nodes
# - Tweak output and templates according to user needs
# - Other suggested improvements

import sys
import logging

import jinja2
import yaml

import kernelci
from kernelci.legacy.cli import Args, Command, parse_opts
from base import Service
from kernelci_pipeline.email_sender import EmailSender
import result_summary
import result_summary.summary as summary
import result_summary.monitor as monitor
import result_summary.utils as utils


class ResultSummary(Service):
    def __init__(self, configs, args):
        super().__init__(configs, args, result_summary.SERVICE_NAME)
        if args.verbose:
            self.log._logger.setLevel(logging.DEBUG)
        self._template_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(result_summary.TEMPLATES_DIR)
        )
        result_summary.logger = self._logger

    def _setup(self, args):
        # Load and sanity check command line parameters
        # config: the complete config file contents
        # preset_name: name of the selected preset to use
        # preset: loaded config for the selected preset
        with open(args.config, 'r') as config_file:
            config = yaml.safe_load(config_file)
        if args.preset:
            preset_name = args.preset
        else:
            preset_name = 'default'
        if preset_name not in config:
            self.log.error(f"No {preset_name} preset found in {args.config}")
            sys.exit(1)
        preset = config[preset_name]
        # Additional query parameters
        extra_query_params = {}
        if args.query_params:
            extra_query_params = utils.split_query_params(args.query_params)
        output_dir = None
        if args.output_dir:
            output_dir = args.output_dir
        output_file = None
        if args.output_file:
            output_file = args.output_file
        self._email_sender = EmailSender(
            args.smtp_host, args.smtp_port,
            email_sender=args.email_sender,
            email_recipient=args.email_recipient,
        ) if args.smtp_host and args.smtp_port else None
        # End of command line argument loading and sanity checks

        # Load presets and template
        metadata = {}
        preset_params = []
        if 'metadata' in preset:
            metadata = preset['metadata']
        for block_name, body in preset['preset'].items():
            preset_params.extend(utils.parse_block_config(body, block_name, 'done'))
        if 'template' not in metadata:
            self.log.error(f"No template defined for preset {preset_name}")
            sys.exit(1)
        template = self._template_env.get_template(metadata['template'])

        context = {
            'metadata': metadata,
            'preset_params': preset_params,
            'extra_query_params': extra_query_params,
            'template': template,
            'output_file': output_file,
            'output_dir': output_dir,
        }
        # Action-specific setup
        if metadata.get('action') == 'summary':
            extra_context = summary.setup(self, args, context)
        elif metadata.get('action') == 'monitor':
            extra_context = monitor.setup(self, args, context)
        else:
            raise Exception("Undefined or unsupported preset action: "
                            f"{metadata.get('action')}")
        return {**context, **extra_context}

    def _stop(self, context):
        if not context or 'metadata' not in context:
            return
        if context['metadata']['action'] == 'summary':
            summary.stop(self, context)
        elif context['metadata']['action'] == 'monitor':
            monitor.stop(self, context)
        else:
            raise Exception("Undefined or unsupported preset action: "
                            f"{metadata.get('action')}")

    def _run(self, context):
        if context['metadata']['action'] == 'summary':
            summary.run(self, context)
        elif context['metadata']['action'] == 'monitor':
            monitor.run(self, context)
        else:
            raise Exception("Undefined or unsupported preset action: "
                            f"{metadata.get('action')}")


class cmd_run(Command):
    help = ("Checks for test results in a specific date range "
            "and generates summary reports (single shot)")
    args = [
        {
            'name': '--config',
            'help': "Path to service-specific config yaml file",
        },
    ]
    opt_args = [
        {
            'name': '--preset',
            'help': "Configuration preset to load ('default' if none)",
        },
        {
            'name': '--created-from',
            'help': ("Collect results created since this date and time"
                     "(YYYY-mm-DDTHH:MM:SS). Default: since last 24 hours"),
        },
        {
            'name': '--created-to',
            'help': ("Collect results created up to this date and time "
                     "(YYYY-mm-DDTHH:MM:SS). Default: until now"),
        },
        {
            'name': '--last-updated-from',
            'help': ("Collect results that were last updated since this date and time"
                     "(YYYY-mm-DDTHH:MM:SS). Default: since last 24 hours"),
        },
        {
            'name': '--last-updated-to',
            'help': ("Collect results that were last updated up to this date and time "
                     "(YYYY-mm-DDTHH:MM:SS). Default: until now"),
        },
        {
            'name': '--output-dir',
            'help': "Override the 'output_dir' preset parameter"
        },
        {
            'name': '--output-file',
            'help': "Override the 'output' preset parameter"
        },
        {
            'name': '--query-params',
            'help': ("Additional query parameters: "
                     "'<paramX>=<valueX>,<paramY>=<valueY>'")
        },
        {
            'name': '--smtp-host',
            'help': "SMTP server host name.  If omitted, emails won't be sent",
        },
        {
            'name': '--smtp-port',
            'help': "SMTP server port number",
            'type': int,
        },
        {
            'name': '--email-sender',
            'help': "Email address of test report sender",
        },
        {
            'name': '--email-recipient',
            'help': "Email address of test report recipient",
        },
        Args.verbose,
    ]

    def __call__(self, configs, args):
        return ResultSummary(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts(result_summary.SERVICE_NAME, globals())
    yaml_configs = opts.get_yaml_configs() or 'config/pipeline.yaml'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
