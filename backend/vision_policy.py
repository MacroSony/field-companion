"""Taxonomy and conservative visual evidence shared by service, app and evals."""

GROUPS = ('Aves', 'Insecta', 'Plantae', 'Mammalia', 'Fungi', 'Reptilia',
          'Amphibia', 'Arachnida', 'Mollusca', 'Actinopterygii')
GROUP_LABELS = {
    'Aves': ('bird', '鸟'), 'Insecta': ('insect', '昆虫'),
    'Plantae': ('plant', '植物'), 'Mammalia': ('mammal', '哺乳动物'),
    'Fungi': ('fungus', '真菌'), 'Reptilia': ('reptile', '爬行动物'),
    'Amphibia': ('amphibian', '两栖动物'), 'Arachnida': ('arachnid', '蛛形动物'),
    'Mollusca': ('mollusc', '软体动物'), 'Actinopterygii': ('fish', '鱼'),
}


def iconic_group(lineage):
    """Use model taxonomy, including reptiles split into several classes."""
    kingdom, phylum, taxon_class, order = (list(lineage)+['']*4)[:4]
    if kingdom in ('Plantae', 'Fungi'):
        return kingdom
    if phylum == 'Mollusca':
        return 'Mollusca'
    if taxon_class in GROUPS:
        return taxon_class
    if taxon_class in ('Reptilia', 'Squamata', 'Testudines', 'Crocodylia') or order in ('Squamata', 'Testudines', 'Crocodylia'):
        return 'Reptilia'
    return 'Other'


def assess_visual(result, target_group=None):
    """Conditional rankings cannot override weak original image evidence."""
    candidates = result.get('candidates', [])
    scope = result.get('scope') or {}
    open_top = result.get('open_top') or []
    unrestricted_group = open_top[0].get('group') if open_top else None
    conflict = bool(target_group and unrestricted_group in GROUPS
                    and unrestricted_group != target_group)
    conflict |= bool(target_group and scope.get('category_conflict'))
    if candidates:
        first = float(candidates[0].get('global_p', candidates[0]['p']))
        second = max((float(c.get('global_p', c['p'])) for c in candidates[1:]), default=0)
        weak = first < .5 or first-second < .2 or bool(target_group and 'global_p' not in candidates[0])
    else:
        weak = True
    return {'status': 'needs_target_confirmation' if conflict else
                     'needs_more_evidence' if weak else 'supported',
            'target_group': target_group, 'unrestricted_group': unrestricted_group,
            'category_conflict': conflict, 'weak_visual_evidence': weak,
            'group_mass': scope.get('group_mass'),
            'candidate_mass': scope.get('candidate_mass')}


def category_question(group, chinese=False):
    en, zh = GROUP_LABELS.get(group, ('subject', '主体'))
    return (f'照片中的证据和你要识别的{zh}不一致。能在预览里点选{zh}，拍近一点后让我再看一次吗？'
            if chinese else f'The photo gives conflicting evidence for the {en} you asked about. '
            f'Could you tap that {en} in the preview, frame it more closely, and ask me to look again?')
