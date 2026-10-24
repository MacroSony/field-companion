"""Pure, conservative recognition of requests for existing task progress.

This module never starts work or infers an investigation ID from its ordinal.
An unrecognized or mixed control request stays with the normal router.
"""
import re


_CAMERA = re.compile(r"\b(?:identification|identifying|recognition|classification|classifying|"
                     r"camera|photo|photos|picture|pictures|image|images|bioclip)\b|"
                     r"识别|鉴别|照片|拍照|相机|图片|图像", re.I)
_INVESTIGATIONS = re.compile(r"\b(?:investigations?|research|expert|gpt)\b|调查|研究|专家", re.I)
_TASK = re.compile(r"\b(?:tasks?|jobs?|analysis|processing)\b|任务|处理|分析", re.I)
_STATUS_NOUN = re.compile(r"\b(?:status|progress|stage|update|updates)\b", re.I)
_RESULT_REQUEST = re.compile(
    r"\b(?:explain|read(?:\s+out)?|summari[sz]e|describe|play)\b|"
    r"\b(?:tell|give|show)\b.{0,60}\b(?:results?|answers?|findings|conclusions?)\b|"
    r"解释|讲解|解读|播报|念一下|(?:告诉我|说说|讲讲|给我看).{0,20}(?:结果|答案|结论)", re.I)
_CONTROL = re.compile(
    r"\b(?:cancel|stop|pause|abort|delete|remove|retry|restart|resume|continue|"
    r"launch|begin|schedule|open|close)\b|"
    r"\b(?:start|run|do|perform|conduct)\s+(?:(?:a|the|another|new|an)\s+)*"
    r"(?:investigation|research|task|analysis|identification)\b|"
    r"(?:^|\b(?:please|can you|could you|would you|will you|help me|let's)\s+)"
    r"(?:investigate|compare|research(?!\s+(?:status|progress|stage|updates?)\b))\b|"
    r"取消(?!了|过|吗)|停止|暂停|重试|重启|重新(?:调查|研究|识别)|"
    r"(?:开始|启动|安排|再来|新建).{0,8}(?:调查|研究|任务|识别)|打开相机|关闭相机", re.I)


def _text(text):
    return re.sub(r"\s+", " ", str(text or "").replace("’", "'").strip()).lower()


def new_subject_request(text):
    """Require both a new organism and a positive visual request.

    A sighting report alone remains a logging input. Retaking the same subject
    is a separate camera action and must not create a new observation.
    """
    value = _text(text)
    if re.search(r"\b(?:if|when)\s+(?:i|we|you)\b|\b(?:suppose|imagine)\b|"
                 r"(?:如果|假如|假设|要是|万一).{0,10}(?:发现|看到|找到|遇到|看见)|"
                 r"\b(?:haven't|have not|didn't|did not|don't|do not|never|no need to)\s+"
                 r"(?:\w+\s+){0,3}(?:find|found|see|seen|saw|look|check|identify|switch|change)\b|"
                 r"(?:没有|没|并未|不曾).{0,3}(?:发现|看到|找到|看见)|"
                 r"(?:不要|不用|别|无需).{0,4}(?:看|识别|辨认|换)", value):
        return False
    if re.match(r"^(?:do\s+(?:i|we)\s+(?:need|have)\s+to|should\s+(?:i|we)|"
                r"(?:can|could|may)\s+i)\b", value):
        return False
    if re.search(r"\b(?:eat|eats|diet|habitat|behavior|behaviour|migration|conservation)\b|"
                 r"吃什么|食性|栖息|行为|迁徙", value):
        return False
    if re.fullmatch(r"(?:(?:please|let's)\s+)?(?:new\s+subject|"
                    r"(?:switch|change|move)\s+(?:(?:to|a|the)\s+)*(?:(?:new|another|different)\s+)?subject|"
                    r"switch\s+to\s+the\s+other\s+one)[.!?]*", value):
        return True
    organism = r"(?:animal|bird|organism|subject|plant|insect|flower|tree|mammal|fish|reptile|spider|mushroom|butterfly)"
    fresh = bool(re.search(r"\b(?:another|new|different)\s+" + organism + r"\b", value))
    visual = bool(re.search(
        r"\b(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:help\s+(?:me|us)\s+)?"
        r"(?:check(?:ed)?\s+(?:out\b|(?:it|this|that)\s+out\b)|look\s+at\b|"
        r"identify\b|recognize\b|take\s+a\s+look\b)|"
        r"(?:^|[.!?,;]\s*|\band\s+)(?:please\s+)?(?:help\s+(?:me|us)\s+)?"
        r"(?:identify\b|recognize\b|look\s+at\b|check(?:ed)?\s+(?:out\b|(?:it|this|that)\s+out\b))|"
        r"\bwhat(?:'s|\s+is)\s+(?:this|that|it)[?!.]*$", value))
    zh_fresh = bool(re.search(r"(?:另(?:一)?(?:个|只|株|棵)?|新的?|不同的|"
                             r"又(?:发现|看到|找到|遇到|看见)了?一?(?:个|只|株|棵))"
                             r"(?:动物|鸟|植物|昆虫|花|树|鱼|蜘蛛|蘑菇|蝴蝶|(?:观察)?对象)", value))
    zh_visual = bool(re.search(r"(?:帮我|请|麻烦你|能不能|能否).{0,4}(?:看看|看一下|识别|辨认)|"
                              r"^(?:看看|看一下|识别|辨认|换)(?:一)?(?:个|只|株|棵)?(?:另|新|不同)|"
                              r"(?:这|那|它)是什么[？?。]*$", value))
    if re.match(r"^换(?:一)?(?:个|只|株|棵)(?:动物|鸟|植物|昆虫|花|树|(?:观察)?对象)[？?。]*$", value):
        return True
    return (fresh and visual) or (zh_fresh and zh_visual)


def explicit_retake(text):
    """Recognize a new camera action, not an unrelated use of 'new/different'."""
    value = _text(text)
    if new_subject_request(value):
        return True
    clauses = [clause.strip() for clause in re.split(r"[;.!?。；！？,，]|\band\b|\bthen\b", value) if clause.strip()]
    if len(clauses) > 1:
        return any(explicit_retake(clause) for clause in clauses)
    if re.match(r"^(?:do\s+(?:i|we)\s+(?:need|have)\s+to|should\s+(?:i|we)|"
                r"does\s+(?:this|that|it)\s+need|(?:can|could|may)\s+i)\b", value):
        return False
    if re.match(r"^(?:what|why|how|when|where)\s+(?:is|are|does|do|did|would|will)\b", value):
        return False
    # A negative instruction is not positive permission to take a photo.
    if re.search(r"\b(?:don't|do not|never|no need (?:to|for))\s+(?:\w+\s+){0,3}"
                 r"(?:retake|re-take|take|capture|photograph|look|switch|change|identify|check|show)\b|"
                 r"\b(?:retake|re-take)\b.{0,20}\b(?:not needed|not necessary|unnecessary)\b|"
                 r"\bno\s+(?:retake|re-take)\b|"
                 r"(?:不要|不用|别|无需).{0,4}(?:重拍|再拍|重新拍|再看|看看|识别|换)|"
                 r"(?:重拍|再拍|重新拍).{0,4}(?:不需要|没必要|不用|不必)", value):
        return False
    return bool(re.search(
        r"\b(?:retake|re-take)\b|\blook\s+(?:at\s+(?:it|this|that)\s+)?again\b|"
        r"\btake\s+another\s+look\b|"
        r"\b(?:take|capture|shoot|get|grab|use)\s+(?:(?:me|a|an|the)\s+)*"
        r"(?:another|new|fresh|different)\s+(?:photo|picture|shot|frame|image)\b|"
        r"^(?:(?:let's|please)\s+)?(?:start\s+(?:a\s+)?)?new\s+subject[.!?]?$|"
        r"\b(?:switch|change|move)\s+(?:(?:to|a|the)\s+)*(?:another|different)\s+subject\b|"
        r"\b(?:look(?:\s+at)?|check(?:\s+out)?|identify|photograph|show)\s+"
        r"(?:(?:me|you|a|the)\s+)*(?:another|different|new)\s+"
        r"(?:animal|bird|organism|subject|plant|insect|flower|tree)\b|"
        r"重拍|再拍|重新拍|另拍|再看(?:一眼|一下)|重新看(?:一眼|一下)|"
        r"换(?:一)?个(?:观察)?对象|新的观察对象|"
        r"(?:看看|看一下|识别|辨认|拍一下)(?:另(?:一)?个|新的?|不同的)(?:动物|鸟|植物|昆虫|花|树)|"
        r"换(?:一)?(?:个|只|株|棵)(?:动物|鸟|植物|昆虫|花|树)", value))


def _english_status(value, known_target):
    value = re.sub(r"[?!.。,，;；:：]+", " ", value).strip()
    # Do not turn biological facts or tutorials into progress queries merely
    # because they mention a photo, identification, or a model.
    if re.search(r"\bhow\s+(?:does|do|can|would)\b.*\bwork\b|"
                 r"\b(?:conservation status|life stage|growth stage)\b", value):
        return False
    if known_target:
        if _STATUS_NOUN.search(value):
            return True
        if re.search(r"\bany\s+results?\b|\bdo\s+you\s+have\s+(?:(?:a|any|the)\s+)?results?\b", value):
            return True
        if re.search(r"\b(?:check(?:\s+out)?|show|tell|give)\b.*\bprocess\b", value):
            return True
        if re.search(r"\bhow(?:'s|\s+is|\s+are)\b.*\b(?:going|doing|coming|looking|progressing)\b|"
                     r"\bhow\b.*\b(?:is|are)\s+(?:going|doing|coming|progressing)\b|"
                     r"\b(?:where|what)\b.*\b(?:up to|at|stage)\b|"
                     r"\bwhere(?:'s|\s+is|\s+are)\b.*\b(?:with|on)\b|"
                     r"\bhow\s+(?:far|long)\b|\bwhy\b.*\btaking\b.*\blong\b", value):
            return True
        if re.search(r"\b(?:is|are|has|have)\b.*\b(?:done|ready|finished|complete|completed|"
                     r"cancelled|canceled|failed|running|queued|started)"
                     r"(?:\s+(?:yet|now|already|still|please))*$", value):
            return True
    # Generic prompts need a narrow form: 'What is its conservation status?'
    # and other unrelated topic questions must not become the latest job.
    return bool(re.fullmatch(
        r"(?:(?:please|so|and|well|hey|also)\s+)*(?:"
        r"(?:any\s+)?(?:status|progress|updates?)(?:\s+(?:yet|now|please))?|"
        r"any\s+results?(?:\s+yet)?|do\s+you\s+have\s+(?:(?:a|any|the)\s+)?results?(?:\s+yet)?|"
        r"what(?:'s|\s+is)\s+(?:the\s+|its\s+|your\s+)?(?:status|progress|update)|"
        r"how(?:'s|\s+is|\s+are)\s+(?:it|that|this|things|everything|they)\s+(?:going|doing)|"
        r"(?:is|are|has|have)\s+(?:it|that|this|everything|they)\s+"
        r"(?:done|ready|finished|complete|completed)(?:\s+(?:yet|now|already))?|"
        r"(?:where\s+are\s+we|how\s+far\s+along\s+are\s+we)(?:\s+(?:now|with\s+it))?"
        r")", value))


def _chinese_status(value, known_target):
    if re.search(r"怎么用|如何(?:使用|操作|识别|鉴别|拍照|工作)|怎么操作|什么原理|怎么工作", value):
        return False
    if known_target and re.search(r"进度|进展|状态|到哪一步|进行到|怎么样|"
                                  r"好了(?:吗|没)|完成了(?:吗|没)|结束了(?:吗|没)|"
                                  r"做完了(?:吗|没)|"
                                  r"取消了(?:吗|没)|开始了(?:吗|没)|还要多久|有没有结果|有结果了?吗", value):
        # 'How is the result?' asks for content, unlike 'Is there a result yet?'.
        if re.search(r"结果|答案|结论", value) and not re.search(
                r"进度|进展|状态|完成|好了|有没有结果|有结果了?吗", value):
            return False
        return True
    return bool(re.fullmatch(
        r"(?:请问|现在|目前|那个|这个|帮我看看|看一下|查一下)*"
        r"(?:(?:查|调查|研究|识别|分析|处理)得?怎么样了?|"
        r"(?:任务)?(?:进度|进展|状态)(?:呢|如何|怎么样)?|"
        r"有进展吗|有结果了?吗|(?:好了|完成了|做完了|结束了)(?:没|吗|没有)|"
        r"到哪一步了|还要多久|还在(?:查|调查|研究|识别|分析|处理)吗)[?？。！!]*", value))


def parse_status_query(text):
    """Return a read-only task selector, or None for normal routing.

    Scope is camera, investigations, all, or auto. Optional selection is first,
    latest, or all. R IDs are explicit identifiers, never inferred from ordinals.
    """
    value = _text(text)
    if not value or explicit_retake(value) or _RESULT_REQUEST.search(value) or _CONTROL.search(value):
        return None
    ids = re.findall(r"(?<![a-z0-9_])r\s*([1-9]\d*)(?![a-z0-9_])", value)
    # The selector represents one ID, not a fabricated subset of several jobs.
    if len(set(ids)) > 1:
        return None
    camera = bool(_CAMERA.search(value))
    investigations = bool(ids or _INVESTIGATIONS.search(value))
    known_target = camera or investigations or bool(_TASK.search(value))
    if not (_english_status(value, known_target) or _chinese_status(value, known_target)):
        return None
    scope = "all" if camera and investigations else "camera" if camera else "investigations" if investigations else "auto"
    result = {"scope": scope}
    if ids:
        if camera:
            return None  # Mixed explicit targets need the regular router.
        result["id"] = "R" + str(int(ids[0]))
    if re.search(r"\b(?:all|every|each|both)\b|所有|全部|各个|每个", value):
        result["selection"] = "all"
        if scope == "auto":
            result["scope"] = "all"
    elif re.search(r"\b(?:first|1st)\b|第一个|第一项|首个|首项|最早", value):
        result["selection"] = "first"
    elif re.search(r"\b(?:latest|last|newest|most recent)\b|最近|最新|最后", value):
        result["selection"] = "latest"
    elif scope == "auto" and re.search(r"\b(?:tasks|jobs)\b", value):
        result.update(scope="all", selection="all")
    return result
