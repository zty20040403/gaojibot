from __future__ import annotations

import threading

from src.bot_storage import StateSource, open_json_state
from .conversation_keys import ScopeSettingAddress
from .conversation_scope import ConversationScope


class ModelPreferenceStore:
    def __init__(
        self,
        state_path: StateSource,
        *,
        namespace: str = "model_preferences",
    ) -> None:
        self._state = open_json_state(state_path, namespace)
        self._lock = threading.RLock()
        self._models = self._load()

    def get(self, conversation_id: str, default: str) -> str:
        with self._lock:
            return self._models.get(conversation_id, default)

    def get_explicit(self, conversation_id: str) -> str | None:
        with self._lock:
            return self._models.get(conversation_id)

    def items(self) -> list[tuple[str, str]]:
        with self._lock:
            return sorted(self._models.items())

    def set(self, conversation_id: str, model: str) -> None:
        with self._lock:
            self._models[conversation_id] = model
            self._save()

    def clear(self, conversation_id: str) -> bool:
        with self._lock:
            removed = self._models.pop(conversation_id, None)
            if removed is None:
                return False
            self._save()
            return True

    def get_group_default(self, group_id: int) -> str | None:
        return self.get_explicit(self.group_default_key(group_id))

    def set_group_default(self, group_id: int, model: str) -> None:
        self.set(self.group_default_key(group_id), model)

    def clear_group_default(self, group_id: int) -> bool:
        return self.clear(self.group_default_key(group_id))

    def get_scope_default(self, scope: ConversationScope) -> str | None:
        return self.get_explicit(ScopeSettingAddress(scope, "default").key)

    def set_scope_default(self, scope: ConversationScope, model: str) -> None:
        self.set(ScopeSettingAddress(scope, "default").key, model)

    def clear_scope_default(self, scope: ConversationScope) -> bool:
        return self.clear(ScopeSettingAddress(scope, "default").key)

    def get_scope_member_default(self, scope: ConversationScope) -> str | None:
        return self.get_explicit(ScopeSettingAddress(scope, "member-default").key)

    def set_scope_member_default(self, scope: ConversationScope, value: str) -> None:
        self.set(ScopeSettingAddress(scope, "member-default").key, value)

    def clear_scope_member_default(self, scope: ConversationScope) -> bool:
        return self.clear(ScopeSettingAddress(scope, "member-default").key)

    def get_scope_enabled_override(self, scope: ConversationScope) -> bool | None:
        return self._get_scope_switch(scope, "enabled")

    def set_scope_enabled(self, scope: ConversationScope, enabled: bool) -> None:
        self.set(ScopeSettingAddress(scope, "enabled").key, "enabled" if enabled else "disabled")

    def clear_scope_enabled_override(self, scope: ConversationScope) -> bool:
        return self.clear(ScopeSettingAddress(scope, "enabled").key)

    def get_scope_vision_auto_describe_override(self, scope: ConversationScope) -> bool | None:
        return self._get_scope_switch(scope, "vision-auto-describe")

    def set_scope_vision_auto_describe(self, scope: ConversationScope, enabled: bool) -> None:
        self.set(ScopeSettingAddress(scope, "vision-auto-describe").key, "enabled" if enabled else "disabled")

    def _get_scope_switch(self, scope: ConversationScope, name: str) -> bool | None:
        stored = self.get_explicit(ScopeSettingAddress(scope, name).key)
        return True if stored == "enabled" else False if stored == "disabled" else None

    def scoped_items(self) -> list[tuple[ScopeSettingAddress, str]]:
        """Typed settings for the control plane; unknown keys remain untouched."""
        result = []
        for key, value in self.items():
            try:
                result.append((ScopeSettingAddress.parse(key), value))
            except ValueError:
                continue
        return result

    def get_group_member_default(self, group_id: int) -> str | None:
        return self.get_explicit(self.group_member_default_key(group_id))

    def set_group_member_default(self, group_id: int, value: str) -> None:
        self.set(self.group_member_default_key(group_id), value)

    def clear_group_member_default(self, group_id: int) -> bool:
        return self.clear(self.group_member_default_key(group_id))

    def get_group_enabled_override(self, group_id: int) -> bool | None:
        stored = self.get_explicit(self.group_enabled_key(group_id))
        if stored == "enabled":
            return True
        if stored == "disabled":
            return False
        return None

    def set_group_enabled(self, group_id: int, enabled: bool) -> None:
        self.set(
            self.group_enabled_key(group_id),
            "enabled" if enabled else "disabled",
        )

    def clear_group_enabled_override(self, group_id: int) -> bool:
        return self.clear(self.group_enabled_key(group_id))

    def get_group_vision_auto_describe_override(self, group_id: int) -> bool | None:
        stored = self.get_explicit(self.group_vision_auto_describe_key(group_id))
        if stored == "enabled":
            return True
        if stored == "disabled":
            return False
        return None

    def set_group_vision_auto_describe(self, group_id: int, enabled: bool) -> None:
        self.set(
            self.group_vision_auto_describe_key(group_id),
            "enabled" if enabled else "disabled",
        )

    @staticmethod
    def group_default_key(group_id: int) -> str:
        normalized = int(group_id)
        if normalized <= 0:
            raise ValueError("group_id must be positive")
        return f"group:{normalized}:default"

    @staticmethod
    def group_member_default_key(group_id: int) -> str:
        normalized = int(group_id)
        if normalized <= 0:
            raise ValueError("group_id must be positive")
        return f"group:{normalized}:member-default"

    @staticmethod
    def group_enabled_key(group_id: int) -> str:
        normalized = int(group_id)
        if normalized <= 0:
            raise ValueError("group_id must be positive")
        return f"group:{normalized}:enabled"

    @staticmethod
    def group_vision_auto_describe_key(group_id: int) -> str:
        normalized = int(group_id)
        if normalized <= 0:
            raise ValueError("group_id must be positive")
        return f"group:{normalized}:vision-auto-describe"

    def _load(self) -> dict[str, str]:
        data = self._state.load()
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, str) and value.strip()
        }

    def _save(self) -> None:
        self._state.save(self._models)
