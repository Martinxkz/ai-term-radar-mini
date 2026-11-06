import json
import re
import urllib.request
from collections import Counter

URL = 'https://hn.algolia.com/api/v1/search_by_date?query=AI&tags=story&hitsPerPage=100'
STOP = set('the and for with this that from your have are was were will can about into over after before more most new now using use used via just not but you our their they them its it is of to in on by as at or an a'.split())

def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'ai-term-radar-demo'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def tokens(text):
    return re.findall(r'[A-Za-z][A-Za-z0-9.+-]{2,}', text.lower())

def main():
    data = fetch(URL)
    cnt = Counter()
    for hit in data.get('hits', []):
        text = (hit.get('title') or '') + ' ' + (hit.get('story_text') or '')
        for term in tokens(text):
            term = term.strip('.-+')
            if len(term) >= 3 and term not in STOP and not term.isdigit():
                cnt[term] += 1
    print('AI term radar candidates from recent HN stories:')
    for term, n in cnt.most_common(20):
        if n >= 2:
            print(f'{term:24s} {n}')

if __name__ == '__main__':
    main()