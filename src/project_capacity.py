"""Shared quota thresholds for the application and its private helper."""


def quota_rejection(used, quota, *, expected_bytes=0, installing_dependencies=False):
    if (
        any(type(value) is not int for value in (used, quota, expected_bytes))
        or min(used, expected_bytes) < 0
        or quota <= 0
    ):
        raise ValueError("Invalid project capacity observation")
    if used + expected_bytes >= quota:
        return "project_quota_full"
    if installing_dependencies and used * 100 >= quota * 95:
        return "project_dependency_limit"
    return None
