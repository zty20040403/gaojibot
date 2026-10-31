from __future__ import annotations


from typing import Any, Union


ToolDefinition = dict[str, Any]
ToolChoice = Union[str, dict[str, Any]]

WEB_SEARCH_TOOL_NAME = "web_search"
WEB_FETCH_TOOL_NAME = "web_fetch"
READ_IMAGE_TEXT_TOOL_NAME = "read_image_text"
VIEW_IMAGE_TOOL_NAME = "view_image"
FIND_STICKERS_TOOL_NAME = "find_stickers"
TRANSCRIBE_VOICE_TOOL_NAME = "transcribe_voice"
SEND_STICKER_TOOL_NAME = "send_sticker"
SANDBOX_CREATE_TOOL_NAME = "sandbox_create"
SANDBOX_DESTROY_TOOL_NAME = "sandbox_destroy"
SANDBOX_LIST_TOOL_NAME = "sandbox_list"
SANDBOX_EXEC_TOOL_NAME = "sandbox_exec"
NIX_SEARCH_TOOL_NAME = "nix_search"
SANDBOX_WRITE_FILE_TOOL_NAME = "sandbox_write_file"
SANDBOX_READ_FILE_TOOL_NAME = "sandbox_read_file"
JOB_STATUS_TOOL_NAME = "job_status"
JOB_CANCEL_TOOL_NAME = "job_cancel"
SEND_FILE_FROM_SANDBOX_TOOL_NAME = "send_file_from_sandbox"
SEND_IMAGE_FROM_SANDBOX_TOOL_NAME = "send_image_from_sandbox"
LIST_RECENT_FILES_TOOL_NAME = "list_recent_files"
IMPORT_FILE_TO_SANDBOX_TOOL_NAME = "import_file_to_sandbox"
SAY_TOOL_NAME = "say"
DELEGATE_AGENT_TOOL_NAME = "delegate_agent"
RUN_SUBAGENTS_TOOL_NAME = "run_subagents"
RESUME_SUBAGENT_TOOL_NAME = "resume_subagent"
MEMORY_ADD_TOOL_NAME = "memory_add"
MEMORY_LIST_TOOL_NAME = "memory_list"
MEMORY_REMOVE_TOOL_NAME = "memory_remove"
USE_SKILL_TOOL_NAME = "use_skill"
INSPECT_SOURCE_TOOL_NAME = "inspect_source"
VIEW_FORWARD_TOOL_NAME = "view_forward"


WEB_SEARCH_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": WEB_SEARCH_TOOL_NAME,
        "description": (
            "搜索互联网。遇到最新消息、新闻、价格、天气、版本、官网、"
            "用户明确要求联网核实时调用；普通常识且无需更新时不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "适合搜索引擎的简洁关键词，保留必要的人名、日期和限定词。",
                },
                "freshness": {
                    "type": "string",
                    "enum": ["auto", "day", "week", "month", "year"],
                    "description": (
                        "时间范围，默认 auto 即不限。只有天气、比分、当天新闻用 day，"
                        "近期新闻用 week；价格、型号、资料、教程不要限定时间，"
                        "结果的日期自己判断。"
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

WEB_FETCH_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": WEB_FETCH_TOOL_NAME,
        "description": (
            "打开一个公网网页并读取正文文字。搜索摘要不够用、需要核对价格、参数、"
            "日期或原文时调用；只读，不执行网页脚本，靠脚本加载的内容可能读不到。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整的 http/https 网址，通常来自搜索结果。"},
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "从正文第几个字符开始读；正文太长时用上次返回的 next_offset 继续。",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}


READ_IMAGE_TEXT_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": READ_IMAGE_TEXT_TOOL_NAME,
        "description": (
            "读取用户本轮图片、回复的图片，或该用户最近发送图片中的文字。"
            "用户要求看图、识图、读取截图、总结截图内容时调用。"
            "它只能读取文字，不能理解没有文字的纯画面。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

VIEW_IMAGE_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": VIEW_IMAGE_TOOL_NAME,
        "description": (
            "通过一次性 Luna 识图任务理解当前消息或指定消息的完整图片画面。"
            "用户要求看图、分析截图、解释表情包或询问图片内容时调用。"
            "普通图片不会存入媒体库；每次 detail 都会根据图片地址重新识别。"
            "未提供 message_handle 时查看当前、回复或该用户最近发送的图片。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_handle": {
                    "type": "string",
                    "pattern": "^msg#[1-9][0-9]*$",
                },
                "segment_index": {
                    "type": "integer",
                    "minimum": 0,
                },
                "mode": {
                    "type": "string",
                    "enum": ["summary", "detail"],
                    "description": "summary 生成简短介绍；detail 重新仔细识别细节。",
                },
                "question": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": "需要视觉模型重点检查的问题。",
                },
            },
            "additionalProperties": False,
        },
    },
}


FIND_STICKERS_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": FIND_STICKERS_TOOL_NAME,
        "description": (
            "只浏览机器人从所有群收集的全局安全表情包，返回候选及 media# 句柄。"
            "用户要求直接发一个表情包时不要调用它，直接调用 send_sticker 并传入检索意图。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

TRANSCRIBE_VOICE_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": TRANSCRIBE_VOICE_TOOL_NAME,
        "description": (
            "把用户当前、回复或最近发送的 QQ 语音转成文字。"
            "用户要求听语音、语音识别、转文字或根据语音内容回答时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


SEND_STICKER_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": SEND_STICKER_TOOL_NAME,
        "description": (
            "从机器人在所有群收集的全局安全表情包中搜索匹配候选，并兼顾相关度、"
            "近期是否发送过和历史使用次数选择一张发送，避免总是重复同一张。"
            "用户要求发图、发表情包或用表情回应时直接调用；通常传 query，query "
            "只写用户明确要求的核心对象或情绪标签，例如‘哆啦A梦’‘Orz’‘橘猫’，"
            "不要擅自补充‘可爱’‘搞笑’等泛化标签。不需要先调用 find_stickers。"
            "只有本轮 find_stickers 返回的 media# 句柄才可精确发送，不能复用旧句柄。"
            "用户说‘换个’或‘再来一个’时，query 要继承上一张的主题；系统会强制排除"
            "刚发过的图片，没有其他匹配候选就明确告诉用户。"
            "普通的随机表情请求可省略 query；指定内容没有匹配时会明确失败。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": "想表达的画面、对象、情绪或用途，例如‘猫娘卖萌’‘震惊’‘无语’。",
                },
                "media_handle": {
                    "type": "string",
                    "pattern": "^media#[1-9][0-9]*$",
                    "description": "可选；仅限本轮 find_stickers 返回的全局 media# 句柄。",
                }
            },
            "additionalProperties": False,
        },
    },
}


SANDBOX_CREATE_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": SANDBOX_CREATE_TOOL_NAME,
        "description": (
            "创建隔离的 Nix 高级 Docker 工作站沙盒。已预装常用 shell 工具、"
            "Git、Python、Node、GCC、SQLite 和可靠的中文 PDF 工具；"
            "Go、Rust、Java、LibreOffice、ffmpeg、OCR、科学计算等大工具通过 "
            "sandbox_exec.packages 按需加入。需要写代码、处理文件、"
            "构建或测试项目时先调用。"
            "工作目录固定为 /workspace，在已开放的额度内直接创建，不需要手机确认。"
            "成功任务确认交付后工作区与本地副本最多保留一小时；未回收前可自动恢复。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "runtime": {
                    "type": "string",
                    "enum": ["python", "node", "debian"],
                    "description": "项目所需运行环境。",
                }
            },
            "required": ["runtime"],
            "additionalProperties": False,
        },
    },
}

SANDBOX_DESTROY_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": SANDBOX_DESTROY_TOOL_NAME,
        "description": (
            "直接删除有权访问的指定沙盒和其中全部文件，不需要手机口令或二次确认。"
            "可用于用户要求的删除或重建，包括未交付文件；删除不可撤销。"
            "先用 sandbox_list 确认目标 ID，不得猜测或跨群访问。"
            "正常任务收尾交给宿主自动回收，不要在下游读取或文件交付前自行销毁。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"sandbox_id": {"type": "string", "pattern": "^s[0-9a-f]{6}$"}},
            "required": ["sandbox_id"], "additionalProperties": False,
        },
    },
}

SANDBOX_LIST_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": SANDBOX_LIST_TOOL_NAME,
        "description": "列出当前用户拥有的 Docker 沙盒及状态。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

SANDBOX_EXEC_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": SANDBOX_EXEC_TOOL_NAME,
        "description": (
            "在指定沙盒的 /workspace 中执行 shell 命令，适合构建、测试、运行程序"
            "和打包文件。缺少工具时不要 apt/pip 全局安装：先用 nix_search 找到"
            "属性名，再放进 packages；首次下载后会被所有 gaoji 沙盒缓存。"
            "中文 PDF 使用 gaoji-pdf，并用 pdffonts、pdftotext 验收。"
            "不要用于读写宿主机。"
            "预计超过一次对话等待时间时，"
            "设置 background=true 交给可恢复的持久任务队列。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "command": {"type": "string", "description": "要执行的 shell 命令。"},
                "packages": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 128},
                    "maxItems": 16,
                    "description": (
                        "仅对本次命令生效的 nixpkgs 属性，如 ffmpeg、go、"
                        "rustc、python3Packages.pandas。"
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                },
                "background": {
                    "type": "boolean",
                    "description": "是否交给重启可恢复的后台任务执行。",
                },
            },
            "required": ["sandbox_id", "command"],
            "additionalProperties": False,
        },
    },
}

NIX_SEARCH_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": NIX_SEARCH_TOOL_NAME,
        "description": (
            "搜索高级沙盒锁定的 nixpkgs 软件包。缺少命令或 Python 库时先调用，"
            "把返回的 attribute 原样传给 sandbox_exec.packages。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 80,
                    "description": "软件名称或关键词，如 ffmpeg、opencv、pandoc。",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

JOB_STATUS_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": JOB_STATUS_TOOL_NAME,
        "description": "查看当前会话中持久后台任务的状态和结果。",
        "parameters": {
            "type": "object",
            "properties": {
                "job_handle": {
                    "type": "string",
                    "pattern": r"^job#[1-9][0-9]*$",
                }
            },
            "required": ["job_handle"],
            "additionalProperties": False,
        },
    },
}

JOB_CANCEL_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": JOB_CANCEL_TOOL_NAME,
        "description": (
            "取消当前会话的持久后台任务。这是危险操作，只有用户当前消息明确要求"
            "取消对应任务时宿主才会批准。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_handle": {
                    "type": "string",
                    "pattern": r"^job#[1-9][0-9]*$",
                }
            },
            "required": ["job_handle"],
            "additionalProperties": False,
        },
    },
}

SANDBOX_WRITE_FILE_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": SANDBOX_WRITE_FILE_TOOL_NAME,
        "description": "向沙盒 /workspace 下写入 UTF-8 文本文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "path": {
                    "type": "string",
                    "description": "相对于 /workspace 的路径，例如 src/main.py。",
                },
                "content": {"type": "string", "description": "完整文件内容。"},
            },
            "required": ["sandbox_id", "path", "content"],
            "additionalProperties": False,
        },
    },
}

SANDBOX_READ_FILE_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": SANDBOX_READ_FILE_TOOL_NAME,
        "description": "读取沙盒 /workspace 下的 UTF-8 文本文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["sandbox_id", "path"],
            "additionalProperties": False,
        },
    },
}

SEND_FILE_FROM_SANDBOX_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": SEND_FILE_FROM_SANDBOX_TOOL_NAME,
        "description": (
            "把沙盒中的构建产物、源码压缩包或文档作为 QQ 群文件发送。"
            "发送前应先完成构建或打包。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "path": {"type": "string"},
                "filename": {
                    "type": "string",
                    "description": "群里显示的文件名；留空则使用路径文件名。",
                },
            },
            "required": ["sandbox_id", "path"],
            "additionalProperties": False,
        },
    },
}

SEND_IMAGE_FROM_SANDBOX_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": SEND_IMAGE_FROM_SANDBOX_TOOL_NAME,
        "description": "把沙盒中的 PNG/JPG/GIF/WebP 图片直接发送到当前 QQ 群。",
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["sandbox_id", "path"],
            "additionalProperties": False,
        },
    },
}

LIST_RECENT_FILES_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": LIST_RECENT_FILES_TOOL_NAME,
        "description": "列出当前 QQ 群最近上传的文件及规范 groupfile# 句柄。",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                }
            },
            "required": ["limit"],
            "additionalProperties": False,
        },
    },
}

IMPORT_FILE_TO_SANDBOX_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": IMPORT_FILE_TO_SANDBOX_TOOL_NAME,
        "description": (
            "把当前群文件或被回复消息中的附件下载并导入指定沙盒。"
            "处理用户所说的“这个文件”时传入被回复消息的完整 msg# 句柄；"
            "如果该消息只有一个附件，可以省略 attachment_handle。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "file_handle": {
                    "type": "string",
                    "pattern": "^groupfile#[0-9a-f]{20}$",
                    "description": "list_recent_files 返回的完整 groupfile# 句柄。",
                },
                "attachment_handle": {
                    "type": "string",
                    "pattern": "^file#[1-9][0-9]*\\.[0-9]+$",
                    "description": "消息工具返回的完整 file#消息.段序号句柄。",
                },
                "message_handle": {
                    "type": "string",
                    "pattern": "^msg#[1-9][0-9]*$",
                    "description": "包含目标附件的完整规范句柄，例如 msg#42。",
                },
                "destination": {
                    "type": "string",
                    "description": "沙盒中的目标相对路径，例如 input/data.zip。",
                },
            },
            "required": ["sandbox_id", "destination"],
            "additionalProperties": False,
        },
    },
}

SAY_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": SAY_TOOL_NAME,
        "description": (
            "长任务中向当前群发送一条简短进度通知。"
            "在下载、安装、编写、构建、测试和打包等关键阶段主动调用，"
            "有新的实际进展时可以多次使用。识图、搜索、普通问答等短任务"
            "不要调用，也不要发送‘正在处理’之类没有实际结果的占位消息。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "不超过 200 字的自然进度消息。",
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}

RUN_SUBAGENTS_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": RUN_SUBAGENTS_TOOL_NAME,
        "description": (
            "把当前复杂目标升级为固定角色的 Sub-Agent 任务。任务涉及多个专业领域、"
            "需要分阶段搜索/读媒体/写代码/处理文件/分析/运维，或预计需要较长时间时"
            "调用；主控会自动拆分依赖、并行执行、汇报每个 Agent 的进度并验收结果。"
            "简单问答、单次搜索、单张识图、普通闲聊不要调用。用户不需要先输入 /task。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "maxLength": 6000,
                    "description": "保留用户约束、交付物和验收要求的完整任务目标。",
                }
            },
            "required": ["goal"],
            "additionalProperties": False,
        },
    },
}

DELEGATE_AGENT_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": DELEGATE_AGENT_TOOL_NAME,
        "description": (
            "把一个边界明确的专业子任务交给独立 Sub-Agent，并把结构化结果返回给"
            "当前主 Agent。适用于一次资料核实、代码处理、文件读取、媒体分析、数据"
            "分析或只读运维检查；它不会接管 QQ 对话。需要多个角色和依赖步骤时改用"
            "run_subagents，简单工具调用不需要委派。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": [
                        "researcher",
                        "coder",
                        "document",
                        "media",
                        "analyst",
                        "operator",
                    ],
                    "description": "最适合该子任务的专业角色。",
                },
                "objective": {
                    "type": "string",
                    "maxLength": 4000,
                    "description": "边界清楚、可独立验收的子任务目标。",
                },
            },
            "required": ["role", "objective"],
            "additionalProperties": False,
        },
    },
}

RESUME_SUBAGENT_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": RESUME_SUBAGENT_TOOL_NAME,
        "description": (
            "从持久检查点继续一个因机器人重启而中断的 Sub-Agent 任务。"
            "已完成步骤不会重跑，每个 Agent 复用原来的独立上下文。仅当用户明确"
            "要求继续 task#编号，或当前对话明确指向一个 interrupted 任务时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "要继续的持久任务编号。",
                }
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
}

MEMORY_ADD_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": MEMORY_ADD_TOOL_NAME,
        "description": (
            "保存一条以后仍然有用的长期记忆。仅当用户明确要求记住，或用户清楚表达了"
            "稳定偏好、身份事实、长期项目约定时调用；不要保存临时聊天、推测、密码、"
            "API Key、验证码或其他秘密。默认保存为当前用户记忆；只有对整个群都适用的"
            "共同约定才使用 group。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["user", "group"],
                    "description": "记忆范围：当前用户或当前群。",
                },
                "content": {
                    "type": "string",
                    "description": "独立、简洁、以后可直接理解的一条事实或偏好。",
                },
            },
            "required": ["scope", "content"],
            "additionalProperties": False,
        },
    },
}

MEMORY_LIST_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": MEMORY_LIST_TOOL_NAME,
        "description": "查看当前用户或当前群已保存的长期记忆及其 ID。",
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["user", "group", "all"],
                    "description": "要查看的记忆范围。",
                }
            },
            "required": ["scope"],
            "additionalProperties": False,
        },
    },
}

MEMORY_REMOVE_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": MEMORY_REMOVE_TOOL_NAME,
        "description": (
            "按 ID 删除当前用户或当前群可见的一条长期记忆。"
            "仅在用户明确要求忘记或更正该记忆时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "memory_list 返回的记忆 ID。",
                }
            },
            "required": ["memory_id"],
            "additionalProperties": False,
        },
    },
}


USE_SKILL_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": USE_SKILL_TOOL_NAME,
        "description": "读取技能目录中某项工作的完整宿主流程，开始对应任务前调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 80,
                    "description": "技能目录中的名称，例如 sandbox。",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
}

INSPECT_SOURCE_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": INSPECT_SOURCE_TOOL_NAME,
        "description": (
            "只读检查机器人随仓库发布的白名单源码。回答自身实现、架构、"
            "命令或默认配置问题时用于取证；不能读取 .env、状态数据或宿主机任意路径。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "search", "read", "identity"],
                },
                "path": {"type": "string", "maxLength": 300},
                "query": {"type": "string", "maxLength": 200},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


VIEW_FORWARD_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": VIEW_FORWARD_TOOL_NAME,
        "description": (
            "展开当前群里一条合并转发消息。传上下文中的 msg# 规范句柄，"
            "返回子消息的发送者、时间和正文；嵌套转发可以继续展开。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_handle": {
                    "type": "string",
                    "pattern": "^msg#[1-9][0-9]*$",
                }
            },
            "required": ["message_handle"],
            "additionalProperties": False,
        },
    },
}


MEMORY_TOOLS = [MEMORY_ADD_TOOL, MEMORY_LIST_TOOL, MEMORY_REMOVE_TOOL]

SANDBOX_TOOLS = [
    SANDBOX_CREATE_TOOL,
    SANDBOX_DESTROY_TOOL,
    SANDBOX_LIST_TOOL,
    SANDBOX_EXEC_TOOL,
    NIX_SEARCH_TOOL,
    SANDBOX_WRITE_FILE_TOOL,
    SANDBOX_READ_FILE_TOOL,
    SEND_FILE_FROM_SANDBOX_TOOL,
    SEND_IMAGE_FROM_SANDBOX_TOOL,
    LIST_RECENT_FILES_TOOL,
    IMPORT_FILE_TO_SANDBOX_TOOL,
    JOB_STATUS_TOOL,
    JOB_CANCEL_TOOL,
]


HISTORY_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "history",
        "description": (
            "查本会话的聊天记录（比上下文里更早的也有）。给 query 就按内容搜，空格隔开多个词，"
            "命中任一个就算；不给 query 就按时间读原文。都能用 who（发言人名字里的几个字）和 "
            "from/to（YYYY-MM-DD、YYYY-MM-DD HH:MM 或 #消息编号）筛选。around=#编号 读那条前后各 10 条。"
            "读原文时有 from 就从区间开头往后读，没有就读最新的；返回 next 时原样传回能接着读。"
            "例：最早的聊天 {from: \"2020-01-01\"}；某人前天说的 {who: \"小明\", from: \"2026-10-29\", to: \"2026-10-29\"}。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 200},
                "who": {"type": "string", "maxLength": 50},
                "from": {"type": "string", "maxLength": 40},
                "to": {"type": "string", "maxLength": 40},
                "around": {"type": "string", "maxLength": 24},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
}


TOOL_DEFINITIONS = (
    WEB_SEARCH_TOOL,
    WEB_FETCH_TOOL,
    READ_IMAGE_TEXT_TOOL, VIEW_IMAGE_TOOL, FIND_STICKERS_TOOL,
    TRANSCRIBE_VOICE_TOOL, SEND_STICKER_TOOL,
    *MEMORY_TOOLS,
    *SANDBOX_TOOLS,
    HISTORY_TOOL,
    USE_SKILL_TOOL, INSPECT_SOURCE_TOOL,
    DELEGATE_AGENT_TOOL, RUN_SUBAGENTS_TOOL, RESUME_SUBAGENT_TOOL,
)


def force_tool(name: str) -> ToolChoice:
    return {
        "type": "function",
        "function": {"name": name},
    }
