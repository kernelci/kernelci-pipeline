#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2026 Linaro Limited
# Author: Ben Copeland <ben.copeland@linaro.org>
#
# Sync stable-rc.yaml with current kernel.org stable/longterm branches

"""
Fetch current stable and longterm kernel versions from kernel.org
and update config/trees/stable-rc.yaml accordingly.
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

RELEASES_URL = "https://www.kernel.org/releases.json"
CONFIG_PATH = Path(__file__).parent.parent / "config" / "trees" / "stable-rc.yaml"


def fetch_releases():
    """Fetch releases.json from kernel.org"""
    with urllib.request.urlopen(RELEASES_URL, timeout=30) as response:
        return json.loads(response.read().decode())


def get_active_branches(releases_data):
    """
    Extract active (non-EOL) stable and longterm branch versions.
    Returns sorted list of (major, minor) tuples.
    """
    branches = set()

    for release in releases_data.get("releases", []):
        moniker = release.get("moniker", "")
        if moniker not in ("stable", "longterm"):
            continue

        if release.get("iseol", False):
            continue

        version = release.get("version", "")
        match = re.match(r"^(\d+)\.(\d+)\.", version)
        if match:
            major, minor = int(match.group(1)), int(match.group(2))
            branches.add((major, minor))

    return sorted(branches)


def generate_yaml(branches):
    """Generate the stable-rc.yaml content"""
    lines = ["build_configs:"]

    for i, (major, minor) in enumerate(branches):
        version_str = f"{major}.{minor}"
        config_name = f"stable-rc_{version_str}"
        branch_name = f"linux-{version_str}.y"

        if i == 0:
            # First entry defines the anchor
            lines.append(f"  {config_name}: &stable-rc")
            lines.append("    tree: stable-rc")
            lines.append(f"    branch: '{branch_name}'")
        else:
            lines.append("")
            lines.append(f"  {config_name}:")
            lines.append("    <<: *stable-rc")
            lines.append(f"    branch: '{branch_name}'")

    lines.append("")
    return "\n".join(lines)


def parse_current_branches(config_path):
    """Parse current branches from existing config file"""
    if not config_path.exists():
        return set()

    branches = set()
    content = config_path.read_text()

    for match in re.finditer(r"branch:\s*'linux-(\d+)\.(\d+)\.y'", content):
        major, minor = int(match.group(1)), int(match.group(2))
        branches.add((major, minor))

    return branches


def main():
    parser = argparse.ArgumentParser(
        description="Sync stable-rc.yaml with kernel.org releases"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would change without modifying the file"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help=f"Path to stable-rc.yaml (default: {CONFIG_PATH})"
    )
    args = parser.parse_args()

    print(f"Fetching releases from {RELEASES_URL}...")
    try:
        releases_data = fetch_releases()
    except Exception as e:
        print(f"Error fetching releases: {e}", file=sys.stderr)
        return 1

    active_branches = get_active_branches(releases_data)
    if not active_branches:
        print("Error: No active branches found", file=sys.stderr)
        return 1

    print(f"Active kernel.org branches: {', '.join(f'{m}.{n}' for m, n in active_branches)}")

    current_branches = parse_current_branches(args.config)
    print(f"Current config branches: {', '.join(f'{m}.{n}' for m, n in sorted(current_branches))}")

    # Calculate differences
    to_add = set(active_branches) - current_branches
    to_remove = current_branches - set(active_branches)

    if not to_add and not to_remove:
        print("Config is already up to date.")
        return 0

    if to_add:
        print(f"Branches to add: {', '.join(f'{m}.{n}' for m, n in sorted(to_add))}")
    if to_remove:
        print(f"Branches to remove (EOL): {', '.join(f'{m}.{n}' for m, n in sorted(to_remove))}")

    new_content = generate_yaml(active_branches)

    if args.dry_run:
        print("\n--- New config (dry-run) ---")
        print(new_content)
        return 0

    args.config.write_text(new_content)
    print(f"Updated {args.config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
