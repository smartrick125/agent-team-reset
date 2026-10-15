"""Plan chat-team rotation. Host operations must be performed separately."""
import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

ACTIONS = ('begin', 'register', 'seal', 'close-plan', 'maintenance-plan', 'observe', 'count', 'renew', 'archived',
           'open-plan', 'handover-plan', 'handed-over', 'roster', 'status', 'check', 'close-team',
           'policy', 'migrate-handoffs', 'configure-source', 'read-receipt', 'cleanup')
CLASS_LIMITS = {'maintenance': 30, 'regular': 20}
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
    if 'tier' in config and not isinstance(config['tier'], str):
        raise ValueError('tier is an optional descriptive string, never an execution policy')
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
        category = member.get('persistent_class')
        if category is not None:
            if member['lifecycle'] != 'persistent' or category not in CLASS_LIMITS:
                raise ValueError('persistent_class requires a persistent maintenance or regular member')
            if threshold is not None and threshold != CLASS_LIMITS[category]:
                raise ValueError('max_user_turns must match the persistent_class limit (30 or 20)')
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


def effective_limit(member):
    if member.get('lifecycle') != 'persistent':
        return None
    return CLASS_LIMITS.get(member.get('persistent_class'), member.get('max_user_turns', CLASS_LIMITS['regular']))


def ordinary_file(path):
    # Resolve only after checking every component, including Windows junctions.
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError(f'Links are not valid handoffs: {path}')
    if not path.is_file():
        raise ValueError(f'Handoff must be an existing regular file: {path}')
    return path


def dedicated_handoff(config_path, relative):
    path = config_path.parent / relative
    ordinary_file(path)
    root = (config_path.parent / 'handoffs').resolve()
    if not path.resolve().is_relative_to(root) or path.resolve() == root:
        raise ValueError('Cleanup and migration require a dedicated file under config-directory/handoffs/')
    return path


def next_member(member):
    result = fresh_member()
    if member.get('handoff'):
        result['handoff'] = member['handoff']
        result['pending_handoff'] = {'thread': member['thread'], 'sha256': member['handoff']}
    return result


def save_state(config_path, state_path, config, state):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, state_path)
    if config.get('roster'):
        render_roster(config, state, config_path)


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


def claude_turns(directory, title, thread=None):
    """Unique messages in the active session, with title matching for standalone legacy audits.

    A resumed Claude chat continues in a new file that repeats earlier entries, so messages are
    deduplicated by uuid."""
    seen, found = set(), False
    for path in directory.glob('*.jsonl'):
        last_title, uuids = None, []
        matched_thread = path.stem == thread if thread is not None else False
        for entry in read_jsonl(path):
            if thread is not None:
                session = entry.get('sessionId')
                if session == thread:
                    matched_thread = True
                if session != thread and not (session is None and path.stem == thread):
                    continue
            if entry.get('customTitle'):
                last_title = entry['customTitle']
            if is_claude_user_turn(entry) and entry.get('uuid'):
                uuids.append(entry['uuid'])
        matches = matched_thread if thread is not None else last_title == title
        if matches:
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
        return claude_turns(directory, member_config['title'], thread)
    return codex_turns(directory, thread)


def render_roster(config, state, config_path):
    """Full team table, rewritten on every saved change so it stays current and never grows."""
    handover = state.get('handover') or {}
    lines = [f"# 成员构成：{state['batch']}", '',
             f"- 平台：{state['host']}；生成：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}；脚本自动生成，勿手改。",
             f"- 运行模式：{state.get('mode', 'running')}",
             f"- 移交：{handover.get('status', '无')}" + (f" → {handover['to']}" if handover.get('to') else ''), '',
             '| 角色 | 聊天名 | 类型 | 用户消息 / 上限 | 聊天 ID | 交接文件 | 已封存 | 已归档 |',
             '|---|---|---|---|---|---|---|---|']
    for m in config['members']:
        s = state['members'][m['role']]
        turns = '未知' if s['user_turns'] is None else str(s['user_turns'])
        limit = effective_limit(m) or '—'
        kind = '长期' if m['lifecycle'] == 'persistent' else '阶段性'
        lines.append(f"| {m['role']} | {m['title']} | {kind} | {turns} / {limit} | {s['thread'] or '未登记'} "
                     f"| {m['handoff']} | {'是' if s['handoff'] else '否'} | {'是' if s['archived'] else '否'} |")
    path = config_path.parent / config['roster']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def execute(config_path, state_path, host, action, batch, role=None, thread=None, user_turns=None, to=None,
            sha256=None, apply=False, live_threads=None, refresh_counts=False):
    config = read_config(config_path)
    max_lines = config.get('max_handoff_lines', MAX_HANDOFF_LINES)
    if host != config['host']:
        raise ValueError('Host does not match config')
    if action not in ACTIONS or not batch.strip():
        raise ValueError('Invalid action or empty batch')
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else None
    if state and state['host'] != host:
        raise ValueError('State belongs to a different host')
    if state and (state.get('handover') or {}).get('status') == 'done' and action not in ('status', 'check'):
        raise ValueError(f"Team was handed over to {state['handover']['to']}; continue with a new state file on that host")
    if state and state['config'] != config and action not in ('policy', 'migrate-handoffs', 'configure-source') and (action != 'begin' or state['batch'] == batch):
        raise ValueError('Change team membership only when beginning a new batch')
    if state:
        state.setdefault('mode', 'handing_over' if state.get('handover') else 'running')
    result = None
    if action == 'begin':
        if state and state['batch'] == batch:
            return state
        history = (state['history'] + [state['batch']] if state else [])[-config.get('keep_history', KEEP_HISTORY):]
        if batch in history:
            raise ValueError('Batch name has already been used')
        retained = {}
        if state:
            if state['mode'] == 'closing' and any(m['thread'] and not m['archived'] for m in state['members'].values()):
                raise ValueError('Finish closing every live member before starting a new batch')
            if state.get('handover'):
                raise ValueError('Finish the handover before beginning a new batch')
            new_config = {m['role']: m for m in config['members']}
            for old_config in state['config']['members']:
                old_member = state['members'][old_config['role']]
                if old_member['archived']:
                    if old_config['role'] in new_config:
                        retained[old_config['role']] = next_member(old_member)
                    continue
                if not old_member['thread']:
                    if old_config['role'] in new_config:
                        retained[old_config['role']] = old_member
                    continue
                if old_config['lifecycle'] == 'task':
                    raise ValueError('Archive old task members before beginning a new batch')
                if new_config.get(old_config['role']) != old_config:
                    raise ValueError('Cannot remove or change a live persistent member during task rollover')
                retained[old_config['role']] = old_member
        retired = state.get('retired_threads', []) if state else []
        if state:
            retired = list(dict.fromkeys(retired + [m['thread'] for m in state['members'].values() if m['archived'] and m['thread']]))[-KEEP_RETIRED:]
        state = {'host': host, 'config': config, 'batch': batch, 'history': history, 'mode': 'running',
                 'retired_threads': retired,
                 'members': {m['role']: retained.get(m['role'], fresh_member()) for m in config['members']}}
    else:
        if not state or state['batch'] != batch:
            raise ValueError('Batch does not match saved state')
        members = state['members']
        handing_over = bool(state.get('handover'))
        if action in ('register', 'seal', 'archived', 'observe', 'renew', 'read-receipt') and role not in members:
            raise ValueError('Select a configured role')
        if action in ('register', 'archived', 'observe', 'renew', 'read-receipt') and (not isinstance(thread, str) or not thread.strip()):
            raise ValueError('A real thread ID is required')
        if handing_over and action in ('register', 'renew', 'open-plan'):
            raise ValueError('The team is being handed over; finish with handed-over')
        if state['mode'] != 'running' and action in ('register', 'renew'):
            raise ValueError('Team is not running; do not rebuild a stopped team')

        def due(member_config):
            count = members[member_config['role']]['user_turns']
            limit = effective_limit(member_config)
            return member_config['lifecycle'] == 'persistent' and limit is not None and count is not None and count >= limit

        def sealed(member_config):
            m = members[member_config['role']]
            path = config_path.parent / member_config['handoff']
            return bool(m['thread'] and m['handoff'] and not m.get('pending_handoff') and path.is_file()
                        and m['handoff'] == digest(ordinary_file(path), max_lines))

        def action_plan():
            actions, missing, reached = [], [], []
            count_status, audit_roles = [], set()
            if refresh_counts:
                for mc in config['members']:
                    m = members[mc['role']]
                    if not m['thread'] or m['archived']:
                        continue
                    counted, note = None, 'source unavailable; configure-source or observe a verified complete count'
                    if config.get('transcripts'):
                        try:
                            counted = count_turns(config, mc, m['thread'])
                        except (ValueError, OSError) as error:
                            note = str(error)
                    status = 'unknown'
                    if counted is not None:
                        if m['user_turns'] is not None and counted < m['user_turns']:
                            status = 'audit'
                            note = f"count fell from {m['user_turns']} to {counted}; audit before rotation"
                        else:
                            m['user_turns'] = counted  # In-memory only; check never saves shared state.
                            status, note = 'verified', 'fresh transcript count, not persisted'
                    if status != 'verified':
                        audit_roles.add(mc['role'])
                    count_status.append({'role': mc['role'], 'status': status, 'user_turns': m['user_turns'],
                                         'transcript_user_turns': counted, 'note': note})
            live = None
            if live_threads is not None:
                live = json.loads(Path(live_threads).read_text(encoding='utf-8'))
                if not isinstance(live, list) or not all(isinstance(t, str) and t.strip() for t in live):
                    raise ValueError('live-threads must contain a JSON list of real thread IDs')
            for mc in config['members']:
                r, m = mc['role'], members[mc['role']]
                absent = not m['thread'] or m['archived'] or (live is not None and m['thread'] not in live)
                if absent:
                    missing.append(r)
                if due(mc) and not m['archived'] and r not in audit_roles:
                    reached.append(r)
                operation = None
                if state['mode'] == 'running':
                    if not m['thread']:
                        operation = 'create'
                    elif m['archived']:
                        operation = 'renew'
                    elif live is not None and m['thread'] not in live:
                        operation = 'reconcile'
                    elif r in audit_roles:
                        operation = 'audit'
                    elif due(mc):
                        operation = 'archive' if sealed(mc) else 'handoff'
                elif state['mode'] in ('closing', 'handing_over') and m['thread'] and not m['archived']:
                    operation = 'archive' if sealed(mc) else 'handoff'
                if operation:
                    instructions = {'create': 'Create only this configured role, then register the real ID.',
                                    'renew': 'Use the archive receipt to renew, then create and register a fresh chat.',
                                    'reconcile': 'Verify host status and a sealed handoff before recording an archive receipt.',
                                    'audit': 'Verify the complete count or configure its source before automatic rotation.',
                                    'handoff': 'At a safe stopping point overwrite core handoff, then confirm and seal it.',
                                    'archive': 'Archive the sealed chat through the host, then record archived.'}
                    actions.append({'role': r, 'action': operation, 'threadId': m['thread'],
                                    'sha256': m['handoff'], 'next_action': instructions[operation]})
            return {'mode': state['mode'], 'missing': missing, 'due': reached, 'actions': actions,
                    'count_status': count_status}

        if action == 'register':
            if members[role]['archived']:
                raise ValueError('Renew the archived member before registering a replacement')
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
            if members[role].get('pending_handoff') and not members[role].get('read_receipt'):
                raise ValueError('Record the new chat read-receipt before overwriting its inherited handoff')
            members[role]['handoff'] = digest(ordinary_file(config_path.parent / member_config['handoff']), max_lines)
            members[role].pop('pending_handoff', None)
            members[role].pop('read_receipt', None)
            members[role].pop('handoff_cleaned', None)
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
            pending = members[role].get('pending_handoff')
            if not members[role]['thread'] and pending and pending['thread'] == thread:
                return state
            if members[role]['thread'] != thread or not members[role]['archived']:
                raise ValueError('Renew requires a matching completed archive receipt')
            state['retired_threads'] = list(dict.fromkeys(state.get('retired_threads', []) + [thread]))[-KEEP_RETIRED:]
            members[role] = next_member(members[role])
        elif action in ('policy', 'migrate-handoffs', 'configure-source'):
            allowed = {'persistent_class', 'max_user_turns'} if action == 'policy' else {'handoff'} if action == 'migrate-handoffs' else set()
            def without_changes(value):
                unchanged = dict(value, members=[{k: v for k, v in m.items() if k not in allowed} for m in value['members']])
                if action == 'policy':
                    unchanged.pop('tier', None)  # Descriptive legacy metadata has no operational effect.
                if action == 'configure-source':
                    unchanged.pop('transcripts', None)
                return unchanged
            if without_changes(state['config']) != without_changes(config):
                raise ValueError('Migration may change only its designated fields, never membership or lifecycle')
            if action == 'configure-source':
                source = config.get('transcripts')
                if not source or not Path(os.path.expanduser(source['dir'])).is_dir():
                    raise ValueError('configure-source requires a configured transcripts directory that exists')
            if action == 'migrate-handoffs':
                for old, new in zip(state['config']['members'], config['members']):
                    if old['handoff'] == new['handoff']:
                        continue
                    previous = ordinary_file(config_path.parent / old['handoff'])
                    destination = dedicated_handoff(config_path, new['handoff'])
                    old_hash = digest(previous, max_lines)
                    if digest(destination, max_lines) != old_hash or members[old['role']]['handoff'] not in (None, old_hash):
                        raise ValueError('Migration requires identical copied and sealed handoff content; preserve the original')
            state['config'] = config
        elif action == 'check':
            return action_plan()
        elif action == 'close-team':
            if state['mode'] == 'handing_over':
                raise ValueError('Finish the host handover before closing the team')
            if state['mode'] != 'closed':
                state['mode'] = 'closing'
                if not any(m['thread'] and not m['archived'] for m in members.values()):
                    state['mode'] = 'closed'
            # Save intent before reading handoffs or returning archive instructions.
            save_state(config_path, state_path, config, state)
            return action_plan()
        elif action == 'read-receipt':
            m = members[role]
            pending = m.get('pending_handoff')
            if m['thread'] != thread or m['archived'] or not pending or sha256 != pending['sha256']:
                raise ValueError('read-receipt requires the active replacement thread and inherited sealed hash')
            mc = next(mc for mc in config['members'] if mc['role'] == role)
            receipt = {'thread': thread, 'sha256': sha256}
            if m.get('handoff_cleaned') and m.get('read_receipt') == receipt:
                return state
            if digest(ordinary_file(config_path.parent / mc['handoff']), max_lines) != sha256:
                raise ValueError('Inherited handoff changed before read confirmation')
            m['read_receipt'] = receipt
        elif action == 'cleanup':
            if role is not None and role not in members:
                raise ValueError('Select a configured role')
            items = []
            for mc in config['members']:
                if role is not None and mc['role'] != role:
                    continue
                m = members[mc['role']]
                pending, receipt = m.get('pending_handoff'), m.get('read_receipt')
                if m.get('handoff_cleaned'):
                    continue
                if (not pending or not receipt or m['archived'] or receipt['thread'] != m['thread']
                        or receipt['sha256'] != pending['sha256'] or m['handoff'] != pending['sha256']):
                    if role:
                        raise ValueError('Cannot clean an unconsumed handoff; obtain the active new chat read-receipt')
                    continue
                raw_path = config_path.parent / mc['handoff']
                if m.get('cleanup_pending') == receipt['sha256'] and not raw_path.exists() and not raw_path.is_symlink():
                    if apply:
                        m['handoff_cleaned'] = True
                        m.pop('cleanup_pending', None)
                    continue
                path = dedicated_handoff(config_path, mc['handoff'])
                protected = {config_path.resolve(), state_path.resolve()}
                if config.get('roster'):
                    protected.add((config_path.parent / config['roster']).resolve())
                if path.resolve() in protected:
                    raise ValueError('Cannot clean a configuration, state or roster file')
                if digest(path, max_lines) != receipt['sha256']:
                    raise ValueError('Handoff changed since consumption; refuse cleanup')
                items.append({'role': mc['role'], 'path': str(path), 'sha256': receipt['sha256'], 'action': 'delete'})
            if apply:
                for item in items:
                    path = dedicated_handoff(config_path, next(mc['handoff'] for mc in config['members'] if mc['role'] == item['role']))
                    if digest(path, max_lines) != item['sha256']:
                        raise ValueError('Handoff changed during cleanup')
                    # Persist the exact approved hash before unlink: retry can reconcile a crash after deletion.
                    members[item['role']]['cleanup_pending'] = item['sha256']
                    save_state(config_path, state_path, config, state)
                    path.unlink()
                    members[item['role']]['handoff_cleaned'] = True
                    members[item['role']].pop('cleanup_pending', None)
            result = {'mode': state['mode'], 'items': items, 'applied': apply}
            if not apply:
                return result
        elif action == 'handover-plan':
            if state['mode'] in ('closing', 'closed'):
                raise ValueError('Team is closing or closed; do not start a host handover')
            state['mode'] = 'handing_over'
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
            state['mode'] = 'closed'
        elif action in ('close-plan', 'maintenance-plan', 'archived'):
            selected = [m for m in config['members'] if members[m['role']]['thread'] and
                        (handing_over or state['mode'] in ('closing', 'closed') or
                         (action != 'maintenance-plan' and m['lifecycle'] == 'task') or due(m))]
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
            if state['mode'] == 'closing' and not any(m['thread'] and not m['archived'] for m in members.values()):
                state['mode'] = 'closed'
        elif action == 'open-plan':
            if state['mode'] != 'running':
                return []
            return [{'role': m['role'], 'title': m['title'], 'batch': batch,
                     'inherited_sha256': members[m['role']].get('pending_handoff', {}).get('sha256'),
                     'prompt': (f"Batch {batch}. Role {m['role']}. Read the project instructions. "
                                f"If an inherited handoff is recorded, read {m['handoff']} and confirm its sealed hash "
                                'with your actual new chat ID before replacing or cleaning it. '
                                'Report the next authorized action and any blocker.')}
                    for m in config['members'] if not members[m['role']]['thread']]
        elif action == 'status':
            return state
    save_state(config_path, state_path, config, state)
    return state if result is None else result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
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
    parser.add_argument('--sha256', help='Sealed handoff hash confirmed read by the new chat')
    parser.add_argument('--apply', action='store_true', help='Execute cleanup, otherwise preview only')
    parser.add_argument('--live-threads', type=Path, help='Host-verified JSON list of active chat IDs for check')
    parser.add_argument('--refresh-counts', action='store_true', help='Read fresh counts in check without saving state')
    args = parser.parse_args()
    if args.config.resolve() == args.state.resolve():
        parser.error('Config and state must be separate files')
    try:
        if args.apply and args.action != 'cleanup':
            raise ValueError('--apply is only supported for cleanup')
        if args.live_threads and args.action != 'check':
            raise ValueError('--live-threads is only supported for check')
        if args.refresh_counts and args.action != 'check':
            raise ValueError('--refresh-counts is only supported for check')
        result = execute(args.config, args.state, args.host, args.action, args.batch, args.role, args.thread, args.user_turns, args.to,
                         args.sha256, args.apply, args.live_threads, args.refresh_counts)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
