"""Independent, failure-contained news and company-announcement collectors."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

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


def fetch_gnews(query, lang, stats):
    stats.calls += 1
    try:
        locale = dict(hl='zh-CN', gl='CN', ceid='CN:zh-Hans') if lang == 'zh' else dict(hl='en-US', gl='US', ceid='US:en')
        response = httpx.get('https://news.google.com/rss/search', params={'q': f'{query} when:7d', **locale},
                              headers=HEADERS, timeout=25)
        response.raise_for_status()
        items = []
        for row in ET.fromstring(response.text).findall('channel/item'):
            outlet = row.findtext('source', '')
            title = row.findtext('title', '')
            suffix = f' - {outlet}'
            if outlet and title.endswith(suffix):
                title = title[:-len(suffix)]
            pub = row.findtext('pubDate')
            published = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
            items.append(_item(title, row.findtext('link', ''), published, outlet, 'headline', '', 'gnews', query_lang=lang))
        return items, None
    except Exception as exc:
        return [], _error(stats, 'gnews', exc)
    finally:
        time.sleep(RULES['gnews']['sleep_seconds'])


def fetch_exa(query, since_dt, stats, num, max_chars, dimension):
    stats.calls += 1
    try:
        response = httpx.post('https://api.exa.ai/search',
            headers={**HEADERS, 'x-api-key': os.getenv('EXA_API_KEY', '')},
            json={'query': query, 'type': 'auto', 'category': 'news', 'numResults': num,
                  'startPublishedDate': since_dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                  'contents': {'text': {'maxCharacters': max_chars}}}, timeout=90)
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
    alias = group['aliases'][0]
    count = add(fetch_gnews(alias, 'zh', stats))
    if group_cfg and count >= rules['gnews']['saturation']:
        for dimension, expression in rules['gnews']['dimensions'].items():
            add(fetch_gnews(f'{alias} {expression}', 'zh', stats), dimension=dimension)
    if en:
        add(fetch_gnews(en, 'en', stats))
    for entity in group.get('entities', []):
        if entity.get('query') in ('zh', 'en'):
            add(fetch_gnews(entity['aliases'][0], entity['query'], stats), entity=entity['name'])
    for dimension, template in rules['exa']['queries'].items():
        if not group_cfg and dimension != '基础':
            continue
        query = ' '.join(template.format(zh=zh, en=en).split())
        add(fetch_exa(query, since_dt, stats, rules['exa']['num_results'], rules['exa']['max_characters'],
                      None if dimension == '基础' else dimension))
    return None if outcomes and all(error is not None for error in outcomes) else items


def collect_industry(name, rules, since_dt, stats):
    config = rules['industries'][name]
    news, _ = fetch_gnews(config['gnews'], 'zh', stats)
    exa, _ = fetch_exa(config['exa'], since_dt, stats, rules['exa']['num_results'],
                       rules['exa']['max_characters'], '行业')
    for item in news + exa:
        item.update(entity=None, dimension='行业')
    return news + exa
