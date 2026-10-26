"""Bind file acceptance to immutable artifacts, independently of workflow success."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import PurePosixPath
import re
from typing import Any


ACCEPTANCE_VERSION = 4


def separate_review_artifacts(
    result: dict[str, Any], upstream: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """A reviewer may cite an authorized snapshot, never produce or re-export it."""
    result = deepcopy(result)
    metadata = result.setdefault("metadata", {})
    metadata.pop("review_artifact_references", None)
    targets = {}
    for source in upstream.values():
        for artifact in source.get("artifacts", []):
            if isinstance(artifact, Mapping) and re.fullmatch(r"[a-f0-9]{64}", str(artifact.get("snapshot", ""))):
                targets[(artifact.get("handle"), artifact["snapshot"])] = artifact
    references = []
    invalid = []
    for artifact in result.get("artifacts", []):
        matches = [target for target in targets.values()
                   if isinstance(artifact, Mapping)
                   and artifact.get("handle") == target.get("handle")
                   and (not artifact.get("snapshot") or artifact["snapshot"] == target["snapshot"])
                   and ("size" not in artifact or artifact["size"] == target.get("size"))]
        if len(matches) == 1:
            target = matches[0]
            references.append({"handle": target["handle"], "snapshot": target["snapshot"],
                               "name": target.get("name"), "kind": "review_reference"})
        else:
            invalid.append(str(artifact.get("handle", "")) if isinstance(artifact, Mapping) else "invalid entry")
    result["artifacts"] = []
    if references:
        metadata["review_artifact_references"] = references
    if invalid:
        result["status"] = "failed"
        result.setdefault("unresolved", []).append(
            "独立验收只能引用获准的上游快照，不能新增或替换交付物：" + ", ".join(invalid))
    return result


def artifact_identity(artifact: Mapping[str, Any]) -> str:
    return str(artifact.get("snapshot") or artifact.get("handle") or "")


def artifact_verdicts(
    checks: Sequence[Mapping[str, Any]],
    result: Mapping[str, Any],
    *,
    executed: bool,
) -> list[dict[str, Any]]:
    metadata = result.get("metadata")
    reviews = metadata.get("artifact_reviews", []) if isinstance(metadata, Mapping) else []
    by_key: dict[str, list[Mapping[str, Any]]] = {}
    if isinstance(reviews, list):
        for review in reviews:
            if isinstance(review, Mapping) and isinstance(review.get("artifact_key"), str):
                by_key.setdefault(review["artifact_key"], []).append(review)
    verdicts = []
    for check in checks:
        key = str(check.get("artifact_key") or "")
        matching = by_key.get(key, [])
        # Missing, duplicated or contradictory declarations never authorize a file.
        review = matching[0] if len(matching) == 1 else {}
        passed = bool(key and check.get("ok") is True and executed
                      and review.get("status") == "passed")
        verdicts.append({
            "artifact_key": key, "step": check.get("step"),
            "name": check.get("artifact"), "status": "passed" if passed else "failed",
            "reason": (str(check.get("error") or "格式或校验和检查失败") if not check.get("ok")
                       else "缺少独立工具验收证据" if not executed
                       else str(review.get("reason") or ("独立文件验收通过" if passed else "缺少此文件的独立验收结论"))),
        })
    return verdicts


def artifact_delivery_allowed(artifact: Mapping[str, Any], validation: Mapping[str, Any]) -> bool:
    key = artifact_identity(artifact)
    reviews = validation.get("artifacts")
    if isinstance(reviews, list):
        matching = [r for r in reviews if isinstance(r, Mapping) and r.get("artifact_key") == key]
        return bool(key and len(matching) == 1 and matching[0].get("status") == "passed")
    # Compatibility for already-completed legacy acceptance, never a failed review.
    return validation.get("status") == "passed"


def artifact_draft_allowed(artifact: Mapping[str, Any], validation: Mapping[str, Any]) -> bool:
    """Content may be incomplete, but the exact snapshot must still be readable."""
    digest = str(artifact.get("snapshot") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        return False
    checks = validation.get("checks", [])
    if not isinstance(checks, list):
        return False
    matching = [c for c in checks if isinstance(c, Mapping) and c.get("artifact_key") == digest]
    if len(matching) != 1 or not (matching[0].get("ok") is True or matching[0].get("readable") is True):
        return False
    required = {"nonempty", "sha256"}
    if PurePosixPath(str(artifact.get("name", ""))).suffix.lower() in {
        ".pdf", ".zip", ".docx", ".xlsx", ".pptx", ".png",
    }:
        required.add("format")
    performed = matching[0].get("checks")
    return isinstance(performed, list) and required.issubset(performed)
