#!/usr/bin/env python3
import json
with open('/tmp/domus-processed.json') as f:
    data = json.load(f)
l = data['listings']

# Nog House titels
print('=== House titels ===')
for x in l:
    if 'House' in x.get('title',''):
        print(json.dumps({k: v for k, v in x.items() if k in ('id','platform','title','address','url')}, indent=2, default=str))

# Dubbels check
print('\n=== Dubbels ===')
seen = {}
for x in l:
    lid = str(x.get('id',''))
    if lid in seen:
        print('DUBBEL id=%s: "%s" en "%s"' % (lid, seen[lid], x.get('title','')[:50]))
    seen[lid] = x.get('title','')[:50]
