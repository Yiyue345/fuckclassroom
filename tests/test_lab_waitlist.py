import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fuckclassroom.plugins.lab_selection.client import LabRejected, LabSelectionError, LabSubmissionUncertain
from fuckclassroom.plugins.lab_selection.waitlist import LabWaitlist


class LabWaitlistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'waitlist.json'
        self.now = 1000
        self.lab = Mock()
        self.lab.target_state.return_value = self.state('waiting')
        self.lab.select.side_effect = self.select
        self.queue = LabWaitlist(self.lab, self.path, clock=lambda: self.now)

    @staticmethod
    def state(status, account='account-a'):
        return {'status': status, 'message': status, 'account_id': account}

    @staticmethod
    def select(**kwargs):
        kwargs['before_submit']()
        return {'verified': True, 'message': '已核验'}

    def add(self):
        return self.queue.add(task_id='task', group_id='group', label='测试实验')['id']

    def job(self):
        return self.queue.list_jobs()[0]

    def test_full_course_only_polls_at_interval(self):
        self.add()
        self.queue.run_due_once()
        self.lab.select.assert_not_called()
        self.assertEqual(self.job()['status'], 'waiting')
        self.assertFalse(self.queue.run_due_once())
        self.now += 30
        self.assertTrue(self.queue.run_due_once())
        self.assertEqual(self.job()['checks'], 2)

    def test_open_seat_submits_once_and_completes(self):
        self.add()
        self.lab.target_state.return_value = self.state('ready')
        self.queue.run_due_once()
        self.assertEqual(self.job()['status'], 'selected')
        self.assertEqual(self.job()['attempts'], 1)
        self.lab.select.assert_called_once()
        self.assertFalse(self.queue.run_due_once())

    def test_duplicate_job_is_not_added(self):
        first = self.add()
        second = self.queue.add(task_id='task', group_id='group')
        self.assertEqual(second, {'id': first, 'created': False})
        self.assertEqual(len(self.queue.list_jobs()), 1)

    def test_jobs_survive_restart(self):
        self.add()
        restored = LabWaitlist(self.lab, self.path, clock=lambda: self.now)
        self.assertEqual(restored.list_jobs()[0]['target']['group_id'], 'group')
        self.assertTrue(restored.run_due_once())

    def test_interrupted_submission_requires_manual_check(self):
        self.add()
        payload = json.loads(self.path.read_text(encoding='utf-8'))
        next(iter(payload['jobs'].values()))['status'] = 'submitting'
        self.path.write_text(json.dumps(payload), encoding='utf-8')
        restored = LabWaitlist(self.lab, self.path, clock=lambda: self.now)
        self.assertEqual(restored.list_jobs()[0]['status'], 'paused')
        self.assertFalse(restored.run_due_once())

    def test_uncertain_submission_is_never_retried(self):
        self.add()
        self.lab.target_state.return_value = self.state('ready')

        def fail(**kwargs):
            kwargs['before_submit']()
            raise LabSubmissionUncertain('请核对')

        self.lab.select.side_effect = fail
        self.queue.run_due_once()
        self.assertEqual(self.job()['status'], 'paused')
        self.now += 3600
        self.assertFalse(self.queue.run_due_once())
        self.lab.select.assert_called_once()

    def test_unverified_success_pauses(self):
        self.add()
        self.lab.target_state.return_value = self.state('ready')
        self.lab.select.side_effect = lambda **kw: (kw['before_submit'](), {'verified': False, 'message': '待核验'})[1]
        self.queue.run_due_once()
        self.assertEqual(self.job()['status'], 'paused')

    def test_explicit_rejection_with_full_seat_returns_to_waiting(self):
        self.add()
        self.lab.target_state.side_effect = [self.state('ready'), self.state('waiting')]

        def rejected(**kwargs):
            kwargs['before_submit']()
            raise LabRejected('人数已满')

        self.lab.select.side_effect = rejected
        self.queue.run_due_once()
        self.assertEqual(self.job()['status'], 'waiting')
        self.assertEqual(self.job()['attempts'], 1)

    def test_cancel_during_read_prevents_submission(self):
        job_id = self.add()

        def check(**kwargs):
            self.queue.cancel(job_id)
            return self.state('ready')

        self.lab.target_state.side_effect = check
        self.queue.run_due_once()
        self.assertEqual(self.job()['status'], 'cancelled')
        self.assertEqual(self.job()['attempts'], 0)

    def test_cancel_inflight_preserves_success(self):
        job_id = self.add()
        self.lab.target_state.return_value = self.state('ready')

        def action(**kwargs):
            kwargs['before_submit']()
            self.queue.cancel(job_id)
            return {'verified': True, 'message': '已选上'}

        self.lab.select.side_effect = action
        self.queue.run_due_once()
        self.assertEqual(self.job()['status'], 'selected')
        self.assertTrue(self.job()['cancel_requested'])

    def test_changed_account_never_submits(self):
        self.add()
        self.lab.target_state.return_value = self.state('ready', account='different')
        self.queue.run_due_once()
        self.assertEqual(self.job()['status'], 'paused')
        self.lab.select.assert_not_called()

    def test_expiration_stops_polling(self):
        self.add()
        self.lab.target_state.return_value = self.state('expired')
        self.queue.run_due_once()
        self.assertEqual(self.job()['status'], 'expired')
        self.assertIsNone(self.job()['next_check'])

    def test_expired_session_waits_then_recovers(self):
        self.add()
        self.lab.target_state.side_effect = LabSelectionError('教务会话已失效，请重新登录')
        self.queue.run_due_once()
        self.assertEqual(self.job()['status'], 'waiting_session')
        self.assertEqual(self.job()['next_check'], self.now + 60)
        self.now += 60
        self.lab.target_state.side_effect = None
        self.lab.target_state.return_value = self.state('ready')
        self.queue.run_due_once()
        self.assertEqual(self.job()['status'], 'selected')

    def test_persist_before_submission(self):
        self.add()
        self.lab.target_state.return_value = self.state('ready')

        def action(**kwargs):
            kwargs['before_submit']()
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            self.assertEqual(next(iter(payload['jobs'].values()))['status'], 'submitting')
            return {'verified': True, 'message': '已选上'}

        self.lab.select.side_effect = action
        self.queue.run_due_once()
        self.assertEqual(self.job()['status'], 'selected')

    def test_reload_stops_previous_owner_before_starting(self):
        self.queue.start()
        self.addCleanup(self.queue.stop)
        replacement = LabWaitlist(self.lab, self.path)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.assertTrue(self.queue._stop.is_set())
        self.assertFalse(self.queue._thread.is_alive())
        self.assertTrue(replacement._thread.is_alive())

    def test_cancelled_job_does_not_restart(self):
        job_id = self.add()
        self.queue.cancel(job_id)
        restored = LabWaitlist(self.lab, self.path)
        self.assertFalse(restored.run_due_once())
        self.assertEqual(restored.list_jobs()[0]['status'], 'cancelled')
