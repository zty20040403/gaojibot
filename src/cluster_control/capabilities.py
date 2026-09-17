from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adapters.ops import OpsOperation


@dataclass(frozen=True)
class CapabilityBinding:
    name: str
    operation: str
    sensitive: bool = False
    cacheable: bool = True


BINDINGS: tuple[CapabilityBinding, ...] = (
    CapabilityBinding("fleet.read", "fleet.overview"),
    CapabilityBinding("fleet.failed_units.read", "units.failed"),
    CapabilityBinding("fleet.alerts.read", "alerts.active"),
    CapabilityBinding("host.facts.read", "host.facts"),
    CapabilityBinding("host.metrics.read", "host.metrics"),
    CapabilityBinding("unit.status.read", "units.status"),
    CapabilityBinding(
        "unit.logs.read", "units.logs", sensitive=True, cacheable=False
    ),
)

BINDING_BY_CAPABILITY = {item.name: item for item in BINDINGS}

_EXPECTED_PARAMS: dict[str, tuple[dict[str, str], set[str]]] = {
    "fleet.overview": ({}, set()),
    "units.failed": ({}, set()),
    "alerts.active": ({}, set()),
    "host.facts": ({"host": "string"}, {"host"}),
    "host.metrics": ({"host": "string"}, {"host"}),
    "units.status": (
        {"host": "string", "unit": "string"},
        {"host", "unit"},
    ),
    "units.logs": (
        {
            "host": "string",
            "unit": "string",
            "lines": "integer",
            "since_seconds": "integer",
        },
        {"host", "unit"},
    ),
}


def operation_compatible(operation: OpsOperation) -> bool:
    expected_properties, expected_required = _EXPECTED_PARAMS.get(
        operation.name, ({}, set())
    )
    schema = operation.params_schema
    if schema.get("type") != "object":
        return False
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        return False
    if {str(item) for item in required} != expected_required:
        return False
    for name, expected_type in expected_properties.items():
        definition = properties.get(name)
        if not isinstance(definition, dict) or definition.get("type") != expected_type:
            return False
    return True


def capability_manifest(
    operations: dict[str, OpsOperation],
    *, backend_name: str = "ops",
) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for binding in BINDINGS:
        operation = operations.get(binding.operation)
        compatible = operation is not None and operation_compatible(operation)
        manifest.append({
            "name": binding.name,
            "backend": backend_name,
            "operation": binding.operation,
            "available": compatible,
            "read_only": True,
            "sensitive": binding.sensitive,
            "reason": (
                ""
                if compatible
                else "upstream_operation_missing"
                if operation is None
                else "upstream_schema_incompatible"
            ),
        })
    return manifest
