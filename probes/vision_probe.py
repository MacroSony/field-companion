#!/usr/bin/env python3
"""Standalone BioCLIP2 probe. Writes artifacts only to --out and report to --report.
Run in the vision environment; optionally set HF_HOME and TMPDIR.
"""
import argparse
import concurrent.futures as cf
import io
import json
import math
import os
from pathlib import Path
import statistics
import time
import urllib.parse
import urllib.request

SPECIES = [
    ('Northern Cardinal', 'Cardinalis cardinalis'), ('Blue Jay', 'Cyanocitta cristata'),
    ('American Robin', 'Turdus migratorius'), ('Black-capped Chickadee', 'Poecile atricapillus'),
    ('Downy Woodpecker', 'Dryobates pubescens'), ('Dark-eyed Junco', 'Junco hyemalis'),
    ('Mallard', 'Anas platyrhynchos'), ('Red-winged Blackbird', 'Agelaius phoeniceus'),
    ('House Sparrow', 'Passer domesticus'), ('Song Sparrow', 'Melospiza melodia')]


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2))


def fetch(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'FieldCompanionVisionFeasibility/0.1'})
            with urllib.request.urlopen(req, timeout=40) as response:
                return response.read()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def api(endpoint, **params):
    return json.loads(fetch('https://api.inaturalist.org/v1/' + endpoint + '?' + urllib.parse.urlencode(params)))


def prepare(out, report):
    from PIL import Image, ImageOps, ImageFilter
    import numpy as np
    out.mkdir(parents=True, exist_ok=True)
    candidates_path = out / 'candidates.json'
    if not candidates_path.exists():
        local = api('observations/species_counts', lat=43.66, lng=-79.39, radius=15,
                    iconic_taxa='Aves', per_page=100, quality_grade='research')
        save(out / 'species_counts-response.json', local)
        candidates = []
        for row in local['results']:
            t = row['taxon']
            if t.get('rank') == 'species':
                candidates.append({'scientific': t['name'], 'common': t.get('preferred_common_name', t['name']), 'count': row['count']})
            if len(candidates) == 60:
                break
        for common, scientific in SPECIES:
            if scientific not in [x['scientific'] for x in candidates]:
                candidates.append({'scientific': scientific, 'common': common, 'count': 1})
        save(candidates_path, candidates)
    if (out / 'manifest.json').exists():
        return

    def get_species(pair):
        common, scientific = pair
        response = api('observations', taxon_name=scientific, quality_grade='research', photos='true',
                       per_page=10, order_by='random')
        slug = scientific.replace(' ', '_')
        save(out / (slug + '-observations.json'), response)
        rows = []
        seen = set()
        for obs in response['results']:
            if obs['id'] in seen or obs['taxon']['name'] != scientific or not obs.get('photos'):
                continue
            photo = obs['photos'][0]
            url = photo['url'].replace('/square.', '/large.')
            path = out / f"{slug}-{obs['id']}-source.jpg"
            try:
                raw = fetch(url)
                image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert('RGB')
                if min(image.size) < 150:
                    continue
                # Store original bytes; Pillow opens by signature, not suffix.
                path.write_bytes(raw)
            except Exception as exc:
                print('download skip', url, str(exc), flush=True)
                continue
            seen.add(obs['id'])
            rows.append({'species': scientific, 'common': common, 'observation_id': obs['id'],
                         'photo_id': photo['id'], 'source': str(path), 'url': url,
                         'attribution': photo.get('attribution'), 'license_code': photo.get('license_code'),
                         'observation_url': obs.get('uri'), 'source_size': image.size})
            if len(rows) == 3:
                break
        print('downloaded', common, len(rows), flush=True)
        return rows

    sources = []
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        for rows in pool.map(get_species, SPECIES):
            sources.extend(rows)
    save(out / 'sources.json', sources)
    manifest = []
    for i, source in enumerate(sources):
        image = ImageOps.exif_transpose(Image.open(source['source'])).convert('RGB')
        rng = np.random.default_rng(2187 + i)
        noise = np.clip(rng.normal([91, 108, 67], [28, 28, 22], (45, 80, 3)), 0, 255).astype('uint8')
        bg = Image.fromarray(noise).resize((1280, 720), Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(15))
        for condition, fraction in [('100', 1.0), ('25', .25), ('6', .06)]:
            frame = bg.copy()
            pasted = ImageOps.contain(image, (round(1280 * math.sqrt(fraction)), round(720 * math.sqrt(fraction))), Image.Resampling.LANCZOS)
            frame.paste(pasted, ((1280-pasted.width)//2, (720-pasted.height)//2))
            path = out / f'{i:02d}-{condition}.jpg'
            frame.save(path, quality=75)
            row = dict(source, condition=condition, frame=str(path), source_area=pasted.width*pasted.height/(1280*720))
            manifest.append(row)
            if condition == '6':
                # Zoom an already compressed frame; no restoration of discarded detail.
                compressed = Image.open(path)
                zoom = compressed.crop((427, 240, 853, 480)).resize((1280, 720), Image.Resampling.BICUBIC)
                zpath = out / f'{i:02d}-6zoom.jpg'
                zoom.save(zpath, quality=75)
                manifest.append(dict(row, condition='6zoom', frame=str(zpath), source_area=row['source_area']*9))
    save(out / 'manifest.json', manifest)
    with report.open('a') as f:
        f.write(f'\n### Download checkpoint\n\nDownloaded {len(sources)} photos from distinct observations; generated {len(manifest)} test frames. '
                f'Candidate list: {len(json.loads(candidates_path.read_text()))} species. Provenance: `sources.json`, `*-observations.json`, `candidates.json`, `species_counts-response.json`.\n')


def run(out, report, threads):
    import numpy as np
    import torch
    from bioclip import TreeOfLifeClassifier, CustomLabelsClassifier, Rank
    from bioclip.predict import create_bioclip_tokenizer
    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    # Avoid CPU compile cost and compiler artifact writes. All math remains the library's.
    torch.compile = lambda model, *args, **kwargs: model
    start = time.perf_counter()
    tree = TreeOfLifeClassifier(device='cpu')
    load_sec = time.perf_counter() - start
    print('tree loaded', load_sec, 'seconds', flush=True)
    candidates = json.loads((out / 'candidates.json').read_text())
    # Reuse loaded encoder, but use real CustomLabelsClassifier methods and 80-template head.
    custom = CustomLabelsClassifier.__new__(CustomLabelsClassifier)
    torch.nn.Module.__init__(custom)
    custom.device, custom.model_str, custom.pretrained_str = tree.device, tree.model_str, tree.pretrained_str
    custom.model, custom.preprocess, custom.recorder = tree.model, tree.preprocess, None
    custom.tokenizer = create_bioclip_tokenizer(tree.model_str)
    custom.classes = [f"{x['common']} ({x['scientific']})" for x in candidates]
    start = time.perf_counter()
    custom.txt_embeddings = custom._get_txt_embeddings(custom.classes)
    head_sec = time.perf_counter() - start
    print('custom head ready', head_sec, 'seconds', flush=True)
    manifest = json.loads((out / 'manifest.json').read_text())
    metadata = {'torch': torch.__version__, 'threads': threads, 'model': tree.model_str,
                'tree_load_s': load_sec, 'custom_head_s': head_sec,
                'open_classes': len(tree.txt_names), 'restricted_classes': len(candidates), 'compile': False}
    save(out / 'runtime.json', metadata)
    with report.open('a') as f:
        f.write(f'\n### Model checkpoint\n\nLoaded `{tree.model_str}` in {load_sec:.1f}s; custom text head in {head_sec:.1f}s. '
                f'Open labels: {len(tree.txt_names):,}; local labels: {len(candidates)}; PyTorch CPU threads: {threads}.\n')
    tree.predict(manifest[0]['frame'], rank=Rank.SPECIES, k=3)
    custom.predict(manifest[0]['frame'])
    results = []
    for i, row in enumerate(manifest):
        start = time.perf_counter()
        open_pred = tree.predict(row['frame'], rank=Rank.SPECIES, k=3)
        open_s = time.perf_counter() - start
        start = time.perf_counter()
        restricted = custom.predict(row['frame'])
        local_s = time.perf_counter() - start
        scores = {x['classification']: x['score'] for x in restricted}
        probs = [scores[x] for x in custom.classes]
        top = np.argsort(probs)[::-1]
        local_names = [candidates[j]['scientific'] for j in top[:3]]
        p1, p2 = probs[top[0]], probs[top[1]]
        result = dict(row, open_top=[x['species'] for x in open_pred],
                      open_scores=[x['score'] for x in open_pred], restricted_top=local_names,
                      probabilities=probs, p1=p1, margin=p1-p2, uncertain=bool(p1 < .5 or p1-p2 < .2),
                      open_s=open_s, restricted_s=local_s)
        results.append(result)
        with (out / 'predictions.jsonl').open('a') as f:
            f.write(json.dumps(result) + '\n')
        if (i+1) % 12 == 0:
            print('classified', i+1, '/', len(manifest), flush=True)
    save(out / 'results.json', results)
    summarize(out, report, results, candidates, metadata)


def summarize(out, report, rows, candidates, metadata):
    import numpy as np
    names = [x['scientific'] for x in candidates]
    prior = np.array([x['count'] for x in candidates], dtype=float)
    prior /= prior.sum()
    lines = ['\n## Completed results\n', f'Inference complete: {len(rows)} frames from {len(set(x["observation_id"] for x in rows))} observations. All percentages below use photos (single frame) or species (fusion) as stated.\n',
             '### Single-frame accuracy\n', '| Nominal source area / zoom | n | Actual source area (median) | Open top-1 | Open top-3 | Local top-1 | Local top-3 |', '|---|---:|---:|---:|---:|---:|---:|']
    pct = lambda a, b: f'{100*a/b:.1f}%' if b else 'n/a'
    for condition in ['100', '25', '6', '6zoom']:
        group = [r for r in rows if r['condition'] == condition]
        n = len(group)
        metrics = [pct(sum(r['species'] in r[key][:k] for r in group), n) for key,k in [('open_top',1),('open_top',3),('restricted_top',1),('restricted_top',3)]]
        lines.append(f'| {condition} | {n} | {statistics.median(r["source_area"] for r in group)*100:.1f}% | ' + ' | '.join(metrics) + ' |')
    lines += ['\n### Restricted uncertainty: p1 < 0.5 OR margin < 0.2\n', '| Condition | Wrong flagged / wrong | Right flagged / right | Accepted accuracy | Coverage |', '|---|---:|---:|---:|---:|']
    for condition in ['100','25','6','6zoom']:
        g = [r for r in rows if r['condition']==condition]
        wrong = [r for r in g if r['restricted_top'][0]!=r['species']]
        right = [r for r in g if r['restricted_top'][0]==r['species']]
        accepted = [r for r in g if not r['uncertain']]
        lines.append(f'| {condition} | {sum(r["uncertain"] for r in wrong)}/{len(wrong)} | {sum(r["uncertain"] for r in right)}/{len(right)} | {pct(sum(r["restricted_top"][0]==r["species"] for r in accepted),len(accepted))} ({len(accepted)} accepted) | {pct(len(accepted),len(g))} |')
    lines += ['\n### Multi-frame fusion (species-level episodes)\n', 'Each episode uses three distinct observations of the same species in manifest order. No selection for correctness. Single = first photo; 2/3 frames = sum(log p). Prior variant adds log(local count) once; missing taxa receive count 1. These are normalized products of scores, not calibrated Bayesian posteriors.\n',
              '| Condition | Species episodes | First frame top-1 | 2 frames | 3 frames | 3 + count prior | 3-frame uncertain |', '|---|---:|---:|---:|---:|---:|---:|']
    fusion_rows = []
    for condition in ['100','25','6','6zoom']:
        episodes=[]
        for common, species in SPECIES:
            g=[r for r in rows if r['condition']==condition and r['species']==species]
            if len(g)!=3:
                continue
            ep={'condition':condition,'species':species,'single':g[0]['restricted_top'][0], 'single_correct':g[0]['restricted_top'][0]==species}
            for count in [2,3]:
                logs=np.log(np.maximum(np.array([r['probabilities'] for r in g[:count]]),1e-30)).sum(axis=0)
                for use_prior in ([False,True] if count==3 else [False]):
                    score=logs + (np.log(prior) if use_prior else 0)
                    p=np.exp(score-score.max()); p/=p.sum(); order=np.argsort(p)[::-1]
                    key=f'{count}' + ('prior' if use_prior else '')
                    ep[key]=names[order[0]]
                    ep[key+'_correct']=names[order[0]]==species
                    ep[key+'_uncertain']=bool(p[order[0]]<.5 or p[order[0]]-p[order[1]]<.2)
            episodes.append(ep)
        fusion_rows.extend(episodes)
        n=len(episodes)
        metrics=[pct(sum(e[k+'_correct'] for e in episodes),n) for k in ['single','2','3','3prior']]
        lines.append(f'| {condition} | {n} | '+ ' | '.join(metrics)+f' | {sum(e["3_uncertain"] for e in episodes)}/{n} |')
    save(out/'fusion.json',fusion_rows)
    lines += ['\n### CPU latency (warm, sequential API calls, includes image decode/preprocess)\n', '| Mode | n | Median | Mean | p95 |', '|---|---:|---:|---:|---:|']
    for key,label in [('open_s','Open TreeOfLife species top-3'),('restricted_s','CustomLabels all scores')]:
        values=[r[key] for r in rows]
        lines.append(f'| {label} | {len(values)} | {statistics.median(values)*1000:.0f} ms | {statistics.mean(values)*1000:.0f} ms | {np.percentile(values,95)*1000:.0f} ms |')
    lines += [f'\nCPU threads: {metadata["threads"]}; eager mode; separate warmup excluded. Each mode re-encodes each image; no shared-image-embedding speedup in timings. Model load {metadata["tree_load_s"]:.1f}s; custom head generation {metadata["custom_head_s"]:.1f}s. Head/encoder resident throughout.\n',
              '### Per-species restricted top-1 counts (out of three)\n', '| Species | 100 | 25 | 6 | 6zoom |', '|---|---:|---:|---:|---:|']
    for common,species in SPECIES:
        counts=[sum(r['restricted_top'][0]==species for r in rows if r['species']==species and r['condition']==c) for c in ['100','25','6','6zoom']]
        lines.append('| '+common+' | '+' | '.join(map(str,counts))+' |')
    with report.open('a') as f:
        f.write('\n'.join(lines)+'\n')
    print('report complete',report,flush=True)


def analyze(out, report):
    """Post-run sensitivity, threshold sweeps, and an adaptive look-again loop."""
    import numpy as np
    rows = json.loads((out/'results.json').read_text())
    candidates = json.loads((out/'candidates.json').read_text())
    names = [x['scientific'] for x in candidates]
    pct = lambda a,b: f'{100*a/b:.1f}%' if b else 'n/a'
    lines = ['\n## Sensitivity and look-again policy\n',
             '### Excluding the detached-feather observation\n',
             '| Condition | Live-bird photos | Open top-1 / top-3 | Local top-1 / top-3 |',
             '|---|---:|---:|---:|']
    for condition in ['100','25','6','6zoom']:
        g = [r for r in rows if r['condition']==condition and r['observation_id']!=129914708]
        metrics = [pct(sum(r['species'] in r[key][:k] for r in g),len(g)) for key,k in [('open_top',1),('open_top',3),('restricted_top',1),('restricted_top',3)]]
        lines.append(f'| {condition} | {len(g)} | {metrics[0]} / {metrics[1]} | {metrics[2]} / {metrics[3]} |')
    lines += ['\n### Candidate acceptance thresholds (exploratory, not held-out calibration)\n',
              'Accept only if BOTH p1 ≥ threshold and top-two margin ≥ threshold. All 30 photos retained here.\n',
              '| Condition | p1 / margin minimum | Accepted / total | Accepted accuracy | Wrong accepted |',
              '|---|---:|---:|---:|---:|']
    threshold_rows=[]
    for condition in ['100','25','6','6zoom']:
        g=[r for r in rows if r['condition']==condition]
        for p_min, margin_min in [(.5,.2),(.7,.2),(.8,.3),(.9,.4)]:
            accepted=[r for r in g if r['p1']>=p_min and r['margin']>=margin_min]
            correct=sum(r['restricted_top'][0]==r['species'] for r in accepted)
            threshold_rows.append({'condition':condition,'p_min':p_min,'margin_min':margin_min,'accepted':len(accepted),'correct':correct,'total':len(g)})
            lines.append(f'| {condition} | {p_min} / {margin_min} | {len(accepted)}/{len(g)} | {pct(correct,len(accepted))} | {len(accepted)-correct} |')
    lines += ['\n### Adaptive look-again loop\n',
              'Start with the first observation; if uncertain, request a different observation and add log probabilities, up to three. At the third frame, abstain if still uncertain. No count prior. Confidence thresholds apply to the normalized product, which can become overconfident. Average frames includes abstained episodes.\n',
              '| Condition | p1 / margin minimum | Episodes | Accepted | Correct / accepted | Wrong accepted | Avg frames | Still uncertain after 3 |',
              '|---|---:|---:|---:|---:|---:|---:|---:|']
    loops=[]
    for condition in ['100','25','6','6zoom']:
        for p_min,margin_min in [(.5,.2),(.8,.3)]:
            episodes=[]
            for _,species in SPECIES:
                g=[r for r in rows if r['condition']==condition and r['species']==species]
                if len(g)!=3:
                    continue
                score=np.zeros(len(names))
                for count,r in enumerate(g,1):
                    score+=np.log(np.maximum(r['probabilities'],1e-30))
                    p=np.exp(score-score.max());p/=p.sum();top=np.argsort(p)[::-1]
                    uncertain=bool(p[top[0]]<p_min or p[top[0]]-p[top[1]]<margin_min)
                    if not uncertain or count==3:
                        episodes.append({'species':species,'prediction':names[top[0]],'correct':names[top[0]]==species,'uncertain':uncertain,'frames':count})
                        break
            accepted=[e for e in episodes if not e['uncertain']]
            correct=sum(e['correct'] for e in accepted)
            loops.append({'condition':condition,'p_min':p_min,'margin_min':margin_min,'episodes':episodes})
            lines.append(f'| {condition} | {p_min} / {margin_min} | {len(episodes)} | {len(accepted)} | {correct}/{len(accepted)} ({pct(correct,len(accepted))}) | {len(accepted)-correct} | {statistics.mean(e["frames"] for e in episodes):.1f} | {sum(e["uncertain"] for e in episodes)} |')
    lines += ['\n### Three-frame product: uncertainty on errors\n',
              '| Condition | Wrong flagged / wrong | Right flagged / right | First-frame errors fixed | First-frame correct harmed |',
              '|---|---:|---:|---:|---:|']
    fusion=json.loads((out/'fusion.json').read_text())
    for condition in ['100','25','6','6zoom']:
        g=[e for e in fusion if e['condition']==condition]
        wrong=[e for e in g if not e['3_correct']];right=[e for e in g if e['3_correct']]
        fixed=sum(not e['single_correct'] and e['3_correct'] for e in g)
        harmed=sum(e['single_correct'] and not e['3_correct'] for e in g)
        lines.append(f'| {condition} | {sum(e["3_uncertain"] for e in wrong)}/{len(wrong)} | {sum(e["3_uncertain"] for e in right)}/{len(right)} | {fixed} | {harmed} |')
    save(out/'policy-analysis.json',{'thresholds':threshold_rows,'loops':loops})
    with report.open('a') as f:
        f.write('\n'.join(lines)+'\n')
    print('policy analysis appended',flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,default=Path(__file__).resolve().parents[1] / 'out/vision-imgs')
    parser.add_argument('--report',type=Path,default=Path(__file__).resolve().parents[1] / 'docs/vision-test.md')
    parser.add_argument('--stage',choices=['prepare','run','analyze','all'],default='all')
    parser.add_argument('--threads',type=int,default=4)
    args=parser.parse_args()
    if args.stage in ['prepare','all']:
        prepare(args.out,args.report)
    if args.stage in ['run','all']:
        if (args.out/'predictions.jsonl').exists():
            raise RuntimeError('Refusing to mix runs; move existing predictions.jsonl first')
        run(args.out,args.report,args.threads)
    if args.stage in ['analyze','all']:
        analyze(args.out,args.report)

if __name__=='__main__':
    main()
