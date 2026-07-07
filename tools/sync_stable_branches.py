#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2026 Linaro Limited
# Author: Ben Copeland <ben.copeland@linaro.org>
#
# Sync stable branch build configs with current kernel.org
# stable/longterm branches

"""
Fetch current stable and longterm kernel versions from kernel.org
and update config/trees/stable-rc.yaml and config/trees/stable.yaml
accordingly.
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

RELEASES_URL = "https://www.kernel.org/releases.json"
CONFIG_DIR = Path(__file__).parent.parent / "config" / "trees"

TREES = {
    "stable-rc": {},
    "stable": {
        "priority": "high",
        "pinned": {(5, 4): "referenced by chromeos platform rules"},
    },
}


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
        # Match both initial releases (e.g. "7.0") and point releases (e.g. "7.0.1")
        match = re.match(r"^(\d+)\.(\d+)", version)
        if match:
            major, minor = int(match.group(1)), int(match.group(2))
            branches.add((major, minor))

    return sorted(branches)


def generate_yaml(tree, branches, priority=None):
    """Generate the build_configs YAML content for one tree"""
    lines = ["build_configs:"]

    for i, (major, minor) in enumerate(branches):
        version_str = f"{major}.{minor}"
        config_name = f"{tree}_{version_str}"
        branch_name = f"linux-{version_str}.y"

        if i == 0:
            # First entry defines the anchor
            lines.append(f"  {config_name}: &{tree}")
            lines.append(f"    tree: {tree}")
            lines.append(f"    branch: '{branch_name}'")
            if priority:
                lines.append(f"    priority: {priority}")
        else:
            lines.append("")
            lines.append(f"  {config_name}:")
            lines.append(f"    <<: *{tree}")
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


def sync_tree(tree, spec, active_branches, config_dir, dry_run):
    """Sync one tree config file; return True if it needed changes"""
    config_path = config_dir / f"{tree}.yaml"
    pinned = spec.get("pinned", {})
    for (major, minor), reason in sorted(pinned.items()):
        print(f"{tree}: keeping pinned branch {major}.{minor} ({reason})")
    desired_branches = sorted(set(active_branches) | set(pinned))

    current_branches = parse_current_branches(config_path)
    print(
        f"{tree}: current branches: "
        f"{', '.join(f'{m}.{n}' for m, n in sorted(current_branches))}"
    )

    # Calculate differences
    to_add = set(desired_branches) - current_branches
    to_remove = current_branches - set(desired_branches)

    if not to_add and not to_remove:
        print(f"{tree}: up to date")
        return False

    if to_add:
        print(
            f"{tree}: branches to add: "
            f"{', '.join(f'{m}.{n}' for m, n in sorted(to_add))}"
        )
    if to_remove:
        print(
            f"{tree}: branches to remove (EOL): "
            f"{', '.join(f'{m}.{n}' for m, n in sorted(to_remove))}"
        )

    new_content = generate_yaml(tree, desired_branches, spec.get("priority"))

    if dry_run:
        print(f"\n--- New {config_path.name} (dry-run) ---")
        print(new_content)
    else:
        config_path.write_text(new_content)
        print(f"Updated {config_path}")
    return True


def main():
    """Sync all configured trees; print per-tree status"""
    parser = argparse.ArgumentParser(
        description="Sync stable branch configs with kernel.org releases"
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would change without modifying the files",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=CONFIG_DIR,
        help=f"Directory containing the tree config files (default: {CONFIG_DIR})",
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

    print(
        f"Active kernel.org branches: {', '.join(f'{m}.{n}' for m, n in active_branches)}"
    )

    changed = False
    for tree, spec in TREES.items():
        if sync_tree(
            tree, spec, active_branches, args.config_dir, args.dry_run
        ):
            changed = True

    if not changed:
        print("Config is already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
