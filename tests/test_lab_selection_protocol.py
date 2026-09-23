import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fuckclassroom.plugins.lab_selection.client import LabClient, LabSelectionError, connect, extract_edu_token
from fuckclassroom.plugins.lab_selection.services import LabSelectionService


def group(**updates):
    now = datetime.now(timezone(timedelta(hours=8)))
    result = {"groupInfo": {"id": "g1", "selectBeginTime": (now - timedelta(days=1)).isoformat(),
                            "selectEndTime": (now + timedelta(days=1)).isoformat()},
              "selectCount": 5, "maxCount": 30, "hasSelect": False}
    result.update(updates)
    return result


class LabProtocolTests(unittest.TestCase):
    def service(self, request):
        client = MagicMock()
        client.request.side_effect = request

        @contextmanager
        def connection(_):
            yield client

        patcher = patch('fuckclassroom.plugins.lab_selection.services.connect', connection)
        patcher.start()
        self.addCleanup(patcher.stop)
        return LabSelectionService(MagicMock()), client

    def test_bootstrap_exchanges_edu_token_using_current_access_base(self):
        session = MagicMock()
        session._selection_http_client.return_value = (MagicMock(), None)
        session._selection_access.return_value.home_url = 'https://example.test/vpn/student/home'
        session._request_selection_bytes.side_effect = [
            (b'<iframe src="/guet-lab-web/#/course/stu?token=a.b.c"></iframe>', 'text/html', 'https://example.test/vpn/entry'),
            (b'{"success":true,"result":{"token":"lab-token"}}', 'application/json', ''),
        ]
        with connect(session) as client:
            self.assertEqual(client.token, 'lab-token')
            call = session._request_selection_bytes.call_args
            self.assertEqual(call.args[1], 'https://example.test/vpn/guet-lab-system/api/authentication/getAccessTokenByEduToken?token=a.b.c')
        self.assertEqual(client.token, '')

    def test_bad_bootstrap_is_rejected(self):
        for source in ('login required', '?token=not-a-token'):
            with self.assertRaises(LabSelectionError):
                extract_edu_token(source)

    def test_group_submit_once_then_verify(self):
        service, client = self.service([[group()], 'ok', [group(hasSelect=True)]])
        self.assertTrue(service.select('t1', group_id='g1')['verified'])
        self.assertEqual(client.request.call_args_list[1].args, ('select_group',))
        self.assertEqual(client.request.call_args_list[1].kwargs, {'taskId': 't1', 'selectWey': 1, 'groupId': 'g1'})

    def test_full_expired_or_selected_group_never_submits(self):
        for candidate in (group(selectCount=30), group(hasSelect=True), group(groupInfo={'id': 'g1'})):
            service, client = self.service([[candidate]])
            with self.assertRaises(LabSelectionError):
                service.select('t1', group_id='g1')
            self.assertEqual(client.request.call_count, 1)

    def test_item_bundle_and_student_id_are_taken_from_server(self):
        before = [{'needComplete': 1, 'selectCount': 0, 'list': [{'id': 's1', 'stuId': 'student', 'selectItemString': ''}]}]
        after = [{'list': [{'id': 's1', 'selectItemString': '周五 5~6节'}]}]
        service, client = self.service([before, [{'list': [{'id': 'i1'}, {'id': 'i2'}]}], 'ok', after])
        self.assertTrue(service.select('t1', subject_id='s1', item_ids='i1,i2')['verified'])
        self.assertEqual(client.request.call_args_list[2].kwargs,
                         {'taskId': 't1', 'selectWey': 1, 'itemIds': 'i1,i2', 'stuId': 'student'})

    def test_stale_or_conflicting_slot_never_submits(self):
        before = [{'needComplete': 1, 'selectCount': 0, 'list': [{'id': 's1', 'stuId': 'student'}]}]
        for slot in ({'list': [{'id': 'different'}]}, {'isTheoryConflict': True, 'list': [{'id': 'i1'}]}):
            service, client = self.service([before, [slot]])
            with self.assertRaises(LabSelectionError):
                service.select('t1', subject_id='s1', item_ids='i1')
            self.assertEqual(client.request.call_count, 2)

    def test_lost_submission_response_is_not_retried(self):
        service, client = self.service([[group()], TimeoutError('timeout')])
        with self.assertRaisesRegex(LabSelectionError, '提交未确认'):
            service.select('t1', group_id='g1')
        self.assertEqual(client.request.call_count, 2)

    def test_failed_verification_does_not_claim_success(self):
        service, _ = self.service([[group()], 'ok', TimeoutError('timeout')])
        self.assertFalse(service.select('t1', group_id='g1')['verified'])

    def test_semesters_and_pagination_use_experiment_protocol(self):
        service, client = self.service([[{'value': '321', 'text': '本学期'}], {'records': [], 'total': 0}])
        data = service.courses(page=2)
        self.assertEqual(data['semester'], '321')
        self.assertEqual(client.request.call_args.kwargs, {'teacherCalendarId': '321', 'pageNo': 2, 'pageSize': 30})

    def test_business_failure_and_non_json_are_errors(self):
        session = MagicMock()
        client = LabClient(session, object(), 'https://example.test', 'private-token')
        for body in (b'<html>login</html>', b'{"success":false,"message":"expired"}'):
            session._request_selection_bytes.return_value = (body, '', '')
            with self.assertRaises(LabSelectionError):
                client.request('semesters')
        self.assertEqual(session._request_selection_bytes.call_args.kwargs['extra_headers']['X-Access-Token'], 'private-token')
