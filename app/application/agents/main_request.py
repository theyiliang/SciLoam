"""Server-bound read-only tools and immutable, range-aware evidence ledger."""
from __future__ import annotations

import hashlib

from app.domain.errors import DomainError
from app.domain.answer_metadata import normalize_answer_metadata
from .answerability import is_space_overview
from .evidence import _Request, _call, _json
from .policies import MAX_EVIDENCE, MAX_SEARCH_LENGTH

MAX_TOOL_CALLS = 10
MAX_READ_CHARS = 24000
MAX_READ_LENGTH = 6000
MAX_LIST_DOCUMENTS = 100
MAX_OVERVIEW_READS = 3
OVERVIEW_READ_LENGTH = 800


def _version_label(value: dict) -> str:
    """Reader-facing name for a version; a raw id never reaches the user."""
    title = value.get('title')
    if not isinstance(title, str) or not title.strip():
        return '该文档'
    number = value.get('number')
    title = title.strip()[:120]
    return f'《{title}》第{number}版' if type(number) is int and number >= 1 else f'《{title}》'


class MainRequest(_Request):
    def __init__(self, *args, selected_version_id=None):
        super().__init__(*args)
        self.selected_version_id = selected_version_id
        self.discovered: set[str] = set()
        self.tool_calls = 0
        self.searched = False
        self.read_chars = 0
        self.read_ranges: dict[str, list[tuple[int, int]]] = {}
        self.document_lengths: dict[str, int] = {}
        self.version_labels: dict[str, str] = {}
        self.selected_document = None
        self.previous_run = None
        self.space_overview = None
        self._read_cache: dict[tuple[str, int, int], dict] = {}

    async def initialize(self, *, include_history=True):
        conversation = await self.conversation_check()
        for message in reversed(conversation.get('messages', [])):
            if message.get('role') != 'assistant':
                continue
            metadata = message.get('metadata') or {key: message[key] for key in (
                'answer_kind', 'answer_status', 'fallback', 'events') if key in message}
            metadata = normalize_answer_metadata(metadata)
            events = metadata.get('events', [])
            failure = next((event.get('reason') for event in reversed(events)
                            if event.get('type') == 'model.fallback'), None)
            self.previous_run = {
                'status': 'failed' if metadata.get('fallback') else 'completed',
                'reason': failure,
                'stage': ('final' if failure == 'model_final_timeout' else
                          'stream' if failure == 'model_stream_timeout' else
                          'request' if failure in {'model_first_response_timeout', 'model_request_timeout'} else 'unknown'),
                'documents_read': sum(event.get('count', 0) for event in events if event.get('type') == 'document.read'),
                'answer_saved': not bool(metadata.get('fallback')),
                'notice': '仅为上一次运行状态，不是研究证据；不代表已定位供应商故障根因。',
            }
            break
        if self.selected_version_id is not None:
            key = self.selected_version_id
            if not isinstance(key, str) or not key.strip() or len(key) > 200:
                raise DomainError(422, 'Invalid selected version id')
            value = await self.version(key)
            self.discovered.add(key)
            self.selected_document = {name: value[name] for name in ('version_id', 'document_id', 'title')}
            self.selected_document.update({name: value[name] for name in ('number',) if name in value})
        if include_history:
            await self.load_history()
            # Reuse only recent persisted source ranges, never assistant prose.
            # Every body is freshly authorized and verified in this request.
            for message in reversed(conversation.get('messages', [])[-12:]):
                if message.get('role') != 'assistant' or message.get('fallback'):
                    continue
                for citation in message.get('citations', [])[:3]:
                    if len(self.evidence) >= 3:
                        break
                    key = citation.get('version_id') if isinstance(citation, dict) else None
                    start, end = citation.get('chunk_start'), citation.get('chunk_end')
                    excerpt = citation.get('excerpt')
                    if (not isinstance(key, str) or type(start) is not int or type(end) is not int
                            or not isinstance(excerpt, str) or not 0 <= start < end
                            or end - start > MAX_READ_LENGTH):
                        continue
                    if any(item['version_id'] == key and item['chunk_start'] == start
                           and item['chunk_end'] == end for item in self.evidence.values()):
                        continue
                    value, body = await self._body(key)
                    if body[start:end] != excerpt:
                        continue
                    self.discovered.add(key)
                    self._record(value, body, start, end, 'history_revalidated')
                if self.evidence:
                    break

    async def load_space_overview(self):
        # Deterministic context for whole-space catalog questions: the real
        # authorized catalog plus short representative openings, so the model
        # answers from this library instead of generic templates. Directory
        # titles alone are not citation evidence; only read fragments are.
        if not is_space_overview(self.question):
            return
        listing = await self.list_documents()
        representative = 0
        for row in listing['documents'][:MAX_OVERVIEW_READS]:
            if len(self.evidence) >= MAX_EVIDENCE or self.read_chars >= MAX_READ_CHARS:
                break
            try:
                await self.read_document(row['version_id'], 0, OVERVIEW_READ_LENGTH)
            except ValueError:
                break
            representative += 1
        self.space_overview = {
            'documents': listing['documents'],
            'possibly_more': listing.get('possibly_more', False),
            'representative_reads': representative,
            'notice': '当前授权库的真实目录及代表片段；目录标题不是引用证据，已读片段可用其真实evidence_id引用。未读全文的文档只能按目录介绍，不冒称已读正文。',
        }

    async def _tool(self):
        await self.check()
        if self.tool_calls >= MAX_TOOL_CALLS:
            raise ValueError('Read-only tool call budget exhausted; explain scope or ask next step')
        self.tool_calls += 1

    async def list_documents(self):
        await self._tool()
        if not callable(getattr(self.service, 'list_documents', None)):
            return {'documents': [], 'unavailable': True}
        rows = await _call(self.service, 'list_documents', self.actor, self.space)
        result = []
        for row in rows:
            version = row.get('latest_version')
            if not isinstance(version, dict) or version.get('index_status', 'ready') != 'ready':
                continue
            key = version.get('id')
            if not isinstance(key, str) or not key:
                continue
            result.append({'document_id': row['id'], 'title': str(row.get('title', ''))[:500],
                           'version_id': key, **({'number': version['number']} if 'number' in version else {})})
            self.discovered.add(key)
            if len(result) >= MAX_LIST_DOCUMENTS:
                break
        await self.check()
        return {'documents': result, 'possibly_more': len(result) == MAX_LIST_DOCUMENTS,
                'scope': 'Only authorized active versions; no implicit selected document.'}

    def _record(self, value, body, start, end, strategy):
        if len(self.evidence) >= MAX_EVIDENCE:
            raise ValueError('Evidence fragment budget exhausted')
        if self.read_chars + end - start > MAX_READ_CHARS:
            raise ValueError('Cumulative body budget exhausted; explain actual read scope')
        key = f'e{len(self.evidence) + 1}'
        item = {**value, 'content': body[start:end], 'evidence_id': key,
                'chunk_start': start, 'chunk_end': end, 'read_start': start, 'read_end': end,
                'document_length': len(body), 'has_more': end < len(body),
                'retrieval_strategy': strategy,
                'remaining_body_chars': MAX_READ_CHARS - self.read_chars - (end - start)}
        self.evidence[key] = item
        self.read_chars += end - start
        self.document_lengths[value['version_id']] = len(body)
        self.version_labels[value['version_id']] = _version_label(value)
        self.read_ranges.setdefault(value['version_id'], []).append((start, end))
        return item

    async def _body(self, key):
        # version() latches authorization failures and fingerprints the complete
        # immutable record. Fetching again is authorized and must match that hash.
        value = await self.version(key)
        try:
            full = await _call(self.service, 'get_version', self.actor, key)
        except Exception as exc:
            self.authorization_error = exc
            raise
        if hashlib.sha256(_json(full).encode('utf-8')).hexdigest() != self.version_hashes[key]:
            error = DomainError(409, 'Evidence changed; ask again')
            self.authorization_error = error
            raise error
        return value, str(full.get('content', ''))

    async def read_document(self, version_id: str, start: int = 0, length: int = MAX_READ_LENGTH):
        await self._tool()
        if (not isinstance(version_id, str) or version_id not in self.discovered
                or type(start) is not int or start < 0 or type(length) is not int
                or not 1 <= length <= MAX_READ_LENGTH):
            raise ValueError('Use a discovered version and a valid character range (length <= 6000)')
        value, body = await self._body(version_id)
        if start > len(body):
            raise ValueError('Read offset is outside document')
        cache_key = (version_id, start, min(len(body), start + length))
        if cache_key in self._read_cache:
            await self.check()
            return {**self._read_cache[cache_key], 'reused': True,
                    'notice': '本次已读取的相同区间；权限已复核，无需重复获取正文。'}
        item = self._record(value, body, start, cache_key[2], 'document_read')
        self._read_cache[cache_key] = item
        item['next_start'] = item['read_end'] if item['has_more'] else None
        item['remaining_document_chars'] = len(body) - item['read_end']
        await self.check()
        await self.emit_retrieval({'type': 'document.read', 'count': 1,
                                   'max_chars': len(item['content']),
                                   'document_title': item['title'], 'version_id': item['version_id']})
        return item

    async def search(self, query: str):
        await self._tool()
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_LENGTH:
            raise ValueError('Search query must contain 1-500 characters')
        self.searched = True
        self._retrieval_context_seen = False
        rows = await self.synchronous_search(query)
        if not self._retrieval_context_seen:
            await self.emit_retrieval({'type': 'retrieval.started', 'strategy': 'legacy_lexical'})
        result = []
        for row in rows[:5]:
            key = row.get('version_id')
            if not isinstance(key, str) or not key:
                continue
            if len(self.evidence) >= MAX_EVIDENCE or self.read_chars >= MAX_READ_CHARS:
                break
            value, body = await self._body(key)
            snippet = row.get('content') or row.get('snippet')
            if not isinstance(snippet, str) or not snippet or snippet not in body:
                # A malformed retrieval hit is not a license to substitute the
                # document's opening and claim it was the retrieved passage.
                continue
            start = row.get('chunk_start')
            if type(start) is not int or start < 0 or body[start:start + len(snippet)] != snippet:
                start = body.index(snippet)
            end = min(start + len(snippet), start + MAX_READ_LENGTH,
                      start + MAX_READ_CHARS - self.read_chars)
            self.discovered.add(key)
            item = self._record(value, body, start, end, row.get('retrieval_strategy', 'legacy_lexical'))
            for name in ('chunk_id', 'rerank_applied'):
                if name in row:
                    item[name] = row[name]
            result.append(item)
        await self.check()
        if not self._retrieval_context_seen:
            await self.emit_retrieval({'type': 'retrieval.completed', 'hit_count': len(result)})
        return {'evidence': result, 'remaining_body_chars': MAX_READ_CHARS - self.read_chars,
                'notice': 'Retrieved passages are candidates, not proof of relevance or semantic correctness.'}

    async def list_tasks(self):
        await self._tool()
        return await super().list_tasks()

    async def check(self):
        if self.commit_guard is not None:
            self.commit_guard.check()
        if self.authorization_error is not None:
            raise self.authorization_error
        await self.conversation_check()
        for key in list(self.version_hashes):
            await self.version(key)
        if self.tasks is not None and await self.task_snapshot() != self.tasks:
            raise DomainError(409, 'Goal records changed; ask again')

    def read_notices(self):
        notices = []
        for key, ranges in self.read_ranges.items():
            end = 0
            covered = 0
            for start, stop in sorted(ranges):
                covered += max(0, stop - max(start, end))
                end = max(end, stop)
            total = self.document_lengths[key]
            if covered < total:
                notices.append(f'{self.version_labels.get(key, "该文档")} 正文共{total}字符，本次实际读取{covered}字符，范围'
                               + '、'.join(f'[{start},{stop})' for start, stop in ranges)
                               + '；未读部分不纳入结论，不能视为全文总结。')
        return notices
