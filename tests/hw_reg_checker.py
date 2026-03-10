#!/usr/bin/env python3
#
# Validate all hardware registry files listed in the registry index against
# the hardware registry schema.
#
# Usage:
#   python3 tests/hw_reg_checker.py
#   python3 tests/hw_reg_checker.py /path/to/hardware_registry/index.yaml

import argparse
import os
import sys

import yaml
from jsonschema import validate, ValidationError


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


def validate_references(data, registry_file):
    """Validate cross-references between sections in a registry file.

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

    for section in ("silicon_vendors", "platform_vendors", "processors",
                    "system_modules", "platforms"):
        for key, entity in data.get(section, {}).items():
            entity_id = entity.get("id")
            if entity_id is not None and entity_id != key:
                errors.append(
                    f"{section}[{key}].id = '{entity_id}' "
                    f"does not match its key '{key}'"
                )

    for key in data.get("processors", {}):
        check_ref("processors", key, "vendor_id", silicon_vendors, "silicon_vendors")

    for key in data.get("system_modules", {}):
        check_ref("system_modules", key, "vendor_id", platform_vendors, "platform_vendors")
        check_ref("system_modules", key, "processor_id", processors, "processors")

    for key in data.get("platforms", {}):
        check_ref("platforms", key, "vendor_id", platform_vendors, "platform_vendors")
        check_ref("platforms", key, "processor_id", processors, "processors")
        check_ref("platforms", key, "system_module_id", system_modules, "system_modules")

    return errors


def main():
    args = parse_args()
    index_path = os.path.abspath(args.index or default_index_path())
    index_dir = os.path.dirname(index_path)

    if not os.path.exists(index_path):
        print(f"ERROR: Index file not found: {index_path}")
        sys.exit(1)

    with open(index_path) as f:
        index = yaml.safe_load(f)

    schema_path = os.path.normpath(os.path.join(index_dir, index["schema"]))
    if not os.path.exists(schema_path):
        print(f"ERROR: Schema file not found: {schema_path}")
        sys.exit(1)

    with open(schema_path) as f:
        schema = yaml.safe_load(f)

    registries = index.get("registries", [])
    if not registries:
        print("WARNING: No registry files listed in index.")
        sys.exit(0)

    print(f"Schema:  {os.path.relpath(schema_path)}")
    print(f"Index:   {os.path.relpath(index_path)}")
    print(f"Entries: {len(registries)}\n")

    all_passed = True

    for registry_file in registries:
        registry_path = os.path.normpath(os.path.join(index_dir, registry_file))
        print(f"Checking {registry_file} ...")

        if not os.path.exists(registry_path):
            print(f"  ERROR: File not found: {registry_path}")
            all_passed = False
            continue

        try:
            with open(registry_path) as f:
                data = yaml.safe_load(f)

            validate(instance=data, schema=schema)

            ref_errors = validate_references(data, registry_file)
            if ref_errors:
                print(f"  FAIL: Reference errors")
                for err in ref_errors:
                    print(f"    {err}")
                all_passed = False
            else:
                print(f"  OK")
                print(f"  Silicon vendors:  {len(data.get('silicon_vendors', {}))}")
                print(f"  Platform vendors: {len(data.get('platform_vendors', {}))}")
                print(f"  Processors:       {len(data.get('processors', {}))}")
                if "system_modules" in data:
                    print(f"  System modules:   {len(data['system_modules'])}")
                print(f"  Platforms:        {len(data.get('platforms', {}))}")

        except ValidationError as e:
            print(f"  FAIL: Validation error")
            print(f"    Message:     {e.message}")
            print(f"    Path:        {' -> '.join(map(str, e.path))}")
            print(f"    Schema path: {' -> '.join(map(str, e.schema_path))}")
            all_passed = False

        print()

    if all_passed:
        print("All registry files passed validation.")
    else:
        print("One or more registry files failed validation.")
        sys.exit(1)


if __name__ == "__main__":
    main()
