"""Bounded source material and draft/check/revise weekly synthesis."""
from collections import Counter
import json
import logging
import os

from http_utils import post_with_retry, call_llm_json, extract_llm_text
from intel_clean import OR_ATTRIBUTION_HEADERS, OPENROUTER_URL, published_key, source_tier

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = '你在一家国际咨询公司做大中华区客户经营，每周给合伙人们写一份重点客户动态。读者都是做了二十年战略和IT咨询的老手：他们要的是"这周这家公司出了什么事、谁在拍板、钱从哪来、我们该不该去、什么时候去、去了能不能顺利交付和收到钱"，不需要你教他们咨询行业常识。\n\n观察面要全：战略、组织与人事、经营与运营、供应链与制造、数字化与IT、市场与竞争、财务、治理与合规、海外。不必每一面都写，有料才写。\n\n写法要求：\n- 像资深同事在周一例会上口头汇报后整理成的文字：有判断、有取舍、敢说"这条不重要"或"这条信息不足，先别动"。\n- 先说结论，再给支撑的事实。事实要具体：数字、日期、人名、交易对手、出处（"据财联社""公司公告称"）。\n- 材料标签的用法：【公司公告】是一手信息；【报道·正文】可以引用细节；【仅标题·线索】只能当线索提一句，不得展开成事实或据此推演；来源层级为"门户与自媒体"或"未分级"的内容，引用时要更谨慎；【行业与竞争】用来判断客户所处的环境，和客户本身的事实分开写。\n- 咨询角度贯穿全文，而不是最后单列一段套话：机会在哪个部门、哪个具体议题；买方是谁；预算和决策节奏；交付难点；付款和回款风险（国企审批链条、并购整合期预算冻结、业绩承压时砍外部开支等）。没有可行动的机会就直说没有。\n- 不写空泛句子。凡是放在任何一家公司身上都成立的话（"数字化转型是长期趋势""需关注后续进展""窗口期""赋能""抓手""值得注意的是"），删掉。\n- 篇幅跟着信息量走：事情多就写长，事情少就两三句话结束。\n- 版式：可以用三级小标题（###）或粗体小标题，不要用一级、二级标题（# 或 ##），不要用分隔线（---）。可以有少量要点列表，主体是成段的判断。'

CHECK_PROMPT = '你是事实核查员。给你一份原始材料和一份据此写成的客户动态草稿。只核查草稿里关于本期发生了什么的事实性陈述：数字、日期、人名与职务、金额、交易与合作、事件本身。\n只报告三类问题：\nunsupported：草稿当作本期事实写出，但材料里找不到依据。\ntitle_only_as_fact：材料里只有标题（【仅标题·线索】），草稿却当作已证实的事实展开。\ncontradicted：与材料矛盾，或草稿自己的计算有误。\n不要报告：行业常识和公司的公开背景（如上市地、企业性质、行业惯例）；明确标为判断、推测或建议的句子（"我判断""可能""建议""倾向于"）；措辞和风格。\n只输出 JSON：{"issues":[{"quote":"草稿原句片段","type":"unsupported|title_only_as_fact|contradicted","why":"材料里实际是什么","fix":"改法"}]}。没有问题输出 {"issues":[]}。'

REVISE_INSTRUCTION = '下面的草稿有若干句被事实核查标出问题。只修改这些句子：删掉，或改成"据标题/尚未证实"的说法，或按材料更正。其余文字逐字保留，不新增内容。直接输出修订后的全文，不要任何说明。'


def build_material(group_name, group_cfg, filings, others, industry_items, rules):
    limits = rules['material']
    sections = []
    entities = group_cfg.get('entities', [])
    if entities:
        sections.append('【集团范围】\n' + '、'.join(f"{e['name']}（{e['relation']}）" for e in entities))

    def dated(row):
        return row['published'].strftime('%Y-%m-%d') if row['published'] else ''

    def headline(row):
        return f"- {dated(row)} {row['title']}（{row['outlet']}｜{source_tier(row, rules)}）"

    def article(row):
        entity = f"｜主体：{row['entity']}" if row['entity'] else ''
        return (f"- {dated(row)} {row['title']}（{row['outlet']}｜{source_tier(row, rules)}"
                f"｜{row['dimension'] or '综合'}{entity}）\n{row['body'][:limits['body_chars']]}")

    ordered_filings = sorted(filings, key=published_key, reverse=True)[:limits['filings_max']]
    if ordered_filings:
        lines = []
        for row in ordered_filings:
            line = f"- {dated(row)} {row['title']}"
            if row['body']:
                line += '\n' + row['body'][:rules['filings']['body_chars']]
            lines.append(line)
        sections.append('【公司公告】\n' + '\n'.join(lines))
    ordered = sorted(others, key=lambda row: (row['label'] != 'keep', -published_key(row)))
    bodies = [row for row in ordered if row['kind'] == 'article'][:limits['bodies_max']]
    if bodies:
        sections.append('【报道·正文】\n' + '\n'.join(article(row) for row in bodies))
    body_ids = {id(row) for row in bodies}
    entity_counts, label_counts = Counter(), Counter()
    lines = []
    for row in ordered:
        if id(row) in body_ids:
            continue
        label = row['label']
        maximum = limits['keep_headlines_max'] if label == 'keep' else limits['mention_headlines_max']
        if label_counts[label] >= maximum or entity_counts[row['entity']] >= limits['per_entity_headlines_max']:
            continue
        line = headline(row)
        if label == 'mention':
            line = line.replace('- ', '- [顺带提及] ', 1)
        lines.append(line)
        label_counts[label] += 1
        entity_counts[row['entity']] += 1
    if lines:
        sections.append('【仅标题·线索】\n' + '\n'.join(lines))
    industry = sorted([row for row in industry_items if row['label'] == 'keep'], key=published_key, reverse=True)
    bodies = [row for row in industry if row['kind'] == 'article' and row['body']][:limits['industry_bodies_max']]
    body_ids = {id(row) for row in bodies}
    headlines = [row for row in industry if id(row) not in body_ids][:limits['industry_headlines_max']]
    if bodies or headlines:
        sections.append('【行业与竞争】\n' + '\n'.join([article(row) for row in bodies] + [headline(row) for row in headlines]))
    return '\n\n'.join(sections)


def user_prompt(zh, en, material, last_week, kb):
    text = (f'客户：{zh}（{en}）\n\n【本周材料】（过去8天）\n{material}'
            f'\n\n【上周已经汇报过的内容】（不要重复；有新进展才写进展本身）\n{last_week or "（无）"}')
    if kb:
        text += f'\n\n【个人知识库背景】（仅供背景参考，不是本周事实）\n{kb}'
    return text + '\n\n请写本周这家客户的动态。中文。'


def _postprocess(text):
    lines = []
    for line in text.splitlines():
        if line == '---':
            continue
        if line.startswith('## '):
            line = '### ' + line[3:]
        elif line.startswith('# '):
            line = '### ' + line[2:]
        lines.append(line)
    return '\n'.join(lines).strip()


def compose_section(zh, en, material, last_week, kb, rules, post=post_with_retry,
                    call_json=call_llm_json, cost_cb=None):
    config = rules['synth']
    headers = {'Authorization': f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}", **OR_ATTRIBUTION_HEADERS}

    def meta(data):
        if cost_cb:
            cost_cb(data.get('usage', {}).get('cost', 0) or 0)

    def generate(prompt):
        data, error = post(OPENROUTER_URL, headers=headers, json_body={
            'model': config['draft_model'],
            'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': prompt}],
            'reasoning': {'effort': config['reasoning_effort']}, 'max_tokens': config['max_tokens'],
            'usage': {'include': True}}, timeout=400)
        if error or not data:
            return None
        meta(data)
        try:
            choice = data['choices'][0]
            text = extract_llm_text(choice['message'])
            if choice.get('finish_reason') == 'length':
                logger.warning('[%s] synthesis finish_reason=length; continuing with available text', zh)
            return text.strip() if text and text.strip() else None
        except (KeyError, IndexError, TypeError):
            return None

    draft = generate(user_prompt(zh, en, material, last_week, kb))
    if not draft:
        return None
    check = call_json(OPENROUTER_URL, headers=headers, json_body={
        'model': config['check_model'],
        'messages': [{'role': 'system', 'content': CHECK_PROMPT},
                     {'role': 'user', 'content': f'【原始材料】\n{material}\n\n【上周已汇报内容】\n{last_week or "（无）"}\n\n【草稿】\n{draft}'}],
        'reasoning': {'effort': config['reasoning_effort']}, 'response_format': {'type': 'json_object'},
        'max_tokens': 12000, 'usage': {'include': True}}, timeout=400, max_attempts=2,
        logger=logger, label=f'{zh} fact check', meta_cb=meta)
    if check is None:
        logger.warning('[%s] fact check failed; keeping draft', zh)
        return _postprocess(draft)
    issues = check.get('issues')
    if not isinstance(issues, list) or not issues:
        return _postprocess(draft)
    prompt = (REVISE_INSTRUCTION + f'\n\n【原始材料】\n{material}\n\n【草稿】\n{draft}\n\n【核查意见】\n'
              + json.dumps(issues, ensure_ascii=False))
    return _postprocess(generate(prompt) or draft)
