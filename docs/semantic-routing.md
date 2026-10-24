# Semantic routing and execution boundaries

Higgs chooses the camera/status operation from the latest user request and
structured backend context. The backend validates that operation and its state;
it does not require a sentence to match a camera-intent keyword whitelist.
This uses the existing routing turn, without another model or routing layer.

## Operation contract

| Model tool | Meaning |
| --- | --- |
| `look(target="new", group=...)` | Prepare identification of a newly encountered unknown subject with independent evidence. |
| `new_observation(group=...)` | Alias for a new subject and preview. |
| `look(target="current", group=...)` | Prepare another photo of the current subject. |
| `get_observation` | Read the current observation/evidence without preparing a photo. |
| `get_task_status` | Read actual camera/investigation progress without restarting work or explaining findings. |
| `get_investigations(explain=true)` | Follow the existing result-request/consent path. |
| `speak` | Answer or ask for clarification when the referent or requested action is unclear. |

A model-routed `look` requires an explicit `current` or `new` target. The router
receives the current observation's group/evidence/status, actual camera request,
canonical task stages and active task-selection question. Short selection replies
are interpreted by that router, rather than a separate status sentence parser.
An unknown plant/animal report can request new identification without containing
the words “new” or “another”; a named sighting still uses the checklist tools.

## State and evidence checks

- Validate tool arguments and category names. A new target with no group defaults
  to `Aves`; the router must set `Plantae` or another category for nonbirds.
- A category change starts fresh evidence instead of relabeling an old bird's
  photos as a plant. Recent BirdNET evidence is attached only to bird observations;
  background bird listening and suggestions can continue beside a plant focus.
- Camera actions must still own the current input/focus after routing and local
  acknowledgement. A delayed route must not replace a newer user action.
- Camera readiness is established by the live preview handshake, not generated
  speech. The scoped Start/polite-photo confirmation fast path operates only on
  the current prepared request. Early confirmation is not saved for later.
  Manual Identify retains its explicit authorization. No model tool bypasses the
  request ID, matching focus, ready-state or single-use upload checks.
- Late classification results must match their original request and observation
  revision. Status reads preserve pending results and do not mark an explanation
  announced, consume consent, confirm a sighting or change checklist counts.
- Checklist authorization, grounded species/count checks, stale-write checks,
  speaker playback protection and cancellation ownership remain in place.
- Automatic photo-to-expert scheduling supplies its own conversation event before
  speaking the short acknowledgement. This avoids requesting another answer from
  an already answered progress turn; a changed input/focus suppresses the old cue.

There is one active camera focus, not an independent queue of photo jobs.
Choosing a new subject replaces/cancels the old unconfirmed photo workflow;
an old classifier may finish computing, but its late result cannot replace the
new request. Confirmed checklist records remain intact. GPT investigations already
scheduled from an earlier observation continue with their original evidence
snapshot, independently of the current focus.

## Verification

From the repository root, the focused offline suites are:

```powershell
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s backend -p test_semantic_routing.py
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s backend -p test_task_status.py
```

Validated on 2026-10-24:

- All 248 offline backend regressions passed with real keys removed and outbound
  sockets blocked, including state races and unchanged checklist authorization.
- Camera and general browser suites passed with fake media/transport and no
  browser runtime exceptions.
- All 14 real Higgs text-routing samples passed against synthetic prior bird or
  plant state, including the three plant requests from the reported logs. This
  made no GPT calls, uploaded no photos and wrote no checklist entries.
- The existing public HTTPS preview served matching assets/build and passed an
  isolated real-model plant-preview/ready/confirmation/capture-command check.
  That protocol check used no microphone audio and uploaded no JPEG.

These checks verify execution boundaries and sampled routing decisions, not a
guarantee that every live Higgs decision is correct. Physical phone voice/photo
capture and plant-identification accuracy have not been retested in this change.
