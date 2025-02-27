#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2017, Kovid Goyal <kovid at kovidgoyal.net>

from __future__ import absolute_import, division, print_function, unicode_literals

import json
import re
import time
from threading import Lock
from collections import defaultdict, namedtuple

try:
    from urllib.parse import parse_qs, quote_plus, unquote, urlencode
except ImportError:
    from urlparse import parse_qs
    from urllib import quote_plus, urlencode, unquote

from lxml import etree

from calibre import browser as _browser, prints, random_user_agent
from calibre.ebooks.chardet import xml_to_unicode
from calibre.utils.monotonic import monotonic
from calibre.utils.random_ua import accept_header_for_ua

current_version = (1, 0, 15)
minimum_calibre_version = (2, 80, 0)


last_visited = defaultdict(lambda: 0)
Result = namedtuple('Result', 'url title cached_url')


def tostring(elem):
    return etree.tostring(elem, encoding='unicode', method='text', with_tail=False)


def browser():
    ua = random_user_agent(allow_ie=False)
    # ua = 'Mozilla/5.0 (Linux; Android 8.0.0; VTR-L29; rv:63.0) Gecko/20100101 Firefox/63.0'
    br = _browser(user_agent=ua)
    br.set_handle_gzip(True)
    br.addheaders += [
        ('Accept', accept_header_for_ua(ua)),
        ('Upgrade-insecure-requests', '1'),
    ]
    return br


def encode_query(**query):
    q = {k.encode('utf-8'): v.encode('utf-8') for k, v in query.items()}
    return urlencode(q).decode('utf-8')


def parse_html(raw):
    try:
        from html5_parser import parse
    except ImportError:
        # Old versions of calibre
        import html5lib
        return html5lib.parse(raw, treebuilder='lxml', namespaceHTMLElements=False)
    else:
        return parse(raw)


last_visited_lock = Lock()


def query(br, url, key, dump_raw=None, limit=1, parser=parse_html, timeout=60, save_raw=None, simple_scraper=None):
    with last_visited_lock:
        lv = last_visited[key]
    delta = monotonic() - lv
    if delta < limit and delta > 0:
        time.sleep(delta)
    try:
        if simple_scraper is None:
            raw = br.open_novisit(url, timeout=timeout).read()
            raw = xml_to_unicode(raw, strip_encoding_pats=True)[0]
        else:
            raw = simple_scraper(url, timeout=timeout)
    finally:
        with last_visited_lock:
            last_visited[key] = monotonic()
    if dump_raw is not None:
        with open(dump_raw, 'w') as f:
            f.write(raw)
    if save_raw is not None:
        save_raw(raw)
    return parser(raw)


def quote_term(x):
    ans = quote_plus(x.encode('utf-8'))
    if isinstance(ans, bytes):
        ans = ans.decode('utf-8')
    return ans


# DDG + Wayback machine {{{

def ddg_term(t):
    t = t.replace('"', '')
    if t.lower() in {'map', 'news'}:
        t = '"' + t + '"'
    if t in {'OR', 'AND', 'NOT'}:
        t = t.lower()
    return t


def ddg_href(url):
    if url.startswith('/'):
        q = url.partition('?')[2]
        url = parse_qs(q.encode('utf-8'))['uddg'][0].decode('utf-8')
    return url


def wayback_machine_cached_url(url, br=None, log=prints, timeout=60):
    q = quote_term(url)
    br = br or browser()
    data = query(br, 'https://archive.org/wayback/available?url=' +
                 q, 'wayback', parser=json.loads, limit=0.25, timeout=timeout)
    try:
        closest = data['archived_snapshots']['closest']
        if closest['available']:
            return closest['url'].replace('http:', 'https:')
    except Exception:
        pass
    from pprint import pformat
    log('Response from wayback machine:', pformat(data))


def wayback_url_processor(url):
    if url.startswith('/'):
        # Use original URL instead of absolutizing to wayback URL as wayback is
        # slow
        m = re.search('https?:', url)
        if m is None:
            url = 'https://web.archive.org' + url
        else:
            url = url[m.start():]
    return url


def ddg_search(terms, site=None, br=None, log=prints, safe_search=False, dump_raw=None, timeout=60):
    # https://duck.co/help/results/syntax
    terms = [quote_term(ddg_term(t)) for t in terms]
    if site is not None:
        terms.append(quote_term(('site:' + site)))
    q = '+'.join(terms)
    url = 'https://duckduckgo.com/html/?q={q}&kp={kp}'.format(
        q=q, kp=1 if safe_search else -1)
    log('Making ddg query: ' + url)
    br = br or browser()
    root = query(br, url, 'ddg', dump_raw, timeout=timeout)
    ans = []
    for a in root.xpath('//*[@class="results"]//*[@class="result__title"]/a[@href and @class="result__a"]'):
        ans.append(Result(ddg_href(a.get('href')), tostring(a), None))
    return ans, url


def ddg_develop():
    br = browser()
    for result in ddg_search('heroes abercrombie'.split(), 'www.amazon.com', dump_raw='/t/raw.html', br=br)[0]:
        if '/dp/' in result.url:
            print(result.title)
            print(' ', result.url)
            print(' ', wayback_machine_cached_url(result.url, br))
            print()
# }}}

# Bing {{{


def bing_term(t):
    t = t.replace('"', '')
    if t in {'OR', 'AND', 'NOT'}:
        t = t.lower()
    return t


def bing_url_processor(url):
    return url


def bing_search(terms, site=None, br=None, log=prints, safe_search=False, dump_raw=None, timeout=60, show_user_agent=False):
    # http://vlaurie.com/computers2/Articles/bing_advanced_search.htm
    terms = [quote_term(bing_term(t)) for t in terms]
    if site is not None:
        terms.append(quote_term(('site:' + site)))
    q = '+'.join(terms)
    url = 'https://www.bing.com/search?q={q}'.format(q=q)
    log('Making bing query: ' + url)
    br = br or browser()
    br.addheaders = [x for x in br.addheaders if x[0].lower() != 'user-agent']
    ua = ''
    from calibre.utils.random_ua import random_common_chrome_user_agent
    while not ua or 'Edg/' in ua:
        ua = random_common_chrome_user_agent()
    if show_user_agent:
        print('User-agent:', ua)
    br.addheaders.append(('User-agent', ua))

    root = query(br, url, 'bing', dump_raw, timeout=timeout)
    ans = []
    for li in root.xpath('//*[@id="b_results"]/li[@class="b_algo"]'):
        a = li.xpath('descendant::h2/a[@href]') or li.xpath('descendant::div[@class="b_algoheader"]/a[@href]')
        a = a[0]
        title = tostring(a)
        try:
            div = li.xpath('descendant::div[@class="b_attribution" and @u]')[0]
        except IndexError:
            log('Ignoring {!r} as it has no cached page'.format(title))
            continue
        d, w = div.get('u').split('|')[-2:]
        cached_url = 'https://cc.bingj.com/cache.aspx?q={q}&d={d}&mkt=en-US&setlang=en-US&w={w}'.format(
            q=q, d=d, w=w)
        ans.append(Result(a.get('href'), title, cached_url))
    if not ans:
        title = ' '.join(root.xpath('//title/text()'))
        log('Failed to find any results on results page, with title:', title)
    return ans, url


def bing_develop():
    for result in bing_search('heroes abercrombie'.split(), 'www.amazon.com', dump_raw='/t/raw.html', show_user_agent=True)[0]:
        if '/dp/' in result.url:
            print(result.title)
            print(' ', result.url)
            print(' ', result.cached_url)
            print()
# }}}

# Google {{{


def google_term(t):
    t = t.replace('"', '')
    if t in {'OR', 'AND', 'NOT'}:
        t = t.lower()
    return t


def google_url_processor(url):
    return url


def google_extract_cache_urls(raw):
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'replace')
    pat = re.compile(r'\\x22(https://webcache\.googleusercontent\.com/.+?)\\x22')
    upat = re.compile(r'\\\\u([0-9a-fA-F]{4})')
    cache_pat = re.compile('cache:([^:]+):(.+)')

    def urepl(m):
        return chr(int(m.group(1), 16))

    seen = set()
    ans = {}
    for m in pat.finditer(raw):
        cache_url = upat.sub(urepl, m.group(1))
        m = cache_pat.search(cache_url)
        cache_id, src_url = m.group(1), m.group(2)
        if cache_id in seen:
            continue
        seen.add(cache_id)
        src_url = src_url.split('+')[0]
        src_url = unquote(src_url)
        ans[src_url] = cache_url
    return ans


def google_parse_results(root, raw, log=prints):
    cache_url_map = google_extract_cache_urls(raw)
    # print('\n'.join(cache_url_map))
    ans = []
    for div in root.xpath('//*[@id="search"]//*[@id="rso"]//div[descendant::h3]'):
        try:
            a = div.xpath('descendant::a[@href]')[0]
        except IndexError:
            log('Ignoring div with no main result link')
            continue
        title = tostring(a)
        src_url = a.get('href')
        if src_url in cache_url_map:
            cached_url = cache_url_map[src_url]
        else:
            try:
                c = div.xpath('descendant::*[@role="menuitem"]//a[@class="fl"]')[0]
            except IndexError:
                log('Ignoring {!r} as it has no cached page'.format(title))
                continue
            cached_url = c.get('href')
        ans.append(Result(a.get('href'), title, cached_url))
    if not ans:
        title = ' '.join(root.xpath('//title/text()'))
        log('Failed to find any results on results page, with title:', title)
    return ans


def google_specialize_broswer(br):
    br.set_simple_cookie('CONSENT', 'YES+', '.google.com', path='/')
    return br


def google_search(terms, site=None, br=None, log=prints, safe_search=False, dump_raw=None, timeout=60):
    terms = [quote_term(google_term(t)) for t in terms]
    if site is not None:
        terms.append(quote_term(('site:' + site)))
    q = '+'.join(terms)
    url = 'https://www.google.com/search?q={q}'.format(q=q)
    log('Making google query: ' + url)
    br = google_specialize_broswer(br or browser())
    r = []
    root = query(br, url, 'google', dump_raw, timeout=timeout, save_raw=r.append)
    return google_parse_results(root, r[0], log=log), url


def google_develop(search_terms='1423146786', raw_from=''):
    if raw_from:
        with open(raw_from, 'rb') as f:
            raw = f.read()
        results = google_parse_results(parse_html(raw), raw)
    else:
        br = browser()
        results = google_search(search_terms.split(), 'www.amazon.com', dump_raw='/t/raw.html', br=br)[0]
    for result in results:
        if '/dp/' in result.url:
            print(result.title)
            print(' ', result.url)
            print(' ', result.cached_url)
            print()
# }}}


def resolve_url(url):
    prefix, rest = url.partition(':')[::2]
    if prefix == 'bing':
        return bing_url_processor(rest)
    if prefix == 'wayback':
        return wayback_url_processor(rest)
    return url


# if __name__ == '__main__':
#     import sys
#     func = sys.argv[-1]
#     globals()[func]()
