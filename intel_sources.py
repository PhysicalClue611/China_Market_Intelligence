"""Independent, failure-contained news and company-announcement collectors."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx
import yaml

logger = logging.getLogger(__name__)
_CONFIG = Path(__file__).parent / 'config'
with (_CONFIG / 'entity_map.yaml').open(encoding='utf-8') as stream:
    ENTITY_MAP = yaml.safe_load(stream)['groups']
with (_CONFIG / 'intel_rules.yaml').open(encoding='utf-8') as stream:
    RULES = yaml.safe_load(stream)
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; MI/1.0)'}


@dataclass
class SourceStats:
    calls: int = 0
    errors: int = 0
    exa_cost_usd: float = 0.0


def _item(title, url, published, outlet, kind, body, origin, dimension=None, entity=None, query_lang=None):
    return dict(title=title, url=url, published=published, outlet=outlet, kind=kind,
                body=body, origin=origin, dimension=dimension, entity=entity, query_lang=query_lang)


def _error(stats, source, exc):
    stats.errors += 1
    logger.warning('%s failed: %s', source, type(exc).__name__)
    return str(exc) or type(exc).__name__


def fetch_eastmoney(code, since_date, stats):
    stats.calls += 1
    try:
        response = httpx.get('https://np-anotice-stock.eastmoney.com/api/security/ann',
            params={'sr': -1, 'page_size': 30, 'page_index': 1,
                    'ann_type': 'H' if len(code) == 5 else 'A', 'client_source': 'web', 'stock_list': code},
            headers=HEADERS, timeout=25)
        response.raise_for_status()
        items = []
        for row in response.json()['data']['list']:
            published = date.fromisoformat(row['notice_date'][:10])
            if published >= since_date:
                url = f"https://data.eastmoney.com/notices/detail/{code}/{row['art_code']}.html"
                items.append(_item(row['title'], url, published, '公司公告', 'filing', '', 'eastmoney'))
        return items, None
    except Exception as exc:
        return [], _error(stats, 'eastmoney', exc)


def fetch_eastmoney_body(art_code, stats):
    stats.calls += 1
    try:
        response = httpx.get('https://np-cnotice-stock.eastmoney.com/api/content/ann',
            params={'art_code': art_code, 'client_source': 'web', 'page_index': 1}, headers=HEADERS, timeout=25)
        response.raise_for_status()
        text = re.sub(r'\s+', ' ', response.json()['data']['notice_content'] or '').strip()
        disclaimer = '承擔任何責任。'
        end = text.find(disclaimer, 0, 400)
        if end >= 0:
            text = text[end + len(disclaimer):]
        return text.strip()
    except Exception as exc:
        _error(stats, 'eastmoney body', exc)
        return ''


def fetch_exa(query, since_dt, stats, num, max_chars, dimension, include_domains=None, exclude_domains=None):
    stats.calls += 1
    try:
        body = {'query': query, 'type': 'auto', 'numResults': num,
                'startPublishedDate': since_dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                'contents': {'text': {'maxCharacters': max_chars}}}
        if include_domains is not None:
            body['includeDomains'] = include_domains
        elif exclude_domains is not None:
            body['excludeDomains'] = exclude_domains
        response = httpx.post('https://api.exa.ai/search',
            headers={**HEADERS, 'x-api-key': os.getenv('EXA_API_KEY', '')}, json=body, timeout=90)
        response.raise_for_status()
        data = response.json()
        stats.exa_cost_usd += (data.get('costDollars') or {}).get('total', 0) or 0
        items = []
        for row in data.get('results', []):
            value = row.get('publishedDate')
            published = None
            if value:
                published = (date.fromisoformat(value[:10]) if value.endswith('T00:00:00.000Z')
                             else datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc))
            text = row.get('text') or ''
            host = (urlsplit(row['url']).hostname or '').removeprefix('www.')
            items.append(_item(row.get('title', ''), row['url'], published, host,
                               'article' if len(text) >= 300 else 'headline', text, 'exa', dimension))
        return items, None
    except Exception as exc:
        return [], _error(stats, 'exa', exc)


def collect_company(group_cfg, zh, en, rules, since_dt, stats):
    group = group_cfg or {'aliases': [a for a in (zh, en) if a], 'entities': []}
    items, outcomes = [], []

    def add(result, **tags):
        rows, error = result
        outcomes.append(error)
        for row in rows:
            row.update(tags)
        items.extend(rows)
        return len(rows)

    for entity in group.get('entities', []):
        for code in entity.get('filings', []):
            add(fetch_eastmoney(code, since_dt.date(), stats), entity=entity['name'])
    for name, config in rules['exa']['queries'].items():
        general = name.startswith('综合')
        if not group_cfg and not general:
            continue
        query = ' '.join(config['template'].format(zh=zh, en=en).split())
        domains = ({'include_domains': rules['exa']['media_domains']} if config['scope'] == 'media'
                   else {'exclude_domains': rules['blocked_domains']})
        add(fetch_exa(query, since_dt, stats, rules['exa']['num_results'], rules['exa']['max_characters'],
                      None if general else name, **domains))
    return None if outcomes and all(error is not None for error in outcomes) else items


def collect_industry(name, rules, since_dt, stats):
    config = rules['industries'][name]
    items, _ = fetch_exa(config['exa'], since_dt, stats, rules['exa']['num_results'],
                         rules['exa']['max_characters'], '行业', exclude_domains=rules['blocked_domains'])
    for item in items:
        item.update(entity=None, dimension='行业')
    return items
