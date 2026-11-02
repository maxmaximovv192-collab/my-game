import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from urllib.parse import urlencode
from uuid import uuid4

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from leaderboard import RatingStore, create_api, deliver_pending, load_questions, validate_init_data

TOKEN = '123:test-token'
QUESTION = {'q': 'Test question?', 'a': ['Right', 'Wrong']}


class InProcessClient:
    """Exercise real aiohttp routes/middleware without opening OS sockets."""
    def __init__(self, app):
        self.app = app
        app.freeze()

    async def start_server(self):
        pass

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def request(self, method, path, headers=None, data=None):
        request = make_mocked_request(method, path, headers=headers or {}, app=self.app)
        if data is not None:
            request._read_bytes = json.dumps(data).encode()
        try:
            response = await self.app._handle(request)
        except web.HTTPException as exc:
            response = exc
        async def read_json():
            return json.loads(response.text)
        return SimpleNamespace(status=response.status, headers=response.headers, json=read_json)

    async def get(self, path, headers=None):
        return await self.request('GET', path, headers)

    async def options(self, path, headers=None):
        return await self.request('OPTIONS', path, headers)

    async def post(self, path, json=None, headers=None):
        return await self.request('POST', path, headers, json)


def signed_data(user_id=42, auth_date=None):
    fields = {'auth_date': str(int(time.time()) if auth_date is None else auth_date),
              'user': json.dumps({'id': user_id, 'first_name': 'Test'})}
    check = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class AuthTests(unittest.TestCase):
    def test_signed_identity(self):
        self.assertEqual(validate_init_data(signed_data(), TOKEN)['id'], 42)

    def test_tampered_expired_future_and_duplicate(self):
        for data in (signed_data().replace('Test', 'Evil'), signed_data(auth_date=1),
                     signed_data(auth_date=int(time.time()) + 3600), signed_data() + '&auth_date=1'):
            with self.subTest(data=data), self.assertRaises(web.HTTPUnauthorized):
                validate_init_data(data, TOKEN)

    def test_actual_bank_loads(self):
        bank = load_questions(Path(__file__).resolve().parents[1] / 'index.html')
        self.assertGreater(len(bank), 100)
        self.assertTrue(all(q['q'] and len(q['a']) >= 2 for q in bank))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = Path(self.tmp.name) / 'test.db'
        self.store = RatingStore(self.path)
        self.store.init()
        self.store.register(42, 'First')
        self.store.register(43, 'Second')

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_preserves_existing_users_and_balance(self):
        path = Path(self.tmp.name) / 'old.db'
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE users(user_id INTEGER PRIMARY KEY,name TEXT,coins INTEGER NOT NULL DEFAULT 0)')
            con.execute("INSERT INTO users VALUES (1,'Existing',123)")
        con.close()
        store = RatingStore(path)
        store.init()
        store.init()
        with store.connect() as con:
            self.assertEqual(con.execute('SELECT coins FROM users WHERE user_id=1').fetchone()[0], 123)

    def test_event_retry_does_not_duplicate_points(self):
        event_id = str(uuid4())
        self.assertTrue(self.store.record(42, event_id, QUESTION, 'Right', points=7))
        self.assertFalse(self.store.record(42, event_id, QUESTION, 'Right', points=7))
        self.assertEqual(self.store.ranking(42)['me']['points'], 7)

    def test_zero_points_by_default(self):
        self.store.record(42, str(uuid4()), QUESTION, 'Right')
        self.assertEqual(self.store.ranking(42)['me']['points'], 0)
        self.assertEqual(self.store.ranking(42)['top'], [])

    def test_ties_and_personal_place(self):
        for uid in (42, 43):
            self.store.record(uid, str(uuid4()), QUESTION, 'Right', points=2)
        self.store.register(44, 'Third')
        self.assertEqual(self.store.ranking(43)['me']['place'], 1)
        self.assertEqual(self.store.ranking(44)['me']['place'], 3)
        self.assertNotIn('user_id', self.store.ranking(42)['top'][0])

    def test_daily_snapshot_and_restart(self):
        self.store.prepare_report('2026-11-02')
        old = self.store.pending()
        self.assertEqual(len(old), 2)
        self.store.record(42, str(uuid4()), QUESTION, 'Right', points=5)
        self.store.prepare_report('2026-11-02')
        self.assertEqual(self.store.pending(), old)
        self.store.mark('2026-11-02', 42, 'sent')
        restarted = RatingStore(self.path)
        restarted.init()
        self.assertEqual([r['user_id'] for r in restarted.pending()], [43])

    def test_unsubscribe_cancels_pending_and_register_preserves_preference(self):
        self.store.prepare_report('2026-11-02')
        self.store.subscribe(42, False)
        self.store.register(42, 'Renamed')
        self.assertEqual([r['user_id'] for r in self.store.pending()], [43])
        self.store.subscribe(42, True)
        self.assertEqual([r['user_id'] for r in self.store.pending()], [43])


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.store = RatingStore(Path(self.tmp.name) / 'test.db')
        self.store.init()
        self.client = InProcessClient(create_api(self.store, TOKEN, [QUESTION], 'https://game.example'))
        await self.client.start_server()
        self.headers = {'Authorization': 'tma ' + signed_data(), 'Origin': 'https://game.example'}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    def event(self, answer='Right'):
        return {'event_id': str(uuid4()), 'question': 0, 'text': QUESTION['q'], 'answer': answer, 'points': 99999, 'user_id': 12345}

    async def test_authenticated_answer_records_without_awarding_client_points(self):
        event = self.event()
        first = await self.client.post('/api/answer', json=event, headers=self.headers)
        self.assertEqual(await first.json(), {'recorded': True, 'points': 0, 'configured': False})
        second = await self.client.post('/api/answer', json=event, headers=self.headers)
        self.assertFalse((await second.json())['recorded'])
        ranking = await self.client.get('/api/rating', headers=self.headers)
        data = await ranking.json()
        self.assertEqual(data['me']['points'], 0)
        with self.store.connect() as con:
            self.assertEqual(con.execute('SELECT user_id FROM game_events').fetchone()[0], 42)

    async def test_wrong_and_timeout_are_preserved_for_future_rules(self):
        for selected in ('Wrong', None):
            response = await self.client.post('/api/answer', json=self.event(selected), headers=self.headers)
            self.assertEqual(response.status, 200)
        with self.store.connect() as con:
            self.assertEqual(con.execute('SELECT SUM(correct), COUNT(*) FROM game_events').fetchone()[:], (0, 2))

    async def test_invalid_answers_and_custom_questions_rejected(self):
        for changes in ({'question': True}, {'question': 99}, {'answer': 'Forged'}, {'text': 'Custom question'}, {'event_id': 'bad'}):
            response = await self.client.post('/api/answer', json={**self.event(), **changes}, headers=self.headers)
            self.assertEqual(response.status, 400)

    async def test_auth_and_cors(self):
        response = await self.client.get('/api/rating')
        self.assertEqual(response.status, 401)
        response = await self.client.get('/api/rating', headers={**self.headers, 'Origin': 'https://evil.example'})
        self.assertEqual(response.status, 403)
        response = await self.client.options('/api/answer', headers={'Origin': 'https://game.example'})
        self.assertEqual(response.status, 204)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], 'https://game.example')
        response = await self.client.get('/api/rating', headers={'Origin': 'https://game.example'})
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], 'https://game.example')

    async def test_future_scoring_is_injected_on_server(self):
        rules = SimpleNamespace(RULES_READY=True, points_for_answer=lambda q, selected: 7 if selected == q['a'][0] else 0)
        async with InProcessClient(create_api(self.store, TOKEN, [QUESTION], 'https://game.example', rules)) as client:
            response = await client.post('/api/answer', json=self.event(), headers=self.headers)
            self.assertEqual((await response.json())['points'], 7)

    async def test_delivery_is_only_sent_once(self):
        self.store.register(42, 'First')
        self.store.prepare_report('2026-11-02')
        bot = SimpleNamespace(send_message=AsyncMock())
        await deliver_pending(bot, self.store)
        await deliver_pending(bot, self.store)
        bot.send_message.assert_awaited_once()

    async def test_blocked_user_is_removed_from_delivery(self):
        self.store.register(42, 'First')
        self.store.prepare_report('2026-11-02')
        method = SendMessage(chat_id=42, text='test')
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramForbiddenError(method=method, message='blocked')))
        await deliver_pending(bot, self.store)
        self.assertEqual(self.store.pending(), [])


if __name__ == '__main__':
    unittest.main()
