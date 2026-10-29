"""Shared group wiki presentation, with bounded output and no permission inference."""

def format_page(page, query=''):
    items=page['items'][:10]
    if not items:
        return '暂时没有匹配的百科条目。' if query else '群百科尚无可展示条目，等待夜间整理。'
    labels={'decision':'决定','faq':'问答','jargon':'术语','expertise':'擅长领域'}
    lines=[f"群百科 · v{page['revision']}"]
    for item in items:
        refs=', '.join(f'msg#{value}' for value in item['evidence_ids'])
        line=f"[{labels[item['category']]}] {item['title']}\n{item['content']}\n依据：{refs}"
        if sum(len(value) for value in lines)+len(line)>5000: break
        lines.append(line)
    displayed=len(lines)-1
    if len(page['items'])>displayed:
        lines.append(f"共 {len(page['items'])} 条，显示前 {displayed} 条；可用 /群百科 关键词 筛选。")
    return '\n\n'.join(lines)
