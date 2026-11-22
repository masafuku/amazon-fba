import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mail_watch
import ops_finance
import stock_ledger
from scripts import gmail_authorize as ga


def email(i, subject, body='', date='2026-10-12T09:00:00+00:00'):
    return {'id': i, 'subject': subject, 'sender': 'x@amazon.com', 'date': date, 'body': body}


class TestMailWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(ops_finance, 'DB_PATH', Path(self.tmp.name) / 't.sqlite3')
        self.patch.start()
        ops_finance.init_ops_tables()
        self.sent = []

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _run(self, emails, **kw):
        with mock.patch.object(mail_watch, 'fetch_emails', return_value=emails):
            return mail_watch.run(send=self.sent.append, **kw)

    def test_new_brand_event_is_ingested_and_notified_once(self):
        mails = [email('m1', 'US - Brand Approval Request for SUN-STAR', 'we determined that you are not eligible to sell SUN-STAR products')]
        first = self._run(mails)
        self.assertTrue(first['sent'])
        self.assertEqual(len(self.sent), 1)
        second = self._run(mails)           # 同じメールを渡しても、新しい動きではないので通知しない
        self.assertFalse(second['sent'])
        self.assertEqual(len(self.sent), 1)

    def test_case_reply_is_notified_once_and_not_ingested(self):
        mails = [email('c1', 'ケース ID22458160971 に新しい返信先アドレスが追加されました')]
        self.assertEqual(self._run(mails)['cases'], ['ケース ID22458160971 に新しい返信先アドレスが追加されました'])
        self.assertEqual(self._run(mails)['cases'], [])
        self.assertEqual(len(self.sent), 1)

    def test_nothing_new_sends_nothing(self):
        self.assertFalse(self._run([])['sent'])
        self.assertEqual(self.sent, [])

    def test_dry_run_does_not_send(self):
        mails = [email('m1', 'US - Brand Approval Request for SUN-STAR', 'not eligible')]
        res = self._run(mails, dry_run=True)
        self.assertIsNotNone(res['message'])
        self.assertFalse(res['sent'])
        self.assertEqual(self.sent, [])

    def test_message_is_truncated(self):
        text = mail_watch.format_message(['x' * 6000], [], [])
        self.assertLessEqual(len(text), mail_watch.MAX_LINE_CHARS)

    def test_query_excludes_newsletters(self):
        self.assertIn('-subject:新着', mail_watch.build_query('2026/10/01'))


class TestAuthorize(unittest.TestCase):
    def test_auth_url_requests_read_only_offline_consent(self):
        url = ga.build_auth_url('cid', 'http://127.0.0.1:1234', 'chal', 'st')
        for part in ('gmail.readonly', 'access_type=offline', 'prompt=consent', 'code_challenge=chal', 'code_challenge_method=S256'):
            self.assertIn(part, url)

    def test_merge_env_replaces_adds_and_keeps_others(self):
        text = '# c\nKEEPA_API_KEY=abc\nGMAIL_CLIENT_ID=\nLINE_USER_ID=U1\n'
        out = ga.merge_env(text, {'GMAIL_CLIENT_ID': 'new', 'GMAIL_REFRESH_TOKEN': 'rt'})
        self.assertIn('KEEPA_API_KEY=abc', out)
        self.assertIn('LINE_USER_ID=U1', out)
        self.assertIn('GMAIL_CLIENT_ID=new', out)
        self.assertEqual(out.count('GMAIL_CLIENT_ID'), 1)
        self.assertIn('GMAIL_REFRESH_TOKEN=rt', out)

    def test_load_client_secret_requires_installed_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / 'g.json'
            good.write_text(json.dumps({'installed': {'client_id': 'a', 'client_secret': 'b'}}))
            self.assertEqual(ga.load_client_secret(good), {'client_id': 'a', 'client_secret': 'b'})
            bad = Path(tmp) / 'b.json'
            bad.write_text('{}')
            with self.assertRaises(ValueError):
                ga.load_client_secret(bad)

    def test_exchange_code_requires_refresh_token(self):
        with mock.patch.object(ga, '_post_form', return_value={'access_token': 'x'}):
            with self.assertRaises(RuntimeError):
                ga.exchange_code({'client_id': 'a', 'client_secret': 'b'}, 'c', 'http://127.0.0.1:1', 'v')
        with mock.patch.object(ga, '_post_form', return_value={'access_token': 'x', 'refresh_token': 'r'}):
            self.assertEqual(ga.exchange_code({'client_id': 'a', 'client_secret': 'b'}, 'c', 'http://127.0.0.1:1', 'v')['refresh_token'], 'r')


if __name__ == '__main__':
    unittest.main()
