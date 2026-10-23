"""Offline deterministic cleaning and injected classifier contract tests."""
from datetime import date, datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
from intel_sources import RULES, ENTITY_MAP

SINCE = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def item(title='海尔投产', **kwargs):
    return dict(dict(title=title, url='https://example.com/' + title, published=date(2026, 10, 20),
                     outlet='Reuters', kind='headline', body='', origin='gnews', dimension=None,
                     entity=None, query_lang='zh'), **kwargs)


@pytest.mark.parametrize('published,expected', [(None, False), (date(2026, 10, 15), True),
    (date(2026, 10, 14), False), (SINCE, True), (SINCE - timedelta(seconds=1), False)])
def test_window(published, expected):
    from intel_clean import in_window
    assert in_window(item(published=published), SINCE) is expected


@pytest.mark.parametrize('text,group,expected', [
    ('利勃海尔与苏海尔', '海尔集团', None), ('海尔智家新厂', '海尔集团', '海尔智家'),
    ('Haier plan', '海尔集团', '__group__'), ('Anta earnings', '安踏集团', '__group__'),
    ('anta earnings', '安踏集团', None), ('BYDX factory', '比亚迪', None),
    ('BYD factory', '比亚迪', '__group__'), ('PUMA earnings', '安踏集团', None),
    ('安踏投资PUMA', '安踏集团', '彪马'), ('红双喜新品', '李宁集团', None),
    ('李宁与红双喜', '李宁集团', '红双喜'), ('haier SMART HOME', '海尔集团', '海尔智家'),
])
def test_entity_matching(text, group, expected):
    from intel_clean import match_group
    assert match_group(text, ENTITY_MAP[group]) == expected


def test_pollution_and_tiers():
    from intel_clean import is_low_value, is_farm, is_routine_filing, source_tier
    assert is_low_value('海尔融资净买入', RULES)
    assert not is_low_value('海尔供应链投产', RULES)
    assert is_farm(item(url='https://sub.pswp.cn/a'), RULES)
    assert is_farm(item(outlet='hotelren.com'), RULES)
    assert is_farm(item(body='Powered by Discuz'), RULES)
    assert is_routine_filing('独立董事年度意见', RULES)
    assert not is_routine_filing('新厂投资公告', RULES)
    assert source_tier(item(kind='filing'), RULES) == '公司公告'
    assert source_tier(item(outlet='REUTERS'), RULES) == '主流媒体'
    assert source_tier(item(outlet='', url='https://www.cnevpost.com/a'), RULES) == '行业媒体'
    assert source_tier(item(outlet='新浪'), RULES) == '门户与自媒体'
    assert source_tier(item(outlet='unknown'), RULES) == '未分级'


@pytest.mark.parametrize('kinds,expected', [(['headline', 'article', 'filing'], 'filing'),
                                         (['headline', 'article'], 'article')])
def test_dedupe_priority_and_earliest(kinds, expected):
    from intel_clean import dedupe
    rows = [item('海尔新厂正式投产！', kind=k, published=date(2026, 10, 23)) for k in kinds]
    rows.append(item('海尔新厂正式投产', kind=expected, published=date(2026, 10, 21)))
    kept, dropped = dedupe(rows)
    assert len(kept) == 1 and dropped == len(rows) - 1
    assert kept[0]['kind'] == expected and kept[0]['published'] == date(2026, 10, 21)
    kept, dropped = dedupe([item('海尔新工厂正式投产'), item('海尔新工厂正式投产运行')])
    assert len(kept) == 1 and dropped == 1


def test_clean_stage_counts():
    from intel_clean import clean_company
    rows = [item(published=None), item('过期', published=date(2020, 1, 1)), item('已见'),
            item('农场', url='https://pswp.cn/a'), item('月报表', kind='filing', origin='eastmoney'),
            item('投资公告', kind='filing', origin='eastmoney'), item('海尔优惠券'), item('无关'),
            item('海尔智家新工厂'), item('海尔智家新工厂！'), item('海尔海外合作', origin='exa')]
    filings, others, funnel = clean_company(rows, ENTITY_MAP['海尔集团'], RULES,
                                             {rows[2]['url']: {}}, SINCE)
    assert len(filings) == 1 and len(others) == 2
    assert {r['entity'] for r in others} == {'海尔智家', None}
    for key in ['undated', 'seen', 'farm', 'routine', 'low_value', 'no_entity', 'deduped']:
        assert funnel[key] == 1, (key, funnel)
    assert funnel['raw'] == 11 and funnel['raw_eastmoney'] == 2 and funnel['raw_exa'] == 1
    assert funnel['after_window'] == 9 and funnel['after_seen'] == 8 and funnel['after_farm'] == 7
    assert funnel['filings_total'] == 2 and funnel['after_dedupe'] == 3


def classifier(outputs, captured, costs):
    def call(url, **kw):
        captured.append(kw)
        kw['meta_cb']({'usage': {'cost': .01}})
        return outputs.pop(0)
    return call


def test_classify_strictest_omissions_unknown_and_filings():
    from intel_clean import classify_two_stage, OR_ATTRIBUTION_HEADERS
    rows = [item(str(i)) for i in range(6)] + [item('公告', kind='filing')]
    labels = lambda seq: {'labels': [{'id': f'x{i + 1}', 'label': label} for i, label in enumerate(seq)]}
    outputs = [labels(['keep', 'keep', 'mention', 'keep', 'unrelated']),
               labels(['keep', 'promo', 'keep', 'mystery', 'keep', 'keep'])]
    captured, costs = [], []
    kept = classify_two_stage('海尔', rows, RULES, 'company', call_json=classifier(outputs, captured, costs), cost_cb=costs.append)
    assert [(r['title'], r['label']) for r in kept] == [('0', 'keep'), ('2', 'mention'), ('3', 'mention'), ('5', 'mention')]
    assert costs == [.01, .01]
    assert len(captured) == 2
    for call in captured:
        assert call['max_attempts'] == 2
        body = call['json_body']
        assert body['max_tokens'] == 6000 and body['reasoning'] == {'effort': 'low'}
        assert body['response_format'] == {'type': 'json_object'} and body['usage'] == {'include': True}
        assert all(call['headers'][k] == v for k, v in OR_ATTRIBUTION_HEADERS.items())
        entries = json.loads(body['messages'][1]['content'])
        assert len(entries) == 6 and all(r['title'] != '公告' for r in entries)
        assert set(entries[0]) == {'id', 'title', 'outlet', 'date', 'snippet'}
    assert captured[0]['json_body']['messages'] == captured[1]['json_body']['messages']
    assert [c['json_body']['model'] for c in captured] == [RULES['classify']['screen_model'], RULES['classify']['review_model']]


@pytest.mark.parametrize('outputs', [[None, {'labels': []}], [{'labels': []}, None]])
def test_classify_failed_batch(outputs, caplog):
    from intel_clean import classify_two_stage
    calls, costs = [], []
    assert classify_two_stage('海尔', [item()], RULES, 'company',
                              call_json=classifier(outputs, calls, costs)) == []
    assert len(calls) == 2 and 'classify_failed' in caplog.text


def test_industry_prompt_and_batching():
    from intel_clean import classify_two_stage
    captured = []
    def call(url, **kw):
        captured.append(kw['json_body'])
        return {'labels': [{'id': row['id'], 'label': 'keep'} for row in json.loads(kw['json_body']['messages'][1]['content'])]}
    rows = [item(str(i)) for i in range(41)]
    assert len(classify_two_stage('家电', rows, RULES, 'industry', call_json=call)) == 41
    assert len(captured) == 4
    prompt = captured[0]['messages'][0]['content']
    assert 'keep：该行业的实质信息，包括竞争对手动态、行业数据、政策与监管、技术与供应链变化、价格与需求变化。' in prompt
    assert 'mention：只顺带提到该行业，信息量很少。' in prompt
    assert 'unrelated：与该行业无关。' in prompt
