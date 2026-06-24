import os

import pytest
import yaml
from jsonschema import ValidationError
from jsonschema import validate as js_validate

import hw_reg_checker

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "config",
    "hardware_registry",
    "schema.yaml",
)


def load_schema():
    with open(SCHEMA_PATH) as f:
        raw = yaml.safe_load(f)
    return raw[hw_reg_checker.NAMESPACE]


def minimal_registry(platform_extra=None):
    platform = {
        "id": "am62-sk",
        "type": "evaluation_board",
        "vendor_id": "ti",
        "processor_id": "am625",
        "url": "https://example.com",
    }
    platform.update(platform_extra or {})
    return {
        "schema_info": {"version": "1.1.0", "revision_date": "2026-06-12"},
        "silicon_vendors": {
            "ti": {
                "id": "ti",
                "type": "silicon_vendor",
                "url": "https://www.ti.com",
            }
        },
        "platform_vendors": {
            "ti": {
                "id": "ti",
                "type": "platform_vendor",
                "url": "https://www.ti.com",
            }
        },
        "processors": {
            "am625": {
                "id": "am625",
                "type": "soc",
                "vendor_id": "ti",
                "architecture": "arm64",
                "url": "https://example.com",
            }
        },
        "platforms": {"am62-sk": platform},
    }


def make_data(**sections):
    base = {
        "silicon_vendors": {},
        "platform_vendors": {},
        "processors": {},
        "system_modules": {},
        "platforms": {},
    }
    base.update(sections)
    return base


def test_entity_id_mismatch_detected():
    data = make_data(
        silicon_vendors={"ti": {"id": "texas", "type": "silicon_vendor"}}
    )
    errors = hw_reg_checker.validate_entity_ids(data)
    assert len(errors) == 1
    assert "does not match its key" in errors[0]


def test_entity_id_match_passes():
    data = make_data(
        silicon_vendors={"ti": {"id": "ti", "type": "silicon_vendor"}}
    )
    assert hw_reg_checker.validate_entity_ids(data) == []


def test_dangling_processor_vendor_ref():
    data = make_data(processors={"am625": {"id": "am625", "vendor_id": "ti"}})
    errors = hw_reg_checker.validate_references(data)
    assert len(errors) == 1
    assert "vendor_id" in errors[0]


def test_valid_references_pass():
    data = make_data(
        silicon_vendors={"ti": {"id": "ti"}},
        platform_vendors={"ti": {"id": "ti"}},
        processors={"am625": {"id": "am625", "vendor_id": "ti"}},
        platforms={
            "am62-sk": {
                "id": "am62-sk",
                "vendor_id": "ti",
                "processor_id": "am625",
            }
        },
    )
    assert hw_reg_checker.validate_references(data) == []


def test_duplicate_id_across_files_detected():
    file_a = make_data(processors={"am625": {"id": "am625", "vendor_id": "ti"}})
    file_b = make_data(processors={"am625": {"id": "am625", "vendor_id": "ti"}})
    merged, errors = hw_reg_checker.merge_registries(
        [("ti.yaml", file_a), ("other.yaml", file_b)]
    )
    assert len(errors) == 1
    assert "ti.yaml" in errors[0] and "other.yaml" in errors[0]


def test_cross_file_reference_resolves():
    file_a = make_data(
        silicon_vendors={"ti": {"id": "ti"}},
        processors={"am625": {"id": "am625", "vendor_id": "ti"}},
    )
    file_b = make_data(
        platform_vendors={"toradex": {"id": "toradex"}},
        system_modules={
            "verdin-am62": {
                "id": "verdin-am62",
                "type": "system_on_module",
                "vendor_id": "toradex",
                "processor_id": "am625",
            }
        },
    )
    merged, errors = hw_reg_checker.merge_registries(
        [("ti.yaml", file_a), ("toradex.yaml", file_b)]
    )
    assert errors == []
    assert hw_reg_checker.validate_references(merged) == []


def test_merged_dangling_reference_detected():
    file_a = make_data(
        system_modules={
            "som-x": {
                "id": "som-x",
                "vendor_id": "nobody",
                "processor_id": "ghost",
            }
        }
    )
    merged, errors = hw_reg_checker.merge_registries([("a.yaml", file_a)])
    assert errors == []
    ref_errors = hw_reg_checker.validate_references(merged)
    assert len(ref_errors) == 2


def test_schema_accepts_platform_compatible():
    data = minimal_registry({"compatible": ["ti,am625-sk", "ti,am625"]})
    js_validate(instance=data, schema=load_schema())


def test_schema_accepts_platform_without_compatible():
    data = minimal_registry()
    js_validate(instance=data, schema=load_schema())


def test_schema_rejects_non_list_compatible():
    data = minimal_registry({"compatible": "ti,am625-sk"})
    with pytest.raises(ValidationError):
        js_validate(instance=data, schema=load_schema())


def test_duplicate_board_compatible_rejected():
    merged = make_data(
        platforms={
            "board-a": {"id": "board-a", "compatible": ["ti,board", "ti,soc"]},
            "board-b": {"id": "board-b", "compatible": ["ti,board", "ti,soc"]},
        }
    )
    errors = hw_reg_checker.check_board_compatibles(merged)
    assert len(errors) == 1
    assert "ti,board" in errors[0]


def test_shared_soc_compatible_allowed():
    merged = make_data(
        platforms={
            "board-a": {
                "id": "board-a",
                "compatible": ["ti,board-a", "ti,soc"],
            },
            "board-b": {
                "id": "board-b",
                "compatible": ["ti,board-b", "ti,soc"],
            },
        }
    )
    assert hw_reg_checker.check_board_compatibles(merged) == []


def test_platforms_without_compatible_ignored():
    merged = make_data(platforms={"board-a": {"id": "board-a"}})
    assert hw_reg_checker.check_board_compatibles(merged) == []


def test_pipeline_platform_matching_registry_board_is_covered():
    merged = make_data(
        platforms={
            "am62-sk": {
                "id": "am62-sk",
                "compatible": ["ti,am625-sk", "ti,am625"],
            }
        }
    )
    pipeline = {"am62x-sk": {"compatible": ["ti,am625-sk", "ti,am625"]}}
    errors, uncovered = hw_reg_checker.check_pipeline_platforms(
        merged, pipeline
    )
    assert errors == []
    assert uncovered == []


def test_pipeline_board_compatible_buried_in_registry_is_drift():
    merged = make_data(
        platforms={
            "board-a": {
                "id": "board-a",
                "compatible": ["ti,board-a", "ti,board-b", "ti,soc"],
            }
        }
    )
    pipeline = {"board-b": {"compatible": ["ti,board-b", "ti,soc"]}}
    errors, uncovered = hw_reg_checker.check_pipeline_platforms(
        merged, pipeline
    )
    assert len(errors) == 1
    assert "ti,board-b" in errors[0]


def test_unmatched_pipeline_platform_is_uncovered():
    merged = make_data()
    pipeline = {
        "rpi-4": {"compatible": ["raspberrypi,4-model-b", "brcm,bcm2711"]},
        "qemu-x86": {},
    }
    errors, uncovered = hw_reg_checker.check_pipeline_platforms(
        merged, pipeline
    )
    assert errors == []
    assert uncovered == ["qemu-x86", "rpi-4"]
