import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
REQUEST = runpy.run_path(str(ROOT / 'scripts/release-request.py'))


class ReleaseRequestTests(unittest.TestCase):
    def run_record(self, **updates):
        run = {
            'id': 123, 'run_attempt': 1, 'event': 'push',
            'status': 'completed', 'conclusion': 'success',
            'head_branch': 'main', 'head_sha': '1' * 40,
            'path': '.github/workflows/candidate.yml',
            'repository': {'full_name': 'eglinuxer/crossforge'},
            'head_repository': {'full_name': 'eglinuxer/crossforge'},
            'html_url': 'https://github.com/eglinuxer/crossforge/actions/runs/123',
        }
        return {**run, **updates}

    def select(self, runs):
        return REQUEST['select_candidate'](runs, 'eglinuxer/crossforge', '1' * 40)

    def test_only_exact_upstream_main_candidate_can_be_selected(self):
        valid = self.run_record()
        self.assertEqual(self.select([valid]), valid)
        for updates in (
            {'head_sha': '2' * 40}, {'event': 'pull_request'},
            {'head_branch': 'feature'}, {'path': '.github/workflows/ci.yml'},
            {'repository': {'full_name': 'someone/crossforge'}},
            {'head_repository': {'full_name': 'someone/crossforge'}},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.select([self.run_record(**updates)])

    def test_later_failure_or_pending_run_never_falls_back_to_success(self):
        valid = self.run_record()
        self.assertIsNone(self.select([valid, self.run_record(
            id=124, status='in_progress', conclusion=None)]))
        for conclusion in ('failure', 'cancelled', 'skipped', 'timed_out'):
            with self.subTest(conclusion=conclusion), self.assertRaises(ValueError):
                self.select([valid, self.run_record(
                    run_attempt=2, conclusion=conclusion)])
        with self.assertRaises(ValueError):
            self.select([])

    def test_push_event_is_preserved_in_promotion_identity(self):
        run = self.run_record()
        identity = REQUEST['PROMOTION']['validate_candidate_run'](
            run, 'eglinuxer/crossforge', run['id'])
        self.assertEqual(identity['event'], 'push')

    def test_pending_candidate_does_not_dispatch_or_rebuild(self):
        function = REQUEST['request_release']
        fake_git = lambda *args: '1' * 40
        run = self.run_record(status='in_progress', conclusion=None)
        with patch.dict(function.__globals__, {
            'git': fake_git, 'check_tag': lambda *args: None,
            'gh_json': lambda *args: [{'workflow_runs': [run]}],
        }), patch('subprocess.run') as dispatch:
            function('push', {'after': '1' * 40}, 'eglinuxer/crossforge',
                     'refs/tags/v0.1.0')
        dispatch.assert_not_called()

    def test_completion_dispatches_main_with_exact_tag_and_candidate(self):
        function = REQUEST['request_release']
        run = self.run_record()
        with patch.dict(function.__globals__, {
            'git': lambda *args: 'v0.1.0', 'check_tag': lambda *args: None,
            'gh_json': lambda *args: run if args[1].startswith('/repos/') else [{'workflow_runs': [run]}],
        }), patch('subprocess.run') as dispatch:
            function('workflow_run', {'workflow_run': run},
                     'eglinuxer/crossforge', 'refs/heads/main')
        dispatch.assert_called_once()
        payload = json.loads(dispatch.call_args.kwargs['input'])
        self.assertEqual(payload['ref'], 'main')
        self.assertEqual(payload['inputs']['candidate_run_id'], '123')
        self.assertEqual(payload['inputs']['release_tag'], 'v0.1.0')

    def test_deleted_tag_is_ignored_and_moved_push_is_rejected(self):
        function = REQUEST['request_release']
        function('push', {'deleted': True}, 'eglinuxer/crossforge', 'refs/tags/v0.1.0')
        with patch.dict(function.__globals__, {
            'git': lambda *args: '1' * 40 if args[1].startswith('refs/') else '2' * 40,
        }), self.assertRaisesRegex(ValueError, 'moved'):
            function('push', {'after': '2' * 40}, 'eglinuxer/crossforge',
                     'refs/tags/v0.1.0')

    def test_lightweight_and_annotated_tags_bind_commit_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(
                    ['git', '-C', directory, *args], text=True,
                    stderr=subprocess.PIPE).strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            (root / 'config').mkdir()
            (root / 'config/release.json').write_text(
                json.dumps({'product': {'version': '0.1.0'}}))
            git('add', '.')
            git('commit', '-m', 'Initial')
            sha = git('rev-parse', 'HEAD')
            git('update-ref', 'refs/remotes/origin/main', sha)
            function = REQUEST['check_tag']
            # Only merge-base uses subprocess.run; anchor it to this fixture.
            real_run = subprocess.run
            def run(args, **kwargs):
                return real_run(args, cwd=directory, **kwargs)
            with patch.dict(function.__globals__, {'git': git}), patch(
                    'subprocess.run', side_effect=run):
                for annotated in (False, True):
                    git('tag', *(['-a', '-m', 'Release'] if annotated else []), 'v0.1.0')
                    function('v0.1.0', sha)
                    self.assertIn('v0.1.0', git('tag', '--points-at', sha).splitlines())
                    with self.assertRaises(ValueError):
                        function('v0.1.0', '2' * 40)
                    git('tag', '-d', 'v0.1.0')
                git('tag', 'v0.2.0')
                with self.assertRaisesRegex(ValueError, 'differs'):
                    function('v0.2.0', sha)
                with self.assertRaises(ValueError):
                    function('v0.1.0-rc1', sha)
                git('checkout', '-b', 'feature')
                (root / 'extra').write_text('feature')
                git('add', '.')
                git('commit', '-m', 'Feature')
                git('tag', 'v0.1.0')
                with self.assertRaises(subprocess.CalledProcessError):
                    function('v0.1.0', git('rev-parse', 'HEAD'))


if __name__ == '__main__':
    unittest.main()
