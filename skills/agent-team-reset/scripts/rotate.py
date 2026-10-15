"""Plan chat-team rotation. Host operations must be performed separately."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path

ACTIONS = ('begin', 'register', 'seal', 'close-plan', 'maintenance-plan', 'observe', 'count', 'renew', 'archived',
           'open-plan', 'handover-plan', 'handed-over', 'roster', 'status')
KEEP_HISTORY = 10
KEEP_RETIRED = 50
MAX_HANDOFF_LINES = 80
TRANSCRIPT_FORMATS = ('claude', 'codex')
SKIPPED_PREFIXES = ('<system-reminder>', '<local-command', '<command-stdout', '[Request interrupted')


def positive_int(value):
    return type(value) is int and value > 0


def read_config(path):
    config = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(config, dict) or not isinstance(config.get('host'), str) or not config['host'].strip():
        raise ValueError('Config requires a nonempty host')
    members = config.get('members')
    if not isinstance(members, list) or not members:
        raise ValueError('Config requires at least one member')
    for key in ('keep_history', 'max_handoff_lines'):
        if key in config and not positive_int(config[key]):
            raise ValueError(f'{key} must be a positive integer')
    if 'roster' in config and (not isinstance(config['roster'], str) or not config['roster'].strip()):
        raise ValueError('roster must be a file path')
    transcripts = config.get('transcripts')
    if transcripts is not None and (not isinstance(transcripts, dict) or transcripts.get('format') not in TRANSCRIPT_FORMATS
                                    or not isinstance(transcripts.get('dir'), str) or not transcripts['dir'].strip()):
        raise ValueError('transcripts requires format (claude or codex) and dir')
    roles, titles, paths = set(), set(), set()
    for member in members:
        if not isinstance(member, dict) or any(not isinstance(member.get(k), str) or not member[k].strip() for k in ('role', 'title', 'handoff')):
            raise ValueError('Each member requires role, title, and handoff strings')
        if member.get('lifecycle') not in ('task', 'persistent'):
            raise ValueError('Each member requires lifecycle: task or persistent')
        threshold = member.get('max_user_turns')
        if threshold is not None and (not positive_int(threshold) or member['lifecycle'] != 'persistent'):
            raise ValueError('max_user_turns must be a positive integer for a persistent member')
        handoff = (path.parent / member['handoff']).resolve()
        if member['role'] in roles or member['title'] in titles or handoff in paths:
            raise ValueError('Roles, titles, and handoff paths must be unique')
        roles.add(member['role'])
        titles.add(member['title'])
        paths.add(handoff)
    return config


def digest(path, max_lines):
    content = path.read_bytes()
    if not content.strip():
        raise ValueError(f'Empty handoff: {path}')
    lines = len(content.decode('utf-8').splitlines())
    if lines > max_lines:
        raise ValueError(f'Handoff has {lines} lines; trim it to {max_lines} or fewer before sealing: {path}')
    return hashlib.sha256(content).hexdigest()


def fresh_member():
    return {'thread': None, 'handoff': None, 'archived': False, 'user_turns': None}


def is_claude_user_turn(entry):
    if entry.get('type') != 'user' or entry.get('isMeta') or entry.get('isSidechain') or entry.get('isCompactSummary'):
        return False
    content = entry.get('message', {}).get('content')
    if isinstance(content, list):
        if any(isinstance(x, dict) and x.get('type') == 'tool_result' for x in content):
            return False
        content = ' '.join(x.get('text', '') for x in content if isinstance(x, dict) and x.get('type') == 'text')
    return isinstance(content, str) and not content.lstrip().startswith(SKIPPED_PREFIXES)


def read_jsonl(path):
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def claude_turns(directory, title):
    """Unique user messages across every transcript file whose latest chat title is `title`.

    A resumed Claude chat continues in a new file that repeats earlier entries, so messages are
    deduplicated by uuid."""
    seen, found = set(), False
    for path in directory.glob('*.jsonl'):
        last_title, uuids = None, []
        for entry in read_jsonl(path):
            if entry.get('customTitle'):
                last_title = entry['customTitle']
            if is_claude_user_turn(entry) and entry.get('uuid'):
                uuids.append(entry['uuid'])
        if last_title == title:
            found = True
            seen.update(uuids)
    return len(seen) if found else None


def codex_turns(directory, thread):
    """Count identified user.text messages, never execution starts or injected context.

    Missing source metadata means unknown, not zero. Archived copies share message IDs.
    """
    files = list(directory.rglob(f'*{thread}.jsonl'))
    if directory.name == 'sessions':
        files += list((directory.parent / 'archived_sessions').rglob(f'*{thread}.jsonl'))
    if not files:
        return None
    seen, classified = set(), False
    for path in files:
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    return None
                if not isinstance(entry, dict) or not isinstance(entry.get('payload'), dict):
                    return None
                payload = entry['payload']
                if entry.get('type') == 'event_msg' and payload.get('type') == 'user_message':
                    return None  # Legacy events do not establish human provenance.
                if entry.get('type') != 'response_item' or payload.get('role') != 'user':
                    continue
                meta = payload.get('internal_chat_message_metadata_passthrough')
                kinds = meta.get('content_item_kinds') if isinstance(meta, dict) else None
                if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) for k in kinds):
                    return None
                if 'user.text' in kinds:
                    message_id = payload.get('id')
                    if not isinstance(message_id, str) or not message_id:
                        return None
                    seen.add(message_id)
                elif not all(k.startswith(('agents_md.', 'environments.', 'additional_content.')) for k in kinds):
                    return None
                classified = True
    return len(seen) if classified else None


def count_turns(config, member_config, thread):
    transcripts = config.get('transcripts')
    if not transcripts:
        raise ValueError('Config has no transcripts source; record counts with observe')
    directory = Path(os.path.expanduser(transcripts['dir']))
    if not directory.is_dir():
        raise ValueError(f'Transcript directory not found: {directory}')
    if transcripts['format'] == 'claude':
        return claude_turns(directory, member_config['title'])
    return codex_turns(directory, thread)


def render_roster(config, state, config_path):
    """Full team table, rewritten on every saved change so it stays current and never grows."""
    handover = state.get('handover') or {}
    lines = [f"# 成员构成：{state['batch']}", '',
             f"- 平台：{state['host']}；生成：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}；脚本自动生成，勿手改。",
             f"- 移交：{handover.get('status', '无')}" + (f" → {handover['to']}" if handover.get('to') else ''), '',
             '| 角色 | 聊天名 | 类型 | 用户消息 / 上限 | 聊天 ID | 交接文件 | 已封存 | 已归档 |',
             '|---|---|---|---|---|---|---|---|']
    for m in config['members']:
        s = state['members'][m['role']]
        turns = '未知' if s['user_turns'] is None else str(s['user_turns'])
        limit = m.get('max_user_turns') or '—'
        kind = '长期' if m['lifecycle'] == 'persistent' else '阶段性'
        lines.append(f"| {m['role']} | {m['title']} | {kind} | {turns} / {limit} | {s['thread'] or '未登记'} "
                     f"| {m['handoff']} | {'是' if s['handoff'] else '否'} | {'是' if s['archived'] else '否'} |")
    path = config_path.parent / config['roster']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def execute(config_path, state_path, host, action, batch, role=None, thread=None, user_turns=None, to=None):
    config = read_config(config_path)
    max_lines = config.get('max_handoff_lines', MAX_HANDOFF_LINES)
    if host != config['host']:
        raise ValueError('Host does not match config')
    if action not in ACTIONS or not batch.strip():
        raise ValueError('Invalid action or empty batch')
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else None
    if state and state['host'] != host:
        raise ValueError('State belongs to a different host')
    if state and (state.get('handover') or {}).get('status') == 'done' and action != 'status':
        raise ValueError(f"Team was handed over to {state['handover']['to']}; continue with a new state file on that host")
    if state and state['config'] != config and (action != 'begin' or state['batch'] == batch):
        raise ValueError('Change team membership only when beginning a new batch')
    result = None
    if action == 'begin':
        if state and state['batch'] == batch:
            return state
        history = (state['history'] + [state['batch']] if state else [])[-config.get('keep_history', KEEP_HISTORY):]
        if batch in history:
            raise ValueError('Batch name has already been used')
        retained = {}
        if state:
            if state.get('handover'):
                raise ValueError('Finish the handover before beginning a new batch')
            new_config = {m['role']: m for m in config['members']}
            for old_config in state['config']['members']:
                old_member = state['members'][old_config['role']]
                if old_member['archived'] or not old_member['thread']:
                    continue
                if old_config['lifecycle'] == 'task':
                    raise ValueError('Archive old task members before beginning a new batch')
                if new_config.get(old_config['role']) != old_config:
                    raise ValueError('Cannot remove or change a live persistent member during task rollover')
                retained[old_config['role']] = old_member
        state = {'host': host, 'config': config, 'batch': batch, 'history': history,
                 'retired_threads': state.get('retired_threads', []) if state else [],
                 'members': {m['role']: retained.get(m['role'], fresh_member()) for m in config['members']}}
    else:
        if not state or state['batch'] != batch:
            raise ValueError('Batch does not match saved state')
        members = state['members']
        handing_over = bool(state.get('handover'))
        if action in ('register', 'seal', 'archived', 'observe', 'renew') and role not in members:
            raise ValueError('Select a configured role')
        if action in ('register', 'archived', 'observe', 'renew') and (not isinstance(thread, str) or not thread.strip()):
            raise ValueError('A real thread ID is required')
        if handing_over and action in ('register', 'renew', 'open-plan'):
            raise ValueError('The team is being handed over; finish with handed-over')

        def due(member_config):
            count = members[member_config['role']]['user_turns']
            limit = member_config.get('max_user_turns')
            return member_config['lifecycle'] == 'persistent' and limit is not None and count is not None and count >= limit

        def sealed(member_config):
            m = members[member_config['role']]
            return bool(m['thread'] and m['handoff'] and m['handoff'] == digest(config_path.parent / member_config['handoff'], max_lines))

        if action == 'register':
            if thread in state.get('retired_threads', []):
                raise ValueError('Cannot register a retired thread')
            if any(m['thread'] == thread for r, m in members.items() if r != role):
                raise ValueError('Thread ID belongs to another role')
            if members[role]['thread'] not in (None, thread):
                raise ValueError('Cannot replace an existing thread ID')
            members[role]['thread'] = thread
        elif action == 'seal':
            if not members[role]['thread'] or members[role]['archived']:
                raise ValueError('Register an active thread before sealing')
            member_config = next(m for m in config['members'] if m['role'] == role)
            members[role]['handoff'] = digest(config_path.parent / member_config['handoff'], max_lines)
        elif action == 'observe':
            m = members[role]
            if m['thread'] != thread or m['archived'] or type(user_turns) is not int or user_turns < 0:
                raise ValueError('Observe requires the active thread ID and a nonnegative user turn count')
            if m['user_turns'] is not None and user_turns < m['user_turns']:
                raise ValueError('User turn count cannot decrease for the same thread')
            m['user_turns'] = user_turns
        elif action == 'count':
            result = []
            for member_config in config['members']:
                m = members[member_config['role']]
                if not m['thread'] or m['archived']:
                    continue
                counted = count_turns(config, member_config, m['thread'])
                if counted is None:
                    note = 'complete transcript count unavailable; use observe with a verified cumulative count'
                elif m['user_turns'] is not None and counted < m['user_turns']:
                    note = (f"count fell from {m['user_turns']} to {counted}; kept the recorded value; "
                            'audit the old count before using threshold plans; observe cannot lower it')
                else:
                    m['user_turns'], note = counted, 'recorded'
                result.append({'role': member_config['role'], 'user_turns': m['user_turns'], 'due': due(member_config), 'note': note})
        elif action == 'renew':
            if members[role]['thread'] != thread or not members[role]['archived']:
                raise ValueError('Renew requires a matching completed archive receipt')
            state['retired_threads'] = (state.get('retired_threads', []) + [thread])[-KEEP_RETIRED:]
            members[role] = fresh_member()
        elif action == 'handover-plan':
            state['handover'] = {'status': 'closing'}
            result = [{'role': m['role'], 'threadId': members[m['role']]['thread'], 'sealed': sealed(m)}
                      for m in config['members'] if members[m['role']]['thread'] and not members[m['role']]['archived']]
        elif action == 'handed-over':
            if not handing_over:
                raise ValueError('Run handover-plan first')
            if not isinstance(to, str) or not to.strip() or to == host:
                raise ValueError('handed-over requires --to with the receiving host')
            live = [m['role'] for m in config['members'] if members[m['role']]['thread'] and not members[m['role']]['archived']]
            if live:
                raise ValueError(f"Archive every member before handing over: {', '.join(live)}")
            state['handover'] = {'status': 'done', 'to': to, 'at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}
        elif action in ('close-plan', 'maintenance-plan', 'archived'):
            selected = [m for m in config['members'] if handing_over or
                        (action != 'maintenance-plan' and m['lifecycle'] == 'task') or due(m)]
            if action == 'archived':
                selected = [m for m in selected if m['role'] == role]
                if not selected:
                    raise ValueError('Member is outside the task scope and has not reached the persistent threshold')
            for member_config in selected:
                if not members[member_config['role']]['archived'] and not sealed(member_config):
                    raise ValueError(f"Confirm and seal the current handoff: {member_config['role']}")
            if action in ('close-plan', 'maintenance-plan'):
                return [{'role': m['role'], 'threadId': members[m['role']]['thread']} for m in selected if not members[m['role']]['archived']]
            if members[role]['thread'] != thread:
                raise ValueError('Archive receipt ID does not match')
            members[role]['archived'] = True
        elif action == 'open-plan':
            return [{'role': m['role'], 'title': m['title'], 'batch': batch,
                     'prompt': f"Batch {batch}. Role {m['role']}. Read the project instructions and your handoff at {m['handoff']}. Report your next action and wait before starting new work."}
                    for m in config['members'] if not members[m['role']]['thread']]
        elif action == 'status':
            return state
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, state_path)
    if config.get('roster'):
        render_roster(config, state, config_path)
    return state if result is None else result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('action', choices=ACTIONS)
    parser.add_argument('batch')
    parser.add_argument('--role')
    parser.add_argument('--thread')
    parser.add_argument('--user-turns', type=int)
    parser.add_argument('--to', help='Receiving host for handed-over')
    args = parser.parse_args()
    if args.config.resolve() == args.state.resolve():
        parser.error('Config and state must be separate files')
    try:
        result = execute(args.config, args.state, args.host, args.action, args.batch, args.role, args.thread, args.user_turns, args.to)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
