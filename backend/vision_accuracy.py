"""Reproducible local BioCLIP gallery check, using recent licensed iNaturalist photos.

No GPT or voice sessions are opened. Photos stay under ignored out/. Run stages
prepare, infer, report (or all). The fixed species set is a convenience sample,
not a representative estimate of field identification accuracy.
"""
import argparse
import base64
from collections import Counter, defaultdict
import csv
import hashlib
import html
import io
import json
from pathlib import Path
import random
import statistics
import subprocess
import time

from PIL import Image, ImageDraw, ImageFont, ImageOps
import requests

ROOT = Path(__file__).resolve().parents[1]
SPECIES = {
    'Insecta': ['Danaus plexippus', 'Limenitis archippus', 'Papilio glaucus',
                'Vanessa atalanta', 'Apis mellifera', 'Bombus terrestris',
                'Harmonia axyridis', 'Anax junius'],
    'Plantae': ['Acer saccharum', 'Acer rubrum', 'Quercus alba', 'Pinus strobus',
                'Asclepias syriaca', 'Solidago canadensis', 'Trifolium pratense',
                'Leucanthemum vulgare'],
    'Aves': ['Dryobates pubescens', 'Leuconotopicus villosus', 'Anas platyrhynchos',
             'Cardinalis cardinalis', 'Turdus migratorius', 'Melospiza melodia',
             'Cyanocitta cristata', 'Buteo jamaicensis'],
    'Mammalia': ['Vulpes vulpes', 'Canis latrans', 'Ursus americanus',
                 'Procyon lotor', 'Sciurus carolinensis', 'Odocoileus virginianus',
                 'Tamias striatus', 'Sylvilagus floridanus'],
}
HOLDOUT_SPECIES = {
    'Insecta':['Pieris rapae','Vanessa cardui','Bombus impatiens','Libellula luctuosa'],
    'Plantae':['Betula papyrifera','Acer platanoides','Quercus rubra','Helianthus annuus'],
    'Aves':['Poecile atricapillus','Agelaius phoeniceus','Spinus tristis','Passer domesticus'],
    'Mammalia':['Mephitis mephitis','Marmota monax','Castor canadensis','Lepus europaeus'],
}
LICENSES = {'cc0', 'cc-by', 'cc-by-sa', 'cc-by-nc'}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def normal(name):
    return ' '.join(name.replace('_', ' ').split()).casefold()


class Acquisition:
    def __init__(self, output):
        self.output = output
        self.http = requests.Session()
        self.http.headers['User-Agent'] = 'FieldCompanion-local-accuracy-evaluation/1.0'
        self.last_api = 0

    def api(self, path, **params):
        key = hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()[:20]
        cached = self.output/'api-cache'/(key+'.json')
        if cached.exists():
            return load(cached)
        for attempt in range(3):
            time.sleep(max(0, 1.1-(time.monotonic()-self.last_api)))
            self.last_api = time.monotonic()
            response = self.http.get('https://api.inaturalist.org/v1/'+path, params=params, timeout=40)
            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(2*(attempt+1))
                continue
            response.raise_for_status()
            value = response.json()
            dump(cached, value)
            return value
        raise RuntimeError('iNaturalist API failed after retries')

    def photo(self, photo, destination):
        url = photo['url'].replace('/square.', '/large.')
        response = self.http.get(url, timeout=35)
        response.raise_for_status()
        if len(response.content) > 12*1024*1024:
            raise ValueError('oversized image')
        with Image.open(io.BytesIO(response.content)) as source:
            image = ImageOps.exif_transpose(source).convert('RGB')
            source_size = list(image.size)
            if min(image.size) < 200:
                raise ValueError('source shorter side below 200 px')
            image.thumbnail((1920,1920))
            image.save(destination, 'JPEG', quality=90)
        return url, source_size, hashlib.sha256(destination.read_bytes()).hexdigest()


def prepare(args):
    manifest = args.out/'manifest.json'
    if manifest.exists():
        data = load(manifest)
        if (data['per_species'] != args.per_species or data['since'] != args.since
                or data['species_set'] != SPECIES):
            raise ValueError('Use a fresh output folder when sampling settings change')
    else:
        data = {'since':args.since, 'per_species':args.per_species, 'seed':20261025,
                'selection':'Seeded shuffle of latest 50 research-grade observations per fixed species; distinct observers per species; first licensed photo; no prediction-based curation.',
                'species_set':SPECIES, 'images':[], 'acquisition_exclusions':[]}
    (args.out/'images').mkdir(parents=True, exist_ok=True)
    client = Acquisition(args.out)
    for group, species_set in SPECIES.items():
        for species in species_set:
            present = [x for x in data['images'] if x['requested_species']==species]
            if len(present) == args.per_species:
                continue
            taxa = client.api('taxa/autocomplete', q=species, per_page=10, locale='en')['results']
            exact = [x for x in taxa if normal(x['name'])==normal(species) and x['rank']=='species']
            # Accept an explicitly matched scientific synonym returned by iNaturalist.
            if not exact:
                exact = [x for x in taxa if x['rank']=='species' and normal((x.get('matched_term') or ''))==normal(species)]
            if not exact:
                raise ValueError('No exact accepted species or scientific synonym: '+species)
            taxon = exact[0]
            observations = client.api('observations', taxon_id=taxon['id'], quality_grade='research',
                photos='true', photo_license=','.join(sorted(LICENSES)), d1=args.since,
                created_d1=args.since, order_by='id', order='desc', per_page=50)['results']
            ordered = list(observations)
            seed = 20261025 + int.from_bytes(hashlib.sha256(species.encode()).digest()[:4])
            random.Random(seed).shuffle(ordered)
            observers = {x['observer_id'] for x in present}
            photo_ids = {x['photo_id'] for x in data['images']}
            hashes = {x['sha256'] for x in data['images']}
            for obs in ordered:
                if len(present) >= args.per_species:
                    break
                actual = obs['taxon']
                if (obs['user']['id'] in observers or obs.get('captive') is True
                        or obs.get('quality_grade') != 'research'
                        or (obs.get('observed_on') or '') < args.since
                        or obs['created_at'][:10] < args.since
                        or taxon['id'] not in [actual['id'], *actual.get('ancestor_ids',[])]) :
                    continue
                photos = [p for p in obs['photos'] if p.get('license_code') in LICENSES and p['id'] not in photo_ids]
                if not photos:
                    continue
                photo = photos[0]
                image_id = f'{group}-{taxon["id"]}-{obs["id"]}'
                destination = args.out/'images'/(image_id+'.jpg')
                try:
                    url, source_size, digest = client.photo(photo, destination)
                except (requests.RequestException, OSError, ValueError) as exc:
                    data['acquisition_exclusions'].append({'observation_id':obs['id'],'reason':str(exc)[:200]})
                    continue
                if digest in hashes:
                    data['acquisition_exclusions'].append({'observation_id':obs['id'],'reason':'duplicate image hash'})
                    continue
                row = {'id':image_id,'group':group,'species':taxon['name'],
                    'requested_species':species, 'taxon_id':taxon['id'],
                    'common_name':taxon.get('preferred_common_name') or taxon['name'],
                    'observation_taxon':actual['name'],'observation_id':obs['id'],
                    'observer_id':obs['user']['id'],'observed_on':obs['observed_on'],
                    'created_at':obs['created_at'],'photo_id':photo['id'],
                    'source_url':url,'source_page':f'https://www.inaturalist.org/observations/{obs["id"]}',
                    'license':photo['license_code'],'attribution':photo.get('attribution',''),
                    'source_dimensions':source_size,'image':destination.relative_to(args.out).as_posix(),
                    'sha256':digest}
                data['images'].append(row);present.append(row)
                observers.add(row['observer_id']);photo_ids.add(row['photo_id']);hashes.add(digest)
                dump(manifest,data)
            if len(present) != args.per_species:
                raise ValueError(f'Insufficient qualifying observations for {species}: {len(present)}')
            print(f'Prepared {group}: {species}: {len(present)} photos; total={len(data["images"])}',flush=True)
    expected = sum(len(v) for v in SPECIES.values())*args.per_species
    assert len(data['images']) == expected
    assert len({x['photo_id'] for x in data['images']}) == expected
    assert len({x['sha256'] for x in data['images']}) == expected
    return data


def infer(args):
    data = load(args.out/'manifest.json')
    health = requests.get(args.url+'/health',timeout=10).json()
    if not health.get('loaded') or health.get('model')!='imageomics/bioclip-2':
        raise ValueError('Expected running BioCLIP 2')
    results_path = args.out/'predictions.jsonl'
    existing = [json.loads(x) for x in results_path.read_text().splitlines()] if results_path.exists() else []
    done = {(x['id'],x['mode']) for x in existing}
    shortlist_path = args.out/'bird-allowlist.json'
    if not shortlist_path.exists():
        key_path = ROOT/'out/livekit/birdnet-worker.key'
        if not key_path.exists():
            raise ValueError('Current local BirdNET worker key file is required for the secondary bird shortlist check')
        response = requests.get('http://127.0.0.1:8811/allowed',params={'lat':43.66,'lng':-79.39},
            headers={'x-field-worker':key_path.read_text().strip()},timeout=90)
        response.raise_for_status()
        species = response.json()['species']
        if len(species)>512:
            raise ValueError(f'Production allowlist ({len(species)}) exceeds vision endpoint limit (512)')
        dump(shortlist_path,{'lat':43.66,'lng':-79.39,'source':'Actual current BirdNET worker /allowed; no benchmark answers inserted','species':species})
    shortlist = load(shortlist_path)['species']
    dump(args.out/'service.json',{'health':health,'bird_allowlist_size':len(shortlist),'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()})
    for image in data['images']:
        image_bytes = (args.out/image['image']).read_bytes()
        assert hashlib.sha256(image_bytes).hexdigest()==image['sha256']
        for mode in (['open','local_bird'] if image['group']=='Aves' else ['open']):
            if (image['id'],mode) in done:
                continue
            payload = {'jpeg_b64':base64.b64encode(image_bytes).decode(),'crop':None,
                       'candidates':shortlist if mode=='local_bird' else None,'k':5}
            start=time.perf_counter()
            try:
                response=requests.post(args.url+'/classify',json=payload,timeout=90)
                response.raise_for_status();value=response.json()
                candidates=value['candidates']
                assert candidates and all(0<=x['p']<=1 for x in candidates)
                row={'id':image['id'],'mode':mode,'roundtrip_ms':round((time.perf_counter()-start)*1000,2),**value}
            except (requests.RequestException,ValueError,KeyError,AssertionError) as exc:
                row={'id':image['id'],'mode':mode,'error':str(exc)[:300]}
            with results_path.open('a',encoding='utf-8') as output:
                output.write(json.dumps(row)+'\n')
            print(f'{mode} {image["species"]} => {(row.get("candidates") or [{}])[0].get("sci",row.get("error"))}',flush=True)


def taxonomy(args, wanted):
    paths=list((ROOT/'out/hf-cache/hub/datasets--imageomics--TreeOfLife-200M/snapshots').glob('*/embeddings/txt_emb_species.json'))
    if not paths:
        raise ValueError('Model taxonomy cache was not found')
    paths.sort(key=lambda p:p.stat().st_mtime,reverse=True)
    rows=load(paths[0]); selected={}
    for ranks,common in rows:
        name=normal(' '.join(ranks[-2:]))
        if name in wanted:
            # Some labels have duplicate incomplete lineages: prefer populated ranks.
            old=selected.get(name)
            if old is None or sum(bool(v) for v in ranks)>sum(bool(v) for v in old['ranks']):
                selected[name]={'ranks':ranks,'common_name':common}
    dump(args.out/'taxonomy.json',{'source':str(paths[0].relative_to(ROOT)),'labels':selected})
    return selected


def pct(num,den):
    return round(100*num/den,1) if den else None


def metrics(rows):
    accepted=[r for r in rows if not r['uncertain'] and not r.get('error')]
    return {'n':len(rows),'errors':sum(bool(r.get('error')) for r in rows),
        'top1_correct':sum(r['top1_correct'] for r in rows),'top1_percent':pct(sum(r['top1_correct'] for r in rows),len(rows)),
        'top3_correct':sum(r['top3_correct'] for r in rows),'top3_percent':pct(sum(r['top3_correct'] for r in rows),len(rows)),
        'genus_percent':pct(sum(r['genus_correct'] for r in rows),len(rows)),
        'group_percent':pct(sum(r['group_correct'] for r in rows),len(rows)),
        'accepted':len(accepted),'accepted_correct':sum(r['top1_correct'] for r in accepted),
        'accepted_accuracy_percent':pct(sum(r['top1_correct'] for r in accepted),len(accepted)),
        'wrong_accepted':sum(not r['top1_correct'] for r in accepted),
        'median_ms':round(statistics.median(r['roundtrip_ms'] for r in rows if not r.get('error')),1)}


def report(args):
    from server import Detection,fuse
    manifest=load(args.out/'manifest.json')
    predictions=[json.loads(x) for x in (args.out/'predictions.jsonl').read_text().splitlines()]
    wanted={normal(x['species']) for x in manifest['images']}
    wanted.update(normal(c['sci']) for row in predictions for c in row.get('candidates',[]))
    labels=taxonomy(args,wanted)
    indexed={(r['id'],r['mode']):r for r in predictions}
    review_path=args.out/'input-review.json'
    review=load(review_path) if review_path.exists() else {'trace_only':{}}
    rows=[]
    for image in manifest['images']:
        for mode in (['open','local_bird'] if image['group']=='Aves' else ['open']):
            value=indexed[(image['id'],mode)]
            candidates=value.get('candidates',[])
            actual=normal(image['species'])
            guess=normal(candidates[0]['sci']) if candidates else ''
            lineage=labels.get(guess,{}).get('ranks',[])
            guessed_group='Plantae' if lineage and lineage[0]=='Plantae' else lineage[2] if len(lineage)>2 else 'Unknown'
            _, uncertain=fuse([Detection('eval','image','',candidates)]) if candidates else ([],True)
            row={**image,'mode':mode,'predicted':candidates[0]['sci'] if candidates else '',
                'score':candidates[0]['p'] if candidates else 0,
                'top1_correct':guess==actual,
                'top3_correct':actual in [normal(x['sci']) for x in candidates[:3]],
                'genus_correct':guess.split(' ')[0]==actual.split(' ')[0],
                'group_correct':guessed_group==image['group'],'predicted_group':guessed_group,
                'uncertain':bool(uncertain),'in_model_vocabulary':actual in labels,
                'trace_only':image['id'] in review['trace_only'],
                'roundtrip_ms':value.get('roundtrip_ms',0),'candidates':candidates}
            if value.get('error'):row['error']=value['error']
            rows.append(row)
    result={'method':manifest['selection'],'service':load(args.out/'service.json'),
        'primary_open':metrics([r for r in rows if r['mode']=='open']),
        'by_group':{group:metrics([r for r in rows if r['mode']=='open' and r['group']==group]) for group in SPECIES},
        'secondary_local_birds':metrics([r for r in rows if r['mode']=='local_bird']),
        'excluding_trace_only':metrics([r for r in rows if r['mode']=='open' and not r['trace_only']]),
        'mammals_excluding_trace_only':metrics([r for r in rows if r['mode']=='open' and r['group']=='Mammalia' and not r['trace_only']]),
        'input_review':review,
        'per_species':{name:metrics([r for r in rows if r['mode']=='open' and r['species']==name]) for name in sorted({x['species'] for x in rows})},
        'missing_vocabulary':sorted({r['species'] for r in rows if not r['in_model_vocabulary']}),
        'limits':['Convenience sample of 32 selected common species, three photos each; not field-population accuracy.',
                  'Research-grade community labels are treated as reference; no expert relabeling.',
                  'Recent upload/observation dates reduce but do not prove absence of training-image duplication.',
                  'Global photos; Toronto shortlist is secondary and may exclude out-of-range birds.',
                  'Still-image identification only; does not test camera framing, focus, speech, GPT or BirdNET audio.',
                  'Softmax scores are rankings, not calibrated accuracy probabilities.'],
        'rows':rows}
    dump(args.out/'results.json',result)
    with (args.out/'results.csv').open('w',newline='',encoding='utf-8-sig') as file:
        columns=['id','group','species','mode','predicted','top1_correct','top3_correct','genus_correct','group_correct','score','uncertain','trace_only','roundtrip_ms','source_page','license','attribution']
        writer=csv.DictWriter(file,fieldnames=columns,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    gallery(args,rows)
    lines=['# Local four-group BioCLIP 2 gallery evaluation','',
        f'Images: {len(manifest["images"])}; observation/upload dates since {manifest["since"]}.',
        'Open mode receives only the JPEG: no species, group, location, filename or reference labels.','',
        '| Group | n | Top-1 species | Top-3 species | Genus | Broad group | Accepted / correct | Wrong accepted | Median ms |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for group,m in [*result['by_group'].items(),('All open',result['primary_open']),('Mammals, excluding trace-only photos',result['mammals_excluding_trace_only']),('All open, excluding trace-only photos',result['excluding_trace_only']),('Toronto shortlist (birds only)',result['secondary_local_birds'])]:
        lines.append(f'| {group} | {m["n"]} | {m["top1_percent"]}% | {m["top3_percent"]}% | {m["genus_percent"]}% | {m["group_percent"]}% | {m["accepted"]} / {m["accepted_correct"]} | {m["wrong_accepted"]} | {m["median_ms"]} |')
    lines+=['','Accepted = existing application fuse/uncertainty logic; confidence is not calibrated.','',
        '## Limitations','',*['- '+x for x in result['limits']],'','## Wrong top-1 predictions','']
    for row in rows:
        if row['mode']=='open' and not row['top1_correct']:
            lines.append(f'- [{row["species"]}, observation {row["observation_id"]}]({row["source_page"]}) -> {row["predicted"]}; p={row["score"]:.3f}, uncertain={row["uncertain"]}.')
    (args.out/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('primary_open','by_group','secondary_local_birds','missing_vocabulary')},indent=2),flush=True)


def gallery(args,rows):
    primary=[r for r in rows if r['mode']=='open']
    style='body{font:16px system-ui;margin:24px;background:#eef2ef;color:#172820}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:18px}article{background:white;padding:12px;border:3px solid #83b08c;border-radius:12px}article.wrong{border-color:#cf5959}img{width:100%;height:220px;object-fit:contain;background:#172820}p{margin:8px 0}small{display:block;font-size:12px;color:#555}'
    cards=[]
    for r in primary:
        esc=lambda s:html.escape(str(s),quote=True)
        cards.append(f'<article class="{"wrong" if not r["top1_correct"] else "correct"}"><img loading="lazy" src="{esc(r["image"])}"><p><b>Reference: {esc(r["species"])}</b></p><p>Top 1: {esc(r["predicted"])}</p><p>{"Correct" if r["top1_correct"] else "Wrong"} · p={r["score"]:.3f} · {"Needs more evidence" if r["uncertain"] else "Accepted"}</p><a href="{esc(r["source_page"])}">Source observation</a><small>{esc(r["attribution"])}</small></article>')
    (args.out/'gallery.html').write_text(f'<!doctype html><html lang="en"><meta charset="utf-8"><title>BioCLIP evaluation gallery</title><style>{style}</style><h1>96-photo BioCLIP 2 evaluation</h1><p>Green: correct species. Red: wrong first choice. Model score is not calibrated confidence.</p><main>{"".join(cards)}</main></html>',encoding='utf-8')
    try:font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',19)
    except OSError:font=ImageFont.load_default()
    for group in SPECIES:
        selected=[r for r in primary if r['group']==group]
        sheet=Image.new('RGB',(1200,320*((len(selected)+3)//4)), '#eef2ef')
        draw=ImageDraw.Draw(sheet)
        for index,r in enumerate(selected):
            x=(index%4)*300;y=(index//4)*320
            with Image.open(args.out/r['image']) as image:
                thumb=ImageOps.contain(image,(286,235));sheet.paste(thumb,(x+(300-thumb.width)//2,y))
            color='#146b32' if r['top1_correct'] else '#b62020'
            draw.text((x+5,y+239),r['species'],fill='#172820',font=font)
            draw.text((x+5,y+263),'-> '+r['predicted'],fill=color,font=font)
            draw.text((x+5,y+288),'CORRECT' if r['top1_correct'] else 'WRONG',fill=color,font=font)
        sheet.save(args.out/f'contact-{group}.jpg',quality=88)


def main():
    global SPECIES
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',choices=['prepare','infer','report','all'],default='all')
    parser.add_argument('--out',type=Path,default=ROOT/'out/vision-accuracy-20261025')
    parser.add_argument('--per-species',type=int,default=3)
    parser.add_argument('--since',default='2026-01-01')
    parser.add_argument('--url',default='http://127.0.0.1:8795')
    parser.add_argument('--cohort',choices=['original','holdout'],default='original')
    args=parser.parse_args()
    if args.cohort=='holdout':SPECIES=HOLDOUT_SPECIES
    if not 1<=args.per_species<=10:parser.error('per-species must be 1..10')
    args.out.mkdir(parents=True,exist_ok=True)
    if args.stage in ('prepare','all'):prepare(args)
    if args.stage in ('infer','all'):infer(args)
    if args.stage in ('report','all'):report(args)


if __name__=='__main__':
    main()
