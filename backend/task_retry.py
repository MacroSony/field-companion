"""Resolve structured retry selectors against canonical tasks, without parsing speech."""

FAILED = {'failed', 'timed_out', 'cancelled'}


def select_retry_tasks(rows, arguments, context_ids=()):
    scope = arguments.get('scope', 'auto')
    selection = arguments.get('selection')
    task_id = str(arguments.get('id') or '').upper()
    if scope not in ('auto', 'camera', 'investigations', 'all') or selection not in (None, 'first', 'latest', 'all'):
        raise ValueError('Invalid retry task selection')
    selected = list(rows)
    if task_id:
        selected = [row for row in selected if row['id'].upper() == task_id]
    if scope in ('camera', 'investigations'):
        kind = 'camera' if scope == 'camera' else 'investigation'
        selected = [row for row in selected if row['kind'] == kind]
    # A photo's background expert is one logical task, not an ambiguous duplicate.
    if scope in ('auto', 'all') and not task_id:
        linked = {job for row in selected if row['kind'] == 'camera'
                  for job in row.get('related_investigation_ids', ())}
        selected = [row for row in selected if row['id'] not in linked or row['id'] in context_ids]
    if not task_id and selection is None:
        # Once a newer attempt exists, an unqualified retry refers to that
        # attempt, not its failed ancestor (which would start duplicate work).
        superseded = {row.get('parent_id') for row in rows if row.get('parent_id')}
        selected = [row for row in selected if row['id'] not in superseded]
    if not task_id and scope == 'auto' and selection is None and context_ids:
        selected = [row for row in selected if row['id'] in context_ids]
    elif not task_id and selection is None:
        # Unqualified retry never reruns a completed task or invents a new one.
        failed = [row for row in selected if row['status'] in FAILED]
        selected = failed or [row for row in selected if row['status'] in ('waiting', 'running', 'queued')]
    if selection == 'first':
        selected = selected[:1]
    elif selection == 'latest':
        selected = selected[-1:]
    return selected
