"""Local topic matching and fixed quotations; RSS text never enters a model/tool."""
import json
import re
import unicodedata
from urllib.parse import urlsplit

from src.plugins.ai_chat.conversation_config import snapshot, proactive_attention, in_quiet_utc
from src.plugins.ai_chat.global_interest import read_candidates
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from .migration_types import MigrationDenied
from .proactive_ingress import wake_available
from .renderer import render_plan

HOSTS = {'nasa-technology': 'www.nasa.gov', 'loc-timeless': 'blogs.loc.gov'}
TOPICS = {'太空': ('太空', 'space'), '月球': ('月球', 'moon'), '火星': ('火星', 'mars'),
          '机器人': ('机器人', 'robot'), '音乐': ('音乐', 'music'), '书籍': ('书籍', 'book'),
          '电影': ('电影', 'film'), '地图': ('地图', 'map')}
FIELDS = ('source', 'title', 'url', 'tags', 'published_at', 'expires_at')


def mentions(text, topic):
    return any(re.search(r'\b' + word + r's?\b', text, re.I) if word.isascii() else word in text
               for word in TOPICS[topic])


def eligible(item, text, now):
    """Reject ambiguous broad tags and link/markup-bearing titles at this boundary."""
    if (any(mark in text for mark in ('@', '://', '`', '\n'))
            or re.search(r'别|不要|不想|不聊|不谈|不讨论|闭嘴|\b(?:stop|no|not|never)\b|don[’\']t', text, re.I)):
        return False
    if not isinstance(item, dict) or any(key not in item for key in FIELDS):return False
    title, url, tags = item['title'], item['url'], item['tags']
    if (not isinstance(title, str) or not 1<=len(title)<=240 or not isinstance(url, str)
            or not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags)
            or any(ord(char)<=32 or ord(char)==127 for char in url)
            or type(item['published_at']) is not int or type(item['expires_at']) is not int
            or not 0<=item['published_at']<=now<item['expires_at']
            or not isinstance(item['source'], str) or item['source'] not in HOSTS
            or any(unicodedata.category(char).startswith('C') or char in '.。:/@<>[]`\\『』' for char in unicodedata.normalize('NFKC', title))):
        return False
    try:parsed = urlsplit(url)
    except ValueError:return False
    if (len(url)>2048 or parsed.scheme!='https' or parsed.netloc!=HOSTS[item['source']]
            or parsed.query or parsed.fragment or not re.fullmatch(r'/[A-Za-z0-9_/-]+', parsed.path)):
        return False
    return len(quotation(item))<=2000 and any(topic in tags and mentions(text, topic) and mentions(title, topic) for topic in TOPICS)


def quotation(item):
    return f"刚聊到这个话题，看到一条相关资讯。来源标题：『{item['title']}』\n来源：{item['source']}\n{item['url']}"


class InterestSharing:
    def __init__(self, proactive, *, state_dir, wake, reader=read_candidates):
        self.p, self.state_dir, self.wake, self.reader = proactive, state_dir, wake, reader

    def candidates(self, now):
        return self.reader(self.state_dir, now=now)

    def allowed(self, c, inbox, ep, now, *, queued=False):
        physical = c.execute('SELECT interest_after_lull FROM qq_official_endpoints WHERE endpoint_key=?', (ep.key,)).fetchone()
        limit, quiet = proactive_attention(c, ep)
        return (snapshot(c, ep)['interest_sharing_enabled'] and limit and not in_quiet_utc(quiet, now)
            and physical['interest_after_lull']==inbox['canonical_message_id']
            and (queued or wake_available(c, ep, now))
            and not c.execute('''SELECT 1 FROM messages WHERE source_endpoint_key=? AND direction='outbound'
                AND occurred_at>? LIMIT 1''', (ep.key, inbox['received_at']-600)).fetchone())

    def reserve(self, c, inbox, ep, now):
        day = (now+28800)//86400
        if not self.allowed(c, inbox, ep, now) or c.execute('''SELECT 1 FROM qq_official_proactive_observations
            WHERE endpoint_key=? AND interest_day=?''', (ep.key, day)).fetchone():return None,None
        candidate = next(({key: item[key] for key in FIELDS} for item in self.candidates(now)
                          if eligible(item, inbox['turn_text'], now)), None)
        # Freeze the selection on INSERT under the existing immutable source guard.
        # A crash or failed delivery consumes the day rather than replaying it.
        return (day,json.dumps(candidate, ensure_ascii=False)) if candidate is not None else (None,None)

    def share(self, source):
        with self.p.tx() as c:
            row, (inbox, ep, actor, message, _) = self.p._candidate(c, source)
            if row['interest_day'] is None:return False
            candidate = json.loads(row['interest_candidate_json'])
            expires = min(row['deadline_at'], candidate['expires_at'], (row['interest_day']+1)*86400-28800)
            ref = self.p.outbox.enqueue(self.p.token,
                rendered=render_plan(MessageBody((TextNode(0, quotation(candidate)),)), ep, markdown=False, max_parts=1),
                requester=actor, logical_key=f"group-interest:{ep.key}:{row['interest_day']}", source_scope_key=message['scope_key'],
                source_native_message_id=inbox['native_message_id'], source_canonical_message_id=message['canonical_message_id'],
                expires_at=expires, purpose='unsolicited', source_proof=source, transaction_cursor=c)
            c.execute("UPDATE qq_official_proactive_observations SET state='queued',plan_id=? WHERE inbox_id=?",
                      (ref.plan_id, source.inbox_id))
        return True

    def guard(self, c, row, *, loaded, plan):
        inbox, ep, _, _ = loaded
        now = self.p._now()
        candidate = json.loads(row['interest_candidate_json'])
        # The queued row counts toward the existing hourly quota; admission already
        # reserved that slot. Policy changes fence it through proactive_generation.
        if (not self.allowed(c, inbox, ep, now, queued=row['state']=='queued')
                or row['interest_day']!=(now+28800)//86400
                or not eligible(candidate, inbox['turn_text'], now)
                or candidate not in [{key: item.get(key) for key in FIELDS} for item in self.candidates(now)]
                or not now<plan['expires_at']<=min(row['deadline_at'], candidate['expires_at'])
                or json.loads(plan['manifest_json'])['rendered']!=render_plan(
                    MessageBody((TextNode(0, quotation(candidate)),)), ep, markdown=False, max_parts=1).to_json()):
            raise MigrationDenied('interest_source_expired_or_changed')
