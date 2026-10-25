"""Frozen, provider-neutral routing cases. Expected answers never enter prompts.

These are synthetic regressions derived from the app's intended workflow, not a
representative sample of field users. Equivalent tools/selectors are accepted.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    text: str
    expected: tuple
    state: str = "empty"
    history: tuple = ()  # (role, text), shared verbatim by both providers
    forbidden: tuple = ()
    audio: bool = False


def choice(tool, **arguments):
    if arguments.get("selection") == "first" and "id" not in arguments:
        arguments["id"] = {"$default": "R1"}
    return {"tool": tool, "arguments": arguments}


MALLARD = {"$in": ["mallard", "mallards", "Anas platyrhynchos", "绿头鸭"]}
CARDINAL = {"$in": ["cardinal", "Northern Cardinal", "Cardinalis cardinalis"]}
ROBIN = {"$in": ["robin", "American Robin", "Turdus migratorius"]}
WRITES = ("add_observation", "add_observations", "confirm_observation", "accept_suggestions",
          "remove_observation", "set_count", "discard_observation", "dismiss_suggestions")
RESTART = ("look", "new_observation", "ask_expert")


def log_mallards(n):
    return (choice("add_observation", name=MALLARD, count=n, group="Aves"),
            choice("add_observations", items={"$unordered": [{"name": MALLARD, "count": n}]}))


def new_subject(group=None):
    args = {"group": group} if group else {}
    return (choice("look", target="new", **args), choice("new_observation", **args))


def status(scope="auto", **selector):
    # Legacy read tools are safe equivalents only when they do NOT explain a result.
    variants = [choice("get_task_status", **selector)]
    if scope != "auto":
        variants = [choice("get_task_status", scope={"$default": {"$in": ["auto", scope]}}, **selector)]
    if scope == "camera":
        # These cases contain exactly one task. Asking for all yields the same
        # photo, so accepting this is an outcome-equivalence rule, not a prompt rule.
        variants[0]["arguments"]["scope"] = {"$default": {"$in": ["auto", "camera", "all"]}}
        variants[0]["arguments"]["id"] = {"$default": "photo:capture-1"}
        variants.append(choice("get_observation"))
    if scope == "investigations":
        variants.append(choice("get_investigations", explain={"$default": False}, **selector))
    return tuple(variants)


def retry(scope='auto', **selector):
    return choice('retry_task', scope={'$default':scope},
                  id=selector.get('id', {'$absent':True}),
                  selection=selector.get('selection', {'$absent':True}))


def case(id, category, text, *expected, **kwargs):
    return Case(id, category, text, expected, **kwargs)


CASES = [
    case("log-two", "checklist", "Two mallards.", *log_mallards(2), audio=True),
    case("log-zh", "checklist", "我看到两只绿头鸭，帮我记下来。", *log_mallards(2), audio=True),
    case("log-mixed", "checklist", "帮我 add two mallards to the checklist.", *log_mallards(2)),
    case("log-many", "checklist", "Two mallards and three American robins.",
         choice("add_observations", items={"$unordered": [
             {"name": MALLARD, "count": 2}, {"name": ROBIN, "count": 3}]})),
    case("correct-total", "checklist", "Actually four mallards in total.",
         choice("set_count", name=MALLARD, count=4), state="checklist", audio=True),
    case("correct-pronoun", "checklist", "Actually four.", choice("set_count", name=MALLARD, count=4),
         state="checklist", history=(("user", "Two mallards."), ("assistant", "Two mallards on your checklist."))),
    case("remove-one", "checklist", "Remove the mallards from my checklist.",
         choice("remove_observation", name=MALLARD, all={"$default": False}), state="checklist"),
    case("clear-list", "checklist", "Clear my entire checklist.",
         choice("remove_observation", all=True), state="checklist"),
    case("list-count", "checklist", "How many species have I logged today?",
         choice("get_checklist"), state="checklist", forbidden=WRITES),
    case("focus-add", "checklist", "Add it to the checklist.",
         choice("confirm_observation"), state="photo-ready"),
    case("focus-two", "checklist", "Two of them.",
         choice("confirm_observation", count=2), state="photo-ready"),
    case("negative-log", "safety", "Don't add that bird to my checklist.",
         choice("ignore"), choice("speak"), state="photo-ready", forbidden=WRITES),
    case("question-not-log", "safety", "Could that be a mallard?",
         choice("ask_expert"), state="photo-ready", forbidden=WRITES),
    case("unknown-animal", "camera", "What kind of animal is that?", *new_subject(), audio=True),
    case("new-after-eagle", "camera", "I saw another animal, could you help me check it out?",
         *new_subject(), state="photo-ready", forbidden=WRITES, audio=True),
    case("plant-after-eagle", "camera", "I see a plant. Can you identify it?",
         *new_subject("Plantae"), state="photo-ready", forbidden=WRITES, audio=True),
    case("plant-zh", "camera", "我发现一株不认识的植物，帮我看看。",
         *new_subject("Plantae"), state="photo-ready", forbidden=WRITES, audio=True),
    case("insect-after-eagle", "camera", "There is an insect on this leaf. Help me identify it.",
         *new_subject("Insecta"), state="photo-ready", forbidden=WRITES),
    case("retake-current", "camera", "Take another photo of the same bird.",
         choice("look", target="current"), state="photo-ready", audio=True),
    case("read-photo", "camera", "What did the photo identify it as?",
         choice("get_observation"), state="photo-ready", forbidden=RESTART),
    case("describe-mark", "camera", "It has a red head and white stripes on its wings.",
         choice("describe", text={"$contains": "red"}), state="photo-ready", forbidden=WRITES),
    case("discard-focus", "camera", "Discard this identification.",
         choice("discard_observation"), state="photo-ready"),
    case("photo-progress", "progress", "Check out the identification process.",
         *status("camera"), state="photo-running", forbidden=RESTART, audio=True),
    case("photo-progress-zh", "progress", "刚才那张照片识别到哪一步了？",
         *status("camera"), state="photo-running", forbidden=RESTART),
    case("photo-completed-status", "progress", "Is the photo identification done yet?",
         *status("camera"), state="photo-ready", forbidden=RESTART),
    case("new-while-analyzing", "camera", "I see a different plant now. Help me identify it.",
         *new_subject("Plantae"), state="photo-running", forbidden=WRITES),
    case("research-progress", "progress", "How is the first investigation going?",
         *status("investigations", selection="first"),
         *status("investigations", id="R1"), state="research", forbidden=RESTART),
    case("research-progress-zh", "progress", "刚才第一个调查查得怎么样了？",
         *status("investigations", selection="first"),
         *status("investigations", id="R1"), state="research", forbidden=RESTART),
    case("research-r2", "progress", "What stage is investigation R2 at?",
         *status("investigations", id="R2"), state="research", forbidden=RESTART),
    case("all-progress", "progress", "Show the status of all my tasks.",
         choice("get_task_status", scope="all", selection={"$default": "all"}, id={"$absent": True}),
         choice("get_task_status", scope={"$default": "auto"}, selection="all", id={"$absent": True}),
         state="mixed", forbidden=RESTART),
    case("done-not-explain", "progress", "Is investigation R1 ready?",
         *status("investigations", id="R1"), state="research-ready", forbidden=RESTART),
    case("expert-compare", "expert", "Could it be a Cooper's Hawk instead?",
         choice("ask_expert"), state="photo-ready", forbidden=WRITES, audio=True),
    case("expert-diet", "expert", "What does it eat?",
         choice("ask_expert"), state="photo-ready", forbidden=WRITES),
    case("expert-habitat-zh", "expert", "这种鸟一般生活在什么环境？",
         choice("ask_expert"), state="photo-ready", forbidden=WRITES),
    case("expert-second-task", "expert", "Start another investigation: why does this bird migrate?",
         choice("ask_expert"), state="research", forbidden=WRITES),
    case("explain-id", "expert", "Explain the result of R1.",
         choice("get_investigations", id="R1", explain=True), state="research-ready", forbidden=RESTART),
    case("offer-okay", "expert", "Okay.", choice("get_investigations", explain=True,
         id={"$default": "R1"}, selection={"$default": "first"}),
         choice("get_investigations", explain=True, id="R1"),
         state="explanation-offer", history=(("assistant", "Investigation R1 is ready. Would you like me to explain?"),),
         forbidden=("accept_suggestions", "ask_expert")),
    case("offer-zh", "expert", "好的，讲一下。", choice("get_investigations", explain=True,
         id={"$default": "R1"}, selection={"$default": "first"}),
         choice("get_investigations", explain=True, id="R1"),
         state="explanation-offer", history=(("assistant", "Investigation R1 is ready. Would you like me to explain?"),)),
    case("offer-later", "expert", "Not now.", choice("ignore"), state="explanation-offer",
         history=(("assistant", "Investigation R1 is ready. Would you like me to explain?"),), forbidden=WRITES),
    case("cancel-first", "expert", "Cancel the first investigation.",
         choice("manage_investigation", action="cancel", selection="first"),
         choice("manage_investigation", action="cancel", id="R1"), state="research"),
    case("retry-id", "expert", "Try R1 again.",
         retry(id='R1'), retry('investigations',id='R1'), state="research-failed"),
    case('retry-photo-plain','retry','Retry.',retry(),state='photo-failed'),
    case('retry-photo-observed','retry','Okay, I want to retry.',retry(),state='photo-failed'),
    case('retry-photo-paraphrase','retry',"Let's give that another go.",retry(),state='photo-failed'),
    case('retry-photo-zh','retry','再试一次',retry(),state='photo-failed'),
    case('retry-photo-explicit','retry','Retry the photo identification.',retry('camera'),
         retry('camera',id='photo:capture-1'),state='retry-mixed'),
    case('retry-photo-spoken-offer','retry','Try again.',retry(),state='retry-photo-offer',
         history=(('assistant','Photo identification could not finish; say Retry to prepare another photo.'),)),
    case('retry-ambiguous','retry','Try again.',retry(),state='retry-mixed'),
    case('retry-selection-photo','retry','The photo.',retry('camera'),retry(id='photo:capture-1'),
         retry('camera',id='photo:capture-1'),state='retry-selection',history=(('assistant',
         'Which task should I retry: investigation R1, investigation R2, photo identification?'),)),
    case('retry-selection-expert','retry','R1.',retry(id='R1'),retry('investigations',id='R1'),
         state='retry-selection',history=(('assistant',
         'Which task should I retry: investigation R1, investigation R2, photo identification?'),)),
    case('retry-photo-running','retry','Retry the photo identification.',retry('camera'),
         retry('camera',id='photo:capture-1'),state='photo-running'),
    case('retry-explicit-latest','retry','Retry the latest investigation.',
         retry('investigations',selection='latest'),retry('investigations',id='R2'),retry(id='R2'),state='research-failed'),
    case('retry-empty','retry','Retry.',retry(),state='empty'),
    case("log-while-research", "checklist", "Two mallards.", *log_mallards(2), state="research"),
    case("suggest-yes", "sounds", "Yes, add them all.",
         choice("accept_suggestions", species={"$all_pending": True}), state="suggestions",
         history=(("assistant", "I'm hearing a Northern Cardinal and an American Robin. Add them?"),)),
    case("suggest-subset", "sounds", "Just the cardinal.",
         choice("accept_suggestions", species={"$unordered": [CARDINAL]}), state="suggestions",
         history=(("assistant", "I'm hearing a Northern Cardinal and an American Robin. Add them?"),)),
    case("suggest-no", "sounds", "No, don't add them.",
         choice("dismiss_suggestions", species={"$all_pending": True}), state="suggestions",
         history=(("assistant", "I'm hearing a Northern Cardinal and an American Robin. Add them?"),)),
    case("suggest-okay", "safety", "Okay.", choice("ignore"), state="suggestions",
         history=(("assistant", "I'm hearing a Northern Cardinal and an American Robin. Add them?"),), forbidden=WRITES),
    case("suggest-question", "sounds", "Did you hear them?", choice("recent_sounds"),
         state="suggestions", forbidden=WRITES),
    case("recent-bird", "sounds", "What bird was singing just now?", choice("recent_sounds"),
         state="suggestions", forbidden=WRITES, audio=True),
    case("local-plants", "sounds", "Which plants have been seen near my location?",
         choice("nearby_species", group="Plantae"), forbidden=WRITES),
    case("chatter-snacks", "silence", "Did you bring the snacks?", choice("ignore"), forbidden=WRITES, audio=True),
    case("chatter-phone", "silence", "Mom, I'll call you back after the walk.", choice("ignore"), forbidden=WRITES),
    case("filler-ah", "silence", "Ah.", choice("ignore"), state="research", forbidden=WRITES),
    case("filler-ordinal", "silence", "First.", choice("ignore"), state="photo-ready", forbidden=WRITES),
    case("fragment", "silence", "As for you, for me.", choice("ignore"), forbidden=WRITES),
    case("mic-check", "conversation", "Can you hear me?", choice("speak"), forbidden=WRITES, audio=True),
    case("greeting-zh", "conversation", "你好，能听到我说话吗？", choice("speak"), forbidden=WRITES, audio=True),
]


def public_case(c):
    return {"id": c.id, "category": c.category, "text": c.text, "state": c.state,
            "history": c.history, "expected": c.expected, "forbidden": c.forbidden, "audio": c.audio}
