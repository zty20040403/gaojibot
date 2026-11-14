"""Message-only vector candidates: cache ranks, canonical history supplies evidence."""
from .ledger import MessageLedger
from .message_vectors import REVISION
from .semantic_recall import _validate_vector,_vector_literal
from .timeline import is_noise


def message_hits(cursor,scope,vector,*,model,identity,after_id=0,before_id=None,who='',limit=50):
    _validate_vector(vector,1024)
    if not any(vector):raise ValueError('semantic_query_vector_zero')
    clauses = ["c.scope_key=?","d.source_type='message'","d.embedding_model=?",
        "d.metadata_json->>'embedding_identity'=?",f"d.metadata_json->>'source_revision'={REVISION}",
        "m.canonical_message_id>=v.min_canonical_message_id","m.canonical_message_id>?",
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
        JOIN conversation_visibility v ON v.conversation_id=m.conversation_id
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
