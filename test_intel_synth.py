"""Offline material, prompt, and synthesis orchestration contracts."""
from copy import deepcopy
from datetime import date
import json
from unittest.mock import Mock

import pytest
from intel_sources import RULES, ENTITY_MAP


def item(title, **kwargs):
    return dict(dict(title=title, url='https://example.com/' + title, published=date(2026, 10, 20),
                     outlet='Reuters', kind='headline', body='', origin='exa', dimension=None,
                     entity=None, query_lang='zh', label='keep'), **kwargs)


def test_material_order_caps_overflow_and_industry():
    from intel_synth import build_material
    rules = deepcopy(RULES)
    rules['material'].update(filings_max=1, bodies_max=1, body_chars=5, industry_bodies_max=1)
    rules['filings']['body_chars'] = 4
    filings = [item('旧公告', kind='filing', body='旧公告正文', published=date(2026, 10, 18)),
               item('新公告', kind='filing', body='公告正文超过限制', published=date(2026, 10, 21))]
    others = [item('正文甲', kind='article', body='正文甲内容超过限制'),
              item('正文溢出', kind='article', body='溢出内容', entity='海尔智家'),
              item('同主体挤掉', entity='海尔智家'), item('集团线索'), item('额外线索', entity='其他'),
              item('提及一', label='mention', entity='品牌'), item('提及二', label='mention', entity='另一品牌')]
    industry = [item('行业正文', kind='article', body='行业正文超过限制'), item('行业标题'),
                item('行业多余'), item('行业顺带', label='mention')]
    text = build_material('海尔集团', ENTITY_MAP['海尔集团'], filings, others, industry, rules)
    headings = ['【集团范围】', '【公司公告】', '【报道·正文】', '【行业与竞争】']
    assert [text.index(h) for h in headings] == sorted(text.index(h) for h in headings)
    assert '海尔智家（core）' in text
    assert '新公告\n公告正文' in text and '旧公告' not in text and '超过限制' not in text
    assert '正文甲（Reuters｜主流媒体｜综合）\n正文甲内容' in text
    assert '正文溢出' not in text and '溢出内容' not in text
    assert '同主体挤掉' not in text and '额外线索' not in text
    assert '[顺带提及]' not in text and '提及一' not in text and '提及二' not in text
    assert '行业正文' in text and '行业标题' not in text and '行业多余' not in text and '行业顺带' not in text


def test_material_body_selection_keep_before_mention_newest_and_tags():
    from intel_synth import build_material
    rules = deepcopy(RULES)
    rules['material']['bodies_max'] = 2
    rows = [item('提及正文', kind='article', body='甲' * 300, label='mention', published=date(2026, 10, 23)),
            item('早正文', kind='article', body='乙' * 300, published=date(2026, 10, 20)),
            item('晚正文', kind='article', body='丙' * 300, published=date(2026, 10, 21), dimension='海外', entity='海尔智家')]
    text = build_material('海尔集团', {}, [], rows, [], rules)
    assert text.index('晚正文') < text.index('早正文') and '提及正文' not in text
    assert '海外｜主体：海尔智家' in text
    assert '【集团范围】' not in text and '【公司公告】' not in text and '【行业与竞争】' not in text
    assert text.count('甲') == 0 and '[顺带提及]' not in text


def test_verbatim_prompts():
    from intel_synth import SYSTEM_PROMPT, CHECK_PROMPT, REVISE_INSTRUCTION, user_prompt
    expected_system = '''你在一家国际咨询公司做大中华区客户经营，每周给合伙人们写一份重点客户动态。读者都是做了二十年战略和IT咨询的老手：他们要的是"这周这家公司出了什么事、谁在拍板、钱从哪来、我们该不该去、什么时候去、去了能不能顺利交付和收到钱"，不需要你教他们咨询行业常识。

观察面要全：战略、组织与人事、经营与运营、供应链与制造、数字化与IT、市场与竞争、财务、治理与合规、海外。但只挑本周最要紧的写，抓大放小。

写法要求：
- 篇幅：全文不超过500字。只写本周最重要的一到三件事和你的判断，其余一概不写；信息少就两三句话结束。
- 像资深同事在周一例会上口头汇报后整理成的文字：有判断、有取舍、敢说"这条不重要"或"这条信息不足，先别动"。
- 先说结论，再给支撑的事实。事实要具体：数字、日期、人名、交易对手、出处（"据财联社""公司公告称"）。
- 材料标签的用法：【公司公告】是一手信息；【报道·正文】可以引用细节；来源层级为"门户与自媒体"或"未分级"的内容，引用时要更谨慎；【行业与竞争】用来判断客户所处的环境，和客户本身的事实分开写。
- 这些标签和来源层级只供你判断把握程度，正文里不要出现标签名或层级名（如"公司公告级""未分级""门户来源"），也不要写材料说明、数据口径说明、不能引用清单这类段落。用自然的说法交代出处和把握，例如"公司公告称""据财联社""目前只有单一来源，尚未证实"。
- 咨询角度贯穿全文，而不是最后单列一段套话：机会在哪个部门、哪个具体议题；买方是谁；预算和决策节奏；交付难点；付款和回款风险（国企审批链条、并购整合期预算冻结、业绩承压时砍外部开支等）。没有可行动的机会就直说没有。
- 不写空泛句子。凡是放在任何一家公司身上都成立的话（"数字化转型是长期趋势""需关注后续进展""窗口期""赋能""抓手""值得注意的是"），删掉。
- 版式：直接从结论写起，不要写标题行、日期行或小标题，不要用分隔线（---）。每件事一段，可以用粗体点出事件名；不要列"下周看什么"之类的清单。'''
    expected_check = '你是事实核查员。给你一份原始材料和一份据此写成的客户动态草稿。逐句核查草稿里关于本期发生了什么的事实性陈述：数字、日期、人名与职务、金额、交易与合作、事件本身。每个数字、日期、人名、金额都必须能在材料里找到原文依据；日期、主体或出处与材料对不上的，同样算错。\n只报告两类问题：\nunsupported：草稿当作事实写出，但材料里找不到依据。\ncontradicted：与材料矛盾（包括日期、主体、出处错配），或草稿自己的计算有误。\n不要报告：行业常识和公司的公开背景（如上市地、企业性质、行业惯例）；明确标为判断、推测或建议的句子（"我判断""可能""建议""倾向于"）；措辞和风格。\n只输出 JSON：{"issues":[{"quote":"草稿原句片段","type":"unsupported|contradicted","why":"材料里实际是什么","fix":"改法"}]}。没有问题输出 {"issues":[]}。'
    assert SYSTEM_PROMPT == expected_system
    assert CHECK_PROMPT == expected_check
    assert REVISE_INSTRUCTION == '下面的草稿有若干句被事实核查标出问题。只修改这些句子：能按材料更正的就更正，无法更正的直接删掉。其余文字逐字保留，不新增内容，修订后全文仍不超过500字。直接输出修订后的全文，不要任何说明。'
    expected = '客户：海尔（Haier）\n\n【本周材料】（过去8天）\n材料\n\n【上周已经汇报过的内容】（不要重复；有新进展才写进展本身）\n（无）\n\n请写本周这家客户的动态。中文。'
    assert user_prompt('海尔', 'Haier', '材料', '', '') == expected
    assert user_prompt('海尔', 'Haier', '材料', '上周', '背景') == expected.replace('（无）', '上周').replace('\n\n请写', '\n\n【个人知识库背景】（仅供背景参考，不是本周事实）\n背景\n\n请写')


def llm_response(text, cost=.1, finish='stop'):
    return {'choices': [{'message': {'content': text}, 'finish_reason': finish}], 'usage': {'cost': cost}}, None


@pytest.mark.parametrize('check,revision,expected,post_count', [
    ({'issues': []}, None, '草稿', 1), (None, None, '草稿', 1), ({}, None, '草稿', 1),
    ({'issues': 'bad'}, None, '草稿', 1),
    ({'issues': [{'quote': '错误', 'type': 'unsupported'}]}, '修订', '修订', 2),
    ({'issues': [{'quote': '错误'}]}, '', '草稿', 2),
    ({'issues': [{'quote': '错误'}]}, None, '草稿', 2),
])
def test_compose_paths_costs_and_requests(check, revision, expected, post_count):
    from intel_synth import compose_section, CHECK_PROMPT, REVISE_INSTRUCTION, SYSTEM_PROMPT
    replies = [llm_response('草稿')]
    if post_count == 2:
        replies.append(llm_response(revision, .3) if revision is not None else (None, 'failed'))
    post = Mock(side_effect=replies)
    def call_json(url, **kw):
        if check is not None:
            kw['meta_cb']({'usage': {'cost': .2}})
        return check
    check_call = Mock(side_effect=call_json)
    costs = []
    result = compose_section('海尔', 'Haier', '材料', '上周', '背景', RULES,
                             post=post, call_json=check_call, cost_cb=costs.append)
    assert result == expected and post.call_count == post_count
    assert sum(costs) == pytest.approx(.1 + (.2 if check is not None else 0) + (.3 if revision is not None else 0))
    draft_kw = post.call_args_list[0].kwargs
    assert draft_kw['timeout'] == 400
    assert draft_kw['json_body']['model'] == RULES['synth']['draft_model']
    assert draft_kw['json_body']['max_tokens'] == RULES['synth']['max_tokens'] == 16000
    assert draft_kw['json_body']['reasoning'] == {'effort': 'medium'}
    assert draft_kw['json_body']['messages'][0]['content'] == SYSTEM_PROMPT
    checker = check_call.call_args.kwargs['json_body']
    assert checker['model'] == RULES['synth']['check_model'] and checker['max_tokens'] == 12000
    assert checker['reasoning'] == {'effort': 'medium'}
    assert checker['messages'] == [{'role': 'system', 'content': CHECK_PROMPT},
        {'role': 'user', 'content': '【原始材料】\n材料\n\n【上周已汇报内容】\n上周\n\n【草稿】\n草稿'}]
    if post_count == 2:
        assert post.call_args.kwargs['json_body']['reasoning'] == {'effort': 'low'}
        revised = post.call_args.kwargs['json_body']['messages'][1]['content']
        assert revised == REVISE_INSTRUCTION + '\n\n【原始材料】\n材料\n\n【草稿】\n草稿\n\n【核查意见】\n' + json.dumps(check['issues'], ensure_ascii=False)


@pytest.mark.parametrize('reply', [(None, 'failed'), llm_response('')])
def test_draft_failure(reply):
    from intel_synth import compose_section
    check = Mock(side_effect=AssertionError('no check after failed draft'))
    assert compose_section('海尔', '', '材料', '', '', RULES, post=Mock(return_value=reply), call_json=check) is None
    check.assert_not_called()


def test_heading_cleanup():
    from intel_synth import compose_section
    text = '# 一级\n## 二级\n### 三级\n---\n正文'
    assert compose_section('海尔', '', '材料', '', '', RULES,
        post=Mock(return_value=llm_response(text)), call_json=Mock(return_value={'issues': []})) == '### 一级\n### 二级\n### 三级\n正文'


def test_draft_length_retries_once_at_lower_effort(caplog):
    """Sample 2026-10-23: BYD draft hit max_tokens and a half-written section was emailed."""
    from intel_synth import compose_section
    post = Mock(side_effect=[llm_response('半截', finish='length'), llm_response('完整')])
    assert compose_section('比亚迪', '', '材料', '', '', RULES, post=post,
                           call_json=Mock(return_value={'issues': []})) == '完整'
    efforts = [c.kwargs['json_body']['reasoning'] for c in post.call_args_list]
    assert efforts == [{'effort': 'medium'}, {'effort': 'low'}]
    assert 'length' in caplog.text


def test_draft_length_twice_drops_section():
    from intel_synth import compose_section
    post = Mock(return_value=llm_response('半截', finish='length'))
    check = Mock(side_effect=AssertionError('no check for a truncated draft'))
    assert compose_section('比亚迪', '', '材料', '', '', RULES, post=post, call_json=check) is None
    assert post.call_count == 2


def test_truncated_revision_keeps_draft():
    from intel_synth import compose_section
    post = Mock(side_effect=[llm_response('草稿'), llm_response('半截修订', finish='length')])
    assert compose_section('比亚迪', '', '材料', '', '', RULES, post=post,
                           call_json=Mock(return_value={'issues': [{'quote': '错'}]})) == '草稿'


def test_body_only_material_and_prompts():
    from intel_synth import build_material, SYSTEM_PROMPT, CHECK_PROMPT, REVISE_INSTRUCTION
    rows = [item('仅标题条目', body='短正文'), item('空正文条目', kind='article'),
            item('正文条目', kind='article', body='可用正文')]
    for company, industry in [(rows, []), ([], rows)]:
        text = build_material('海尔', {}, [], company, industry, RULES)
        assert '仅标题·线索' not in text
        assert '仅标题条目' not in text and '空正文条目' not in text
        assert '正文条目' in text and '可用正文' in text
    assert all('仅标题' not in prompt for prompt in (SYSTEM_PROMPT, CHECK_PROMPT, REVISE_INSTRUCTION))
    assert 'title_only_as_fact' not in CHECK_PROMPT


@pytest.mark.parametrize('check,revision,final', [
    ({'issues': []}, None, '草稿'), (None, None, '草稿'),
    ({'issues': [{'quote': '错误'}]}, '修订', '修订'),
    ({'issues': [{'quote': '错误'}]}, None, '草稿'),
])
def test_audit_paths(tmp_path, check, revision, final):
    from intel_synth import compose_section
    path = tmp_path / 'audit.md'
    replies = [llm_response('草稿')]
    if check and check.get('issues'):
        replies.append(llm_response(revision) if revision else (None, 'failed'))
    assert compose_section('海尔', '', '原始材料', '', '', RULES, post=Mock(side_effect=replies),
                           call_json=Mock(return_value=check), audit_path=path) == final
    text = path.read_text(encoding='utf-8')
    opinion = '核查失败' if check is None else json.dumps(check, ensure_ascii=False)
    assert text == f'## 材料\n\n原始材料\n\n## 草稿\n\n草稿\n\n## 核查意见\n\n{opinion}\n\n## 终稿\n\n{final}\n'


def test_audit_draft_failure_and_write_failure(tmp_path, caplog):
    from intel_synth import compose_section
    path = tmp_path / 'failed.md'
    assert compose_section('海尔', '', '原始材料', '', '', RULES,
        post=Mock(return_value=(None, 'failed')), call_json=Mock(), audit_path=path) is None
    assert path.read_text(encoding='utf-8') == '## 材料\n\n原始材料\n\n## 草稿\n\n（无）\n\n## 核查意见\n\n（无）\n\n## 终稿\n\n（无）\n'
    assert compose_section('海尔', '', '材料', '', '', RULES,
        post=Mock(return_value=llm_response('草稿')), call_json=Mock(return_value={'issues': []}),
        audit_path=tmp_path / 'missing' / 'audit.md') == '草稿'
    assert any(r.levelname == 'WARNING' and 'audit' in r.message.lower() for r in caplog.records)
