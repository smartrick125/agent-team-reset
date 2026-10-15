"""Plan chat-team rotation. Host operations must be performed separately."""
import argparse
import hashlib
import json
import os
from pathlib import Path

ACTIONS = ('begin', 'register', 'seal', 'close-plan', 'maintenance-plan', 'observe', 'renew', 'archived', 'open-plan', 'status')


def read_config(path):
    config = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(config, dict) or not isinstance(config.get('host'), str) or not config['host'].strip():
        raise ValueError('Config requires a nonempty host')
    members = config.get('members')
    if not isinstance(members, list) or not members:
        raise ValueError('Config requires at least one member')
    roles, paths = set(), set()
    for member in members:
        if not isinstance(member, dict) or any(not isinstance(member.get(k), str) or not member[k].strip() for k in ('role', 'title', 'handoff')):
            raise ValueError('Each member requires role, title, and handoff strings')
        if member.get('lifecycle') not in ('task', 'persistent'):
            raise ValueError('Each member requires lifecycle: task or persistent')
        threshold = member.get('max_user_turns')
        if threshold is not None and (type(threshold) is not int or threshold <= 0 or member['lifecycle'] != 'persistent'):
            raise ValueError('max_user_turns must be a positive integer for a persistent member')
        handoff = (path.parent / member['handoff']).resolve()
        if member['role'] in roles or handoff in paths:
            raise ValueError('Roles and handoff paths must be unique')
        roles.add(member['role'])
        paths.add(handoff)
    return config


def digest(path):
    content = path.read_bytes()
    if not content.strip():
        raise ValueError(f'Empty handoff: {path}')
    return hashlib.sha256(content).hexdigest()


def fresh_member():
    return {'thread': None, 'handoff': None, 'archived': False, 'user_turns': None}


def execute(config_path, state_path, host, action, batch, role=None, thread=None, user_turns=None):
    config = read_config(config_path)
    if host != config['host']:
        raise ValueError('Host does not match config')
    if action not in ACTIONS or not batch.strip():
        raise ValueError('Invalid action or empty batch')
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else None
    if state and state['host'] != host:
        raise ValueError('State belongs to a different host')
    if state and state['config'] != config and (action != 'begin' or state['batch'] == batch):
        raise ValueError('Change team membership only when beginning a new batch')
    if action == 'begin':
        if state and state['batch'] == batch:
            return state
        history = state['history'] + [state['batch']] if state else []
        if batch in history:
            raise ValueError('Batch name has already been used')
        retained = {}
        if state:
            new_config = {m['role']: m for m in config['members']}
            for old_config in state['config']['members']:
                old_member = state['members'][old_config['role']]
                if old_member['archived']:
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
        if action in ('register', 'seal', 'archived', 'observe', 'renew') and role not in members:
            raise ValueError('Select a configured role')
        if action in ('register', 'archived', 'observe', 'renew') and (not isinstance(thread, str) or not thread.strip()):
            raise ValueError('A real thread ID is required')
        def due(member_config):
            count = members[member_config['role']]['user_turns']
            limit = member_config.get('max_user_turns')
            return member_config['lifecycle'] == 'persistent' and limit is not None and count is not None and count >= limit
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
            members[role]['handoff'] = digest(config_path.parent / member_config['handoff'])
        elif action == 'observe':
            m = members[role]
            if m['thread'] != thread or m['archived'] or type(user_turns) is not int or user_turns < 0:
                raise ValueError('Observe requires the active thread ID and a nonnegative user turn count')
            if m['user_turns'] is not None and user_turns < m['user_turns']:
                raise ValueError('User turn count cannot decrease for the same thread')
            m['user_turns'] = user_turns
        elif action == 'renew':
            if members[role]['thread'] != thread or not members[role]['archived']:
                raise ValueError('Renew requires a matching completed archive receipt')
            state.setdefault('retired_threads', []).append(thread)
            members[role] = fresh_member()
        elif action in ('close-plan', 'maintenance-plan', 'archived'):
            selected = [m for m in config['members'] if
                        (action != 'maintenance-plan' and m['lifecycle'] == 'task') or due(m)]
            if action == 'archived':
                selected = [m for m in selected if m['role'] == role]
                if not selected:
                    raise ValueError('Member is outside the task scope and has not reached the persistent threshold')
            for member_config in selected:
                m = members[member_config['role']]
                if m['archived']:
                    continue
                if not m['thread'] or not m['handoff'] or m['handoff'] != digest(config_path.parent / member_config['handoff']):
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
    return state


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
    args = parser.parse_args()
    if args.config.resolve() == args.state.resolve():
        parser.error('Config and state must be separate files')
    try:
        result = execute(args.config, args.state, args.host, args.action, args.batch, args.role, args.thread, args.user_turns)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
