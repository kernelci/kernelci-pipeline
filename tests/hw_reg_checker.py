#!/usr/bin/env python3
#
# Validate all hardware registry files listed in the registry index against
# the hardware registry schema.
#
# Usage:
#   python3 tests/hw_reg_checker.py
#   python3 tests/hw_reg_checker.py /path/to/hardware_registry/index.yaml

import argparse
import glob
import os
import sys

import yaml
from jsonschema import ValidationError, validate

# Every YAML file in the hardware registry directory must nest its content
# under this single top-level key. config/ is loaded recursively (and flattened
# into a single configmap in production), so any other top-level key here would
# be merged into the pipeline config namespace and could collide with it
# (notably `platforms`).
NAMESPACE = "hardware_registry"

SECTIONS = (
    "silicon_vendors",
    "platform_vendors",
    "processors",
    "system_modules",
    "platforms",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Validate hardware registry files against the schema"
    )
    parser.add_argument(
        "index",
        nargs="?",
        help="Path to hardware registry index.yaml (default: config/hardware_registry/index.yaml)",
    )
    return parser.parse_args()


def default_index_path():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    return os.path.join(repo_root, "config", "hardware_registry", "index.yaml")


def check_namespace_prefix(registry_dir):
    """Ensure every YAML file in the registry dir is namespaced.

    Each file's sole top-level key must be NAMESPACE, so that merging these
    files into the recursively-loaded pipeline config cannot collide with it.
    Returns a list of error strings (empty if all files conform).
    """
    errors = []
    for path in sorted(glob.glob(os.path.join(registry_dir, "*.yaml"))):
        rel = os.path.relpath(path)
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: invalid YAML: {exc}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{rel}: top level must be a mapping")
            continue
        keys = list(data.keys())
        if keys != [NAMESPACE]:
            errors.append(
                f"{rel}: top-level keys {keys} must be exactly "
                f"['{NAMESPACE}'] to stay out of the pipeline config namespace"
            )
    return errors


def load_namespaced(path):
    """Load a registry YAML file and return its unwrapped content.

    Raises KeyError/ValueError-free; returns (data, error_string)."""
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict) or list(raw.keys()) != [NAMESPACE]:
        return None, (
            f"content must be nested under a single top-level "
            f"'{NAMESPACE}:' key"
        )
    return raw[NAMESPACE], None


def validate_entity_ids(data):
    """Validate that each entity's id matches its collection key.

    Returns a list of error strings (empty if all ids match).
    """
    errors = []
    for section in SECTIONS:
        for key, entity in (data.get(section) or {}).items():
            entity_id = entity.get("id")
            if entity_id is not None and entity_id != key:
                errors.append(
                    f"{section}[{key}].id = '{entity_id}' "
                    f"does not match its key '{key}'"
                )
    return errors


def validate_references(data):
    """Validate cross-references between sections.

    Returns a list of error strings (empty if all references are valid).
    """
    errors = []

    silicon_vendors = set(data.get("silicon_vendors", {}).keys())
    platform_vendors = set(data.get("platform_vendors", {}).keys())
    processors = set(data.get("processors", {}).keys())
    system_modules = set(data.get("system_modules", {}).keys())

    def check_ref(section, entry_key, field, valid_set, valid_set_name):
        value = data.get(section, {}).get(entry_key, {}).get(field)
        if value is not None and value not in valid_set:
            errors.append(
                f"{section}[{entry_key}].{field} = '{value}' "
                f"not found in {valid_set_name}"
            )

    for key in data.get("processors", {}):
        check_ref(
            "processors", key, "vendor_id", silicon_vendors, "silicon_vendors"
        )

    for key in data.get("system_modules", {}):
        check_ref(
            "system_modules",
            key,
            "vendor_id",
            platform_vendors,
            "platform_vendors",
        )
        check_ref(
            "system_modules", key, "processor_id", processors, "processors"
        )

    for key in data.get("platforms", {}):
        check_ref(
            "platforms", key, "vendor_id", platform_vendors, "platform_vendors"
        )
        check_ref("platforms", key, "processor_id", processors, "processors")
        check_ref(
            "platforms",
            key,
            "system_module_id",
            system_modules,
            "system_modules",
        )

    return errors


def merge_registries(datasets):
    """Merge per-file registry data into a single set of sections.

    Takes a list of (filename, data) pairs and returns (merged, errors).
    The same entity id appearing in two files is an error. This is the
    cross-file entity namespace, distinct from the top-level NAMESPACE wrap.
    """
    merged = {section: {} for section in SECTIONS}
    origin = {section: {} for section in SECTIONS}
    errors = []
    for filename, data in datasets:
        for section in SECTIONS:
            for key, entity in (data.get(section) or {}).items():
                if key in merged[section]:
                    errors.append(
                        f"{section}[{key}] defined in both "
                        f"{origin[section][key]} and {filename}"
                    )
                else:
                    merged[section][key] = entity
                    origin[section][key] = filename
    return merged, errors


def check_board_compatibles(merged):
    """Require board-level compatibles to be unique across the registry.

    The first compatible string identifies the board, so two platforms
    claiming the same one would make a compatible-based join ambiguous.
    SoC-level compatibles in later positions may repeat.
    """
    errors = []
    seen = {}
    for key, platform in merged["platforms"].items():
        compatible = platform.get("compatible")
        if not compatible:
            continue
        board = compatible[0]
        if board in seen:
            errors.append(
                f"platforms[{key}] board-level compatible '{board}' "
                f"already used by platforms[{seen[board]}]"
            )
        else:
            seen[board] = key
    return errors


def main():
    args = parse_args()
    index_path = os.path.abspath(args.index or default_index_path())
    index_dir = os.path.dirname(index_path)

    if not os.path.exists(index_path):
        print(f"ERROR: Index file not found: {index_path}")
        sys.exit(1)

    # Enforce that the whole directory stays namespaced before anything else.
    ns_errors = check_namespace_prefix(index_dir)
    if ns_errors:
        print("Namespace check FAILED:")
        for err in ns_errors:
            print(f"  {err}")
        sys.exit(1)
    print(f"Namespace: all files nested under '{NAMESPACE}:' OK")

    index, err = load_namespaced(index_path)
    if err:
        print(f"ERROR: index {err}")
        sys.exit(1)

    schema_path = os.path.normpath(os.path.join(index_dir, index["schema"]))
    if not os.path.exists(schema_path):
        print(f"ERROR: Schema file not found: {schema_path}")
        sys.exit(1)

    schema, err = load_namespaced(schema_path)
    if err:
        print(f"ERROR: schema {err}")
        sys.exit(1)

    registries = index.get("registries", [])
    if not registries:
        print("WARNING: No registry files listed in index.")
        sys.exit(0)

    print(f"Schema:  {os.path.relpath(schema_path)}")
    print(f"Index:   {os.path.relpath(index_path)}")
    print(f"Entries: {len(registries)}\n")

    all_passed = True
    datasets = []

    for registry_file in registries:
        registry_path = os.path.normpath(os.path.join(index_dir, registry_file))
        print(f"Checking {registry_file} ...")

        if not os.path.exists(registry_path):
            print(f"  ERROR: File not found: {registry_path}")
            all_passed = False
            continue

        data, err = load_namespaced(registry_path)
        if err:
            print(f"  FAIL: {err}")
            all_passed = False
            print()
            continue

        try:
            validate(instance=data, schema=schema)
        except ValidationError as e:
            print("  FAIL: Validation error")
            print(f"    Message:     {e.message}")
            print(f"    Path:        {' -> '.join(map(str, e.path))}")
            print(f"    Schema path: {' -> '.join(map(str, e.schema_path))}")
            all_passed = False
            print()
            continue

        id_errors = validate_entity_ids(data)
        if id_errors:
            print("  FAIL: Entity id errors")
            for err in id_errors:
                print(f"    {err}")
            all_passed = False
        else:
            print("  OK")
            print(f"  Silicon vendors:  {len(data.get('silicon_vendors', {}))}")
            print(
                f"  Platform vendors: {len(data.get('platform_vendors', {}))}"
            )
            print(f"  Processors:       {len(data.get('processors', {}))}")
            if "system_modules" in data:
                print(f"  System modules:   {len(data['system_modules'])}")
            print(f"  Platforms:        {len(data.get('platforms', {}))}")
            datasets.append((registry_file, data))

        print()

    merged, merge_errors = merge_registries(datasets)
    ref_errors = validate_references(merged)
    compat_errors = check_board_compatibles(merged)
    for err in merge_errors + ref_errors + compat_errors:
        print(f"MERGED REGISTRY ERROR: {err}")
    if merge_errors or ref_errors or compat_errors:
        all_passed = False

    if all_passed:
        print("All registry files passed validation.")
    else:
        print("One or more registry files failed validation.")
        sys.exit(1)


if __name__ == "__main__":
    main()
