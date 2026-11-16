"""No-network metering checks, including compressed provider responses."""
import gzip
import json
from decimal import Decimal

import httpx
import pytest

from scripts.research_embedding_meter import EmbeddingMeter

URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings'
BODY = {'model': 'text-embedding-v4', 'input': ['合成资料']}


def test_usage_recorded_without_secret_headers(tmp_path):
    raw = gzip.compress(json.dumps({'usage': {'total_tokens': 3}, 'data': []}).encode())
    meter = EmbeddingMeter(tmp_path, prior_reserved='2.5', inner=httpx.MockTransport(
        lambda _: httpx.Response(200, content=raw, headers={'content-encoding': 'gzip'})))
    with httpx.Client(transport=meter) as client:
        result = client.post(URL, json=BODY, headers={'Authorization': 'Bearer TEST-SECRET'})
    assert result.json()['usage']['total_tokens'] == 3
    assert meter.known == Decimal('0.0000015') and not meter.stopped
    assert meter.reserved > Decimal('2.5')
    assert all('TEST-SECRET' not in p.read_text(encoding='utf-8') for p in tmp_path.iterdir())


@pytest.mark.parametrize('body,status', [({}, 200), ({'usage': {'total_tokens': 999999}}, 200),
                                       ({'error': 'invalid_key'}, 401)])
def test_invalid_usage_stops_later_requests(tmp_path, body, status):
    called = []

    def handler(request):
        called.append(True)
        return httpx.Response(status, json=body)

    meter = EmbeddingMeter(tmp_path, prior_reserved='0', inner=httpx.MockTransport(handler))
    with httpx.Client(transport=meter) as client:
        with pytest.raises(RuntimeError):
            client.post(URL, json=BODY)
        with pytest.raises(RuntimeError):
            client.post(URL, json=BODY)
    assert len(called) == 1 and meter.stopped


def test_prior_reservations_prevent_overspend_before_network(tmp_path):
    # Prior reservations sit just under the cumulative batch cap.
    meter = EmbeddingMeter(tmp_path, prior_reserved='9.999',
                           inner=httpx.MockTransport(lambda _: pytest.fail('must not send')))
    with httpx.Client(transport=meter) as client:
        with pytest.raises(RuntimeError):
            client.post(URL, json=BODY)
    assert meter.stopped and not meter.calls


def test_wrong_endpoint_never_receives_credentials(tmp_path):
    meter = EmbeddingMeter(tmp_path, prior_reserved='0',
                           inner=httpx.MockTransport(lambda _: pytest.fail('must not send')))
    with httpx.Client(transport=meter) as client:
        with pytest.raises(ValueError):
            client.post('https://example.org/embeddings', json=BODY)
    assert not meter.calls
