"""Offline collection contract tests for issue #23."""
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

SINCE = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, 'get', Mock(side_effect=AssertionError('unmocked HTTP GET')))
    monkeypatch.setattr(httpx, 'post', Mock(side_effect=AssertionError('unmocked HTTP POST')))


def response(data=None, text=''):
    return Mock(json=Mock(return_value=data), text=text)


@pytest.mark.parametrize('code,ann_type', [('600690', 'A'), ('06690', 'H')])
def test_eastmoney_list_window_and_market(monkeypatch, code, ann_type):
    import intel_sources as s
    get = Mock(return_value=response({'data': {'list': [
        {'notice_date': '2026-10-15 00:00', 'title': '投产', 'art_code': 'AN1'},
        {'notice_date': '2026-10-14', 'title': '旧闻', 'art_code': 'AN0'},
    ]}}))
    monkeypatch.setattr(s.httpx, 'get', get)
    stats = s.SourceStats()
    items, err = s.fetch_eastmoney(code, SINCE.date(), stats)
    assert err is None and len(items) == 1
    assert items[0]['published'] == date(2026, 10, 15)
    assert items[0]['url'] == f'https://data.eastmoney.com/notices/detail/{code}/AN1.html'
    assert items[0]['kind'] == 'filing' and items[0]['outlet'] == '公司公告'
    assert get.call_args.kwargs['params']['ann_type'] == ann_type
    assert get.call_args.kwargs['timeout'] == 25 and 'Mozilla' in get.call_args.kwargs['headers']['User-Agent']
    assert stats.calls == 1 and stats.errors == 0


def test_notice_body_disclaimer_and_whitespace(monkeypatch):
    import intel_sources as s
    get = Mock(return_value=response({'data': {'notice_content': '香港 免责 承擔任何責任。\n 正文  \n 营收增长'}}))
    monkeypatch.setattr(s.httpx, 'get', get)
    assert s.fetch_eastmoney_body('AN1', s.SourceStats()) == '正文 营收增长'
    assert get.call_args.kwargs['params']['art_code'] == 'AN1'
    get.return_value = response({'data': {'notice_content': '甲' * 401 + '承擔任何責任。正文'}})
    assert s.fetch_eastmoney_body('AN2', s.SourceStats()).startswith('甲')


@pytest.mark.parametrize('lang,ceid', [('zh', 'CN:zh-Hans'), ('en', 'US:en')])
def test_gnews_rss_utc_source_suffix_sleep(monkeypatch, lang, ceid):
    import intel_sources as s
    get = Mock(return_value=response(text='''<rss><channel><item><title>新厂投产 - Reuters</title><link>https://news.example/1</link><pubDate>Fri, 23 Oct 2026 09:00:00 -0400</pubDate><source>Reuters</source></item></channel></rss>'''))
    monkeypatch.setattr(s.httpx, 'get', get)
    sleep = Mock()
    monkeypatch.setattr(s.time, 'sleep', sleep)
    items, err = s.fetch_gnews('海尔', lang, s.SourceStats())
    assert err is None and items[0]['title'] == '新厂投产'
    assert items[0]['outlet'] == 'Reuters' and items[0]['query_lang'] == lang
    assert items[0]['published'] == datetime(2026, 10, 23, 13, tzinfo=timezone.utc)
    assert get.call_args.kwargs['params']['q'] == '海尔 when:7d'
    assert get.call_args.kwargs['params']['ceid'] == ceid
    sleep.assert_called_once_with(1.1)


def test_exa_dates_cost_kind_request(monkeypatch):
    import intel_sources as s
    post = Mock(return_value=response({'costDollars': {'total': .012}, 'results': [
        {'title': '日期', 'url': 'https://www.example.com/1', 'publishedDate': '2026-10-15T00:00:00.000Z', 'text': '字' * 300},
        {'title': '时刻', 'url': 'https://example.com/2', 'publishedDate': '2026-10-15T08:30:00.000Z', 'text': '短'},
    ]}))
    monkeypatch.setattr(s.httpx, 'post', post)
    stats = s.SourceStats()
    items, err = s.fetch_exa('查询', SINCE, stats, 10, 3000, '战略竞争')
    assert err is None and stats.exa_cost_usd == .012
    assert type(items[0]['published']) is date and items[0]['kind'] == 'article'
    assert items[1]['published'] == datetime(2026, 10, 15, 8, 30, tzinfo=timezone.utc)
    assert items[1]['kind'] == 'headline' and items[0]['outlet'] == 'example.com'
    assert items[0]['dimension'] == '战略竞争'
    body = post.call_args.kwargs['json']
    assert body == {'query': '查询', 'type': 'auto', 'category': 'news', 'numResults': 10,
                    'startPublishedDate': '2026-10-15T12:00:00.000Z', 'contents': {'text': {'maxCharacters': 3000}}}
    assert post.call_args.kwargs['timeout'] == 90


@pytest.mark.parametrize('fetch,args,empty', [
    ('fetch_eastmoney', ('600690', SINCE.date()), []),
    ('fetch_gnews', ('海尔', 'zh'), []),
    ('fetch_exa', ('海尔', SINCE), []),
])
def test_source_errors_are_contained(monkeypatch, fetch, args, empty):
    import intel_sources as s
    monkeypatch.setattr(s.time, 'sleep', Mock())
    stats = s.SourceStats()
    extra = (10, 3000, None) if fetch == 'fetch_exa' else ()
    items, err = getattr(s, fetch)(*args, stats, *extra)
    assert items == empty and err and stats.calls == stats.errors == 1


def test_body_error_returns_empty():
    import intel_sources as s
    stats = s.SourceStats()
    assert s.fetch_eastmoney_body('AN1', stats) == ''
    assert stats.errors == 1


@pytest.mark.parametrize('count,dimensions', [(89, False), (90, True)])
def test_company_collection_order_saturation(monkeypatch, count, dimensions):
    import intel_sources as s
    group = {'aliases': ['海尔'], 'entities': [{'name': '智家', 'aliases': ['海尔智家'], 'filings': ['600690'], 'query': 'zh'}]}
    calls = []
    def news(query, lang, stats):
        calls.append(('gnews', query, lang))
        return ([{'title': '新闻'} for _ in range(count)] if query == '海尔' else [{'title': query}]), None
    def filings(code, since, stats):
        calls.append(('filing', code))
        return [{'title': '公告'}], None
    def exa(query, *args):
        calls.append(('exa', query))
        return [{'title': query, 'dimension': args[-1]}], None
    monkeypatch.setattr(s, 'fetch_gnews', news)
    monkeypatch.setattr(s, 'fetch_eastmoney', filings)
    monkeypatch.setattr(s, 'fetch_exa', exa)
    items = s.collect_company(group, '海尔集团', 'Haier', s.RULES, SINCE, s.SourceStats())
    assert calls[0] == ('filing', '600690')
    assert calls[1] == ('gnews', '海尔', 'zh')
    assert ('gnews', 'Haier', 'en') in calls and ('gnews', '海尔智家', 'zh') in calls
    assert sum(c[0] == 'gnews' for c in calls) == 3 + (5 if dimensions else 0)
    assert sum(c[0] == 'exa' for c in calls) == 3
    assert items[0]['entity'] == '智家'
    assert any(i.get('dimension') == '战略组织' for i in items) == dimensions


def test_fallback_queries_and_total_outage(monkeypatch):
    import intel_sources as s
    news, exa = Mock(return_value=([], 'failed')), Mock(return_value=([], 'failed'))
    monkeypatch.setattr(s, 'fetch_gnews', news)
    monkeypatch.setattr(s, 'fetch_exa', exa)
    stats = s.SourceStats()
    assert s.collect_company(None, '陌生公司', 'Unknown', s.RULES, SINCE, stats) is None
    assert news.call_count == 2 and exa.call_count == 1
    assert news.call_args_list[0].args[:2] == ('陌生公司', 'zh')
    exa.return_value = ([], None)
    assert s.collect_company(None, '陌生公司', '', s.RULES, SINCE, stats) == []


def test_industry_tags_and_failure(monkeypatch):
    import intel_sources as s
    monkeypatch.setattr(s, 'fetch_gnews', Mock(return_value=([{'title': '产业'}], None)))
    monkeypatch.setattr(s, 'fetch_exa', Mock(return_value=([], 'failed')))
    items = s.collect_industry('家电', s.RULES, SINCE, s.SourceStats())
    assert items == [{'title': '产业', 'entity': None, 'dimension': '行业'}]
    s.fetch_gnews.return_value = ([], 'failed')
    assert s.collect_industry('家电', s.RULES, SINCE, s.SourceStats()) == []


def test_notice_disclaimer_position_after_whitespace_collapse(monkeypatch):
    import intel_sources as s
    monkeypatch.setattr(s.httpx, 'get', Mock(return_value=response(
        {'data': {'notice_content': '香港' + ' ' * 500 + '承擔任何責任。\n有效正文'}})))
    assert s.fetch_eastmoney_body('AN1', s.SourceStats()) == '有效正文'
