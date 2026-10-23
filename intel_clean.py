"""Deterministic pollution filters and independent two-stage relevance screening."""
from datetime import datetime, timezone
import json
import logging
import os
import re
from urllib.parse import urlsplit

from http_utils import call_llm_json

logger = logging.getLogger(__name__)
OR_ATTRIBUTION_HEADERS = {
    'HTTP-Referer': 'https://github.com/PhysicalClue611/China_Market_Intelligence',
    'X-OpenRouter-Title': 'MI',
}
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'

COMPANY_LABELS = '''keep：与该集团或其下属主体直接相关的实质信息，包括战略、组织与人事、经营与业绩、供应链与制造、产品与技术、数字化与IT、市场与竞争、财务与资本运作、治理与合规、海外业务、重大合作与交易。
mention：该集团只是被顺带提及，或作为行业综述、排行中的一员出现。
promo：软文、导购、测评、选购指南、荐股、行情页、榜单、广告、招聘、公益或活动通稿等没有信息量的内容。
unrelated：与该集团无关，包括同名的其他主体或人物。'''
INDUSTRY_LABELS = '''keep：该行业的实质信息，包括竞争对手动态、行业数据、政策与监管、技术与供应链变化、价格与需求变化。
mention：只顺带提到该行业，信息量很少。
promo：软文、导购、测评、荐股、行情页、榜单、广告、招聘、活动通稿等没有信息量的内容。
unrelated：与该行业无关。'''


def in_window(item, since_dt):
    published = item['published']
    if published is None:
        return False
    return published >= (since_dt if isinstance(published, datetime) else since_dt.date())


def _alias_match(text, alias):
    if re.search('[A-Za-z]', alias):
        return bool(re.search(r'(?<![A-Za-z])' + re.escape(alias) + r'(?![A-Za-z])',
                              text, 0 if len(alias) <= 4 else re.I))
    return alias in text


def match_group(text, group):
    for excluded in group.get('exclude', []):
        text = text.replace(excluded, '')
    group_matches = any(_alias_match(text, alias) for alias in group.get('aliases', []))
    for entity in group.get('entities', []):
        if entity.get('require_comention') and not group_matches:
            continue
        if any(_alias_match(text, alias) for alias in entity.get('aliases', [])):
            return entity['name']
    return '__group__' if group_matches else None


def _matches(text, patterns):
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def is_low_value(title, rules):
    return _matches(title, rules['low_value_patterns'])


def is_farm(item, rules):
    source = ((urlsplit(item['url']).hostname or '') + ' ' + item['outlet']).lower()
    return (any(domain.lower() in source for domain in rules['blocked_domains'])
            or _matches(item['title'] + ' ' + item['body'][:500], rules['farm_page_patterns']))


def is_routine_filing(title, rules):
    return _matches(title, rules['filings']['routine_patterns'])


def source_tier(item, rules):
    if item['kind'] == 'filing':
        return '公司公告'
    source = (item['outlet'] + ' ' + (urlsplit(item['url']).hostname or '')).lower()
    for tier, aliases in rules['source_tiers'].items():
        if any(alias.lower() in source for alias in aliases):
            return tier
    return '未分级'


def published_key(item):
    """Comparable UTC key for mixed date-only and timestamp publications."""
    published = item['published']
    if published is None:
        return float('-inf')
    if not isinstance(published, datetime):
        published = datetime.combine(published, datetime.min.time(), timezone.utc)
    return published.timestamp()


def dedupe(items):
    titles = [re.sub(r'\W+', '', row['title'].lower()) for row in items]
    grams = [{title[i:i + 2] for i in range(len(title) - 1)} for title in titles]
    parents = list(range(len(items)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i in range(len(items)):
        for j in range(i):
            union = grams[i] | grams[j]
            if titles[i] == titles[j] or (union and len(grams[i] & grams[j]) / len(union) >= .6):
                parents[root(i)] = root(j)
    clusters = {}
    for i, item in enumerate(items):
        clusters.setdefault(root(i), []).append(item)
    priority = {'filing': 0, 'article': 1, 'headline': 2}
    kept = [min(cluster, key=lambda row: (priority[row['kind']], published_key(row)))
            for cluster in clusters.values()]
    return kept, len(items) - len(kept)


def clean_company(items, group, rules, seen_urls, since_dt):
    funnel = {key: 0 for key in ('raw_eastmoney', 'raw_exa', 'undated', 'window',
        'seen', 'farm', 'routine', 'low_value', 'no_entity', 'deduped', 'filings_total')}
    funnel['raw'] = len(items)
    for row in items:
        funnel['raw_' + row['origin']] += 1
    funnel['undated'] = sum(row['published'] is None for row in items)
    fresh = [row for row in items if in_window(row, since_dt)]
    funnel['window'] = len(items) - len(fresh)
    funnel['after_window'] = len(fresh)
    unseen = [row for row in fresh if row['url'] not in seen_urls]
    funnel['seen'] = len(fresh) - len(unseen)
    funnel['after_seen'] = len(unseen)
    safe = [row for row in unseen if not is_farm(row, rules)]
    funnel['farm'] = len(unseen) - len(safe)
    funnel['after_farm'] = len(safe)
    kept = []
    for row in safe:
        if row['kind'] == 'filing':
            funnel['filings_total'] += 1
            if is_routine_filing(row['title'], rules):
                funnel['routine'] += 1
                continue
        else:
            if is_low_value(row['title'], rules):
                funnel['low_value'] += 1
                continue
            entity = match_group(row['title'] + ' ' + row['body'][:1500], group)
            if entity is None:
                funnel['no_entity'] += 1
                continue
            row['entity'] = None if entity == '__group__' else entity
        kept.append(row)
    funnel['after_routine'] = len(safe) - funnel['routine']
    funnel['after_low_value'] = funnel['after_routine'] - funnel['low_value']
    funnel['after_entity'] = len(kept)
    kept, funnel['deduped'] = dedupe(kept)
    funnel['after_dedupe'] = len(kept)
    return ([row for row in kept if row['kind'] == 'filing'],
            [row for row in kept if row['kind'] != 'filing'], funnel)


class ClassifiedItems(list):
    """A list of survivors carrying the failed-item count for FUNNEL logging."""
    classify_failed = 0


def classify_two_stage(subject, items, rules, mode, call_json=call_llm_json, cost_cb=None):
    items = [row for row in items if row['kind'] != 'filing']
    result = ClassifiedItems()
    config = rules['classify']
    introduction = (f'你是情报筛选员。下面是一批关于「{subject}」（含其下属公司和品牌）的新闻条目。'
                    '逐条判断它对一家咨询公司的客户经营团队有没有价值，只输出 JSON。')
    if mode == 'industry':
        introduction = introduction.replace('（含其下属公司和品牌）', '')
    system = (introduction + '\n标签：\n' + (COMPANY_LABELS if mode == 'company' else INDUSTRY_LABELS)
              + '\n输出格式：{"labels":[{"id":"x1","label":"keep"}]}，每个 id 都要给出标签。')
    rank = {'keep': 0, 'mention': 1, 'promo': 2, 'unrelated': 3}

    def meta(data):
        if cost_cb:
            cost_cb(data.get('usage', {}).get('cost', 0) or 0)

    for offset in range(0, len(items), config['batch_size']):
        batch = items[offset:offset + config['batch_size']]
        entries = [{'id': f'x{i + 1}', 'title': row['title'], 'outlet': row['outlet'],
                    'date': row['published'].isoformat() if row['published'] else None,
                    'snippet': row['body'][:200]} for i, row in enumerate(batch)]
        messages = [{'role': 'system', 'content': system},
                    {'role': 'user', 'content': json.dumps(entries, ensure_ascii=False)}]
        outputs = []
        for model in (config['screen_model'], config['review_model']):
            outputs.append(call_json(OPENROUTER_URL,
                headers={'Authorization': f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}", **OR_ATTRIBUTION_HEADERS},
                json_body={'model': model, 'messages': messages, 'max_tokens': 6000,
                           'response_format': {'type': 'json_object'}, 'reasoning': {'effort': config['reasoning_effort']},
                           'usage': {'include': True}},
                timeout=400, max_attempts=2, logger=logger, label=f'{subject} classify', meta_cb=meta))
        if any(output is None for output in outputs):
            result.classify_failed += len(batch)
            logger.warning('[%s] classify_failed: %d items dropped', subject, len(batch))
            continue
        maps = [{entry.get('id'): entry.get('label') for entry in output.get('labels', [])
                 if isinstance(entry, dict)} for output in outputs]
        for i, row in enumerate(batch):
            labels = [mapping.get(f'x{i + 1}', 'mention') for mapping in maps]
            label = max((label if label in rank else 'mention' for label in labels), key=rank.get)
            if label in ('keep', 'mention'):
                row['label'] = label
                result.append(row)
    return result
