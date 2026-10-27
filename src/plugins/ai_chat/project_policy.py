"""Deterministic idle and version-retention decisions; no host effects."""

from dataclasses import dataclass
from datetime import datetime, timezone


DAY = 24 * 3600


@dataclass(frozen=True)
class ProjectPolicy:
    idle_stop_seconds: int = 30 * 60
    hibernate_seconds: int = 7 * DAY
    archive_seconds: int = 60 * DAY
    keep_recent: int = 10

    def __post_init__(self):
        values = (self.idle_stop_seconds, self.hibernate_seconds, self.archive_seconds)
        if (
            any(type(value) is not int or value <= 0 for value in values)
            or not values[0] < values[1] < values[2]
            or type(self.keep_recent) is not int
            or self.keep_recent < 1
        ):
            raise ValueError(
                "Project idle thresholds must increase; retention must be positive"
            )

    def idle_action(self, project, *, now):
        if (
            project["workspace_generation"] == 0
            or project["lifecycle_job_id"] is not None
            or project["locked_task_id"] is not None
        ):
            return None
        next_state = {
            "active": ("stop", self.idle_stop_seconds),
            "stopped": ("hibernate", self.hibernate_seconds),
            "hibernated": ("archive", self.archive_seconds),
        }.get(project["status"])
        if next_state is None:
            return None
        action, threshold = next_state
        if action == "archive" and project["pinned"]:
            return None
        return action if now - project["last_active_at"] >= threshold else None

    def retain_versions(self, versions, *, now, archived=False):
        """UTC calendar buckets, newest entry per bucket, independent of input order.

        Labels and pins are permanent; unlabelled/unpinned versions count toward
        the automatic-version allowance. Referenced baselines are also protected
        by the store at deletion time, regardless of this selection.
        """
        rows = sorted(
            versions,
            key=lambda row: (row["created_at"], row["version_no"]),
            reverse=True,
        )
        keep = {max(row["version_no"] for row in rows)} if rows else set()
        recent, buckets = 0, set()
        for row in rows:
            number, created = row["version_no"], row["created_at"]
            if row["pinned"] or row["label"]:
                keep.add(number)
                continue
            # A clock rollback must not make a future-dated version disposable.
            if created > now:
                keep.add(number)
            if archived:
                continue
            if recent < self.keep_recent:
                recent += 1
                keep.add(number)
                continue
            date = datetime.fromtimestamp(created, timezone.utc).date()
            bucket = (
                ("day", date)
                if now - created <= 30 * DAY
                else ("week", *date.isocalendar()[:2])
            )
            if bucket not in buckets:
                buckets.add(bucket)
                keep.add(number)
        return keep
