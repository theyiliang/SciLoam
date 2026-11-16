"""Synchronous, fail-closed metering for the approved synthetic embedding experiment."""
import json
from decimal import Decimal
from pathlib import Path
import time

import httpx


# Cumulative embedding batch cap, kept separate from the overall 50 CNY project
# budget. Raised from 5 to 10 because a historical single-call batch reserved
# 4.98 CNY conservatively while actually spending 0.0012 CNY, which exhausted
# the original cap and blocked every later batch.
BATCH_CAP_CNY = Decimal('10')


class EmbeddingMeter(httpx.BaseTransport):
    def __init__(self, output: Path, *, prior_reserved, inner=None):
        self.output = output
        self.prior = Decimal(str(prior_reserved))
        if not self.prior.is_finite() or not 0 <= self.prior < BATCH_CAP_CNY:
            raise ValueError(f'Prior reservations must be within the {BATCH_CAP_CNY} CNY batch cap')
        self.reserved = self.prior
        self.known = Decimal('0')
        self.stopped = False
        self.calls = []
        self.inner = inner or httpx.HTTPTransport(retries=0)
        self.write()

    def write(self):
        value = {'limit_cny': str(BATCH_CAP_CNY), 'prior_reserved_cny': str(self.prior),
                 'reserved_worst_case_cny': str(self.reserved), 'stopped': self.stopped,
                 'known_usage_subtotal_estimate_cny': str(self.known), 'settled_bill_cny': None,
                 'price_cny_per_million_input_tokens': '0.5',
                 'reserve_price_cny_per_million_input_tokens': '1',
                 'source': 'https://help.aliyun.com/zh/model-studio/model-pricing',
                 'note': 'Public list price estimate, no discount/free quota assumed; not an invoice.',
                 'calls': self.calls}
        (self.output / 'embedding_usage.json').write_text(json.dumps(value, indent=2), encoding='utf-8')

    def handle_request(self, request):
        if self.stopped:
            raise RuntimeError('Embedding metering stopped')
        if (request.method != 'POST' or request.url.scheme != 'https'
                or request.url.host != 'dashscope.aliyuncs.com'
                or request.url.path != '/compatible-mode/v1/embeddings' or request.url.query):
            raise ValueError('Unapproved embedding route')
        body = json.loads(request.content)
        texts = body.get('input')
        if (body.get('model') != 'text-embedding-v4' or not isinstance(texts, list)
                or not 1 <= len(texts) <= 10 or any(not isinstance(t, str) or not t for t in texts)
                or set(body) != {'model', 'input'}):
            raise ValueError('Unapproved embedding payload')
        bound = len(json.dumps(body, ensure_ascii=False).encode('utf-8')) + 8192
        reserve = Decimal(bound) / 1000000
        if bound > 120000 or len(self.calls) >= 100 or self.reserved + reserve > BATCH_CAP_CNY:
            self.stopped = True
            self.write()
            raise RuntimeError('Embedding budget or input limit reached')
        self.reserved += reserve
        row = {'call': len(self.calls) + 1, 'status': 'pending', 'input_token_bound': bound,
               'reserved_cny': str(reserve)}
        self.calls.append(row)
        self.write()
        prefix = self.output / f'embedding_{row["call"]:03d}'
        Path(str(prefix) + '_request.json').write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')
        start = time.monotonic()
        try:
            response = self.inner.handle_request(request)
            try:
                raw = response.read()
                value = json.loads(raw)
                # Vector responses stay local; headers/credentials are never recorded.
                Path(str(prefix) + '_response.json').write_text(json.dumps(value), encoding='utf-8')
                usage = value.get('usage', {})
                tokens = usage.get('total_tokens', usage.get('prompt_tokens'))
                row.update(http_status=response.status_code, seconds=round(time.monotonic() - start, 3))
                if response.status_code != 200 or type(tokens) is not int or not 0 <= tokens <= bound:
                    raise ValueError('Unknown or invalid embedding usage')
                cost = Decimal(tokens) * Decimal('0.5') / 1000000
                self.known += cost
                row.update(status='usage_recorded', usage=usage, list_price_estimate_cny=str(cost))
                self.write()
                headers = {k: v for k, v in response.headers.items()
                           if k.lower() not in {'content-encoding', 'content-length', 'transfer-encoding'}}
                return httpx.Response(response.status_code, content=raw, headers=headers, request=request)
            finally:
                response.close()
        except BaseException as error:
            row.update(status='unknown_or_invalid_usage_stop', error_type=type(error).__name__)
            self.stopped = True
            self.write()
            raise RuntimeError('Embedding request stopped; usage requires reconciliation') from None

    def close(self):
        self.inner.close()
