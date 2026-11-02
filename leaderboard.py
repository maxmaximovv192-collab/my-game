"""Telegram-authenticated rating API and persistent daily delivery queue."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
from pathlib import Path
import sqlite3
import time
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramAPIError
import rating_rules

MOSCOW = timezone(timedelta(hours=3))
log = logging.getLogger(__name__)


def validate_init_data(raw, token, now=None):
    """Trust only the signed user, never initDataUnsafe or a submitted user ID."""
    try:
        pairs = parse_qsl(raw, strict_parsing=True)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError('Duplicate fields')
        signature = fields.pop('hash')
        check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError('Bad signature')
        age = (time.time() if now is None else now) - int(fields['auth_date'])
        if age < -30 or age > 86400:
            raise ValueError('Expired session')
        user = json.loads(fields['user'])
        if type(user.get('id')) is not int or user['id'] <= 0:
            raise ValueError('Invalid user')
        return user
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise web.HTTPUnauthorized(text='Откройте приложение заново из бота.') from exc


def load_questions(path):
    # Read the exact bank shipped to the frontend, without maintaining a second copy.
    source = Path(path).read_text(encoding='utf-8')
    start = source.index('var BASE = ') + len('var BASE = ')
    bank, _ = json.JSONDecoder().raw_decode(source[start:])
    return bank


class RatingStore:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def init(self):
        with self.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, coins INTEGER NOT NULL DEFAULT 0)')
            columns = {row['name'] for row in con.execute('PRAGMA table_info(users)')}
            if 'subscribed' not in columns:
                con.execute('ALTER TABLE users ADD COLUMN subscribed INTEGER NOT NULL DEFAULT 1')
            con.executescript('''
                CREATE TABLE IF NOT EXISTS game_events (
                    user_id INTEGER NOT NULL, event_id TEXT NOT NULL,
                    question_key TEXT NOT NULL, answer TEXT, correct INTEGER NOT NULL,
                    points INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                    PRIMARY KEY(user_id, event_id));
                CREATE TABLE IF NOT EXISTS daily_reports (
                    day TEXT PRIMARY KEY, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS daily_deliveries (
                    day TEXT NOT NULL, user_id INTEGER NOT NULL, text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY(day, user_id));
            ''')

    def register(self, user_id, name):
        with self.connect() as con:
            con.execute('INSERT INTO users(user_id,name) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name', (user_id, name[:128]))

    def subscribe(self, user_id, enabled):
        with self.connect() as con:
            con.execute('UPDATE users SET subscribed=? WHERE user_id=?', (int(enabled), user_id))
            if not enabled:
                con.execute("UPDATE daily_deliveries SET status='cancelled' WHERE user_id=? AND status='pending'", (user_id,))

    def record(self, user_id, event_id, question, selected, points=0, now=None):
        current = datetime.now(MOSCOW) if now is None else now
        key = hashlib.sha256(question['q'].strip().encode()).hexdigest()
        with self.connect() as con:
            result = con.execute('INSERT OR IGNORE INTO game_events VALUES (?,?,?,?,?,?,?)',
                                 (user_id, event_id, key, selected, int(selected == question['a'][0]), points, current.timestamp()))
            return result.rowcount == 1

    def _ranking(self, con):
        return [dict(row) for row in con.execute('''
            SELECT u.user_id, u.name, COALESCE(SUM(a.points),0) AS points
            FROM users u LEFT JOIN game_events a ON a.user_id=u.user_id
            GROUP BY u.user_id ORDER BY points DESC, u.user_id ASC
        ''')]

    @staticmethod
    def _places(rows):
        last_points, place = None, 0
        for index, row in enumerate(rows, 1):
            if row['points'] != last_points:
                place, last_points = index, row['points']
            row['place'] = place
        return rows

    def ranking(self, user_id):
        with self.connect() as con:
            rows = self._places(self._ranking(con))
        me = next((row for row in rows if row['user_id'] == user_id), None)
        # No Telegram IDs are exposed through the API.
        public = lambda row: {k: row[k] for k in ('name', 'points', 'place')}
        return {'top': [public(row) for row in rows[:10] if row['points'] > 0],
                'me': public(me) if me else None, 'total': len(rows)}

    def prepare_report(self, day):
        """Freeze everyone's text in one transaction for consistent delivery."""
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute('SELECT 1 FROM daily_reports WHERE day=?', (day,)).fetchone():
                return
            rows = self._places(self._ranking(con))
            top = [row for row in rows if row['points'] > 0][:3]
            lines = [f'🏆 Рейтинг за всё время · {day}', '']
            lines += [f"{row['place']}. {row['name']}: {row['points']} балл." for row in top]
            if not top:
                lines.append('Пока нет результатов в рейтинге.')
            base = '\n'.join(lines)
            recipients = {row['user_id'] for row in con.execute('SELECT user_id FROM users WHERE subscribed=1')}
            for row in rows:
                if row['user_id'] in recipients:
                    own = f"\n\nВаше место: {row['place']} из {len(rows)}\nВаши баллы: {row['points']}\n/play — играть · /stop — отключить рассылку"
                    con.execute('INSERT INTO daily_deliveries(day,user_id,text) VALUES (?,?,?)', (day, row['user_id'], base + own))
            con.execute('INSERT INTO daily_reports VALUES (?,?)', (day, time.time()))

    def pending(self):
        with self.connect() as con:
            return [dict(row) for row in con.execute("SELECT d.* FROM daily_deliveries d JOIN users u USING(user_id) WHERE d.status='pending' AND u.subscribed=1 ORDER BY d.day,d.user_id")]

    def mark(self, day, user_id, status):
        with self.connect() as con:
            con.execute('UPDATE daily_deliveries SET status=? WHERE day=? AND user_id=?', (status, day, user_id))


def create_api(store, token, bank, origin, rules=rating_rules):
    @web.middleware
    async def middleware(request, handler):
        request_origin = request.headers.get('Origin')
        if request_origin and request_origin != origin:
            raise web.HTTPForbidden(text='Origin is not allowed')
        try:
            if request.method == 'OPTIONS':
                response = web.Response(status=204)
            else:
                if request.path != '/health':
                    auth = request.headers.get('Authorization', '')
                    if not auth.startswith('tma '):
                        raise web.HTTPUnauthorized(text='Откройте игру из Telegram.')
                    user = validate_init_data(auth[4:], token)
                    request['user_id'] = user['id']
                    name = ' '.join(str(user.get(k, '')) for k in ('first_name', 'last_name')).strip() or 'Игрок'
                    store.register(user['id'], name)
                response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        if request_origin == origin:
            response.headers.update({'Access-Control-Allow-Origin': origin, 'Vary': 'Origin',
                                     'Access-Control-Allow-Headers': 'Authorization, Content-Type',
                                     'Access-Control-Allow-Methods': 'GET, POST, OPTIONS'})
        response.headers['Cache-Control'] = 'no-store'
        return response

    async def health(request):
        return web.json_response({'ok': True})

    async def ranking(request):
        return web.json_response({**store.ranking(request['user_id']), 'configured': rules.RULES_READY})

    async def answer(request):
        try:
            data = await request.json()
            index, selected = data['question'], data['answer']
            event_id = data['event_id']
            if not isinstance(event_id, str) or len(event_id) != 36:
                raise ValueError()
            from uuid import UUID
            UUID(event_id)
            if type(index) is not int or not 0 <= index < len(bank) or not (selected is None or isinstance(selected, str)):
                raise ValueError()
            question = bank[index]
            if (selected is not None and selected not in question['a']) or data.get('text') != question['q']:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(text='Некорректный ответ.')
        points = rules.points_for_answer(question, selected) if rules.RULES_READY else 0
        recorded = store.record(request['user_id'], event_id, question, selected, points)
        return web.json_response({'recorded': recorded, 'points': points if recorded else 0, 'configured': rules.RULES_READY})

    app = web.Application(middlewares=[middleware], client_max_size=16384)
    app.router.add_get('/health', health)
    app.router.add_get('/api/rating', ranking)
    app.router.add_post('/api/answer', answer)
    app.router.add_route('OPTIONS', '/{path:.*}', health)
    return app


async def deliver_pending(bot, store):
    for row in store.pending():
        try:
            await bot.send_message(row['user_id'], row['text'], parse_mode=None)
        except TelegramForbiddenError:
            store.subscribe(row['user_id'], False)
            store.mark(row['day'], row['user_id'], 'blocked')
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(exc.retry_after + 1, 60))
            return
        except (TelegramAPIError, OSError):
            log.exception('Daily delivery failed for user %s', row['user_id'])
            return
        else:
            store.mark(row['day'], row['user_id'], 'sent')
        await asyncio.sleep(0.05)


async def daily_worker(bot, store, daily_time='21:00'):
    hour, minute = map(int, daily_time.split(':'))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError('DAILY_TIME must be HH:MM')
    while True:
        try:
            now = datetime.now(MOSCOW)
            if (now.hour, now.minute) >= (hour, minute):
                store.prepare_report(now.date().isoformat())
            await deliver_pending(bot, store)
        except (sqlite3.Error, OSError):
            log.exception('Daily worker failed; retrying in 30 seconds')
        await asyncio.sleep(30)
