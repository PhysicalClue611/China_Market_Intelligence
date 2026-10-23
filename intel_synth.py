"""Bounded source material and draft/check/revise weekly synthesis."""
import json
import logging
import os

from http_utils import post_with_retry, call_llm_json, extract_llm_text
from intel_clean import OR_ATTRIBUTION_HEADERS, OPENROUTER_URL, published_key, source_tier

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = '你在一家国际咨询公司做大中华区客户经营，每周给合伙人们写一份重点客户动态。读者都是做了二十年战略和IT咨询的老手：他们要的是"这周这家公司出了什么事、谁在拍板、钱从哪来、我们该不该去、什么时候去、去了能不能顺利交付和收到钱"，不需要你教他们咨询行业常识。\n\n观察面要全：战略、组织与人事、经营与运营、供应链与制造、数字化与IT、市场与竞争、财务、治理与合规、海外。但只挑本周最要紧的写，抓大放小。\n\n写法要求：\n- 篇幅：全文不超过500字。只写本周最重要的一到三件事和你的判断，其余一概不写；信息少就两三句话结束。\n- 像资深同事在周一例会上口头汇报后整理成的文字：有判断、有取舍、敢说"这条不重要"或"这条信息不足，先别动"。\n- 先说结论，再给支撑的事实。事实要具体：数字、日期、人名、交易对手、出处（"据财联社""公司公告称"）。\n- 材料标签的用法：【公司公告】是一手信息；【报道·正文】可以引用细节；来源层级为"门户与自媒体"或"未分级"的内容，引用时要更谨慎；【行业与竞争】用来判断客户所处的环境，和客户本身的事实分开写。\n- 这些标签和来源层级只供你判断把握程度，正文里不要出现标签名或层级名（如"公司公告级""未分级""门户来源"），也不要写材料说明、数据口径说明、不能引用清单这类段落。用自然的说法交代出处和把握，例如"公司公告称""据财联社""目前只有单一来源，尚未证实"。\n- 咨询角度贯穿全文，而不是最后单列一段套话：机会在哪个部门、哪个具体议题；买方是谁；预算和决策节奏；交付难点；付款和回款风险（国企审批链条、并购整合期预算冻结、业绩承压时砍外部开支等）。没有可行动的机会就直说没有。\n- 不写空泛句子。凡是放在任何一家公司身上都成立的话（"数字化转型是长期趋势""需关注后续进展""窗口期""赋能""抓手""值得注意的是"），删掉。\n- 版式：直接从结论写起，不要写标题行、日期行或小标题，不要用分隔线（---）。每件事一段，可以用粗体点出事件名；不要列"下周看什么"之类的清单。'

CHECK_PROMPT = '你是事实核查员。给你一份原始材料和一份据此写成的客户动态草稿。逐句核查草稿里关于本期发生了什么的事实性陈述：数字、日期、人名与职务、金额、交易与合作、事件本身。每个数字、日期、人名、金额都必须能在材料里找到原文依据；日期、主体或出处与材料对不上的，同样算错。\n只报告两类问题：\nunsupported：草稿当作事实写出，但材料里找不到依据。\ncontradicted：与材料矛盾（包括日期、主体、出处错配），或草稿自己的计算有误。\n不要报告：行业常识和公司的公开背景（如上市地、企业性质、行业惯例）；明确标为判断、推测或建议的句子（"我判断""可能""建议""倾向于"）；措辞和风格。\n只输出 JSON：{"issues":[{"quote":"草稿原句片段","type":"unsupported|contradicted","why":"材料里实际是什么","fix":"改法"}]}。没有问题输出 {"issues":[]}。'

REVISE_INSTRUCTION = '下面的草稿有若干句被事实核查标出问题。只修改这些句子：能按材料更正的就更正，无法更正的直接删掉。其余文字逐字保留，不新增内容，修订后全文仍不超过500字。直接输出修订后的全文，不要任何说明。'


def build_material(group_name, group_cfg, filings, others, industry_items, rules):
    limits = rules['material']
    sections = []
    entities = group_cfg.get('entities', [])
    if entities:
        sections.append('【集团范围】\n' + '、'.join(f"{e['name']}（{e['relation']}）" for e in entities))

    def dated(row):
        return row['published'].strftime('%Y-%m-%d') if row['published'] else ''

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
    bodies = [row for row in ordered if row['kind'] == 'article' and row['body']][:limits['bodies_max']]
    if bodies:
        sections.append('【报道·正文】\n' + '\n'.join(article(row) for row in bodies))
    industry = sorted([row for row in industry_items if row['label'] == 'keep'], key=published_key, reverse=True)
    bodies = [row for row in industry if row['kind'] == 'article' and row['body']][:limits['industry_bodies_max']]
    if bodies:
        sections.append('【行业与竞争】\n' + '\n'.join(article(row) for row in bodies))
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
                    call_json=call_llm_json, cost_cb=None, audit_path=None):
    config = rules['synth']
    headers = {'Authorization': f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}", **OR_ATTRIBUTION_HEADERS}

    def meta(data):
        if cost_cb:
            cost_cb(data.get('usage', {}).get('cost', 0) or 0)

    def generate(prompt, effort):
        """Return the generated text, or None on failure or when cut off at max_tokens.

        A truncated generation is never returned: the 2026-10-23 sample emailed a
        half-written section after the draft ran out of tokens.
        """
        data, error = post(OPENROUTER_URL, headers=headers, json_body={
            'model': config['draft_model'],
            'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': prompt}],
            'reasoning': {'effort': effort}, 'max_tokens': config['max_tokens'],
            'usage': {'include': True}}, timeout=400)
        if error or not data:
            return None
        meta(data)
        try:
            choice = data['choices'][0]
            text = extract_llm_text(choice['message'])
            if choice.get('finish_reason') == 'length':
                logger.warning('[%s] synthesis finish_reason=length at effort=%s; discarding truncated text', zh, effort)
                return None
            return text.strip() if text and text.strip() else None
        except (KeyError, IndexError, TypeError):
            return None

    draft, check = None, None

    def finish(result):
        if audit_path is not None:
            try:
                opinion = ('（无）' if not draft else '核查失败' if check is None
                           else json.dumps(check, ensure_ascii=False))
                audit_path.write_text(
                    f'## 材料\n\n{material}\n\n## 草稿\n\n{draft or "（无）"}'
                    f'\n\n## 核查意见\n\n{opinion}\n\n## 终稿\n\n{result if result is not None else "（无）"}\n',
                    encoding='utf-8')
            except Exception as exc:
                logger.warning('[%s] audit write failed: %s', zh, type(exc).__name__)
        return result

    prompt = user_prompt(zh, en, material, last_week, kb)
    draft = generate(prompt, config['draft_effort']) or generate(prompt, config['retry_effort'])
    if not draft:
        return finish(None)
    check = call_json(OPENROUTER_URL, headers=headers, json_body={
        'model': config['check_model'],
        'messages': [{'role': 'system', 'content': CHECK_PROMPT},
                     {'role': 'user', 'content': f'【原始材料】\n{material}\n\n【上周已汇报内容】\n{last_week or "（无）"}\n\n【草稿】\n{draft}'}],
        'reasoning': {'effort': config['check_effort']}, 'response_format': {'type': 'json_object'},
        'max_tokens': 12000, 'usage': {'include': True}}, timeout=400, max_attempts=2,
        logger=logger, label=f'{zh} fact check', meta_cb=meta)
    if check is None:
        logger.warning('[%s] fact check failed; keeping draft', zh)
        return finish(_postprocess(draft))
    issues = check.get('issues')
    if not isinstance(issues, list) or not issues:
        return finish(_postprocess(draft))
    prompt = (REVISE_INSTRUCTION + f'\n\n【原始材料】\n{material}\n\n【草稿】\n{draft}\n\n【核查意见】\n'
              + json.dumps(issues, ensure_ascii=False))
    return finish(_postprocess(generate(prompt, config['revise_effort']) or draft))
