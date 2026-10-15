import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('rotate', Path(__file__).resolve().parents[1] / 'skills/agent-team-reset/scripts/rotate.py')
rotate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rotate)


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'team.json'
        self.state = self.root / 'state.json'
        self.config.write_text(json.dumps({'host': 'test', 'members': [
            {'role': r, 'title': r, 'handoff': f'{r}.md', 'lifecycle': 'task'} for r in ('A', 'B')]}), encoding='utf-8')
        self.run_action('begin')

    def run_action(self, action, role=None, thread=None, batch='one', host='test'):
        return rotate.execute(self.config, self.state, host, action, batch, role, thread)

    def prepare(self):
        for role in ('A', 'B'):
            (self.root / f'{role}.md').write_text('Next: inspect the inbox.', encoding='utf-8')
            self.run_action('register', role, f'thread-{role}')
            self.run_action('seal', role)

    def test_resume_without_duplicate_creation(self):
        self.assertEqual([m['role'] for m in self.run_action('open-plan')], ['A', 'B'])
        self.run_action('register', 'A', 'thread-A')
        self.run_action('begin')
        self.assertEqual([m['role'] for m in self.run_action('open-plan')], ['B'])

    def test_close_requires_all_handoffs(self):
        with self.assertRaises(ValueError):
            self.run_action('close-plan')
        self.prepare()
        self.assertEqual(self.run_action('close-plan'), [
            {'role': 'A', 'threadId': 'thread-A'}, {'role': 'B', 'threadId': 'thread-B'}])
        (self.root / 'A.md').write_text('Changed after confirmation.', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.run_action('close-plan')

    def test_empty_handoff_is_rejected(self):
        self.run_action('register', 'A', 'thread-A')
        (self.root / 'A.md').write_text('  ', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.run_action('seal', 'A')

    def test_receipts_retry_and_new_batch(self):
        self.prepare()
        with self.assertRaises(ValueError):
            self.run_action('begin', batch='two')
        with self.assertRaises(ValueError):
            self.run_action('archived', 'A', 'wrong')
        self.run_action('archived', 'A', 'thread-A')
        self.run_action('archived', 'A', 'thread-A')
        self.assertEqual(self.run_action('close-plan'), [{'role': 'B', 'threadId': 'thread-B'}])
        self.run_action('archived', 'B', 'thread-B')
        self.run_action('begin', batch='two')
        self.assertEqual(len(self.run_action('open-plan', batch='two')), 2)
        with self.assertRaises(ValueError):
            self.run_action('begin', batch='one')

    def test_ids_and_hosts_are_checked(self):
        self.run_action('register', 'A', 'thread-A')
        for role, thread in [('B', 'thread-A'), ('A', 'replacement')]:
            with self.assertRaises(ValueError):
                self.run_action('register', role, thread)
        with self.assertRaises(ValueError):
            self.run_action('status', host='other')
        with self.assertRaises(ValueError):
            self.run_action('status', batch='wrong')

    def mixed_team(self):
        config = json.loads(self.config.read_text(encoding='utf-8'))
        config['members'][1].update(lifecycle='persistent', max_user_turns=3)
        self.config.write_text(json.dumps(config), encoding='utf-8')
        # A separate state file starts a new configured team without altering old state.
        self.state = self.root / 'mixed.json'
        self.run_action('begin')
        self.prepare()

    def test_daily_rollover_preserves_persistent_chat(self):
        self.mixed_team()
        self.assertEqual(self.run_action('close-plan'), [{'role': 'A', 'threadId': 'thread-A'}])
        with self.assertRaises(ValueError):
            self.run_action('archived', 'B', 'thread-B')
        self.run_action('archived', 'A', 'thread-A')
        state = self.run_action('begin', batch='two')
        self.assertEqual(state['members']['B']['thread'], 'thread-B')
        self.assertEqual([m['role'] for m in self.run_action('open-plan', batch='two')], ['A'])

    def test_persistent_threshold_and_renewal(self):
        self.mixed_team()
        self.assertEqual(self.run_action('maintenance-plan'), [])
        rotate.execute(self.config, self.state, 'test', 'observe', 'one', 'B', 'thread-B', 2)
        self.assertEqual(self.run_action('maintenance-plan'), [])
        rotate.execute(self.config, self.state, 'test', 'observe', 'one', 'B', 'thread-B', 3)
        self.assertEqual(self.run_action('maintenance-plan'), [{'role': 'B', 'threadId': 'thread-B'}])
        with self.assertRaises(ValueError):
            self.run_action('renew', 'B', 'thread-B')
        self.run_action('archived', 'B', 'thread-B')
        self.run_action('renew', 'B', 'thread-B')
        self.assertEqual([m['role'] for m in self.run_action('open-plan')], ['B'])
        with self.assertRaises(ValueError):
            self.run_action('register', 'B', 'thread-B')
        self.run_action('register', 'B', 'new-B')
        self.assertEqual(self.run_action('status')['members']['B']['user_turns'], None)

    def test_next_day_can_have_different_task_members(self):
        self.mixed_team()
        self.run_action('archived', 'A', 'thread-A')
        config = json.loads(self.config.read_text(encoding='utf-8'))
        config['members'][0] = {'role': 'C', 'title': 'C', 'handoff': 'C.md', 'lifecycle': 'task'}
        self.config.write_text(json.dumps(config), encoding='utf-8')
        self.run_action('begin', batch='two')
        self.assertEqual([m['role'] for m in self.run_action('open-plan', batch='two')], ['C'])
        self.assertEqual(self.run_action('status', batch='two')['members']['B']['thread'], 'thread-B')

    def test_forty_turn_boundary(self):
        self.mixed_team()
        config = json.loads(self.config.read_text(encoding='utf-8'))
        config['members'][1]['max_user_turns'] = 40
        self.config.write_text(json.dumps(config), encoding='utf-8')
        self.state = self.root / 'forty.json'
        self.run_action('begin')
        self.prepare()
        rotate.execute(self.config, self.state, 'test', 'observe', 'one', 'B', 'thread-B', 39)
        self.assertEqual(self.run_action('maintenance-plan'), [])
        for count in (40, 41):
            rotate.execute(self.config, self.state, 'test', 'observe', 'one', 'B', 'thread-B', count)
            self.assertEqual(self.run_action('maintenance-plan'), [{'role': 'B', 'threadId': 'thread-B'}])


if __name__ == '__main__':
    unittest.main()
