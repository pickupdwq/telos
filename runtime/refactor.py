"""Public refactoring brief and admission of a particular, user-approved revision."""
from pathlib import Path
import json
import re
import time
import refactor_design

from codex_rpc import RPCError
from providers import ProviderError as GateError

FIELDS = ('scope', 'preserve', 'improve', 'verify')


# Conservative backstop for the explicit user command. Ordinary changes that
# merely mention refactoring keep the existing implementation workflow.
def explicit_request(task):
    if not isinstance(task, str):
        return False
    return bool(re.fullmatch(
        r'(?:请)?(?:使用|用)\s*telos\s*(?:来)?(?:对)?(?:这个|当前|该)(?:老)?(?:项目|仓库)(?:进行)?重构'
        r'|(?:please\s+)?use\s+telos\s+to\s+refactor\s+(?:this|the current)\s+(?:project|repository|repo)',
        task.strip().rstrip('。.!！').strip(), re.I))


def validate(plan):
    if not isinstance(plan, dict) or set(plan) != set(FIELDS):
        raise GateError('A refactor brief needs scope, preserve, improve and verify.', code='invalid_arguments')
    for field in FIELDS:
        items = plan[field]
        if not isinstance(items, list) or not items or any(not isinstance(x, str) or not x.strip() for x in items):
            raise GateError(f'Refactor {field} must list concrete public points.', code='invalid_arguments')
    return plan


def proposal(b, project):
    plan = project.get('contract', {}).get('refactor_plan')
    if plan is None:
        return None
    batches = [{k: task[k] for k in ('id', 'task', 'message', 'allowed_paths', 'depends_on', 'refactor_flows') if k in task}
               for task in project['tasks']]
    signature = b.digest({'project': project['project_id'], 'base': project['base_commit'],
                          'contract': project['contract_hash'], 'batches': batches})
    receipt = project.get('refactor_approval') or {}
    if receipt.get('signature') == signature:
        return {'revision': signature, 'confirmed': True}
    planning = project['contract'].get('planning', {})
    if 'refactor_design' not in planning:
        return {'revision': signature, 'confirmed': False, 'next_action': 'telos_meeting_open',
                'reason': 'This pending plan predates the walkthrough. Prepare a replacement from a refactor discussion.'}
    folder = b.project_manager().path(planning['meeting_id']).parent
    return {'plan': plan, 'batches': batches, 'revision': signature, 'confirmed': False,
            'current_prd': planning['current_prd'], 'design': planning['refactor_design'],
            'documents': {'current_prd': str(folder / 'current-prd.md'), 'design': str(folder / 'refactor-design.md')},
            'confirm_text': '确认重构 ' + signature[:12]}


def waiting(b, project):
    info = proposal(b, project)
    return info is not None and not info['confirmed']


def text_content(parts):
    if isinstance(parts, str):
        return parts.strip()
    if not isinstance(parts, list) or not all(isinstance(x, dict) and x.get('type') == 'text' for x in parts):
        return ''
    return '\n'.join(x.get('text', '') for x in parts if isinstance(x.get('text'), str)).strip()


def claude_exchange(owner):
    import claude_supervisor as claude
    attestation = claude.CURRENT.get('attestation') or {}
    path = attestation.get('transcript_path')
    if not path or not claude.is_current(owner['thread_id']):
        return None
    previous = ''
    latest = None
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get('sessionId') != owner['thread_id'] or row.get('isSidechain'):
                continue
            msg = row.get('message') or {}
            if row.get('type') == 'assistant':
                for part in msg.get('content', []):
                    if isinstance(part, dict) and part.get('type') == 'text':
                        previous = str(part.get('text', ''))
            elif row.get('type') == 'user' and not row.get('isMeta'):
                content = msg.get('content')
                if isinstance(content, list) and any(isinstance(x, dict) and x.get('type') == 'tool_result' for x in content):
                    continue
                latest = (row.get('uuid'), text_content(content), previous)
                previous = ''
    return latest


def latest_exchange(b, owner):
    if owner['transport'] == 'claude_hook':
        return claude_exchange(owner)
    with b.owner_rpc(owner) as rpc:
        turns = b.recent_turns(rpc, owner, limit=3)
    # The visible prompt immediately preceding this user reply can bind a plain
    # "yes" to the plan. Clients without assistant history use the short code.
    previous = ''
    latest = None
    for turn in reversed(turns):
        for index, item in enumerate(turn.get('items', [])):
            if item.get('type') == 'agentMessage':
                previous = str(item.get('text', ''))
            elif item.get('type') == 'userMessage' and turn.get('id'):
                key = item.get('id') or f"{turn['id']}:{index}"
                latest = (key, text_content(item.get('content')), previous)
                previous = ''
    return latest


def check_revision(b, project, revision):
    info = proposal(b, project)
    if info and info.get('next_action') == 'telos_meeting_open':
        raise GateError(info['reason'], code='refactor_meeting_required', next_action='telos_meeting_open')
    if info is None or revision != info['revision']:
        raise GateError('The refactoring plan changed or does not exist. Show the current plan.',
                        code='refactor_plan_mismatch', next_action='telos_project_status')
    if not info['confirmed']:
        planning = project['contract']['planning']
        refactor_design.verify_documents(b.project_manager().path(planning['meeting_id']).parent, planning['document_hashes'])
    return info


def read_reply(b, owner):
    try:
        return latest_exchange(b, owner)
    except (OSError, ValueError, RPCError):
        return None


def confirm(b, project, revision, exchange):
    info = check_revision(b, project, revision)
    if info['confirmed']:
        return
    if exchange:
        message_id, reply, prompt = exchange
        reply = reply.strip().rstrip('。.!！').strip().casefold()
        code = info['revision'][:12]
        explicit = reply in (info['confirm_text'].casefold(), 'confirm refactor ' + code)
        short = reply in ('好', '好的', '可以', '可以的', '确认', '确认以上几点', '同意', '按这个方案执行', '确认，按这个方案执行', 'yes', 'approved', 'confirm')
        # If multiple revisions are being discussed a bare yes is ambiguous.
        markers = re.findall(r'(?:确认重构|confirm refactor)\s+([0-9a-f]{12})\b', prompt, re.I)
        if message_id and (explicit or (short and set(markers) == {code})):
            record = {'signature': info['revision'], 'message_id': message_id, 'at': time.time(),
                      'owner_thread_id': project['owner']['thread_id']}
            project['refactor_approval'] = record
            project['history'].append({'action': 'refactor_confirmed', **record})
            return
    raise GateError('Await the user in the original conversation. Show the points and ask them to reply: '
                    + info['confirm_text'], code='refactor_confirmation_required',
                    next_action='ask_user_to_confirm_refactor')
