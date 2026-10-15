"""rotate.py 行为测试：自动计数、成员表、清理上限、全组移交。由 检查.ps1 调用。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'skills' / 'agent-team-reset' / 'scripts'))
import rotate  # noqa: E402


def expect_error(text, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError as error:
        assert text in str(error), f'错误信息不对：{error}'
        return
    raise AssertionError(f'应该报错：{text}')


def write_jsonl(path, entries):
    path.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in entries) + '\n', encoding='utf-8')


def user(uuid, text):
    return {'type': 'user', 'uuid': uuid, 'message': {'role': 'user', 'content': text}}


def codex_message(message_id, kinds):
    return {'type': 'response_item', 'payload': {
        'type': 'message', 'role': 'user', 'id': message_id,
        'internal_chat_message_metadata_passthrough': {'content_item_kinds': kinds}}}


def check_codex_counts(root):
    directory = root / 'codex-cases'
    directory.mkdir()
    started = {'type': 'event_msg', 'payload': {'type': 'task_started'}}
    injected = codex_message('injection', ['agents_md.instructions', 'environments.environment_context'])
    human = codex_message('human-1', ['user.text'])
    path = directory / 'rollout-case-C1.jsonl'
    # 建聊和转发仅启动执行，有完整来源标记的注入不计人工消息。
    write_jsonl(path, [started, injected, started])
    assert rotate.codex_turns(directory, 'C1') == 0
    # 取消保留已提交消息；无新输入的重试不增加；人工继续与新提交分别计数。
    entries = [started, injected, human,
               {'type': 'event_msg', 'payload': {'type': 'turn_aborted'}},
               started, human, codex_message('human-2', ['user.text'])]
    write_jsonl(path, entries)
    write_jsonl(directory / 'copy-C1.jsonl', entries)
    assert rotate.codex_turns(directory, 'C1') == 2
    # 混合旧格式、缺失身份、损坏记录都不能声称完整计数。
    for unknown in (
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user'}},
        codex_message(None, ['user.text']),
        {'type': 'event_msg', 'payload': {'type': 'user_message'}},
    ):
        write_jsonl(path, entries + [unknown])
        assert rotate.codex_turns(directory, 'C1') is None
    write_jsonl(path, entries)
    with path.open('a', encoding='utf-8') as handle:
        handle.write('{broken\n')
    assert rotate.codex_turns(directory, 'C1') is None
    write_jsonl(path, [started])
    (directory / 'copy-C1.jsonl').unlink()
    assert rotate.codex_turns(directory, 'C1') is None
    assert rotate.codex_turns(directory, 'missing') is None
    sessions, archived = root / 'sessions', root / 'archived_sessions'
    sessions.mkdir()
    archived.mkdir()
    write_jsonl(archived / 'rollout-C2.jsonl', [human])
    assert rotate.codex_turns(sessions, 'C2') == 1
    write_jsonl(sessions / 'copy-C2.jsonl', [human])
    assert rotate.codex_turns(sessions, 'C2') == 1


def main():
    root = Path(tempfile.mkdtemp())
    check_codex_counts(root)
    claude_dir, codex_dir = root / 'claude', root / 'codex' / '2026' / '10'
    claude_dir.mkdir()
    codex_dir.mkdir(parents=True)
    # 续接的聊天会在新文件里重复旧消息；工具结果和系统提示不算用户消息。
    first = [user('a', '一'), user('b', '二'), {'customTitle': 'Builder [维护]'}]
    write_jsonl(claude_dir / 's1.jsonl', first)
    write_jsonl(claude_dir / 's2.jsonl', first + [
        user('c', '三'), user('d', '<system-reminder>x'),
        {'type': 'user', 'uuid': 'e', 'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}},
        {'customTitle': 'Builder [维护]'}])
    write_jsonl(claude_dir / 'other.jsonl', [user('z', '别人'), {'customTitle': 'Herald [推广]'}])
    write_jsonl(codex_dir / 'rollout-x-T2.jsonl', [{'type': 'event_msg', 'payload': {'type': 'task_started'}}] * 2)

    for name in ('Builder', 'Herald', 'Tutor'):
        (root / f'{name}.md').write_text(f'{name} 交接\n', encoding='utf-8')
    config = {'host': 'claude-desktop', 'roster': '成员构成.md', 'keep_history': 2, 'max_handoff_lines': 3,
              'transcripts': {'format': 'claude', 'dir': str(claude_dir)},
              'members': [
                  {'role': 'Builder', 'title': 'Builder [维护]', 'handoff': 'Builder.md', 'lifecycle': 'persistent', 'max_user_turns': 3},
                  {'role': 'Herald', 'title': 'Herald [推广]', 'handoff': 'Herald.md', 'lifecycle': 'persistent', 'max_user_turns': 60},
                  {'role': 'Tutor', 'title': 'Tutor [指导]', 'handoff': 'Tutor.md', 'lifecycle': 'task'}]}
    cfg, state = root / 'team.json', root / 'state.json'
    cfg.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    run = lambda action, batch='b1', **kw: rotate.execute(cfg, state, 'claude-desktop', action, batch, **kw)

    # 成员表：开工即生成，随状态变化重写。
    run('begin')
    for role, thread in (('Builder', 'T1'), ('Herald', 'T2'), ('Tutor', 'T3')):
        run('register', role=role, thread=thread)
    roster = (root / '成员构成.md').read_text(encoding='utf-8')
    assert '| Builder | Builder [维护] | 长期 | 未知 / 3 | T1 |' in roster, roster

    # 自动计数：去重续接文件、跳过工具结果和系统提示；到上限即 due。
    counts = {r['role']: r for r in run('count')}
    assert counts['Builder']['user_turns'] == 3 and counts['Builder']['due'], counts
    assert counts['Herald']['user_turns'] == 1 and not counts['Herald']['due'], counts
    assert 'use observe' in counts['Tutor']['note'], counts
    assert '3 / 3' in (root / '成员构成.md').read_text(encoding='utf-8')
    expect_error('cannot decrease', run, 'observe', role='Builder', thread='T1', user_turns=1)

    # 只有启动事件无法证明人工消息数。
    assert rotate.codex_turns(root / 'codex', 'T2') is None
    # 旧错误值不能静默降低；来源未知不覆盖累计值，并给出处理提示。
    codex_config = dict(config, transcripts={'format': 'codex', 'dir': str(codex_dir)})
    codex_cfg, codex_state = root / 'codex-team.json', root / 'codex-state.json'
    codex_cfg.write_text(json.dumps(codex_config, ensure_ascii=False), encoding='utf-8')
    codex_run = lambda action, **kw: rotate.execute(codex_cfg, codex_state, 'claude-desktop', action, 'c1', **kw)
    codex_run('begin')
    for role, thread, turns in (('Builder', 'T1', 3), ('Herald', 'T2', 1)):
        codex_run('register', role=role, thread=thread)
        codex_run('observe', role=role, thread=thread, user_turns=turns)
    write_jsonl(codex_dir / 'rollout-T1.jsonl', [codex_message('inject', ['agents_md.instructions'])])
    counts = {r['role']: r for r in codex_run('count')}
    assert counts['Builder']['user_turns'] == 3 and 'from 3 to 0' in counts['Builder']['note'], counts
    assert 'audit the old count' in counts['Builder']['note'], counts
    assert counts['Herald']['user_turns'] == 1 and 'use observe' in counts['Herald']['note'], counts

    # 交接文件超过行数上限不能封存。
    (root / 'Herald.md').write_text('1\n2\n3\n4\n', encoding='utf-8')
    expect_error('trim it to 3', run, 'seal', role='Herald')
    (root / 'Herald.md').write_text('Herald 交接\n', encoding='utf-8')

    # 普通收口：只选阶段性成员和到上限的长期成员。
    for role in ('Builder', 'Herald', 'Tutor'):
        run('seal', role=role)
    assert {p['role'] for p in run('close-plan')} == {'Builder', 'Tutor'}

    # 全组移交：不看上限全部选中；没全归档不能完成；完成后旧状态锁死。
    plan = run('handover-plan')
    assert {p['role'] for p in plan} == {'Builder', 'Herald', 'Tutor'} and all(p['sealed'] for p in plan), plan
    expect_error('handed over', run, 'register', role='Herald', thread='T9')
    for role, thread in (('Builder', 'T1'), ('Herald', 'T2')):
        run('archived', role=role, thread=thread)
    expect_error('Archive every member', run, 'handed-over', to='codex')
    run('archived', role='Tutor', thread='T3')
    expect_error('receiving host', run, 'handed-over', to='claude-desktop')
    run('handed-over', to='codex')
    assert '移交：done → codex' in (root / '成员构成.md').read_text(encoding='utf-8')
    expect_error('handed over to codex', run, 'close-plan')

    # 历史上限：只留最近 keep_history 个批次。
    state.unlink()
    for batch in ('b1', 'b2', 'b3', 'b4'):
        run('begin', batch)
    assert json.loads(state.read_text(encoding='utf-8'))['history'] == ['b2', 'b3']

    print('rotate.py 行为测试通过：自动计数、成员表、清理上限、全组移交')


if __name__ == '__main__':
    main()

import unittest

class NewFeatureTests(unittest.TestCase):
    def test_behavior(self):
        main()
