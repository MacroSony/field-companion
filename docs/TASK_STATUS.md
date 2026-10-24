# Existing photo and investigation progress

A question about progress reads existing work. It must not take a new photo,
start another investigation, explain findings without consent, or consume an
unread result. The backend implements this with canonical task snapshots and
`get_task_status`, shared by camera identification and background investigations.

## Interaction

| User input | Behavior |
| --- | --- |
| “Check out the identification process.” / “How is identification progressing?” | Report the current photo's actual stage. |
| “Any progress?” / “Is it done yet?” / “任务进度” | Select existing active work, otherwise the latest retained task; clarify independent matches. |
| “Any results yet?” / “有结果了吗？” | Report whether a result is ready, without reading it. |
| “R1进行到哪一步了？” / “How is the first investigation going?” | Read the selected investigation's stage and queue position when applicable. |
| “What is the status of all tasks?” | Summarize photo and investigation progress. |
| “Tell me the result.” / “Explain R1.” / acceptance of an actual explanation offer | Follow the existing result/explanation path, not a progress check. |
| “Look again.” / “Take another photo.” / “Identify a different animal.” | Keep explicit camera intent; prepare another capture using the normal confirmation rules. |
| “I found another animal. Could you help me check it out?” | Create a fresh subject, prepare the camera and wait for Start; never answer from the previous animal's evidence. |
| “I saw another animal.” / “我又看到一只动物。” | A generic unresolved new target prepares a fresh preview; it does not take or log a photo. Named species/count reports keep their logging path. |
| “Take the photo for me.” | With a genuinely ready request, confirm capture. Without one, prepare the preview and wait for confirmation; an early command is never saved for later. |
| Manual **Identify this subject** / **Look again** / **New subject** | Keep the button's explicit camera authorization. |

The parser in `backend/task_status.py` is conservative. It recognizes clear
progress/existence questions in English and Chinese; other language and paraphrases
still use model routing. It does not interpret biological questions such as
“What does it eat?”, “How does photosynthesis work?” or “What is that?” as task
progress. Cancel/retry/new-research requests and explanation consent remain with
their existing tools. An ordinal such as “first” selects retained task order;
it is not guessed to mean a particular R ID.

## Source of truth

`Session.task_status_snapshot()` reads investigation queue records and the current
photo's observation/request/revision. It does not ask GPT to infer whether work
is running. Investigation rows expose ID, kind, observation ID, status, stage,
creation time and current queue position. Camera rows additionally expose their
request ID, image revision, elapsed seconds, pending-result availability and
related investigation IDs. Elapsed time is not a percentage or completion estimate.

`get_task_status` accepts `scope=auto|camera|investigations|all`, an optional explicit
R ID, and `selection=first|latest|all`. Auto prefers active tasks; if none are active,
it considers the latest retained task. Multiple independent candidates prompt the
user to choose. Explicit all speaks at most three summaries and notes additional
matches. An empty selection truthfully reports that no matching task exists.

After a task-selection question, a short “Photo”, “R1” or “the first one” selects
one of its tasks without another model-routing decision. This context expires
after 30 seconds, a different user turn, or a newer assistant question, so it
does not intercept an investigation's separate result-explanation consent.

The snapshot does not contain findings, species answers or source contents.
Checking status does not set an investigation's `announced`, mark a photo in
`vision_answered`, or clear the latest explanation offer. A task remains available
after completion even while its automatic readout is queued. Existing investigation
messages sent to the UI keep their result data; the read-only status response and
spoken progress sentence omit it.

## Photo lifecycle

`analysis_status` remains the coarse compatibility state. `analysis_stage` and
`analysis_created_at` are additional Observation fields; request/revision identity
keeps progress and late results attached to the correct photo.

| Stage | Actual operation |
| --- | --- |
| `camera_opening` | The preview is being prepared; no photo has been taken. |
| `awaiting_confirmation` | Usable video is ready; wait for Start or Identify. |
| `capturing` | Capture and upload are in progress. |
| `range_lookup` | For birds, obtain locally possible species before recognition. |
| `recognizing` | Run the image-identification service. |
| `resolving_species` | Resolve candidate scientific names and display metadata. |
| `fusing` | Combine image results with available location/sound evidence. |
| `ready` | Image processing has completed; its result can still be unread. |
| `failed` / `cancelled` | Report the actual failed/cancelled operation. |

The status snapshot can further project `awaiting_expert` when uncertain image
evidence is waiting for its queued visual readout to dispatch expert work. Once
the visual-expert investigation exists, its revision-matched stage is reflected
as `expert_<stage>` in the camera status row. This is derived task status, not a
second image-recognition run. The photo's own visual investigation and camera
task are treated as one related task for auto selection.

Investigation stages remain `queued`, `retrieving`, `analyzing`, `completed`,
`failed`, `timed_out` and `cancelled`. Status speech uses fixed English/Chinese
wording derived from these records. The existing Higgs output path speaks it;
checking progress does not enqueue a new GPT investigation. Before speech starts,
the backend rechecks the task fingerprint so a stage that changed during voice
setup is not read as current. Repeated changes defer and queue a fresh read.

## Routing and unread photo protection

Clear status input bypasses Higgs tool selection. The execution guard recognizes
the same query if a model-selected tool reaches it and redirects to
`get_task_status`, including an erroneous `look` call. A completed current photo
also survives a mistaken `look`/`new_observation` from a normal follow-up: its
existing evidence is reused rather than preparing another photo. In-flight
identification queries report progress without restarting inference.

Status reads preserve queued `vision` work. Only explicit new camera intent,
manual capture controls, or existing cancellation/focus-reset rules can replace
it. This distinction also applies after image inference finishes but before
the result is spoken; `ready` is not permission to discard unread work.

New-subject inspection requests are resolved before old-photo reuse protection.
The backend corrects a mistaken `look`, evidence-read or speech selection to a
new observation with empty image evidence, candidates and hints. Existing
confirmed sightings and investigation snapshots remain intact. A retake stays
with the same observation; a named sighting/count report or a species comparison
does not authorize a camera switch. Both new-subject and retake previews retain
the separate Start/Identify confirmation.

An unproven model-selected new-subject action asks for clarification; it never
falls back to announcing the old species. Direct photo requests cannot turn into
evidence reads merely because the model selected the wrong tool. A bare Start
without an active camera request also clarifies without reading an old identity.
General answer instructions receive the actual camera request state: a completed
photo is not permission to claim that a camera preview is ready.

## Verification and limits

Offline tests cover English/Chinese parsing, IDs and ordering, exclusions for
control/result/nature questions, actual camera/investigation snapshots, linked
expert work, empty and ambiguous selections, result-consent preservation, queued
visual work, mistaken tool routing, explicit retake, and stage changes during
speech setup. Browser camera fixtures use synthetic media and model transport.
Physical phone voice timing and the live camera-to-model workflow still need
hardware verification.

This change does not alter the browser's existing 35-second upload timeout or
claim that slow model loading has disappeared. Stage reporting describes observed
operations; it does not invent an exact model-loading substage, ETA or percentage.
Speaker playback protection still applies: wait for the reply or use Tap to talk
before speaking; earphones retain their existing interruption policy.
