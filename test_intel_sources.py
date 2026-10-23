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
    assert body == {'query': '查询', 'type': 'auto', 'numResults': 10,
                    'startPublishedDate': '2026-10-15T12:00:00.000Z', 'contents': {'text': {'maxCharacters': 3000}}}
    assert post.call_args.kwargs['timeout'] == 90


@pytest.mark.parametrize('fetch,args,empty', [
    ('fetch_eastmoney', ('600690', SINCE.date()), []),
    ('fetch_exa', ('海尔', SINCE), []),
])
def test_source_errors_are_contained(monkeypatch, fetch, args, empty):
    import intel_sources as s
    stats = s.SourceStats()
    extra = (10, 3000, None) if fetch == 'fetch_exa' else ()
    items, err = getattr(s, fetch)(*args, stats, *extra)
    assert items == empty and err and stats.calls == stats.errors == 1


def test_body_error_returns_empty():
    import intel_sources as s
    stats = s.SourceStats()
    assert s.fetch_eastmoney_body('AN1', stats) == ''
    assert stats.errors == 1


def test_company_collection_order_saturation(monkeypatch):
    import intel_sources as s
    group = {'aliases': ['海尔'], 'entities': [{'name': '智家', 'aliases': ['海尔智家'], 'filings': ['600690'], 'query': 'zh'}]}
    calls = []
    def filings(code, since, stats):
        calls.append(('filing', code))
        return [{'title': '公告'}], None
    def exa(query, since, stats, num, chars, dimension, **kw):
        calls.append(('exa', query, num, chars, dimension, kw))
        return [{'title': query, 'dimension': dimension}], None
    monkeypatch.setattr(s, 'fetch_eastmoney', filings)
    monkeypatch.setattr(s, 'fetch_exa', exa)
    items = s.collect_company(group, '海尔集团', 'Haier', s.RULES, SINCE, s.SourceStats())
    assert calls == [('filing', '600690'),
        ('exa', '海尔集团 最新动态', 20, 2000, None, {'include_domains': s.RULES['exa']['media_domains']}),
        ('exa', '海尔集团 Haier 本周 新闻', 20, 2000, None, {'exclude_domains': s.RULES['blocked_domains']}),
        ('exa', '海尔集团 数字化 智能制造 供应链 IT系统 合作 签约', 20, 2000, '数字化运营', {'exclude_domains': s.RULES['blocked_domains']}),
        ('exa', '海尔集团 战略 组织调整 管理层 竞争 海外', 20, 2000, '战略竞争', {'exclude_domains': s.RULES['blocked_domains']})]
    assert items[0]['entity'] == '智家'
    assert [r['dimension'] for r in items[1:]] == [None, None, '数字化运营', '战略竞争']


def test_fallback_queries_and_total_outage(monkeypatch):
    import intel_sources as s
    exa = Mock(return_value=([], 'failed'))
    monkeypatch.setattr(s, 'fetch_exa', exa)
    stats = s.SourceStats()
    assert s.collect_company(None, '陌生公司', 'Unknown', s.RULES, SINCE, stats) is None
    assert [c.args[0] for c in exa.call_args_list] == ['陌生公司 最新动态', '陌生公司 Unknown 本周 新闻']
    exa.return_value = ([], None)
    assert s.collect_company(None, '陌生公司', '', s.RULES, SINCE, stats) == []
    mapped = {'aliases': ['海尔'], 'entities': [{'name': '智家', 'filings': ['600690']}]}
    monkeypatch.setattr(s, 'fetch_eastmoney', Mock(return_value=([], 'failed')))
    exa.return_value = ([], 'failed')
    assert s.collect_company(mapped, '海尔', '', s.RULES, SINCE, stats) is None
    s.fetch_eastmoney.return_value = ([], None)
    assert s.collect_company(mapped, '海尔', '', s.RULES, SINCE, stats) == []


def test_industry_tags_and_failure(monkeypatch):
    import intel_sources as s
    exa = Mock(return_value=([{'title': '产业'}], None))
    monkeypatch.setattr(s, 'fetch_exa', exa)
    stats = s.SourceStats()
    assert s.collect_industry('家电', s.RULES, SINCE, stats) == [{'title': '产业', 'entity': None, 'dimension': '行业'}]
    exa.assert_called_once_with(s.RULES['industries']['家电']['exa'], SINCE, stats, 20, 2000, '行业',
                                exclude_domains=s.RULES['blocked_domains'])
    exa.return_value = ([], 'failed')
    assert s.collect_industry('家电', s.RULES, SINCE, stats) == []


def test_notice_disclaimer_position_after_whitespace_collapse(monkeypatch):
    import intel_sources as s
    monkeypatch.setattr(s.httpx, 'get', Mock(return_value=response(
        {'data': {'notice_content': '香港' + ' ' * 500 + '承擔任何責任。\n有效正文'}})))
    assert s.fetch_eastmoney_body('AN1', s.SourceStats()) == '有效正文'


def test_fallback_company_never_runs_dimension_queries(monkeypatch):
    import intel_sources as s
    exa = Mock(return_value=([{'title': '新闻'} for _ in range(120)], None))
    monkeypatch.setattr(s, 'fetch_exa', exa)
    filings = Mock(side_effect=AssertionError('unmapped company has no filings'))
    monkeypatch.setattr(s, 'fetch_eastmoney', filings)
    s.collect_company(None, '陌生公司', 'Unknown', s.RULES, SINCE, s.SourceStats())
    assert [c.args[0] for c in exa.call_args_list] == ['陌生公司 最新动态', '陌生公司 Unknown 本周 新闻']
    assert [c.args[5] for c in exa.call_args_list] == [None, None]
    filings.assert_not_called()


def test_exa_null_cost_keeps_results(monkeypatch):
    """Review F3: costDollars present but null must not discard the query's results."""
    import intel_sources as s
    monkeypatch.setattr(s.httpx, 'post', Mock(return_value=response({'costDollars': None, 'results': [
        {'title': '保留', 'url': 'https://example.com/1', 'publishedDate': '2026-10-16T00:00:00.000Z', 'text': '字' * 300},
    ]})))
    stats = s.SourceStats()
    items, err = s.fetch_exa('查询', SINCE, stats, 10, 3000, None)
    assert err is None and [i['title'] for i in items] == ['保留']
    assert stats.exa_cost_usd == 0 and stats.errors == 0


@pytest.mark.parametrize('include,exclude,expected', [
    (['yicai.com'], None, {'includeDomains': ['yicai.com']}),
    (None, ['epochtimes.com'], {'excludeDomains': ['epochtimes.com']}),
    (['yicai.com'], ['epochtimes.com'], {'includeDomains': ['yicai.com']}),
    ([], ['epochtimes.com'], {'includeDomains': []}),
    (None, None, {}),
])
def test_exa_domain_request(monkeypatch, include, exclude, expected):
    import intel_sources as s
    post = Mock(return_value=response({'results': []}))
    monkeypatch.setattr(s.httpx, 'post', post)
    rows, error = s.fetch_exa('查询', SINCE, s.SourceStats(), 7, 2000, None,
                              include_domains=include, exclude_domains=exclude)
    assert error is None and rows == []
    body = post.call_args.kwargs['json']
    assert 'category' not in body and body['numResults'] == 7
    assert {k: body[k] for k in ('includeDomains', 'excludeDomains') if k in body} == expected


def test_gnews_removed():
    import intel_sources as s
    assert not hasattr(s, 'fetch_gnews')
    assert 'gnews' not in s.RULES
    assert all(set(value) == {'exa'} for value in s.RULES['industries'].values())
