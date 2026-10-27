from __future__ import annotations

import time
import threading

from src.bot_storage import StateSource, open_json_state


class GroupUserProfileStore:
    def __init__(self, state_path: StateSource) -> None:
        self._lock = threading.RLock()
        self._state = open_json_state(state_path, "user_profiles")
        self._profiles = self._load()

    def observe(
        self,
        group_id: int,
        user_id: int,
        nickname: str = "",
        card: str = "",
    ) -> None:
        with self._lock:
            group_key = str(group_id)
            user_key = str(user_id)
            nickname = " ".join(nickname.split())
            card = " ".join(card.split())
            now = int(time.time())

            group = self._profiles.setdefault(group_key, {})
            previous = group.get(user_key, {})
            should_save = (
                previous.get("nickname") != nickname
                or previous.get("card") != card
                or now - int(previous.get("last_seen", 0)) >= 300
            )
            if not should_save:
                return

            group[user_key] = {
                "nickname": nickname,
                "card": card,
                "last_seen": now,
            }
            self._save()

    def describe_user(self, group_id: int, user_id: int) -> str:
        with self._lock:
            profile = self._profiles.get(str(group_id), {}).get(str(user_id))
            if not isinstance(profile, dict):
                return f"QQ {user_id}"

            nickname = str(profile.get("nickname", "")).strip()
            card = str(profile.get("card", "")).strip()
            parts = [f"QQ {user_id}"]
            if card:
                parts.append(f"群名片“{card}”")
            if nickname and nickname != card:
                parts.append(f"QQ昵称“{nickname}”")
            return "，".join(parts)

    def render_group(self, group_id: int, max_users: int = 30) -> str:
        with self._lock:
            group = self._profiles.get(str(group_id), {})
            if not isinstance(group, dict):
                return ""

            recent_users = sorted(
                group.items(),
                key=lambda item: (
                    int(item[1].get("last_seen", 0)) if isinstance(item[1], dict) else 0
                ),
                reverse=True,
            )[:max_users]
            lines: list[str] = []
            for raw_user_id, profile in recent_users:
                if not isinstance(profile, dict):
                    continue
                try:
                    user_id = int(raw_user_id)
                except ValueError:
                    continue
                lines.append(f"- {self.describe_user(group_id, user_id)}")
            return "\n".join(lines)

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

    def clear_group(self, group_id: int) -> int:
        with self._lock:
            removed = self._profiles.pop(str(group_id), {})
            self._save()
            return len(removed) if isinstance(removed, dict) else 0

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

    def _save(self) -> None:
        self._state.save(self._profiles)
