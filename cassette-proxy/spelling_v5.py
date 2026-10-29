"""Music-aware edit correction for full phrases and trailing qualifiers.

Corrections are only accepted by discovery after a real catalog lookup.
"""
import re


def _aligned(query,candidate):
    from music_search_upgrade import norm,typo_distance
    q=norm(query).split();c=norm(candidate).split()
    if not q or not c or len(q)>14 or len(c)>12:return []
    opts=[]
    if len(q)==len(c):
        dist=typo_distance(' '.join(q),' '.join(c))
        if dist is not None:opts.append((dist,' '.join(c),0))
    # Repair a phrase within a longer search, preserving outside qualifiers.
    if len(c)<=len(q) and len(q)-len(c)<=3:
        for start in range(len(q)-len(c)+1):
            chunk=' '.join(q[start:start+len(c)])
            dist=typo_distance(chunk,' '.join(c))
            if dist is None:continue
            repaired=' '.join(q[:start]+c+q[start+len(c):])
            opts.append((dist,repaired,0))
            # If the full corrected query still produces no result, try the
            # verified music phrase alone rather than discarding it entirely.
            if len(q)>len(c):opts.append((dist+1,' '.join(c),1))
    return opts


def phrase_suggestions(query,names,limit=6):
    scores={}
    for name in names:
        if not isinstance(name,str) or not name.strip():continue
        for cost,rewrite,dropped in _aligned(query,name):
            if rewrite not in scores or (cost,dropped)<scores[rewrite]:
                scores[rewrite]=(cost,dropped)
    return [v for v,_ in sorted(scores.items(),key=lambda z:(z[1][0],z[1][1],-len(z[0])))[:limit]]


async def suggestions(d,client,query,base):
    """Prefer saved music phrases; use bounded public metadata on a cache miss."""
    from music_search_upgrade import norm,STOP
    names=[]
    try:
        with d.connect() as db:
            for table,col in [('discovery_artists','name'),('discovery_albums','name'),('discovery','title')]:
                names += [r[0] for r in db.execute(
                    f'SELECT DISTINCT {col} FROM {table} ORDER BY created DESC LIMIT 1200')]
    except Exception:
        pass
    local=phrase_suggestions(query,names)
    if local:
        return [v for v in local if norm(v)!=norm(query)][:5]

    found=[]
    try:
        found.extend(await base(d,client,query))
    except Exception:
        pass
    q=norm(query).split()
    if len(q)>=3 and not found:
        # Query fuzzy phrases minus one qualifier, such as "... mike".
        # Each category is bounded and cached by the existing MB metadata gate.
        for tokens in (q[:-1],q[1:]):
            meaningful=[w for w in tokens if len(w)>=4 and w not in STOP][:3]
            if len(meaningful)<2:continue
            for category,field,key in [('artist/','artist','artists'),
                                       ('release-group/','releasegroup','release-groups')]:
                expr=' AND '.join(f'{field}:{re.sub(r"[^\w]","",w)}~0.6' for w in meaningful)
                try:
                    data=await d._artist_mb_get(client,category,{'query':expr,'limit':15})
                    names.extend([x.get('name') or x.get('title') or '' for x in
                                  data.get(key,[]) if isinstance(x,dict)])
                except Exception:
                    pass
            if phrase_suggestions(query,names):break
    for suggestion in phrase_suggestions(query,names):
        if suggestion not in found and norm(suggestion)!=norm(query):found.append(suggestion)
    return found[:5]
