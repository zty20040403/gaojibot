"""Message-only vector candidates: cache ranks, canonical history supplies evidence."""
from .billing_context import active_billing
from .ledger import MessageLedger
from .message_vectors import REVISION,embedding_identity,valid_history_scope
from .semantic_recall import _validate_vector,_vector_literal
from .timeline import is_noise


def message_hits(cursor,scope,vector,*,model,identity,after_id=0,before_id=None,who='',limit=50):
    _validate_vector(vector,1024)
    if not any(vector):raise ValueError('semantic_query_vector_zero')
    clauses = ["c.scope_key=?","d.source_type='message'","d.embedding_model=?",
        "d.metadata_json->>'embedding_identity'=?",f"d.metadata_json->>'source_revision'={REVISION}",
        "m.canonical_message_id>=c.min_canonical_message_id","m.canonical_message_id>?",
        "m.message_kind='chat'","m.direction IN ('inbound','outbound')"]
    args = [scope.key,model,identity,max(int(after_id),0)]
    if before_id is not None:
        clauses.append('m.canonical_message_id<?'); args.append(int(before_id))
    if who:
        clauses.append("lower(m.sender_display) LIKE ? ESCAPE '\\'")
        args.append('%'+who.lower().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
    args.extend((_vector_literal(vector),min(max(int(limit),1),90)))
    rows = cursor.execute(f'''SELECT m.*,c.scope_key FROM semantic_documents d
        JOIN conversations c ON c.scope_key=d.scope_key
        JOIN messages_with_captions m ON m.conversation_id=c.conversation_id
          AND d.source_handle='msg#' || m.canonical_message_id::text
        WHERE {' AND '.join(clauses)}
        ORDER BY d.embedding <=> ?::vector,m.canonical_message_id DESC LIMIT ?''',args).fetchall()
    return [message for row in rows if not is_noise(message:=MessageLedger._row_to_message(row))]


def merge_hits(keyword,semantic,*,limit):
    """Equal-weight reciprocal ranks; a jointly found message remains one result."""
    scores,messages = {},{}
    for candidates in (keyword,semantic):
        seen = set()
        for rank,message in enumerate(candidates,1):
            key = message.canonical_message_id
            if key in seen:continue
            seen.add(key); messages[key] = message
            scores[key] = scores.get(key,0)+1/(60+rank)
    return [messages[key] for key in sorted(scores,key=lambda key:(-scores[key],-key))[:limit]]


async def keyword_hits(app,scope,*,terms,limit,**filters):
    rows = await app.db_call(app.message_ledger.history,scope,terms=terms,limit=limit*3,newest=True,**filters)
    terms = tuple(term.casefold() for term in terms)
    ranked = [(sum(term in item.prompt_text.casefold() for term in terms),item) for item in rows if not is_noise(item)]
    return [item for score,item in sorted(ranked,key=lambda pair:(-pair[0],-pair[1].canonical_message_id)) if score]


async def search_messages(app,source,query,*,terms,who,after_id,before_id,limit):
    from .adapters.qq_official.vision_adapter import guarded_wait
    billing,service = active_billing(),getattr(app,'semantic_recall',None)
    if (billing is None or not callable(billing.current) or billing.store is not app.model_billing
        or billing.source.endpoint!=source.endpoint or billing.source.actor!=source.actor):
        raise PermissionError('semantic_source_unavailable')
    if service is None or service.embedder.dimensions!=1024 or service.backend.dimensions!=1024 or service.backend.schema!=billing.store.database.schema:
        raise RuntimeError('semantic_provider_unavailable')
    def check_alias():
        with billing.store.transaction() as cursor:
            row = cursor.execute('''SELECT 1 FROM qq_official_endpoints WHERE account_key=? AND endpoint_key=?
                AND binding_state='active' AND membership_state='present' AND canonical_scope_key=? AND route_epoch=?''',
                (source.endpoint.account.key,source.endpoint.key,source.scope.key,source.route_epoch)).fetchone()
            if not row or not valid_history_scope(source.scope,source.endpoint):raise PermissionError('semantic_route_changed')
    async def current():
        await billing.current()
        if source.scope != source.endpoint.scope:await app.db_call(check_alias)
    identity = embedding_identity(service.embedder)
    await current()
    vector = (await guarded_wait(service.embedder.embed([query]),current,
        timeout=service.embedder.timeout_seconds+5))[0]
    if identity!=embedding_identity(service.embedder):raise PermissionError('semantic_space_changed')
    def candidates():
        with billing.store.transaction() as cursor:
            return message_hits(cursor,source.scope,vector,model=service.embedder.model,identity=identity,
                after_id=after_id,before_id=before_id,who=who,limit=min(limit*3,90))
    semantic = await app.db_call(candidates)
    keyword = await keyword_hits(app,source.scope,terms=terms,who=who,after_id=after_id,before_id=before_id,limit=limit)
    merged = merge_hits(keyword,semantic,limit=limit)
    # Rehydrate after both candidate reads: clear/content changes must not leak
    # old cache or a stale projection merely because it was ranked earlier.
    fresh = await app.db_call(app.message_ledger.visible_messages_by_ids,source.scope,
        [item.canonical_message_id for item in merged])
    by_id = {item.canonical_message_id:item for item in fresh if item.message_kind=='chat' and item.direction in {'inbound','outbound'} and not is_noise(item)}
    result = [by_id[item.canonical_message_id] for item in merged if item.canonical_message_id in by_id
        and by_id[item.canonical_message_id].prompt_text==item.prompt_text
        and (not who or who.casefold() in by_id[item.canonical_message_id].sender_display.casefold())]
    await current()
    if identity!=embedding_identity(service.embedder):raise PermissionError('semantic_space_changed')
    return result
