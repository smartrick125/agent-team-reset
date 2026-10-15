"""Public CLI acceptance tests for rotation, reconciliation and consumed handoffs."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'agent-team-reset' / 'scripts' / 'rotate.py'
ROLES = ('Owner', 'Builder', 'Repair', 'Review', 'Promote', 'Member')


class TeamLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cfg = self.root / 'team.json'
        self.state = self.root / 'state.json'
        (self.root / 'handoffs').mkdir()
        self.config = {'host': 'codex', 'roster': '成员构成.md', 'members': [
            {'role': role, 'title': role + ' [测试]', 'lifecycle': 'persistent',
             'persistent_class': 'regular' if role == 'Member' else 'maintenance',
             'max_user_turns': 20 if role == 'Member' else 30,
             'handoff': f'handoffs/{role}.md'} for role in ROLES]}
        self.write_config()
        for role in ROLES:
            self.handoff(role).write_text('核心决策\n未完成任务\n下一动作\n', encoding='utf-8')
        self.run_cli('begin')
        for role in ROLES:
            self.run_cli('register', '--role', role, '--thread', 'id-' + role)

    def handoff(self, role='Owner'):
        return self.root / 'handoffs' / (role + '.md')

    def write_config(self):
        self.cfg.write_text(json.dumps(self.config, ensure_ascii=False), encoding='utf-8')

    def run_cli(self, action, *args, succeeds=True, batch='batch1'):
        process = subprocess.run([sys.executable, str(SCRIPT), '--config', str(self.cfg),
                                  '--state', str(self.state), '--host', 'codex', action, batch, *args],
                                 capture_output=True, encoding='utf-8')
        if succeeds:
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)
        self.assertNotEqual(process.returncode, 0)
        return process.stderr

    def load(self):
        return json.loads(self.state.read_text(encoding='utf-8'))

    def observe(self, role, count, thread=None):
        return self.run_cli('observe', '--role', role, '--thread', thread or 'id-' + role,
                            '--user-turns', str(count))

    def test_class_thresholds_and_seal_before_archive(self):
        self.observe('Owner', 29)
        self.observe('Member', 19)
        self.assertEqual(self.run_cli('check')['due'], [])
        self.observe('Owner', 30)
        self.observe('Member', 20)
        check = self.run_cli('check')
        self.assertEqual(set(check['due']), {'Owner', 'Member'})
        actions = {entry['role']: entry['action'] for entry in check['actions']}
        self.assertEqual(actions['Owner'], 'handoff')
        self.run_cli('archived', '--role', 'Owner', '--thread', 'id-Owner', succeeds=False)
        self.run_cli('seal', '--role', 'Owner')
        actions = {entry['role']: entry['action'] for entry in self.run_cli('check')['actions']}
        self.assertEqual(actions['Owner'], 'archive')
        self.handoff().write_text('交接改变，旧封存失效\n', encoding='utf-8')
        self.run_cli('archived', '--role', 'Owner', '--thread', 'id-Owner', succeeds=False)
        self.config['members'][0]['max_user_turns'] = 60
        self.write_config()
        self.run_cli('status', succeeds=False)

    def test_one_of_six_rotates_and_new_member_reads_before_cleanup(self):
        self.observe('Owner', 30)
        self.run_cli('seal', '--role', 'Owner')
        seal = self.load()['members']['Owner']['handoff']
        self.run_cli('archived', '--role', 'Owner', '--thread', 'id-Owner')
        check = self.run_cli('check')
        self.assertEqual(check['missing'], ['Owner'])
        self.assertEqual([entry['action'] for entry in check['actions']], ['renew'])
        self.run_cli('renew', '--role', 'Owner', '--thread', 'id-Owner')
        for _ in range(3):
            plan = self.run_cli('open-plan')
            self.assertEqual([entry['role'] for entry in plan], ['Owner'])
        self.run_cli('register', '--role', 'Owner', '--thread', 'new-Owner')
        self.assertEqual(self.run_cli('open-plan'), [])
        self.assertEqual(self.run_cli('check')['missing'], [])
        self.run_cli('seal', '--role', 'Owner', succeeds=False)
        self.run_cli('cleanup', '--role', 'Owner', '--apply', succeeds=False)
        self.assertTrue(self.handoff().exists())
        self.run_cli('read-receipt', '--role', 'Owner', '--thread', 'id-Owner', '--sha256', seal, succeeds=False)
        self.run_cli('read-receipt', '--role', 'Owner', '--thread', 'new-Owner', '--sha256', '0' * 64, succeeds=False)
        self.run_cli('read-receipt', '--role', 'Owner', '--thread', 'new-Owner', '--sha256', seal)
        preview = self.run_cli('cleanup', '--role', 'Owner')
        self.assertEqual([entry['role'] for entry in preview['items']], ['Owner'])
        self.assertTrue(self.handoff().exists())
        self.run_cli('cleanup', '--role', 'Owner', '--apply')
        self.assertFalse(self.handoff().exists())
        self.assertEqual(self.run_cli('cleanup', '--apply')['items'], [])
        self.assertEqual([role for role, item in self.load()['members'].items() if item['thread']], list(ROLES))
        self.handoff().write_text('本轮新核心交接\n', encoding='utf-8')
        self.run_cli('seal', '--role', 'Owner')
        self.assertFalse(self.load()['members']['Owner'].get('read_receipt'))

    def test_whole_team_close_never_refills_even_when_empty(self):
        self.run_cli('close-team')
        self.assertEqual(self.load()['mode'], 'closing')
        self.assertEqual(self.run_cli('open-plan'), [])
        self.run_cli('register', '--role', 'Owner', '--thread', 'replacement', succeeds=False)
        for role in ROLES:
            self.run_cli('seal', '--role', role)
            self.run_cli('archived', '--role', role, '--thread', 'id-' + role)
            self.assertFalse(any(entry['action'] in ('create', 'renew')
                                 for entry in self.run_cli('check')['actions']))
        self.assertEqual(self.load()['mode'], 'closed')
        for _ in range(3):
            self.assertEqual(self.run_cli('open-plan'), [])
            self.assertEqual(self.run_cli('check')['actions'], [])
        self.run_cli('renew', '--role', 'Owner', '--thread', 'id-Owner', succeeds=False)
        self.run_cli('begin')
        self.assertEqual(self.load()['mode'], 'closed')
        self.run_cli('begin', batch='explicit-restart')
        self.assertEqual(self.load()['mode'], 'running')

    def test_disappeared_host_thread_is_reconciled_not_blindly_recreated(self):
        snapshot = self.root / 'live.json'
        snapshot.write_text(json.dumps(['id-' + role for role in ROLES if role != 'Review']), encoding='utf-8')
        check = self.run_cli('check', '--live-threads', str(snapshot))
        actions = {entry['role']: entry['action'] for entry in check['actions']}
        self.assertEqual(actions, {'Review': 'reconcile'})
        self.assertEqual(self.run_cli('open-plan'), [])
        snapshot.write_text('[]', encoding='utf-8')
        self.assertTrue(all(entry['action'] == 'reconcile'
                            for entry in self.run_cli('check', '--live-threads', str(snapshot))['actions']))
        self.assertEqual(self.load()['mode'], 'running')

    def test_policy_migrates_legacy_without_resetting_ids_or_counts(self):
        # A legacy state starts with an explicit historical limit, without classification.
        old = copy.deepcopy(self.config)
        for member in old['members']:
            member.pop('persistent_class')
            member['max_user_turns'] = 60
        legacy_cfg = self.root / 'legacy.json'
        legacy_state = self.root / 'legacy-state.json'
        legacy_cfg.write_text(json.dumps(old), encoding='utf-8')
        original_cfg, original_state = self.cfg, self.state
        self.cfg, self.state = legacy_cfg, legacy_state
        self.run_cli('begin')
        self.run_cli('register', '--role', 'Owner', '--thread', 'legacy-owner')
        self.observe('Owner', 12, 'legacy-owner')
        self.run_cli('seal', '--role', 'Owner')
        before = self.load()
        self.write_config()
        self.run_cli('policy')
        after = self.load()
        self.assertEqual(after['members'], before['members'])
        self.assertEqual(after['batch'], before['batch'])
        self.assertEqual(after['config']['members'][0]['max_user_turns'], 30)
        self.assertEqual(after['config']['members'][-1]['max_user_turns'], 20)
        self.config['tier'] = '维护30、普通20，取代旧说明'
        self.write_config()
        self.run_cli('policy')
        self.assertEqual(self.load()['members'], before['members'])
        self.assertEqual(self.load()['config']['tier'], '维护30、普通20，取代旧说明')
        self.config['tier'] = 30
        self.write_config()
        self.run_cli('policy', succeeds=False)
        self.config['tier'] = '维护30、普通20，取代旧说明'
        self.config['max_handoff_lines'] = 999
        self.write_config()
        self.run_cli('policy', succeeds=False)
        self.config.pop('max_handoff_lines')
        self.config['members'][0]['title'] = 'Changed'
        self.write_config()
        self.run_cli('policy', succeeds=False)
        self.cfg, self.state = original_cfg, original_state

    def prepare_consumed_handoff(self):
        self.observe('Owner', 30)
        self.run_cli('seal', '--role', 'Owner')
        seal = self.load()['members']['Owner']['handoff']
        self.run_cli('archived', '--role', 'Owner', '--thread', 'id-Owner')
        self.run_cli('renew', '--role', 'Owner', '--thread', 'id-Owner')
        self.run_cli('register', '--role', 'Owner', '--thread', 'new-Owner')
        self.run_cli('read-receipt', '--role', 'Owner', '--thread', 'new-Owner', '--sha256', seal)

    def test_changed_consumed_file_is_not_deleted_and_other_records_survive(self):
        self.prepare_consumed_handoff()
        before = self.cfg.read_bytes(), self.state.read_bytes(), (self.root / '成员构成.md').read_bytes()
        self.handoff().write_text('收到后新增的任务，不能清理\n', encoding='utf-8')
        self.run_cli('cleanup', '--role', 'Owner', '--apply', succeeds=False)
        self.assertTrue(self.handoff().exists())
        self.assertEqual((self.cfg.read_bytes(), self.state.read_bytes(),
                          (self.root / '成员构成.md').read_bytes()), before)

    def test_handoff_path_migration_keeps_original_report_and_rejects_wrong_copy(self):
        self.observe('Owner', 10)
        self.run_cli('seal', '--role', 'Owner')
        before = self.load()['members']
        old_path = self.handoff()
        new_path = self.root / 'handoffs' / 'Owner-core.md'
        new_path.write_text('错误副本\n', encoding='utf-8')
        self.config['members'][0]['handoff'] = 'handoffs/Owner-core.md'
        self.write_config()
        self.run_cli('migrate-handoffs', succeeds=False)
        new_path.write_bytes(old_path.read_bytes())
        self.run_cli('migrate-handoffs')
        self.assertEqual(self.load()['members'], before)
        self.assertTrue(old_path.exists())
        self.assertEqual(new_path.read_bytes(), old_path.read_bytes())

    def test_consumed_role_report_is_never_deleted_as_disposable_handoff(self):
        self.cfg = self.root / 'report-team.json'
        self.state = self.root / 'report-state.json'
        self.config['members'][0]['handoff'] = 'reports/Owner.md'
        report = self.root / 'reports' / 'Owner.md'
        report.parent.mkdir()
        report.write_text('角色工作报告，不能当作一次性交接删除\n', encoding='utf-8')
        self.write_config()
        self.run_cli('begin')
        self.run_cli('register', '--role', 'Owner', '--thread', 'id-Owner')
        self.observe('Owner', 30)
        self.run_cli('seal', '--role', 'Owner')
        seal = self.load()['members']['Owner']['handoff']
        self.run_cli('archived', '--role', 'Owner', '--thread', 'id-Owner')
        self.run_cli('renew', '--role', 'Owner', '--thread', 'id-Owner')
        self.run_cli('register', '--role', 'Owner', '--thread', 'new-Owner')
        self.run_cli('read-receipt', '--role', 'Owner', '--thread', 'new-Owner', '--sha256', seal)
        before = report.read_bytes()
        self.run_cli('cleanup', '--role', 'Owner', '--apply', succeeds=False)
        self.assertEqual(report.read_bytes(), before)

    def test_new_unclassified_persistent_defaults_to_twenty(self):
        self.cfg = self.root / 'default-team.json'
        self.state = self.root / 'default-state.json'
        self.config['members'][-1].pop('persistent_class')
        self.config['members'][-1].pop('max_user_turns')
        self.write_config()
        self.run_cli('begin')
        self.run_cli('register', '--role', 'Member', '--thread', 'id-Member')
        self.observe('Member', 19)
        self.assertNotIn('Member', self.run_cli('check')['due'])
        self.observe('Member', 20)
        self.assertIn('Member', self.run_cli('check')['due'])

    def test_repeated_rotations_reuse_one_core_file_and_do_not_duplicate_members(self):
        current = 'id-Owner'
        for generation in range(1, 4):
            self.handoff().write_text(f'第{generation}轮核心任务与下一动作\n', encoding='utf-8')
            self.observe('Owner', 30, current)
            self.run_cli('seal', '--role', 'Owner')
            seal = self.load()['members']['Owner']['handoff']
            self.run_cli('archived', '--role', 'Owner', '--thread', current)
            self.run_cli('renew', '--role', 'Owner', '--thread', current)
            current = f'new-Owner-{generation}'
            self.run_cli('register', '--role', 'Owner', '--thread', current)
            self.run_cli('read-receipt', '--role', 'Owner', '--thread', current, '--sha256', seal)
            self.run_cli('cleanup', '--role', 'Owner', '--apply')
            self.assertEqual(self.run_cli('open-plan'), [])
            self.assertEqual(len(self.load()['members']), 6)
            self.assertFalse(self.handoff().exists())
        self.assertEqual(len(list((self.root / 'handoffs').glob('*.md'))), 5)
        self.assertEqual(len(self.load()['retired_threads']), 3)

    def test_consumed_directory_cannot_be_deleted(self):
        self.prepare_consumed_handoff()
        self.handoff().unlink()
        self.handoff().mkdir()
        self.run_cli('cleanup', '--role', 'Owner', '--apply', succeeds=False)
        self.assertTrue(self.handoff().is_dir())

    def test_consumed_symlink_cannot_delete_target(self):
        self.prepare_consumed_handoff()
        target = self.root / 'outside-core.md'
        target.write_bytes(self.handoff().read_bytes())
        self.handoff().unlink()
        try:
            os.symlink(target, self.handoff())
        except OSError as error:
            self.skipTest(f'本机未开放临时符号链接权限：{error}')
        before = target.read_bytes()
        self.run_cli('cleanup', '--role', 'Owner', '--apply', succeeds=False)
        self.assertTrue(self.handoff().is_symlink())
        self.assertEqual(target.read_bytes(), before)

    def test_connect_source_preserves_team_and_refresh_is_read_only(self):
        source = self.root / 'codex-logs'
        source.mkdir()
        entries = [{'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user', 'id': f'human-{index}',
            'internal_chat_message_metadata_passthrough': {'content_item_kinds': ['user.text']}}}
            for index in range(30)]
        (source / 'rollout-id-Owner.jsonl').write_text(
            '\n'.join(json.dumps(entry) for entry in entries) + '\n', encoding='utf-8')
        before_members = self.load()['members']
        self.config['transcripts'] = {'format': 'codex', 'dir': str(source)}
        self.write_config()
        self.run_cli('configure-source')
        self.assertEqual(self.load()['members'], before_members)
        before = self.state.read_bytes(), (self.root / '成员构成.md').read_bytes()
        check = self.run_cli('check', '--refresh-counts')
        self.assertIn('Owner', check['due'])
        self.assertEqual(next(item['action'] for item in check['actions'] if item['role'] == 'Owner'), 'handoff')
        self.assertEqual((self.state.read_bytes(), (self.root / '成员构成.md').read_bytes()), before)
        self.config['members'][0]['title'] = '非法顺带改名'
        self.write_config()
        self.run_cli('configure-source', succeeds=False)
        self.config['members'][0]['title'] = 'Owner [测试]'
        self.config['transcripts']['dir'] = str(self.root / 'missing-source')
        self.write_config()
        self.run_cli('configure-source', succeeds=False)

    def test_refresh_unknown_or_lower_count_never_archives_automatically(self):
        source = self.root / 'codex-source'
        source.mkdir()
        self.config['transcripts'] = {'format': 'codex', 'dir': str(source)}
        self.write_config()
        self.run_cli('configure-source')
        self.observe('Owner', 31)
        unknown = self.run_cli('check', '--refresh-counts')
        self.assertNotIn('Owner', unknown['due'])
        self.assertFalse(any(item['role'] == 'Owner' and item['action'] in ('handoff', 'archive')
                             for item in unknown['actions']))
        # The old count is retained, but cannot drive retirement when the new source contradicts it.
        entry = {'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user', 'id': 'only-human',
            'internal_chat_message_metadata_passthrough': {'content_item_kinds': ['user.text']}}}
        (source / 'rollout-id-Owner.jsonl').write_text(json.dumps(entry) + '\n', encoding='utf-8')
        before = self.state.read_bytes()
        audited = self.run_cli('check', '--refresh-counts')
        self.assertNotIn('Owner', audited['due'])
        self.assertEqual(next(item['action'] for item in audited['actions'] if item['role'] == 'Owner'), 'audit')
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.load()['members']['Owner']['user_turns'], 31)


if __name__ == '__main__':
    unittest.main(verbosity=2)
