"""Public project schemas; identity and storage configuration stay host-owned."""


def _definition(name, description, properties=None, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


HANDLE = {"type": "string", "pattern": "^prj#[1-9][0-9]{0,14}$"}
TARGET = {"project_handle": HANDLE}
PROJECT_TOOLS = (
    _definition(
        "project_create",
        "在当前会话新建持久项目并设为当前项目。仅在用户要求创建项目时调用；返回 preparing 只表示唤醒已排队，不代表已完成。",
        {
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
        },
        ("title",),
    ),
    _definition(
        "project_list", "列出当前会话可见项目及当前用户选择的项目，不读取其他群或私聊。"
    ),
    _definition(
        "project_open",
        "切换当前项目；有编辑权限时排队唤醒，群内其他成员只读查看。只改变当前用户的选择。",
        TARGET,
        ("project_handle",),
    ),
    _definition(
        "project_status",
        "读取项目状态、配额类型、实测用量、任务和最近版本。省略句柄使用当前项目；未知或过期用量不能说成零。",
        TARGET,
    ),
    _definition(
        "project_versions",
        "读取项目版本历史。版本号 vN 与配置修订号 revision 不同，省略句柄使用当前项目。",
        TARGET,
    ),
    _definition(
        "project_snapshot",
        "保存项目工作区版本，可附标签；省略句柄使用当前项目。后台完成后 saved=true 才代表保存成功，排队不等于完成；用 project_status 查询。不覆盖历史，不停止未结束的任务。",
        {**TARGET, "label": {"type": "string", "maxLength": 120}},
    ),
    _definition(
        "project_restore",
        "仅在用户要求回滚时恢复指定项目版本。先保存当前文件，再重建工作区，保留历史并生成新版本；将唤醒项目。返回 restored=true 才表示已核实完成，排队不等于完成，用 project_status 查询。",
        {**TARGET, "version_handle": {"type": "string", "pattern": "^v[1-9][0-9]{0,14}$"}},
        ("version_handle",),
    ),
    _definition(
        "project_set_rebuild",
        "记录项目下次唤醒时的依赖重建命令，不立即执行。需要编辑权限和最新配置修订号；实际执行仍要求受支持的锁文件配方。",
        {
            **TARGET,
            "expected_revision": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2**53 - 1,
            },
            "command": {"type": "string", "maxLength": 4000},
        },
        ("project_handle", "expected_revision", "command"),
    ),
)
