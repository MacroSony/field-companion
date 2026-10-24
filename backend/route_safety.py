"""Conservative execution checks for model-selected checklist actions.

These checks reject unsupported writes; they never infer a species or execute a
different write. Buttons already express explicit intent and bypass model routing.
"""
import re


def words(text):
    return " ".join(re.findall(r"[\u3400-\u9fff]|[^\W_\u3400-\u9fff]+", str(text).lower()))


def log_intent(text):
    if re.search(r"\b(what|which|how|why|should|did i|have i)\b|什么|怎么|是否|有没有", text, re.I):
        if not re.search(r"\b(please|can you|could you|would you)\s+(?:add|log|record|save|note)\b|请.*(?:加入|记录|记下)|帮我.*(?:加入|记录|记下)", text, re.I):
            return False
    return bool(re.search(r"\b(add|log|record|save|note)\b|加入|加进|记下|记录|记到", text, re.I))


def route_guard(name, args, text, *, suggestions=False, log_offer=False):
    """Return allow, ignore or clarify, with a stable audit reason."""
    text = str(text).strip()
    normalized = words(text)
    if not text:
        return "allow", "no_voice_context"  # trusted internal/manual calls
    fillers = {"ah", "uh", "um", "oh", "okay", "ok", "mm", "mm hmm", "hmm", "好 的", "嗯"}
    if normalized in fillers and name not in ("get_investigations", "manage_investigation"):
        return "ignore", "filler"
    if name == "speak":
        reason = str(args.get("reason", "")).lower()
        # The observed failure explicitly said "ignore per routing rules" but
        # emitted speak. Do not reinterpret legitimate questions about ignoring.
        contradiction = re.search(r"\b(ignore per|should (?:be )?ignore|call ignore|use ignore|not addressed|unintelligible (?:filler|fragment))", reason)
        question = "?" in text or "？" in text or re.match(r"^(what|why|how|can|could|do|does|is|are)\b", text, re.I)
        if contradiction and not question:
            return "ignore", "speak_ignore_contradiction"
        return "allow", "read_only"
    writes = {"confirm_observation", "add_observation", "add_observations", "accept_suggestions",
              "dismiss_suggestions", "remove_observation", "set_count", "discard_observation"}
    if name not in writes:
        return "allow", "read_only"
    negative = re.search(r"\b(don't|do not|never)\s+(?:add|log|record|save)\b|不要(?:加|记)|别(?:加|记)", text, re.I)
    if negative and name in ("confirm_observation", "add_observation", "add_observations", "accept_suggestions"):
        return "clarify", "negated_write"
    yes = normalized in {"yes", "yeah", "yes please", "sure", "yes both", "yes all", "是 的", "加 吧"}
    focus_count = bool(re.fullmatch(r"(?:[1-9]\d*|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:of them|birds?|individuals?)", normalized))
    focus_count |= bool(re.fullmatch(r"[一二两三四五六七八九十\d]+(?:只|个)(?:它们)?", re.sub(r"[\s。！!.,]", "", text)))
    if name == "confirm_observation":
        return ("allow", "explicit_focus_confirmation") if (log_intent(text) or focus_count or (yes and log_offer)) else ("clarify", "ambiguous_focus_confirmation")
    if name == "accept_suggestions":
        explicit = (yes and log_offer) or log_intent(text) or bool(re.match(r"^(just|only)\s+", text, re.I))
        return ("allow", "explicit_suggestion_confirmation") if suggestions and explicit else ("clarify", "ambiguous_suggestion_confirmation")
    if name == "dismiss_suggestions":
        explicit = bool(re.search(r"\b(no|dismiss|don't|do not)\b|不要|不用|不加", text, re.I))
        explicit |= bool(re.search(r"\bclear\b.*\b(all|everything)\b|清空|全部清除", text, re.I))
        return ("allow", "explicit_dismissal") if explicit else ("clarify", "ambiguous_dismissal")
    if name in ("remove_observation", "discard_observation"):
        explicit = bool(re.search(r"\b(remove|delete|clear|discard|forget)\b|删除|清空|丢弃|不要这", text, re.I))
        if args.get("all") and not re.search(r"\b(all|everything|clear)\b|全部|所有|清空", text, re.I):
            explicit = False
        return ("allow", "explicit_removal") if explicit else ("clarify", "ambiguous_removal")
    if name == "set_count":
        explicit = bool(re.search(r"\b(actually|instead|make|change|correct|total|count)\b|其实|改成|改为|一共|总共", text, re.I))
        return ("allow", "explicit_count_correction") if explicit else ("clarify", "ambiguous_count_correction")
    entries = args.get("items", []) if name == "add_observations" else [args]
    question = bool(re.search(r"[?？]|\b(what|which|how many|could it|is that)\b|是什么|哪种|多少", text, re.I))
    named = bool(entries) and all(words(e.get("name", "")) and words(e["name"]) in normalized for e in entries)
    report = named and (not question or log_intent(text))
    # Chinese reports may be translated into English tool names by the router.
    report |= bool(re.search(r"[一二两三四五六七八九十\d]+只[\u3400-\u9fff]{2,}", text)) and not question
    return ("allow", "grounded_sighting_report") if report and (not question or log_intent(text)) else ("clarify", "ungrounded_sighting")
