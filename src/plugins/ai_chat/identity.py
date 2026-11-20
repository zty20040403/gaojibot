from __future__ import annotations

import threading

from src.bot_storage import StateSource, open_json_state


class GroupUserProfileStore:
    def __init__(self, state_path: StateSource) -> None:
        self._lock = threading.RLock()
        self._state = open_json_state(state_path, "user_profiles")
        self._profiles = self._load()

    def group_ids(self) -> tuple[int, ...]:
        with self._lock:
            result: list[int] = []
            for raw_group_id in self._profiles:
                try:
                    result.append(int(raw_group_id))
                except ValueError:
                    continue
            return tuple(sorted(result))

    def members(
        self,
        group_id: int,
        *,
        max_users: int = 500,
    ) -> list[dict[str, object]]:
        with self._lock:
            group = self._profiles.get(str(group_id), {})
            if not isinstance(group, dict):
                return []

            members: list[dict[str, object]] = []
            for raw_user_id, raw_profile in group.items():
                if not isinstance(raw_profile, dict):
                    continue
                try:
                    user_id = int(raw_user_id)
                    last_seen = max(int(raw_profile.get("last_seen", 0)), 0)
                except (TypeError, ValueError):
                    continue
                nickname = str(raw_profile.get("nickname", "")).strip()
                card = str(raw_profile.get("card", "")).strip()
                members.append(
                    {
                        "user_id": user_id,
                        "nickname": nickname,
                        "card": card,
                        "display_name": card or nickname or f"QQ {user_id}",
                        "last_seen": last_seen,
                    }
                )
            members.sort(
                key=lambda item: (-int(item["last_seen"]), int(item["user_id"]))
            )
            return members[: min(max(int(max_users), 1), 1000)]

    def _load(self) -> dict[str, dict[str, dict[str, object]]]:
        data = self._state.load()
        if not isinstance(data, dict):
            return {}

        profiles: dict[str, dict[str, dict[str, object]]] = {}
        for raw_group_id, raw_group in data.items():
            if not isinstance(raw_group, dict):
                continue
            group: dict[str, dict[str, object]] = {}
            for raw_user_id, raw_profile in raw_group.items():
                if not isinstance(raw_profile, dict):
                    continue
                try:
                    group_id = str(int(raw_group_id))
                    user_id = str(int(raw_user_id))
                    last_seen = int(raw_profile.get("last_seen", 0))
                except (TypeError, ValueError):
                    continue
                group[user_id] = {
                    "nickname": str(raw_profile.get("nickname", "")),
                    "card": str(raw_profile.get("card", "")),
                    "last_seen": max(0, last_seen),
                }
                profiles[group_id] = group
        return profiles
