"""Paired category-assisted vs unrestricted image evaluation and wrong-target controls.

The supplied category simulates explicit user intent; it is not a blind-image
accuracy claim. Original/open rankings come from the same full image scores.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import time

import requests
from vision_policy import assess_visual

ROOT=Path(__file__).resolve().parents[1]


def load(p):return json.loads(p.read_text(encoding='utf-8-sig'))


def save(p,value):
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')


def stats(rows):
    n=len(rows)
    result={'n':n}
    for mode in ('open','scoped'):
        good=sum(r[mode+'_correct'] for r in rows)
        accepted=[r for r in rows if r[mode+'_assessment']['status']=='supported']
        result[mode]={'top1_correct':good,'top1_percent':round(100*good/n,1) if n else None,
            'top3_correct':sum(r[mode+'_top3'] for r in rows),'accepted':len(accepted),
            'wrong_accepted':sum(not r[mode+'_correct'] for r in accepted),
            'accepted_accuracy_percent':round(100*sum(r[mode+'_correct'] for r in accepted)/len(accepted),1) if accepted else None}
    result['category_conflicts']=sum(r['scoped_assessment']['category_conflict'] for r in rows)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8795')
    parser.add_argument('--out',type=Path,default=ROOT/'out/vision-category-fix')
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+150
    while True:
        try:
            health=requests.get(args.url+'/health',timeout=3).json()
            if 'target_group_scope' in health.get('capabilities',[]):break
        except requests.RequestException:pass
        if time.monotonic()>deadline:raise TimeoutError('Scoped vision service did not become ready')
        time.sleep(2)
    sources={'regression':ROOT/'out/vision-accuracy-20261025','holdout':args.out/'holdout'}
    manifests={key:load(path/'manifest.json') for key,path in sources.items()}
    original_names={r['species'] for r in manifests['regression']['images']}
    new_names={r['species'] for r in manifests['holdout']['images']}
    assert not original_names & new_names
    assert len(manifests['regression']['images'])==96 and len(manifests['holdout']['images'])==48
    output=args.out/'paired.jsonl'
    existing=[json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []
    done={(r['cohort'],r['id'],r['target_group']) for r in existing}
    jobs=[]
    for cohort,manifest in manifests.items():
        for image in manifest['images']:
            jobs.append((cohort,image,image['group']))
    # Freeze these controls by manifest order, before looking at any predictions.
    for group in ('Insecta','Plantae','Aves','Mammalia'):
        for image in [r for r in manifests['holdout']['images'] if r['group']==group][:2]:
            jobs.append(('wrong_target',image,'Insecta' if group=='Plantae' else 'Plantae'))
        for cohort in ('regression','holdout'):
            image=next(r for r in manifests[cohort]['images'] if r['group']==group)
            jobs.append(('unknown_target_'+cohort,image,None))
    save(args.out/'evaluation-plan.json',{'assumption':'Correct broad category is supplied as explicit user intent in assisted cases. Unknown and deliberately wrong categories are separate controls.',
        'jobs':[{'cohort':c,'id':im['id'],'target_group':g} for c,im,g in jobs],
        'service':health,'source_sha256':{name:hashlib.sha256((ROOT/'backend'/name).read_bytes()).hexdigest()
            for name in ('vision_service.py','vision_policy.py','server.py')}})
    for cohort,image,group in jobs:
        if (cohort,image['id'],group) in done:continue
        source=sources['regression'] if cohort.endswith('regression') or cohort=='regression' else sources['holdout']
        data=(source/image['image']).read_bytes()
        assert hashlib.sha256(data).hexdigest()==image['sha256']
        payload={'jpeg_b64':base64.b64encode(data).decode(),'k':5,'target_group':group}
        started=time.perf_counter()
        response=requests.post(args.url+'/classify',json=payload,timeout=90)
        response.raise_for_status();result=response.json()
        open_candidates=result['open_top']; scoped=result['candidates']
        truth=image['species'].casefold()
        baseline=assess_visual({'candidates':open_candidates,'open_top':open_candidates})
        assessment=assess_visual(result,group)
        row={'cohort':cohort,'id':image['id'],'species':image['species'],'group':image['group'],
            'target_group':group,'open_correct':open_candidates[0]['sci'].casefold()==truth,
            'scoped_correct':bool(scoped) and scoped[0]['sci'].casefold()==truth,
            'open_top3':any(c['sci'].casefold()==truth for c in open_candidates[:3]),
            'scoped_top3':any(c['sci'].casefold()==truth for c in scoped[:3]),
            'open_assessment':baseline,'scoped_assessment':assessment,
            'roundtrip_ms':round(1000*(time.perf_counter()-started),1),'result':result}
        if group:
            assert all(c['group']==group for c in scoped)
            assert all(c['global_p']<=c['p']+1e-5 for c in scoped)
        if not group:
            assert [c['sci'] for c in scoped[:3]]==[c['sci'] for c in open_candidates]
        with output.open('a',encoding='utf-8') as stream:stream.write(json.dumps(row)+'\n')
        print(cohort,image['species'],'=>',scoped[0]['sci'] if scoped else '(no candidate)',assessment['status'],flush=True)
    rows=[json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows)==len(jobs)==160
    summary={cohort:{'all':stats([r for r in rows if r['cohort']==cohort]),
        'by_group':{group:stats([r for r in rows if r['cohort']==cohort and r['group']==group])
            for group in ('Insecta','Plantae','Aves','Mammalia')}} for cohort in ('regression','holdout')}
    wrong=[r for r in rows if r['cohort']=='wrong_target']
    summary['wrong_target_controls']={'n':len(wrong),'clarified_or_uncertain':sum(r['scoped_assessment']['status']!='supported' for r in wrong),
        'wrongly_accepted':sum(r['scoped_assessment']['status']=='supported' for r in wrong)}
    unknown=[r for r in rows if r['cohort'].startswith('unknown_target_')]
    summary['unknown_target_controls']={'n':len(unknown),'unrestricted_rankings_preserved':all([c['sci'] for c in r['result']['candidates'][:3]]==[c['sci'] for c in r['result']['open_top']] for r in unknown)}
    summary['assumptions']=['Category-assisted accuracy assumes the user supplied the correct broad category.',
        'Unspecified targets remain unrestricted; this change does not establish reliable automatic subject detection.',
        'Holdout species and observations are disjoint from the original 96-photo convenience sample.',
        'No confidence thresholds were tuned on either dataset; existing .5/.2 visual thresholds are retained.']
    save(args.out/'summary.json',summary)
    print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__':main()
