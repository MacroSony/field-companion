# Visual target intent and category conflicts

Photo observations keep the user's requested category separately from the model's
identified category. A narrow category supplied through Look/new_observation or
the camera selector sets `target_group`. The tool contract requires an explicit
user category; unspecified organisms and generic animals use Any. A model guess,
an old observation's identified category and a retry never create a new user
constraint. The preview selector shows the user target, not the model guess.

`Observation.group` remains the effective category for checklist compatibility;
`identified_group` records the selected image candidate's category. For an
unspecified target the model may update the effective category. For an explicit
target it cannot silently replace the user's intent.

## Image API

`POST /classify` accepts an optional `target_group` from the supported iconic
groups. It scores the entire vocabulary once, then ranks all members of the
requested category, intersected with an optional existing species shortlist.
This uses the model's taxonomy, without per-species exceptions, text re-encoding
or a benchmark answer list. An empty intersection remains empty.

The response retains unrestricted `open_top`, candidate `group`, original
`global_p`, conditional ranking `p`, and scope mass. The app uses original scores
for its visual uncertainty check. Restricting a shortlist to one name may give
that name ranking score 1.0; it cannot make weak original evidence confident.
Missing original scores on a scoped result are also treated as weak evidence.

An unrestricted winner from a different known category raises a
`category_conflict`. The app preserves the requested category, marks the result
tentative, displays a subject-framing prompt, and asks for clarification/a closer
photo. Automatic expert research is deferred until this input ambiguity is
resolved; an explicit expert request is still available. Progress and result
queries report the same conflict. Recognition does not add a checklist entry;
the existing deliberate confirmation remains necessary.

The browser already supports tapping a subject and selecting a crop. This change
reuses that control. It does not add automatic object detection or segmentation.
Unknown targets retain unrestricted identification and can still suffer the
model's original errors. A user-provided category is a useful constraint, not
proof of a species or proof that the user selected the correct category.

## Validation

- `test_vision_policy.py`: user intent survives conflicting evidence, a service
  ignoring scope cannot publish a cross-category result, weak/missing original
  scores stay tentative, unspecified/inferred categories remain correctable,
  and user category changes create separate observations.
- `test_vision_service_scope.py`: Torch numerical tests in `.venv-vision`, with
  fixed image probabilities and no model download/load.
- `web/camera-test.py`: complete camera regression plus selector provenance and
  conflict warning checks in a headless browser with fake media.
- `vision_scope_eval.py`: paired real-model evaluation on the original 96 photos
  plus 48 photos of 16 different species. Eight deliberately wrong-category and
  eight unspecified-category controls are frozen before prediction.

Category-assisted accuracy assumes the user supplied the correct broad category.
It must be reported separately from blind image accuracy. Evaluation keeps the
existing .5/.2 visual evidence thresholds; no threshold tuning uses the new set.
Downloaded images, per-image results and source/license manifests stay in ignored
`out/vision-category-fix/`. Results are convenience-sample measurements, not
population-level field accuracy or calibration guarantees.

### Paired image results (2026-10-25)

| Set | Images | Unrestricted top-1 | With user category | Wrong results accepted, before / after |
|---|---:|---:|---:|---:|
| Original regression set | 96 | 79/96 (82.3%) | 81/96 (84.4%) | 3 / 0 |
| Different-species holdout | 48 | 40/48 (83.3%) | 42/48 (87.5%) | 2 / 1 |

These are category-assisted results: the correct broad category is supplied as
user intent, not discovered automatically. The remaining accepted holdout error
is American Beaver versus Eurasian Beaver, within the same genus/category.
The conservative policy accepts fewer results (73 -> 70 and 37 -> 36). It does
not turn every hard image into a correct species identification.

All eight deliberately wrong-category controls requested clarification or stayed
uncertain. All eight unspecified-category controls preserved unrestricted
rankings. The original 96 unrestricted rankings and scores also match the prior
benchmark. Source IDs and image hashes do not overlap between the two cohorts.
Full counts and policy/hash verification are in
[the paired benchmark report](benchmarks/vision_scope_20261025.json).
