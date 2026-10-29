"""Bounded recursive evidence to one M3 plan or an M6-compatible ZIP proposal.

This is content transformation, never delivery authority. The optional ZIP must
still enter the real ArtifactStore/source/quota/worker/M3 Type7 path. No native
forward API and no per-element message sends are attempted.
"""
from dataclasses import dataclass, field
from io import BytesIO
import zipfile

from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.message_ir import ForwardNode, MessageBody, TextNode
from .media_refs import MediaDenied


@dataclass(frozen=True)
class ForwardLimits:
    depth: int = 8
    elements: int = 128
    nodes: int = 512
    text_bytes: int = 32768
    message_bytes: int = 2000

    def __post_init__(self):
        if any(type(v) is not int or not 1 <= v <= maximum for v, maximum in (
            (self.depth, 8), (self.elements, 128), (self.nodes, 512),
            (self.text_bytes, 262144), (self.message_bytes, 20000))):
            raise MediaDenied('invalid_forward_limits')


@dataclass(frozen=True)
class ForwardExport:
    plan: object = field(repr=False)
    filename: str | None = None
    content: bytes | None = field(default=None, repr=False)
    truncated: bool = False
    # Never interpret this transformation as registration or accepted delivery.
    state: str = 'prepared_content'


def _node_text(node):
    kind = node.get('type')
    if kind == 'text':
        return node.get('text', '')
    if kind == 'emote':
        return '[' + str(node.get('name') or '表情' + str(node.get('native_id', ''))) + ']'
    if kind == 'mention':
        return '@' + str(node.get('display') or '转发中提及的人')
    if kind == 'media':
        return '[转发附件；未自动下载]'
    if kind == 'card':
        return '[转发卡片；未自动打开]'
    return '[未识别的转发内容]'


def forward_text(node, *, limits=ForwardLimits()):
    if (not isinstance(node, ForwardNode) or node.raw_data.get('source') != 'qq-official.forward'
            or node.raw_data.get('version') != 1):
        raise MediaDenied('forward_evidence_unavailable')
    roots = node.raw_data.get('elements')
    if not isinstance(roots, list):
        raise MediaDenied('forward_evidence_unavailable')
    # Every slice is bounded before expansion. Cycles cannot come from JSON but
    # are rejected defensively for in-process objects; no recursive serializer.
    stack = [(item, 0) for item in reversed(roots[:limits.elements])]
    text, used, elements, nodes, seen = [], 0, 0, 0, set()
    truncated = bool(node.raw_data.get('truncated')) or len(roots) > limits.elements

    def append(value):
        nonlocal used, truncated
        if not isinstance(value, str):
            truncated = True
            return
        # Bound characters before encoding hostile strings. NUL is never valid.
        raw = value[:limits.text_bytes].replace('\x00', '').encode('utf-8', errors='replace')
        part = raw[:limits.text_bytes - used].decode('utf-8', errors='ignore')
        text.append(part)
        used += len(part.encode())
        truncated |= len(raw) > len(part.encode()) or len(value) > limits.text_bytes

    while stack:
        item, depth = stack.pop()
        if not isinstance(item, dict) or id(item) in seen or depth >= limits.depth:
            truncated = True
            continue
        seen.add(id(item))
        if elements >= limits.elements or nodes >= limits.nodes or used >= limits.text_bytes:
            truncated = True
            break
        elements += 1
        author = item.get('author')
        display = author.get('display') if isinstance(author, dict) else None
        display = display if isinstance(display, str) and display else '未提供署名'
        append('\n' + '  ' * depth + f'{elements}. {display[:128]}（转发署名，未验证身份）\n')
        body = item.get('body')
        raw_nodes = body.get('nodes', []) if isinstance(body, dict) and body.get('v') == 1 else []
        if isinstance(raw_nodes, list):
            remaining_nodes = limits.nodes - nodes
            for raw in raw_nodes[:remaining_nodes]:
                nodes += 1
                if isinstance(raw, dict):
                    append(_node_text(raw))
            truncated |= len(raw_nodes) > remaining_nodes
        children = item.get('children')
        if isinstance(children, list):
            remaining = limits.elements - elements
            truncated |= len(children) > remaining
            stack.extend((child, depth + 1) for child in reversed(children[:remaining]))
        truncated |= item.get('truncated') is True
    if not elements:
        append('[转发结构缺失，未读取原内容]')
    # The entire output, including truncation evidence, stays within the budget.
    value = ''.join(text)
    if truncated:
        marker = '\n[转发证据超出读取预算或结构不完整，已截断]'
        size = max(0, limits.text_bytes - len(marker.encode()))
        value = value.encode()[:size].decode('utf-8', errors='ignore') + marker
        value = value.encode()[:limits.text_bytes].decode('utf-8', errors='ignore')
    return value, truncated


def render_forward(node, endpoint, *, limits=ForwardLimits(), file_available=False):
    text, truncated = forward_text(node, limits=limits)
    plan = render_plan(MessageBody((TextNode(0, text),)), endpoint, max_parts=1,
                       max_chars=20000, max_bytes=limits.message_bytes)
    if plan.status == 'ready':
        return ForwardExport(plan, truncated=truncated)
    if file_available is not True:
        marker = '\n[仅显示有限转发摘要；文件出口不可用]'
        size = max(0, limits.message_bytes // 2 - len(marker.encode()))
        text = text.encode()[:size].decode('utf-8', errors='ignore') + marker
        plan = render_plan(MessageBody((TextNode(0, text),)), endpoint, max_parts=1,
                           max_chars=20000, max_bytes=limits.message_bytes)
        return ForwardExport(plan, truncated=True)
    target = BytesIO()
    with zipfile.ZipFile(target, 'w') as archive:
        info = zipfile.ZipInfo('forward.txt', (1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, text.encode('utf-8'))
    summary = '转发内容已整理为文件内容，等待受控文件出口确认投递。'
    plan = render_plan(MessageBody((TextNode(0, summary),)), endpoint,
                       max_parts=1, max_chars=20000, max_bytes=limits.message_bytes)
    return ForwardExport(plan, 'forward.zip', target.getvalue(), truncated)
