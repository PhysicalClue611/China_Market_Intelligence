"""Offline main-loop integration tests; every external seam is mocked."""
from datetime import date, datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
import run_intel as run
from intel_sources import RULES


def article(title='海尔智家工厂投产', **kw):
    return dict(dict(title=title, url='https://example.com/' + title, published=datetime.now(timezone.utc),
        outlet='Reuters', kind='article', body='正文' * 200, origin='exa', dimension=None, entity=None,
        query_lang=None), **kw)


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    # Prevent any transport, including accidental calls through the retired pipeline.
    import socket
    monkeypatch.setattr(socket.socket, 'connect', Mock(side_effect=AssertionError('network forbidden')))
    if hasattr(run, 'fetch_company_raw'):
        monkeypatch.setattr(run, 'fetch_company_raw', Mock(side_effect=AssertionError('retired pipeline used')))
    patches = {
        '_validate_intel_config': Mock(), 'get_companies_full': Mock(return_value=[{'zh': '海尔集团', 'en': 'Haier'}]),
        '_load_seen_urls': Mock(return_value={}), '_load_fetch_log': Mock(return_value={}),
        'load_last_report_section': Mock(return_value='上周'), 'collect_company': Mock(return_value=[article()]),
        'collect_industry': Mock(return_value=[]), 'compose_section': Mock(return_value='本周总结'),
        'get_company_context': Mock(return_value='背景'), 'fetch_eastmoney_body': Mock(return_value='公告正文'),
        '_save_seen_urls': Mock(), '_save_fetch_log': Mock(), '_save_processed_id': Mock(),
        'get_recipients': Mock(return_value=['offline@example.invalid']), 'send_report': Mock(return_value='sid'),
        '_send_telegram_alert': Mock(), 'post_slack_report': Mock(), 'get_footer': Mock(return_value=''),
    }
    def classify(subject, items, rules, mode, **kwargs):
        for row in items:
            row['label'] = 'keep'
        return items
    patches['classify_two_stage'] = Mock(side_effect=classify)
    for name, value in patches.items():
        monkeypatch.setattr(run, name, value, raising=False)
    monkeypatch.setattr(run, 'OBSIDIAN_DIR', tmp_path / 'vault')
    patches['tmp'] = tmp_path
    return patches


@pytest.mark.parametrize('missing', ['OPENROUTER_API_KEY', 'EXA_API_KEY'])
def test_missing_required_key_raises(monkeypatch, missing):
    # Ported prefilter validation test, extended to the new Exa requirement.
    monkeypatch.setattr(run, 'OPENROUTER_API_KEY', 'k')
    monkeypatch.setattr(run, 'EXA_API_KEY', 'k', raising=False)
    monkeypatch.setattr(run, missing, '')
    monkeypatch.setenv('HERMES_DATA', '/tmp/mi-test')
    monkeypatch.setenv('OBSIDIAN_PATH', '/tmp/mi-test')
    with pytest.raises(run.IntelConfigError, match=missing):
        run._validate_intel_config()


def test_all_keys_present_passes(monkeypatch):
    monkeypatch.setattr(run, 'OPENROUTER_API_KEY', 'k')
    monkeypatch.setattr(run, 'EXA_API_KEY', 'k', raising=False)
    monkeypatch.setenv('HERMES_DATA', '/tmp/mi-test')
    monkeypatch.setenv('OBSIDIAN_PATH', '/tmp/mi-test')
    run._validate_intel_config()


def test_sample_no_writes_default_recipients(pipeline):
    run.run_intel(sample=True)
    p = pipeline
    assert not (p['tmp'] / 'vault').exists()
    for name in ['_save_seen_urls', '_save_fetch_log', '_save_processed_id', '_send_telegram_alert', 'post_slack_report']:
        p[name].assert_not_called()
    sent = p['send_report'].call_args.kwargs
    assert sent['subject'].startswith('[Hermes MI][样文] 中国企业情报周报 ')
    assert sent['recipients'] == ['offline@example.invalid']
    assert '# 中国企业情报周报 ' in sent['markdown_body']
    title_line = sent['markdown_body'].splitlines().index(next(line for line in sent['markdown_body'].splitlines() if line.startswith('# ')))
    assert sent['markdown_body'].splitlines()[title_line + 1] == '> 样文：新采集与合成流程的试运行，用于展示今后周报的样子，不计入正式周报的去重。'
    p['_load_seen_urls'].assert_called_once_with(persist_prune=False)


def test_sample_preserves_expired_seen_bytes(pipeline, monkeypatch):
    path = pipeline['tmp'] / 'seen_urls.json'
    content = b'{"https://expired.invalid": {"ts": 0, "company": "old"}}\n'
    path.write_bytes(content)
    monkeypatch.setattr(run, 'SEEN_URLS_PATH', path)
    monkeypatch.setattr(run, '_load_seen_urls', REAL_LOAD_SEEN)
    run.run_intel(sample=True)
    assert path.read_bytes() == content
    assert REAL_LOAD_SEEN(persist_prune=False) == {}
    assert path.read_bytes() == content


REAL_LOAD_SEEN = run._load_seen_urls


def test_load_seen_default_still_prunes(tmp_path, monkeypatch):
    path = tmp_path / 'seen.json'
    path.write_text('{"https://expired.invalid": {"ts": 0}}')
    monkeypatch.setattr(run, 'SEEN_URLS_PATH', path)
    assert run._load_seen_urls() == {}
    assert json.loads(path.read_text()) == {}


def test_sample_empty_sends_nothing(pipeline):
    pipeline['collect_company'].return_value = []
    run.run_intel(sample=True)
    pipeline['send_report'].assert_not_called()
    assert not pipeline['_load_fetch_log'].return_value


def test_normal_weekly_header_costs_and_delivery(pipeline):
    def collect(*args):
        args[-1].exa_cost_usd += .04
        return [article()]
    def classify(subject, rows, rules, mode, **kw):
        kw['cost_cb'](.02)
        for row in rows:
            row['label'] = 'keep'
        return rows
    def compose(*args, **kw):
        kw['cost_cb'](.03)
        return '本周总结'
    pipeline['collect_company'].side_effect = collect
    pipeline['classify_two_stage'].side_effect = classify
    pipeline['compose_section'].side_effect = compose
    run.run_intel()
    sent = pipeline['send_report'].call_args.kwargs
    assert sent['subject'].startswith('[Hermes MI] 中国企业情报周报 ')
    expected = '> 采集：东方财富公告 + Google News + Exa | 筛选：anthropic/claude-haiku-5.5 + openai/gpt-6-luna | 合成：anthropic/claude-haiku-5.5（openai/gpt-6-luna 核查）'
    assert expected in sent['markdown_body'] and '费用 ~$0.1100' in sent['markdown_body']
    assert list((pipeline['tmp'] / 'vault' / 'Hermes' / 'MI').glob('*-china-companies.md'))
    for name in ['_save_seen_urls', '_save_fetch_log', '_save_processed_id', '_send_telegram_alert', 'post_slack_report']:
        pipeline[name].assert_called_once()
    assert article()['url'] in pipeline['_save_seen_urls'].call_args.args[0]


def test_company_source_outage_is_skip_not_failure(pipeline, caplog):
    pipeline['collect_company'].return_value = None
    run.run_intel()
    assert 'all sources failed, will retry next run' in caplog.text
    assert pipeline['_save_fetch_log'].call_args.args[0] == {}
    assert '本周无新情报' in pipeline['send_report'].call_args.kwargs['subject']
    pipeline['compose_section'].assert_not_called()


def test_industry_failure_nonfatal_and_once_per_industry(pipeline):
    pipeline['get_companies_full'].return_value = [{'zh': '海尔集团'}, {'zh': 'TCL'}, {'zh': '李宁集团'}, {'zh': '安踏集团'}]
    pipeline['collect_industry'].side_effect = RuntimeError('industry failed')
    pipeline['collect_company'].side_effect = lambda group, zh, *a: [article(zh + '新厂投产')]
    run.run_intel()
    assert pipeline['collect_industry'].call_count == 4
    assert pipeline['compose_section'].call_count == 4
    assert pipeline['send_report'].call_count == 1


def test_industry_cleaned_and_shared_with_material(pipeline, monkeypatch):
    from intel_synth import build_material
    pipeline['collect_industry'].return_value = [article('行业竞品新厂'), article('优惠券'),
        article('过期', published=date(2000, 1, 1)), article('无日期', published=None),
        article('农场', url='https://pswp.cn/a')]
    build = Mock(wraps=build_material)
    monkeypatch.setattr(run, 'build_material', build, raising=False)
    run.run_intel()
    assert [i['title'] for i in build.call_args.args[4]] == ['行业竞品新厂']


def test_filing_body_limit_order_and_funnel(pipeline, caplog):
    caplog.set_level('INFO')
    rows = [article(f'公告第{i}项独特内容', kind='filing', origin='eastmoney',
                    url=f'https://data.eastmoney.com/notices/detail/600690/AN{i}.html',
                    published=date(2026, 10, 15 + i)) for i in range(8)]
    # Use dissimilar titles so this test checks the body cap rather than title deduplication.
    titles = ['投资新厂计划', '重大诉讼进展', '业绩预增数据', '收购海外品牌', '执行总裁任命', '资产出售交易', '供应链技术合作', '资本增资方案']
    for row, title in zip(rows, titles):
        row['title'] = title
    pipeline['collect_company'].return_value = rows
    run.run_intel()
    assert [c.args[0] for c in pipeline['fetch_eastmoney_body'].call_args_list] == [f'AN{i}' for i in range(7, 1, -1)]
    funnel = json.loads(next(r.message.split(' FUNNEL ', 1)[1] for r in caplog.records if '[海尔集团] FUNNEL ' in r.message))
    assert funnel['filings_bodies'] == 6 and funnel['raw_eastmoney'] == 8 and funnel['material_chars'] > 0
    assert all(k in funnel for k in ['classify_keep', 'classify_mention', 'classify_dropped', 'classify_failed'])


def test_missing_en_fallback_and_overrides(pipeline):
    pipeline['get_companies_full'].return_value = [{'zh': '陌生企业'}]
    pipeline['collect_company'].return_value = [article('陌生企业新厂')]
    run.run_intel(recipients=['override@example.invalid'], sample=True)
    assert pipeline['collect_company'].call_args.args[0] is None
    assert pipeline['collect_company'].call_args.args[1:3] == ('陌生企业', '')
    assert pipeline['send_report'].call_args.kwargs['recipients'] == ['override@example.invalid']


def test_force_ignores_seen_and_does_not_save(pipeline):
    pipeline['_load_seen_urls'].return_value = {article()['url']: {}}
    run.run_intel(force=True)
    pipeline['load_last_report_section'].assert_not_called()
    pipeline['_save_seen_urls'].assert_not_called()
    pipeline['_save_fetch_log'].assert_not_called()
    assert pipeline['compose_section'].call_count == 1


def test_last_week_limit_3000_preserves_loader_behavior(monkeypatch):
    report = Mock(stem='2000-01-01-china-companies')
    report.read_text.return_value = '## 海尔集团\n' + '甲' * 3500 + '\n---'
    monkeypatch.setattr(run, '_glob_report_files', Mock(return_value=[report]))
    text = run.load_last_report_section('海尔集团')
    assert text == '甲' * 3000 + '\n[...截断]'
