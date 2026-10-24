# BioCLIP 2 phone-camera feasibility probe

Status: complete (2026-10-24 01:26 UTC). Written incrementally; approximately 12 minutes wall time including API inspection, downloads, setup, inference, and analysis.

## Bottom line

**Viable as a crop-assisted, local-list suggestion feature; not reliable as unattended distant-bird identification.** At nominal 6% source-photo area, top-1 was **10.0% open / 36.7% local**; center 3× zoom raised this to **76.7% / 86.7%**. Three independent photos raised uncropped local accuracy to **60%** (10 species episodes), but scores became overconfident. Warm CPU medians: **791 ms open / 699 ms local**. Details and caveats below.

## Plan and boundaries

30 research-grade iNaturalist observation photos: three distinct observations for each of ten common Toronto birds. Compare open TreeOfLife species identification with CustomLabelsClassifier over the top ~60 local bird species from iNaturalist species_counts (43.66, -79.39; 15 km), adding test taxa if absent. Simulate nominal 100%, 25%, and 6% source-photo area in 1280×720 JPEG quality 75 frames, then centered 3× zoom of the 6% frame. Preserve source aspect ratio; actual source area can be smaller than the nominal rectangle. A source photo is not a segmented bird: actual bird area is unknown and smaller. Background is smooth green/brown noise, not a real camera scene.

Fuse restricted-class probabilities across independent observations using summed log probabilities, with and without a local-count prior. These are different birds/photographers, not actual adjacent video frames. Uncertainty rule: top-1 probability <0.5 OR top-two margin <0.2. Probabilities are softmax scores, not calibrated confidence.

Artifacts (sources, provenance, synthetic JPEGs, per-image predictions, run logs) are outside the repo in `/data/build/birding-demo/vision-imgs/`. No backend/web changes.

## API inspection

Installed `bioclip.predict.CustomLabelsClassifier(cls_ary=[...], device='cpu')` uses BioCLIP 2 by default, encodes each custom class using 80 text templates, averages normalized text embeddings, and softmaxes scaled image/text similarities. `predict(images, k=None)` returns all class scores. `TreeOfLifeClassifier.predict(images, rank=Rank.SPECIES, k=3)` returns species-level scores. Both use the same encoder and 224×224 preprocessing. Model loading calls `torch.compile`; the probe uses eager CPU execution to avoid compile startup and shares one loaded encoder when constructing the custom-label head with the actual library embedding method. Full per-image API timings will be measured separately after warmup.

## Limitations to retain in interpretation

- iNaturalist images may overlap BioCLIP training data: optimistic accuracy, not held-out validation.
- Only 30 photos, known in-list bird species, no empty frames/nonbirds/out-of-list negatives.
- Synthetic distance changes the entire original photo; no measured bird bounding boxes or physical distance.
- Centered target, artificial background, independent-photo fusion: not a test of detection, tracking, autofocus, motion blur, or correlated video frames.
- Research-grade labels are treated as ground truth; no manual species relabeling.

## Results

Checkpoints below record downloads and model setup; completed tables follow.

### Download checkpoint

Downloaded 30 photos from distinct observations; generated 120 test frames. Candidate list: 60 species. Provenance: `sources.json`, `*-observations.json`, `candidates.json`, `species_counts-response.json`.

### Input review / integrity checkpoint

Integrity checks passed: 30 unique observation IDs, exactly three observations per test species, 120 valid 1280×720 JPEG frames, and 60 unique local candidate taxa. All ten exact scientific labels exist in the open TreeOfLife vocabulary, so no synonym adjustment is needed. All ten were already in the top-60 local list.

A contact-sheet review found genuinely difficult inputs (small/off-center birds, foliage, feeder-camera imagery). One research-grade Downy Woodpecker observation (`129914708`) is **a detached feather, not a visible live bird**. It is retained in the unfiltered primary sample to avoid silently curating the API sample; the live-bird sensitivity analysis below excludes its four derived frames. This also shows that research-grade + photos is not a live-bird filter. Source observations were sampled globally by exact taxon name, not restricted to Toronto; only candidate selection/counts use Toronto geography.

Visual evidence: `/data/build/birding-demo/vision-imgs/source-contact-sheet.jpg` and `distance-example.jpg` (100%, 25%, 6%, 6% + 3× zoom; left-to-right, top-to-bottom). The zoom crops the center 426×240 pixels of the already JPEG-compressed frame, then resizes and recompresses; it cannot recover detail already lost.

### Model checkpoint

Loaded `hf-hub:imageomics/bioclip-2` in 10.2s; custom text head in 330.2s. Open labels: 867,455; local labels: 60; PyTorch CPU threads: 4.

## Completed results

Inference complete: 120 frames from 30 observations. All percentages below use photos (single frame) or species (fusion) as stated.

### Single-frame accuracy

| Nominal source area / zoom | n | Actual source area (median) | Open top-1 | Open top-3 | Local top-1 | Local top-3 |
|---|---:|---:|---:|---:|---:|---:|
| 100 | 30 | 75.0% | 93.3% | 93.3% | 93.3% | 100.0% |
| 25 | 30 | 18.8% | 60.0% | 63.3% | 70.0% | 83.3% |
| 6 | 30 | 4.5% | 10.0% | 13.3% | 36.7% | 40.0% |
| 6zoom | 30 | 40.4% | 76.7% | 83.3% | 86.7% | 96.7% |

### Restricted uncertainty: p1 < 0.5 OR margin < 0.2

| Condition | Wrong flagged / wrong | Right flagged / right | Accepted accuracy | Coverage |
|---|---:|---:|---:|---:|
| 100 | 1/2 | 0/28 | 96.6% (29 accepted) | 96.7% |
| 25 | 6/9 | 1/21 | 87.0% (23 accepted) | 76.7% |
| 6 | 14/19 | 4/11 | 58.3% (12 accepted) | 40.0% |
| 6zoom | 2/4 | 0/26 | 92.9% (28 accepted) | 93.3% |

### Multi-frame fusion (species-level episodes)

Each episode uses three distinct observations of the same species in manifest order. No selection for correctness. Single = first photo; 2/3 frames = sum(log p). Prior variant adds log(local count) once (missing candidates would receive count 1; none were missing here). These are normalized products of scores, not calibrated Bayesian posteriors.

| Condition | Species episodes | First frame top-1 | 2 frames | 3 frames | 3 + count prior | 3-frame uncertain |
|---|---:|---:|---:|---:|---:|---:|
| 100 | 10 | 100.0% | 100.0% | 100.0% | 100.0% | 0/10 |
| 25 | 10 | 80.0% | 90.0% | 90.0% | 90.0% | 0/10 |
| 6 | 10 | 20.0% | 30.0% | 60.0% | 60.0% | 1/10 |
| 6zoom | 10 | 90.0% | 100.0% | 100.0% | 100.0% | 0/10 |

### CPU latency (warm, sequential API calls, includes image decode/preprocess)

| Mode | n | Median | Mean | p95 |
|---|---:|---:|---:|---:|
| Open TreeOfLife species top-3 | 120 | 791 ms | 791 ms | 798 ms |
| CustomLabels all scores | 120 | 699 ms | 699 ms | 705 ms |

CPU threads: 4; eager mode; separate warmup excluded. Each mode re-encodes each image; no shared-image-embedding speedup in timings. Model load 10.2s; custom head generation 330.2s. Head/encoder resident throughout.

### Per-species restricted top-1 counts (out of three)

| Species | 100 | 25 | 6 | 6zoom |
|---|---:|---:|---:|---:|
| Northern Cardinal | 3 | 3 | 2 | 3 |
| Blue Jay | 3 | 3 | 1 | 3 |
| American Robin | 2 | 2 | 2 | 2 |
| Black-capped Chickadee | 3 | 3 | 0 | 3 |
| Downy Woodpecker | 2 | 2 | 1 | 2 |
| Dark-eyed Junco | 3 | 2 | 2 | 3 |
| Mallard | 3 | 2 | 0 | 2 |
| Red-winged Blackbird | 3 | 3 | 2 | 3 |
| House Sparrow | 3 | 1 | 1 | 3 |
| Song Sparrow | 3 | 0 | 0 | 2 |

## Sensitivity and look-again policy

### Excluding the detached-feather observation

| Condition | Live-bird photos | Open top-1 / top-3 | Local top-1 / top-3 |
|---|---:|---:|---:|
| 100 | 29 | 96.6% / 96.6% | 96.6% / 100.0% |
| 25 | 29 | 62.1% / 65.5% | 72.4% / 82.8% |
| 6 | 29 | 10.3% / 13.8% | 37.9% / 41.4% |
| 6zoom | 29 | 79.3% / 86.2% | 89.7% / 96.6% |

### Candidate acceptance thresholds (exploratory, not held-out calibration)

Accept only if BOTH p1 ≥ threshold and top-two margin ≥ threshold. All 30 photos retained here.

| Condition | p1 / margin minimum | Accepted / total | Accepted accuracy | Wrong accepted |
|---|---:|---:|---:|---:|
| 100 | 0.5 / 0.2 | 29/30 | 96.6% | 1 |
| 100 | 0.7 / 0.2 | 27/30 | 100.0% | 0 |
| 100 | 0.8 / 0.3 | 27/30 | 100.0% | 0 |
| 100 | 0.9 / 0.4 | 27/30 | 100.0% | 0 |
| 25 | 0.5 / 0.2 | 23/30 | 87.0% | 3 |
| 25 | 0.7 / 0.2 | 23/30 | 87.0% | 3 |
| 25 | 0.8 / 0.3 | 20/30 | 95.0% | 1 |
| 25 | 0.9 / 0.4 | 19/30 | 100.0% | 0 |
| 6 | 0.5 / 0.2 | 12/30 | 58.3% | 5 |
| 6 | 0.7 / 0.2 | 9/30 | 66.7% | 3 |
| 6 | 0.8 / 0.3 | 6/30 | 83.3% | 1 |
| 6 | 0.9 / 0.4 | 5/30 | 100.0% | 0 |
| 6zoom | 0.5 / 0.2 | 28/30 | 92.9% | 2 |
| 6zoom | 0.7 / 0.2 | 25/30 | 96.0% | 1 |
| 6zoom | 0.8 / 0.3 | 25/30 | 96.0% | 1 |
| 6zoom | 0.9 / 0.4 | 25/30 | 96.0% | 1 |

### Adaptive look-again loop

Start with the first observation; if uncertain, request a different observation and add log probabilities, up to three. At the third frame, abstain if still uncertain. No count prior. Confidence thresholds apply to the normalized product, which can become overconfident. Average frames includes abstained episodes.

| Condition | p1 / margin minimum | Episodes | Accepted | Correct / accepted | Wrong accepted | Avg frames | Still uncertain after 3 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 100 | 0.5 / 0.2 | 10 | 10 | 10/10 (100.0%) | 0 | 1.0 | 0 |
| 100 | 0.8 / 0.3 | 10 | 10 | 10/10 (100.0%) | 0 | 1.0 | 0 |
| 25 | 0.5 / 0.2 | 10 | 10 | 8/10 (80.0%) | 2 | 1.1 | 0 |
| 25 | 0.8 / 0.3 | 10 | 9 | 9/9 (100.0%) | 0 | 1.4 | 1 |
| 6 | 0.5 / 0.2 | 10 | 9 | 3/9 (33.3%) | 6 | 2.0 | 1 |
| 6 | 0.8 / 0.3 | 10 | 7 | 6/7 (85.7%) | 1 | 2.7 | 3 |
| 6zoom | 0.5 / 0.2 | 10 | 10 | 9/10 (90.0%) | 1 | 1.0 | 0 |
| 6zoom | 0.8 / 0.3 | 10 | 10 | 9/10 (90.0%) | 1 | 1.0 | 0 |

### Three-frame product: uncertainty on errors

| Condition | Wrong flagged / wrong | Right flagged / right | First-frame errors fixed | First-frame correct harmed |
|---|---:|---:|---:|---:|
| 100 | 0/0 | 0/10 | 0 | 0 |
| 25 | 0/1 | 0/9 | 1 | 0 |
| 6 | 1/4 | 0/6 | 4 | 0 |
| 6zoom | 0/0 | 0/10 | 1 | 0 |

## Recommendation

1. **Use CustomLabels over the local shortlist, with a user-centered crop/zoom before encoding.** Offer top-three suggestions, not an authoritative identification. At nominal 25%, local top-1 was 70%; at 6% it fell to 36.7%. Excluding the feather changes the 6% result only to 37.9%, while zoom reaches 89.7%. Thus the distant-frame failure is not explained by that anomalous input. The large zoom gain is mainly giving the bird more of the model's 224×224 input, not recovering missing optical detail.
2. **Prototype look-again threshold: p1 < 0.8 OR margin < 0.3**, then ask the user to center/zoom, hold still, or change viewpoint. Treat scores above that threshold as provisional, not calibrated certainty: the 6% case still accepted one wrong answer out of six accepted. At 6% + zoom it accepted 25/30, with 24 correct. Raising to 0.9 / 0.4 did not remove that zoomed-frame error. No tested threshold establishes safe production confidence.
3. **Do not stop based on a high product score alone.** The original 0.5 / 0.2 rule caught 14/19 distant-frame errors but missed five, and flagged 4/11 correct answers. Always collecting three different photos reached 60% at 6%, versus 36.7% over all single photos (20% on the first-photo-per-species baseline). But only 1/4 three-frame mistakes remained uncertain. The adaptive baseline loop stopped early on wrong answers: 3/9 accepted episodes correct at 6%; stricter 0.8 / 0.3 reached 6/7, with three abstentions. With zoom, always-three fusion was 10/10 versus 9/10 first frames, but adaptive confidence stopped after one frame and stayed 9/10. A sensible **next, unvalidated policy** is to require two agreeing, sufficiently detailed views even if the first is confident, cap at three, and abstain on conflict/insufficient subject pixels. Avoid treating neighboring, nearly identical video frames as independent evidence; consider tempered/averaged log scores and calibrate on actual sequences.
4. **Skip the count prior initially.** It changed none of the three-frame top-1 outcomes here. Counts are all-time, research-grade iNaturalist observations, biased by observer activity and detectability, not seasonal occurrence probabilities. Keep a broader/open fallback for out-of-list birds, but neither the shortlist nor the current uncertainty rule has been tested on nonbirds, empty frames, or unseen species. A bird detector or tap-to-select ROI is needed before claiming general camera support.
5. **Keep the model and text head resident; precompute/cache the head.** Warm CPU latency supports roughly one inference/second with some headroom, not 30-fps recognition. The custom head took **330 seconds** to build; the cached open model loaded in 10 seconds. Both heads can share an encoder and image embedding in a production implementation (not used for these independent-mode latency measurements). These are server CPU measurements, **not phone-device performance**.

CustomLabels used `common name (scientific name)` strings and the library's 80-template ensemble; open TreeOfLife used its supplied taxonomic embeddings. Accuracy differences therefore combine candidate restriction **and text-head differences**, not a controlled test of restriction alone. No season filter or location prior was used in single-frame results.

### Confidence in this conclusion

This is a feasibility signal, not a validation study. There are only 30 independent observations / 10 species episodes; the four conditions reuse images. Episode results move by ten percentage points per species. Centered synthetic inserts and smooth noise are unlike real distant camera scenes; true bird area and distance are unmeasured. The 100% condition has median actual pasted-photo area 75%, the 6% condition 4.5%, and the bird is only a fraction of that. The crop is geometrically privileged because the entire source photo was centered. iNaturalist training overlap can make results optimistic, though poor original framing and the feather can make this particular sample harder. The next decisive test is held-out real phone sequences with bird bounding boxes/pixel sizes, empty/nonbird/out-of-list controls, varied light/motion, and observer/date separation from training where possible.

## Reproduction and verification

Environment: pybioclip 2.1.6, PyTorch 2.14.1+cpu, open_clip_torch 3.3.0, Pillow 12.3.0, NumPy 2.5.2. Host reports AMD `100-000000011-11`, 64 logical CPUs; the probe explicitly used four intra-op threads and one inter-op thread, eager execution. One warmup per classifier was excluded. Timings include file decode, preprocessing, encoding, class scoring, and API result formatting; local mode returned all 60 scores and open mode top-three among 867,455 species labels.

```bash
cd /data/build/field-companion
export HF_HOME=/data/build/hf-cache TMPDIR=/data/build/tmp
export PYTHONDONTWRITEBYTECODE=1
/data/build/birding-demo/.venv-clip/bin/python probes/vision_probe.py --stage all --threads 4
```

The executed run used `--stage all` for acquisition/inference, then `--stage analyze` after the policy analysis was added. The current script runs both in `all`. It refuses to mix an existing `predictions.jsonl` with a new inference run. Use a fresh subdirectory under `/data/build/birding-demo/vision-imgs/` with `--out` for a new sample; results append to `--report`. iNaturalist random sampling is not seeded; the saved API responses, original photos, and manifest preserve this exact sample. Preparation reuses an existing manifest.

Evidence directory: `/data/build/birding-demo/vision-imgs/`:
- `sources.json`, `*-observations.json`: observation/photo IDs, source URLs, licenses/attribution, source dimensions, raw API responses.
- `species_counts-response.json`, `candidates.json`: local species selection and counts.
- `manifest.json`, `*-source.jpg`, `00-100.jpg` etc.: sources and all 120 synthetic test frames.
- `predictions.jsonl`, `results.json`: incremental and complete predictions, full 60-way probabilities, top-three labels, uncertainty flags, individual API timings.
- `fusion.json`, `policy-analysis.json`, `runtime.json`, `run.log`: fusion episodes, threshold/adaptive-loop details, setup metadata, execution log.
- `source-contact-sheet.jpg`, `distance-example.jpg`: visual input checks.

No commit/push, backend changes, or web changes were made.

Verification passed: Python syntax compilation; source/observation/frame integrity checks; 120 unique frames with 30 per condition; incremental JSONL exactly matching completed results; every 60-way probability vector finite and normalized within 1e-5; uncertainty flags matching the stated rule; independent recomputation of all single-frame top-1 and three-frame fusion counts. Direct trailing-whitespace/final-newline checks on both new repo files passed; no-index diff checks emitted no whitespace diagnostics (exit 1 denotes differences from the empty file). Both allowed files remain untracked; nothing was committed.
