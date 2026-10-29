"""Manufacturing planning and lot traceability. Run: python app.py --demo."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
from functools import wraps
import io
import json
import math
import smtplib
import ssl
from email.message import EmailMessage
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # PostgreSQL is optional for local SQLite development.
    psycopg = None
    dict_row = None

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.python-packages'))
from flask import Flask, g, jsonify, request, session, send_file
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode
import qrcode.image.svg

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
 password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('manager','operator','client')), status TEXT NOT NULL DEFAULT 'approved' CHECK(status IN ('pending','approved')), requested_machine_id INTEGER REFERENCES machines(id));
CREATE TABLE IF NOT EXISTS machines(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('production','qc','storage')), available INTEGER NOT NULL DEFAULT 1,class_code TEXT,unavailable_until TEXT,retired INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS machine_faults(id INTEGER PRIMARY KEY,machine_id INTEGER NOT NULL REFERENCES machines(id),operation_id INTEGER REFERENCES operations(id),reported_by INTEGER REFERENCES users(id),reported_at TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',repair_minutes REAL,status TEXT NOT NULL DEFAULT 'open',resolved_at TEXT);
CREATE TABLE IF NOT EXISTS work_sessions(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),machine_id INTEGER NOT NULL REFERENCES machines(id),operation_id INTEGER REFERENCES operations(id),started_at TEXT NOT NULL,ended_at TEXT);
CREATE TABLE IF NOT EXISTS shift_logs(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),machine_id INTEGER NOT NULL REFERENCES machines(id),shift_code TEXT NOT NULL,group_key TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT,extra_hours REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS shift_schedule(user_id INTEGER NOT NULL REFERENCES users(id),work_date TEXT NOT NULL,shift_code TEXT NOT NULL,PRIMARY KEY(user_id,work_date));
CREATE TABLE IF NOT EXISTS assignments(user_id INTEGER REFERENCES users(id), machine_id INTEGER REFERENCES machines(id), PRIMARY KEY(user_id,machine_id));
CREATE TABLE IF NOT EXISTS parts(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routes(id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES parts(id), version INTEGER NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(part_id,version));
CREATE TABLE IF NOT EXISTS route_steps(id INTEGER PRIMARY KEY, route_id INTEGER NOT NULL REFERENCES routes(id), seq INTEGER NOT NULL,
 machine_id INTEGER NOT NULL REFERENCES machines(id), minutes REAL NOT NULL, transition_minutes REAL NOT NULL, instructions TEXT NOT NULL,min_minutes REAL NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, client_id INTEGER NOT NULL REFERENCES users(id),
 part_id INTEGER NOT NULL REFERENCES parts(id), route_id INTEGER NOT NULL REFERENCES routes(id), quantity INTEGER NOT NULL,
 priority INTEGER NOT NULL, due_date TEXT NOT NULL, created_at TEXT NOT NULL, approved_eta TEXT, client_approved_at TEXT);
CREATE TABLE IF NOT EXISTS lots(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), code TEXT UNIQUE NOT NULL,
 token TEXT UNIQUE NOT NULL, quantity INTEGER NOT NULL, location TEXT);
CREATE TABLE IF NOT EXISTS operations(id INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL REFERENCES lots(id), seq INTEGER NOT NULL,
 machine_id INTEGER NOT NULL REFERENCES machines(id), minutes REAL NOT NULL, transition_minutes REAL NOT NULL, instructions TEXT NOT NULL,min_minutes REAL NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'pending', rank REAL NOT NULL DEFAULT 0, planned_start TEXT, planned_end TEXT,
 actual_start TEXT, actual_end TEXT, operator_id INTEGER REFERENCES users(id), UNIQUE(lot_id,seq));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_machine ON operations(machine_id) WHERE status='running';
CREATE TABLE IF NOT EXISTS quality(id INTEGER PRIMARY KEY, operation_id INTEGER REFERENCES operations(id), result TEXT NOT NULL,
 notes TEXT NOT NULL, user_id INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, lot_id INTEGER REFERENCES lots(id), user_id INTEGER REFERENCES users(id),
 kind TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS warehouse_slots(id INTEGER PRIMARY KEY, machine_id INTEGER NOT NULL REFERENCES machines(id),
 row_number INTEGER NOT NULL, bay_number INTEGER NOT NULL, UNIQUE(machine_id,row_number,bay_number));
CREATE TABLE IF NOT EXISTS warehouse_reservations(slot_id INTEGER PRIMARY KEY REFERENCES warehouse_slots(id),
 order_id INTEGER NOT NULL REFERENCES orders(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS route_warehouse_slots(route_id INTEGER NOT NULL REFERENCES routes(id),
 slot_id INTEGER NOT NULL REFERENCES warehouse_slots(id), PRIMARY KEY(route_id,slot_id));
CREATE TABLE IF NOT EXISTS app_migrations(name TEXT PRIMARY KEY);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS machines(id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('production','qc','storage')), available INTEGER NOT NULL DEFAULT 1, class_code TEXT, unavailable_until TEXT, retired INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
 password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('manager','operator','client')), status TEXT NOT NULL DEFAULT 'approved' CHECK(status IN ('pending','approved')), requested_machine_id INTEGER REFERENCES machines(id), worker_type TEXT NOT NULL DEFAULT 'production');
CREATE TABLE IF NOT EXISTS assignments(user_id INTEGER REFERENCES users(id), machine_id INTEGER REFERENCES machines(id), PRIMARY KEY(user_id,machine_id));
CREATE TABLE IF NOT EXISTS parts(id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS warehouse_slots(id SERIAL PRIMARY KEY, machine_id INTEGER NOT NULL REFERENCES machines(id),
 row_number INTEGER NOT NULL, bay_number INTEGER NOT NULL, UNIQUE(machine_id,row_number,bay_number));
CREATE TABLE IF NOT EXISTS routes(id SERIAL PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES parts(id), version INTEGER NOT NULL,
 created_at TEXT NOT NULL, warehouse_slot_id INTEGER REFERENCES warehouse_slots(id), UNIQUE(part_id,version));
CREATE TABLE IF NOT EXISTS route_steps(id SERIAL PRIMARY KEY, route_id INTEGER NOT NULL REFERENCES routes(id), seq INTEGER NOT NULL,
 machine_id INTEGER NOT NULL REFERENCES machines(id), minutes REAL NOT NULL, transition_minutes REAL NOT NULL, instructions TEXT NOT NULL,min_minutes REAL NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders(id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, client_id INTEGER NOT NULL REFERENCES users(id),
 part_id INTEGER NOT NULL REFERENCES parts(id), route_id INTEGER NOT NULL REFERENCES routes(id), quantity INTEGER NOT NULL,
 priority INTEGER NOT NULL, due_date TEXT NOT NULL, created_at TEXT NOT NULL, approved_eta TEXT, warehouse_slot_id INTEGER REFERENCES warehouse_slots(id), client_approved_at TEXT);
CREATE TABLE IF NOT EXISTS lots(id SERIAL PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), code TEXT UNIQUE NOT NULL,
 token TEXT UNIQUE NOT NULL, quantity INTEGER NOT NULL, location TEXT);
CREATE TABLE IF NOT EXISTS operations(id SERIAL PRIMARY KEY, lot_id INTEGER NOT NULL REFERENCES lots(id), seq INTEGER NOT NULL,
 machine_id INTEGER NOT NULL REFERENCES machines(id), minutes REAL NOT NULL, transition_minutes REAL NOT NULL, instructions TEXT NOT NULL, min_minutes REAL NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'pending', rank REAL NOT NULL DEFAULT 0, planned_start TEXT, planned_end TEXT,
 actual_start TEXT, actual_end TEXT, operator_id INTEGER REFERENCES users(id), UNIQUE(lot_id,seq));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_machine ON operations(machine_id) WHERE status='running';
CREATE TABLE IF NOT EXISTS quality(id SERIAL PRIMARY KEY, operation_id INTEGER REFERENCES operations(id), result TEXT NOT NULL,
 notes TEXT NOT NULL, user_id INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id SERIAL PRIMARY KEY, lot_id INTEGER REFERENCES lots(id), user_id INTEGER REFERENCES users(id),
 kind TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS warehouse_reservations(slot_id INTEGER PRIMARY KEY REFERENCES warehouse_slots(id),
 order_id INTEGER NOT NULL REFERENCES orders(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS route_warehouse_slots(route_id INTEGER NOT NULL REFERENCES routes(id),
 slot_id INTEGER NOT NULL REFERENCES warehouse_slots(id), PRIMARY KEY(route_id,slot_id));
CREATE TABLE IF NOT EXISTS app_migrations(name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS machine_faults(id SERIAL PRIMARY KEY,machine_id INTEGER NOT NULL REFERENCES machines(id),operation_id INTEGER REFERENCES operations(id),reported_by INTEGER REFERENCES users(id),reported_at TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',repair_minutes REAL,status TEXT NOT NULL DEFAULT 'open',resolved_at TEXT);
CREATE TABLE IF NOT EXISTS work_sessions(id SERIAL PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),machine_id INTEGER NOT NULL REFERENCES machines(id),operation_id INTEGER REFERENCES operations(id),started_at TEXT NOT NULL,ended_at TEXT);
CREATE TABLE IF NOT EXISTS shift_logs(id SERIAL PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),machine_id INTEGER NOT NULL REFERENCES machines(id),shift_code TEXT NOT NULL,group_key TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT,extra_hours REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS shift_schedule(user_id INTEGER NOT NULL REFERENCES users(id),work_date TEXT NOT NULL,shift_code TEXT NOT NULL,PRIMARY KEY(user_id,work_date));
CREATE UNIQUE INDEX IF NOT EXISTS one_station_per_operator ON assignments(user_id);
"""

ID_TABLES = {'users','machines','parts','routes','route_steps','orders','lots','operations','quality','events','warehouse_slots','machine_faults','work_sessions','shift_logs'}

class PostgresCursor:
    def __init__(self, cursor, lastrowid=None):
        self.cursor, self.lastrowid = cursor, lastrowid

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)

class PostgresConnection:
    """Small compatibility layer that keeps the application's parameterized SQL portable."""
    def __init__(self, url):
        if psycopg is None:
            raise RuntimeError('PostgreSQL support is not installed. Run: pip install -r requirements.txt')
        self.connection = psycopg.connect(url, autocommit=True, row_factory=dict_row)

    def execute(self, sql, args=()):
        statement = sql.strip()
        if statement.upper() == 'BEGIN IMMEDIATE':
            statement = 'BEGIN'
        statement = statement.replace('?', '%s')
        table = None
        if statement.upper().startswith('INSERT INTO '):
            table = statement.split()[2].split('(')[0].lower()
        wants_id = table in ID_TABLES and 'RETURNING ' not in statement.upper()
        if wants_id:
            statement += ' RETURNING id'
        cursor = self.connection.execute(statement, args)
        returned = cursor.fetchone() if wants_id else None
        lastrowid = returned['id'] if returned else None
        return PostgresCursor(cursor, lastrowid)

    def executemany(self, sql, args):
        cursor = self.connection.cursor()
        cursor.executemany(sql.replace('?', '%s'), args)
        return PostgresCursor(cursor)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()

    def __enter__(self):
        self.connection.execute('BEGIN')
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.rollback() if exc_type else self.commit()

def connect_database(database):
    if str(database).startswith(('postgres://', 'postgresql://')):
        return PostgresConnection(str(database))
    connection = sqlite3.connect(database, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    return connection

def utcnow():
    return datetime.now(timezone.utc)

def stamp(value=None):
    return (value or utcnow()).isoformat(timespec='seconds')

def dt(value):
    return datetime.fromisoformat(value)

class Invalid(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status

def require(condition, message, status=400):
    if not condition:
        raise Invalid(message, status)

def text_value(data, key, maximum=200):
    value = str(data.get(key, '')).strip()
    require(0 < len(value) <= maximum, f'{key.replace("_", " ").capitalize()} is required (maximum {maximum} characters).')
    return value

def number(data, key, low=0, high=1000000, integer=False):
    try:
        value = float(data[key])
    except (ValueError, TypeError, KeyError):
        raise Invalid(f'{key} must be a number.')
    require(math.isfinite(value) and low <= value <= high and (not integer or value.is_integer()), f'{key} must be between {low} and {high}' + (' and a whole number.' if integer else '.'))
    return int(value) if integer else value

def create_app(database=None, testing=False):
    app = Flask(__name__, static_folder='static', static_url_path='/static')
    instance = ROOT / 'instance'
    database = database or os.environ.get('DATABASE_URL') or instance / 'manufacturing.db'
    if os.environ.get('VERCEL') and not os.environ.get('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL is required on Vercel. Connect a PostgreSQL database and redeploy.')
    if not str(database).startswith(('postgres://', 'postgresql://')):
        instance.mkdir(exist_ok=True)
    secret_path = instance / 'session-key'
    configured_secret = os.environ.get('SECRET_KEY')
    if not configured_secret:
        if str(database).startswith(('postgres://', 'postgresql://')):
            raise RuntimeError('SECRET_KEY must be set when using PostgreSQL.')
        if not secret_path.exists():
            secret_path.write_text(secrets.token_hex(32), encoding='utf8')
        configured_secret = secret_path.read_text()
    app.config.update(SECRET_KEY=configured_secret, DATABASE=str(database), TESTING=testing,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == '1',
                      FACTORY_UTC_OFFSET_MINUTES=int(os.environ.get('FACTORY_UTC_OFFSET_MINUTES', '330')),
                      SMTP_HOST=os.environ.get('SMTP_HOST'), SMTP_PORT=int(os.environ.get('SMTP_PORT', '587')),
                      SMTP_USER=os.environ.get('SMTP_USER'), SMTP_PASSWORD=os.environ.get('SMTP_PASSWORD'),
                      SMTP_FROM=os.environ.get('SMTP_FROM') or os.environ.get('SMTP_USER'),
                      MAX_CONTENT_LENGTH=1024 * 1024, PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    is_postgres = app.config['DATABASE'].startswith(('postgres://', 'postgresql://'))
    with closing(connect_database(app.config['DATABASE'])) as conn, conn:
        if is_postgres:
            conn.execute(POSTGRES_SCHEMA)
            conn.execute('ALTER TABLE machines ADD COLUMN IF NOT EXISTS class_code TEXT')
            conn.execute('ALTER TABLE machines ADD COLUMN IF NOT EXISTS unavailable_until TEXT')
            conn.execute('ALTER TABLE machines ADD COLUMN IF NOT EXISTS retired INTEGER NOT NULL DEFAULT 0')
            conn.execute('ALTER TABLE route_steps ADD COLUMN IF NOT EXISTS min_minutes REAL NOT NULL DEFAULT 1')
            conn.execute('ALTER TABLE operations ADD COLUMN IF NOT EXISTS min_minutes REAL NOT NULL DEFAULT 1')
            conn.execute("ALTER TABLE machine_faults ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT ''")
            conn.execute('ALTER TABLE orders ADD COLUMN IF NOT EXISTS client_approved_at TEXT')
            conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS worker_type TEXT NOT NULL DEFAULT \'production\'')
            conn.execute('UPDATE machines SET class_code=code WHERE class_code IS NULL')
            conn.execute('ALTER TABLE warehouse_reservations DROP CONSTRAINT IF EXISTS warehouse_reservations_order_id_key')
        else:
            conn.executescript(SCHEMA)
            for table, column, definition in (('machines','class_code','TEXT'),('machines','unavailable_until','TEXT'),('machines','retired','INTEGER NOT NULL DEFAULT 0'),('route_steps','min_minutes','REAL NOT NULL DEFAULT 1'),('operations','min_minutes','REAL NOT NULL DEFAULT 1'),('machine_faults','reason',"TEXT NOT NULL DEFAULT ''"),('orders','client_approved_at','TEXT')):
                if column not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
            conn.execute('UPDATE machines SET class_code=code WHERE class_code IS NULL')
        if not conn.execute("SELECT 1 FROM app_migrations WHERE name='manager_order_approval'").fetchone():
            conn.execute('UPDATE orders SET client_approved_at=COALESCE(approved_eta,created_at) WHERE client_approved_at IS NULL')
            conn.execute("INSERT INTO app_migrations(name) VALUES('manager_order_approval')")
        if is_postgres:
            if os.environ.get('INITIAL_MANAGER_EMAIL') and not conn.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                initial_password = os.environ.get('INITIAL_MANAGER_PASSWORD', '')
                require(len(initial_password) >= 12, 'INITIAL_MANAGER_PASSWORD must contain at least 12 characters.')
                conn.execute('INSERT INTO users(name,email,password,role) VALUES(?,?,?,\'manager\') ON CONFLICT (email) DO NOTHING',
                             (os.environ.get('INITIAL_MANAGER_NAME', 'System Manager'), os.environ['INITIAL_MANAGER_EMAIL'].strip().lower(), generate_password_hash(initial_password)))
        else:
            user_columns = {r[1] for r in conn.execute('PRAGMA table_info(users)')}
            if 'status' not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
            if 'requested_machine_id' not in user_columns:
                conn.execute('ALTER TABLE users ADD COLUMN requested_machine_id INTEGER REFERENCES machines(id)')
            if 'worker_type' not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN worker_type TEXT NOT NULL DEFAULT 'production'")
                conn.execute("UPDATE users SET worker_type=COALESCE((SELECT m.kind FROM assignments a JOIN machines m ON m.id=a.machine_id WHERE a.user_id=users.id LIMIT 1),(SELECT kind FROM machines WHERE id=users.requested_machine_id),'production') WHERE role='operator'")
            for table in ('routes','orders'):
                if 'warehouse_slot_id' not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN warehouse_slot_id INTEGER REFERENCES warehouse_slots(id)')
            if not conn.execute("SELECT 1 FROM app_migrations WHERE name='multiple_warehouse_reservations'").fetchone():
                conn.executescript('''
                    PRAGMA foreign_keys=OFF;
                    BEGIN;
                    ALTER TABLE warehouse_reservations RENAME TO warehouse_reservations_single;
                    CREATE TABLE warehouse_reservations(slot_id INTEGER PRIMARY KEY REFERENCES warehouse_slots(id),
                     order_id INTEGER NOT NULL REFERENCES orders(id), created_at TEXT NOT NULL);
                    INSERT INTO warehouse_reservations SELECT slot_id,order_id,created_at FROM warehouse_reservations_single;
                    DROP TABLE warehouse_reservations_single;
                    INSERT INTO app_migrations(name) VALUES('multiple_warehouse_reservations');
                    COMMIT;
                    PRAGMA foreign_keys=ON;
                ''')
            # Extend the original demo floor once; real installations manage their own inventory.
            if not conn.execute("SELECT 1 FROM app_migrations WHERE name='expanded_demo_floor'").fetchone():
                if conn.execute("SELECT 1 FROM users WHERE email='manager@demo.local'").fetchone():
                    for n in range(1,13):
                        conn.execute("INSERT OR IGNORE INTO machines(code,name,kind) VALUES(?,?,'production')",(f'M{n}',f'Production machine {n}'))
                    conn.execute("INSERT INTO app_migrations VALUES('expanded_demo_floor')")
            for (warehouse_id,) in conn.execute("SELECT id FROM machines WHERE kind='storage'").fetchall():
                if not conn.execute('SELECT 1 FROM warehouse_slots WHERE machine_id=?',(warehouse_id,)).fetchone():
                    conn.executemany('INSERT INTO warehouse_slots(machine_id,row_number,bay_number) VALUES(?,?,?)',[(warehouse_id,r,b) for r in range(1,4) for b in range(1,11)])
            conn.execute('DELETE FROM assignments WHERE rowid NOT IN (SELECT MIN(rowid) FROM assignments GROUP BY user_id)')
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_station_per_operator ON assignments(user_id)')
            conn.commit()
            conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('''INSERT INTO route_warehouse_slots(route_id,slot_id)
                        SELECT id,warehouse_slot_id FROM routes WHERE warehouse_slot_id IS NOT NULL
                        ON CONFLICT(route_id,slot_id) DO NOTHING''')

    def db():
        if 'db' not in g:
            g.db = connect_database(app.config['DATABASE'])
        return g.db

    def rows(sql, args=()):
        return [dict(r) for r in db().execute(sql, args)]

    def one(sql, args=()):
        result = db().execute(sql, args).fetchone()
        require(result is not None, 'Record not found.', 404)
        return dict(result)

    def maybe_one(sql, args=()):
        result = db().execute(sql, args).fetchone()
        return dict(result) if result is not None else None

    def event(lot, kind, details):
        db().execute('INSERT INTO events(lot_id,user_id,kind,details,created_at) VALUES(?,?,?,?,?)',
                     (lot, g.user['id'], kind, json.dumps(details), stamp()))

    def notify_maintenance_delay(machine_id, fault_id, reason, lot_ids=()):
        machine=one('SELECT code,name FROM machines WHERE id=?',(machine_id,))
        impacted=set(lot_ids)
        impacted.update(r['lot_id'] for r in rows("SELECT DISTINCT lot_id FROM operations WHERE machine_id=? AND status IN ('pending','hold')",(machine_id,)))
        for lot_id in sorted(impacted):
            event(lot_id,'Maintenance delay update',dict(fault_id=fault_id,machine_code=machine['code'],machine_name=machine['name'],
                reason=reason or 'Unexpected machine maintenance',repair_minutes=None,eta=None))

    def send_account_email(email, name, password):
        require(app.config['SMTP_HOST'] and app.config['SMTP_FROM'], 'Email delivery is not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_FROM before creating a client.', 503)
        message = EmailMessage()
        message['Subject'] = 'Your ForgeFlow client account'
        message['From'] = app.config['SMTP_FROM']
        message['To'] = email
        message.set_content(f"Hello {name},\n\nYour ForgeFlow client account is ready.\nEmail: {email}\nTemporary password: {password}\n\nSign in to your company's ForgeFlow workspace. Keep these credentials private.")
        with smtplib.SMTP(app.config['SMTP_HOST'], app.config['SMTP_PORT'], timeout=20) as server:
            server.starttls(context=ssl.create_default_context())
            if app.config['SMTP_USER']:
                server.login(app.config['SMTP_USER'], app.config['SMTP_PASSWORD'] or '')
            server.send_message(message)

    def auth(*roles):
        def decorator(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                require(g.user is not None, 'Please sign in.', 401)
                require(not roles or g.user['role'] in roles, 'Your account cannot perform this action.', 403)
                return fn(*args, **kwargs)
            return wrapped
        return decorator

    @app.before_request
    def prepare():
        g.user = None
        if session.get('uid'):
            found = rows("SELECT id,name,email,role,worker_type FROM users WHERE id=? AND status='approved'", (session['uid'],))
            g.user = found[0] if found else None
        if request.path.startswith('/api/') and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            require(session.get('csrf') and secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), session['csrf']), 'Session expired. Refresh the page and try again.', 403)
            require(isinstance(request.get_json(silent=True), dict), 'Send a valid JSON object.')
            db().execute('BEGIN IMMEDIATE')
        elif request.path.startswith('/api/'):
            # One coherent read snapshot even when another device creates a lot mid-request.
            db().execute('BEGIN')

    @app.after_request
    def secure(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        if 'db' in g:
            if response.status_code < 400:
                g.db.commit()
            else:
                g.db.rollback()
        return response

    @app.teardown_appcontext
    def close(error=None):
        connection = g.pop('db', None)
        if connection:
            connection.close()

    @app.errorhandler(Invalid)
    def invalid(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(sqlite3.IntegrityError)
    def conflict(error):
        return jsonify(error='That code or email already exists, or the machine is already running. Refresh and try again.'), 409

    if psycopg:
        app.register_error_handler(psycopg.IntegrityError, conflict)

    @app.get('/')
    def index():
        return app.send_static_file('index.html')

    @app.get('/api/session')
    def current_session():
        session.setdefault('csrf', secrets.token_hex(24))
        return jsonify(user=g.user, csrf=session['csrf'])

    login_attempts = {}

    @app.post('/api/login')
    def login():
        key = request.remote_addr
        attempts = [t for t in login_attempts.get(key, []) if time.monotonic() - t < 300]
        require(len(attempts) < 15, 'Too many attempts. Please wait five minutes.', 429)
        data = request.get_json() or {}
        user = rows('SELECT * FROM users WHERE email=?', (str(data.get('email', '')).strip().lower(),))
        if not user or not check_password_hash(user[0]['password'], str(data.get('password', ''))):
            login_attempts[key] = attempts + [time.monotonic()]
            raise Invalid('Email or password is incorrect.', 401)
        if user[0]['status'] != 'approved':
            raise Invalid('Your staff registration is awaiting manager approval.', 403)
        login_attempts.pop(key, None)
        session.clear()
        session.update(uid=user[0]['id'], csrf=secrets.token_hex(24))
        session.permanent = True
        return jsonify(user={k:user[0][k] for k in ('id','name','email','role','worker_type')}, csrf=session['csrf'])

    @app.post('/api/operator-register')
    def operator_register():
        data = request.get_json() or {}
        name = text_value(data, 'name', 120)
        email = text_value(data, 'email').lower()
        require('@' in email and '.' in email.split('@')[-1], 'Enter a valid email address.')
        password = text_value(data, 'password', 128)
        require(len(password) >= 12, 'Use a password of at least 12 characters.')
        worker_type=data.get('worker_type','production')
        require(worker_type in ('production','qc','storage','maintenance'),'Choose a valid staff role.')
        db().execute("INSERT INTO users(name,email,password,role,status,requested_machine_id,worker_type) VALUES(?,?,?,'operator','pending',NULL,?)", (name,email,generate_password_hash(password),worker_type))
        return jsonify(ok=True, message='Registration submitted. A manager must approve your account before you can sign in.'), 201

    @app.get('/api/registration-machines')
    def registration_machines():
        worker_type=request.args.get('worker_type','production')
        require(worker_type in ('production','qc','storage','maintenance'),'Choose a valid job role.')
        return jsonify(machines=rows('SELECT id,code,name,kind FROM machines WHERE kind=? AND retired=0 ORDER BY id',(worker_type,)) if worker_type!='maintenance' else rows('SELECT id,code,name,kind FROM machines WHERE retired=0 ORDER BY id'))

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    def all_operations():
        return rows('''SELECT x.*, m.code machine_code,m.name machine_name,m.kind,m.available,m.class_code,m.unavailable_until,
          l.code lot_code,l.quantity,l.order_id,l.location,o.code order_code,o.part_id,o.route_id,o.priority,o.created_at,o.client_id,
          o.warehouse_slot_id,o.client_approved_at,ws.row_number || '/' || ws.bay_number warehouse_address,
          p.code part_code,p.name part_name FROM operations x JOIN machines m ON m.id=x.machine_id
          JOIN lots l ON l.id=x.lot_id JOIN orders o ON o.id=l.order_id JOIN parts p ON p.id=o.part_id
          LEFT JOIN warehouse_slots ws ON ws.id=o.warehouse_slot_id''')

    def warehouse_state():
        return rows('''SELECT s.*,s.row_number || '/' || s.bay_number address,m.code warehouse_code,
          r.order_id,o.code order_code,
          CASE WHEN r.order_id IS NULL THEN 'available'
            WHEN EXISTS(SELECT 1 FROM lots l WHERE l.order_id=r.order_id AND l.location=s.row_number || '/' || s.bay_number) THEN 'occupied'
            ELSE 'reserved' END status
          FROM warehouse_slots s JOIN machines m ON m.id=s.machine_id
          LEFT JOIN warehouse_reservations r ON r.slot_id=s.id LEFT JOIN orders o ON o.id=r.order_id
          ORDER BY s.machine_id,s.row_number,s.bay_number''')

    def available_slot(slot_id, warehouse_id):
        slot=one('SELECT * FROM warehouse_slots WHERE id=?',(slot_id,))
        require(slot['machine_id']==warehouse_id,'Choose an address in the route warehouse.')
        require(not rows('SELECT 1 FROM warehouse_reservations WHERE slot_id=?',(slot_id,)), 'That warehouse address has already been reserved. Refresh and select another available address.',409)
        return slot

    def available_slots(slot_ids, warehouse_id):
        require(isinstance(slot_ids,list) and slot_ids,'Choose at least one warehouse address.')
        require(len(slot_ids)<=50,'Select no more than 50 warehouse addresses.')
        unique=[]
        for value in slot_ids:
            try:
                slot_id=int(value)
            except (TypeError,ValueError):
                raise Invalid('Choose valid warehouse addresses.')
            if slot_id not in unique:
                unique.append(slot_id)
        return [available_slot(slot_id,warehouse_id) for slot_id in unique]

    def forecast(ops, offline_ready=None):
        """Serial schedule generation; continuous capacity, one operation per machine.
        Held/unavailable predecessors block their descendants. Active work is never preempted.
        """
        now = utcnow()
        offline_ready = offline_ready or {}
        available = {machine_id:max(now,ready_at) for machine_id,ready_at in offline_ready.items()}
        ends, result = {}, {}
        predecessors = {}
        by_lot = {}
        for op in ops:
            by_lot.setdefault(op['lot_id'], []).append(op)
        for lot_ops in by_lot.values():
            previous = None
            for op in sorted(lot_ops, key=lambda x:x['seq']):
                predecessors[op['id']] = previous
                previous = op['id']
        for op in ops:
            if op['status'] == 'complete':
                ends[op['id']] = dt(op['actual_end'])
                result[op['id']] = (op['actual_start'], op['actual_end'])
            elif op['status'] == 'running':
                end = max(now + timedelta(minutes=1), dt(op['actual_start']) + timedelta(minutes=op['minutes']))
                ends[op['id']] = end
                available[op['machine_id']] = end
                result[op['id']] = (op['actual_start'], stamp(end))
        pending = [o for o in ops if o['status'] == 'pending' and (o['available'] or o['machine_id'] in offline_ready) and o['client_approved_at']]
        while pending:
            eligible = [o for o in pending if predecessors[o['id']] is None or predecessors[o['id']] in ends]
            if not eligible:
                break
            op = min(eligible, key=lambda o:(o['rank'], -o['priority'], o['created_at'], o['order_id'], o['lot_id'], o['seq']))
            prev = predecessors[op['id']]
            ready = (ends[prev] + timedelta(minutes=op['transition_minutes'])) if prev else now
            start = max(now, ready, available.get(op['machine_id'], now))
            end = start + timedelta(minutes=op['minutes'])
            ends[op['id']] = end
            available[op['machine_id']] = end
            result[op['id']] = (stamp(start), stamp(end))
            pending.remove(op)
        return result

    def assemble():
        ops = all_operations()
        schedule = forecast(ops)
        history = {}
        for op in ops:
            if op['status'] == 'complete' and op['actual_start'] and op['actual_end']:
                key = (op['part_id'], op['route_id'], op['machine_id'], op['quantity'], op['seq'])
                history.setdefault(key, []).append((dt(op['actual_end'])-dt(op['actual_start'])).total_seconds()/60)
        for op in ops:
            op['forecast_start'], op['forecast_end'] = schedule.get(op['id'], (None, None))
            samples = history.get((op['part_id'], op['route_id'], op['machine_id'], op['quantity'], op['seq']), [])
            # Exclude this completed observation from its own benchmark.
            if op['status'] == 'complete':
                samples = samples.copy()
                samples.remove((dt(op['actual_end'])-dt(op['actual_start'])).total_seconds()/60)
            op['sample_count'] = len(samples)
            op['historical_minutes'] = sum(samples)/len(samples) if len(samples) >= 5 else None
            actual_finish = dt(op['actual_end']) if op['actual_end'] else utcnow()
            op['actual_minutes'] = max(0, (actual_finish-dt(op['actual_start'])).total_seconds()/60) if op['actual_start'] else None
            op['deviation'] = ((op['actual_minutes']/op['historical_minutes'])-1)*100 if op['historical_minutes'] and op['actual_minutes'] is not None else None
            op['start_delay'] = (dt(op['actual_start'])-dt(op['planned_start'])).total_seconds()/60 if op['actual_start'] and op['planned_start'] else None
            op['completion_delay'] = (dt(op['actual_end'])-dt(op['planned_end'])).total_seconds()/60 if op['actual_end'] and op['planned_end'] else None
        orders = rows('''SELECT o.*,p.code part_code,p.name part_name,u.name client_name,r.version route_version
                         FROM orders o JOIN parts p ON p.id=o.part_id JOIN users u ON u.id=o.client_id JOIN routes r ON r.id=o.route_id ORDER BY o.id DESC''')
        lots = rows('SELECT * FROM lots ORDER BY id')
        for lot in lots:
            lot['operations'] = sorted([o for o in ops if o['lot_id']==lot['id']], key=lambda o:o['seq'])
            for i, op in enumerate(lot['operations']):
                ready = lot['operations'][i-1]['actual_end'] if i else op['created_at']
                op['waiting_minutes'] = max(0, (dt(op['actual_start'])-dt(ready)).total_seconds()/60) if ready and op['actual_start'] else None
            completed = sum(o['status']=='complete' for o in lot['operations'])
            lot['progress'] = round(100*completed/len(lot['operations']))
            current = next((o for o in lot['operations'] if o['status']!='complete'), None)
            lot['status'] = ('finished' if not current else 'hold' if current['status']=='hold' else 'processing' if current['status']=='running' else 'waiting')
            lot['stage'] = current['machine_code'] if current else 'Warehouse'
            lot['eta'] = lot['operations'][-1]['forecast_end']
        for order in orders:
            order['lots'] = [l for l in lots if l['order_id']==order['id']]
            order['progress'] = round(sum(l['progress']*l['quantity'] for l in order['lots'])/order['quantity'])
            states = [l['status'] for l in order['lots']]
            order['status'] = 'finished' if all(s=='finished' for s in states) else 'hold' if 'hold' in states else 'processing' if 'processing' in states else 'waiting'
            order['completed_at'] = max((l['operations'][-1]['actual_end'] for l in order['lots'] if l['operations'] and l['operations'][-1]['actual_end']), default=None) if order['status']=='finished' else None
            etas = [l['eta'] for l in order['lots']]
            order['eta'] = max(etas) if all(etas) else None
            order['started'] = any(l['operations'] and l['operations'][0]['actual_start'] for l in order['lots'])
            factory_tz = timezone(timedelta(minutes=app.config['FACTORY_UTC_OFFSET_MINUTES']))
            order['late'] = bool(order['eta'] and dt(order['eta']).astimezone(factory_tz).date().isoformat() > order['due_date'])
        return orders, ops

    @app.get('/api/state')
    @auth()
    def state():
        orders, ops = assemble()
        if g.user['role'] == 'client':
            safe_orders = []
            for order in orders:
                if order['client_id'] != g.user['id']:
                    continue
                safe = {k:order[k] for k in ('id','code','part_code','part_name','quantity','due_date','progress','status','completed_at')}
                safe['approved']=bool(order['client_approved_at'])
                safe['under_process']=order['started']
                safe['approved_eta']=order['approved_eta'] if order['started'] else None
                safe['eta']=order['eta'] if order['started'] else None
                safe['stages'] = sorted(set(l['stage'] for l in order['lots'])) if order['started'] else ['Awaiting manufacturing']
                safe['maintenance_updates'] = []
                for lot in order['lots']:
                    updates = rows("SELECT details,created_at FROM events WHERE lot_id=? AND kind='Maintenance delay update' ORDER BY id DESC", (lot['id'],))
                    if updates:
                        update = updates[0]
                        details = json.loads(update['details'])
                        safe['maintenance_updates'].append({'lot_code':lot['code'], 'reason':details.get('reason','Machine maintenance'),
                            'machine_code':details.get('machine_code'), 'machine_name':details.get('machine_name'),
                            'repair_minutes':details.get('repair_minutes'), 'eta':details.get('eta'), 'created_at':update['created_at']})
                safe['lots']=[]
                for lot in order['lots']:
                    tracked=next((op for op in lot['operations'] if op['status']!='complete' and op['kind']=='production'),None)
                    if not tracked:
                        tracked=next((op for op in reversed(lot['operations']) if op['kind']=='production'),None)
                    safe['lots'].append({'code':lot['code'],'quantity':lot['quantity'],'status':lot['status'],'location':lot['location'],
                        'stage':tracked['machine_code'] if tracked else lot['stage'],'progress':lot['progress'],
                        'started_at':tracked['actual_start'] if tracked else None,'stopped_at':tracked['actual_end'] if tracked else None,
                        'estimated_minutes':tracked['minutes'] if tracked else None,'progress_complete':bool(tracked and tracked['status']=='complete')})
                safe_orders.append(safe)
            return jsonify(orders=safe_orders, server_time=stamp())
        archive_cutoff=utcnow()-timedelta(days=7)
        archived_orders=[o for o in orders if o['status']=='finished' and o['completed_at'] and dt(o['completed_at'])<=archive_cutoff]
        archived_ids={o['id'] for o in archived_orders}
        orders=[o for o in orders if o['id'] not in archived_ids]
        machines = rows('SELECT * FROM machines ORDER BY id')
        start = utcnow()-timedelta(hours=24)
        prior_attempts = [json.loads(e['details'])['previous_attempt'] for e in rows("SELECT details FROM events WHERE kind='QC reinspection authorized'")]
        for machine in machines:
            machine_ops = [o for o in ops if o['machine_id']==machine['id']]
            attempts = machine_ops + [o for o in prior_attempts if o['machine_id']==machine['id']]
            busy = sum(max(0, (min(dt(o['actual_end']) if o['actual_end'] else utcnow(),utcnow())-max(dt(o['actual_start']),start)).total_seconds()/60) for o in attempts if o['actual_start'])
            machine.update(busy_minutes=round(busy,1), utilization=round(min(100,busy/1440*100),1),
                           queue_count=sum(o['status']=='pending' for o in machine_ops),
                           workload_minutes=round(sum(o['minutes'] for o in machine_ops if o['status']=='pending')))
        if g.user['role'] == 'operator':
            assigned = [r['machine_id'] for r in rows('SELECT machine_id FROM assignments WHERE user_id=?', (g.user['id'],))]
            maintenance=g.user['worker_type']=='maintenance'
            my_hours=sum(max(0,(dt(w['ended_at'] or stamp())-dt(w['started_at'])).total_seconds()/3600) for w in rows('SELECT started_at,ended_at FROM shift_logs WHERE user_id=?',(g.user['id'],)))
            active_shift=maybe_one('''SELECT s.*,m.code machine_code,m.name machine_name FROM shift_logs s JOIN machines m ON m.id=s.machine_id
                                WHERE s.user_id=? AND s.ended_at IS NULL ORDER BY s.id DESC LIMIT 1''',(g.user['id'],))
            local_today=(utcnow()+timedelta(minutes=app.config['FACTORY_UTC_OFFSET_MINUTES'])).date().isoformat()
            scheduled_shift=maybe_one('SELECT work_date,shift_code FROM shift_schedule WHERE user_id=? AND work_date=?',(g.user['id'],local_today))
            return jsonify(machines=machines, assignments=assigned, operations=[o for o in ops if (maintenance or o['machine_id'] in assigned) and o['status']!='complete'],
                           faults=rows("SELECT f.*,m.code machine_code,m.name machine_name,u.name reporter FROM machine_faults f JOIN machines m ON m.id=f.machine_id LEFT JOIN users u ON u.id=f.reported_by WHERE f.status='open' ORDER BY f.reported_at"),
                           work_hours=[{'user_id':g.user['id'],'name':g.user['name'],'hours':round(my_hours,2)}],active_shift=active_shift,scheduled_shift=scheduled_shift,
                           warehouse_slots=[s for s in warehouse_state() if s['machine_id'] in assigned], server_time=stamp())
        routes = rows('SELECT * FROM routes ORDER BY id DESC')
        for route in routes:
            route['steps'] = rows('SELECT * FROM route_steps WHERE route_id=? ORDER BY seq', (route['id'],))
            route['warehouse_slot_ids'] = [r['slot_id'] for r in rows('SELECT slot_id FROM route_warehouse_slots WHERE route_id=? ORDER BY slot_id',(route['id'],))]
        for order in orders:
            order['warehouse_slot_ids'] = [r['slot_id'] for r in rows('SELECT slot_id FROM warehouse_reservations WHERE order_id=? ORDER BY slot_id',(order['id'],))]
        sessions=rows("SELECT u.id user_id,u.name,w.started_at,w.ended_at FROM users u LEFT JOIN shift_logs w ON w.user_id=u.id WHERE u.role='operator' AND u.worker_type!='maintenance' ORDER BY u.name,w.started_at")
        totals={};monthly_map={}
        for w in sessions:
            item=totals.setdefault(w['user_id'],{'user_id':w['user_id'],'name':w['name'],'hours':0.0})
            if w['started_at']:
                hrs=max(0,(dt(w['ended_at'] or stamp())-dt(w['started_at'])).total_seconds()/3600);item['hours']+=hrs
                if dt(w['started_at'])>=utcnow()-timedelta(days=365):
                    key=(w['user_id'],w['started_at'][:7]);row=monthly_map.setdefault(key,{'user_id':w['user_id'],'name':w['name'],'month':w['started_at'][:7],'hours':0.0});row['hours']+=hrs
        hours=[{**v,'hours':round(v['hours'],2)} for v in totals.values()]
        monthly=[{**v,'hours':round(v['hours'],2)} for v in sorted(monthly_map.values(),key=lambda x:(x['month'],x['name']))]
        local_today=(utcnow()+timedelta(minutes=app.config['FACTORY_UTC_OFFSET_MINUTES'])).date()
        roster_week=(local_today-timedelta(days=local_today.weekday())).isoformat()
        return jsonify(orders=orders, archived_orders=archived_orders, machines=machines, parts=rows('SELECT * FROM parts ORDER BY code'), routes=routes,
                       warehouse_slots=warehouse_state(),
                       users=rows('SELECT id,name,email,role,status,requested_machine_id,worker_type FROM users ORDER BY name'),
                       assignments=rows('SELECT * FROM assignments'),
                       worker_hours=hours,monthly_worker_hours=monthly,
                       shift_logs=rows('''SELECT s.id,s.user_id,u.name worker_name,s.machine_id,m.code machine_code,m.name machine_name,
                         s.shift_code,s.group_key,s.started_at,s.ended_at,s.extra_hours FROM shift_logs s JOIN users u ON u.id=s.user_id
                         JOIN machines m ON m.id=s.machine_id ORDER BY s.id DESC'''),
                       shift_schedule=rows('''SELECT s.user_id,u.name worker_name,s.work_date,s.shift_code FROM shift_schedule s
                         JOIN users u ON u.id=s.user_id WHERE s.work_date>=? AND s.work_date<? ORDER BY u.name,s.work_date''',
                         (roster_week,(local_today+timedelta(days=6-local_today.weekday())).isoformat())),shift_week=roster_week,
                       faults=rows("SELECT f.*,m.code machine_code,m.name machine_name,u.name reporter FROM machine_faults f JOIN machines m ON m.id=f.machine_id LEFT JOIN users u ON u.id=f.reported_by WHERE f.status='open' ORDER BY f.reported_at"),
                       events=rows('''SELECT e.*,u.name actor,l.code lot_code FROM events e LEFT JOIN users u ON u.id=e.user_id
                         LEFT JOIN lots l ON l.id=e.lot_id ORDER BY e.id DESC LIMIT 100'''), server_time=stamp(), scheduling_mode='Continuous availability (24/7)')

    @app.post('/api/parts')
    @auth('manager')
    def add_part():
        data = request.get_json() or {}
        cursor = db().execute('INSERT INTO parts(code,name) VALUES(?,?)', (text_value(data,'code',40),text_value(data,'name',120)))
        return jsonify(id=cursor.lastrowid), 201

    @app.post('/api/machines')
    @auth('manager')
    def add_machine():
        data = request.get_json() or {}
        require(data.get('kind') in ('production','qc','storage'), 'Select a valid station type.')
        prefix=text_value(data,'code',24).strip(); name=text_value(data,'name',100); count=number(data,'count',1,50,True)
        class_code=prefix.upper(); made=[]
        for n in range(1,count+1):
            code=f'{class_code} {n:02d}' if count>1 else class_code
            cur=db().execute('INSERT INTO machines(code,name,kind,class_code) VALUES(?,?,?,?)',(code,name if count==1 else f'{name} {n:02d}',data['kind'],class_code))
            if data['kind']=='storage':
                db().executemany('INSERT INTO warehouse_slots(machine_id,row_number,bay_number) VALUES(?,?,?)',[(cur.lastrowid,r,b) for r in range(1,4) for b in range(1,11)])
            made.append(code)
        return jsonify(codes=made), 201

    @app.put('/api/machine-groups/<path:class_code>')
    @auth('manager')
    def resize_machine_group(class_code):
        data=request.get_json() or {}
        target=number(data,'count',1,50,True)
        group=rows('SELECT * FROM machines WHERE class_code=? ORDER BY code,id',(class_code.upper(),))
        require(group,'Machine group not found.',404)
        active=[m for m in group if not m['retired']]
        require(target>=1,'A machine group must keep at least one active machine.')
        if target<len(active):
            retiring=sorted(active,key=lambda m:(int(m['code'].rsplit(' ',1)[1]) if m['code'].rsplit(' ',1)[-1].isdigit() else 1,m['id']),reverse=True)[:len(active)-target]
            keep=[m for m in active if m['id'] not in {r['id'] for r in retiring}]
            for machine in retiring:
                require(not rows("SELECT 1 FROM operations WHERE machine_id=? AND status='running' LIMIT 1",(machine['id'],)),
                        f"{machine['code']} has work in progress. Finish it before reducing this group.",409)
                if machine['kind']=='storage':
                    require(not rows('SELECT 1 FROM warehouse_reservations wr JOIN warehouse_slots ws ON ws.id=wr.slot_id WHERE ws.machine_id=? LIMIT 1',(machine['id'],)),
                            f"{machine['code']} has reserved stock. Move those reservations before reducing this group.",409)
                peers=[m for m in keep if m['kind']==machine['kind'] and m['available']]
                pending=rows("SELECT id FROM operations WHERE machine_id=? AND status='pending' ORDER BY id",(machine['id'],))
                require(not pending or peers,f"{machine['code']} has queued work and no available replacement.",409)
                for op in pending:
                    peer=min(peers,key=lambda m:len(rows("SELECT id FROM operations WHERE machine_id=? AND status IN ('pending','running')",(m['id'],))))
                    db().execute('UPDATE operations SET machine_id=? WHERE id=?',(peer['id'],op['id']))
                db().execute('DELETE FROM assignments WHERE machine_id=?',(machine['id'],))
                db().execute('UPDATE users SET requested_machine_id=NULL WHERE requested_machine_id=?',(machine['id'],))
                db().execute('UPDATE machines SET retired=1 WHERE id=?',(machine['id'],))
        elif target>len(active):
            restore=sorted((m for m in group if m['retired']),key=lambda m:m['id'],reverse=True)
            for machine in restore[:target-len(active)]:
                db().execute('UPDATE machines SET retired=0,available=1,unavailable_until=NULL WHERE id=?',(machine['id'],))
            missing=target-len(active)-min(target-len(active),len(restore))
            last=max(group,key=lambda m:m['id'])
            code_numbers=[int(m['code'].rsplit(' ',1)[1]) for m in group if m['code'].rsplit(' ',1)[-1].isdigit()]
            next_number=max(code_numbers or [0])+1
            for offset in range(missing):
                number_value=next_number+offset
                code=f"{class_code.upper()} {number_value:02d}"
                name=last['name'].rsplit(' ',1)[0] if last['name'].rsplit(' ',1)[-1].isdigit() else last['name']
                cur=db().execute('INSERT INTO machines(code,name,kind,class_code) VALUES(?,?,?,?)',(code,f'{name} {number_value:02d}',last['kind'],class_code.upper()))
                if last['kind']=='storage':
                    db().executemany('INSERT INTO warehouse_slots(machine_id,row_number,bay_number) VALUES(?,?,?)',[(cur.lastrowid,r,b) for r in range(1,4) for b in range(1,11)])
        event(None,'Machine group resized',dict(class_code=class_code.upper(),previous_count=len(active),new_count=target))
        return jsonify(class_code=class_code.upper(),count=target)

    @app.delete('/api/machines/<int:machine_id>')
    @auth('manager')
    def delete_machine(machine_id):
        machine=one('SELECT * FROM machines WHERE id=?',(machine_id,))
        blockers=[]
        if rows('SELECT 1 FROM route_steps WHERE machine_id=? LIMIT 1',(machine_id,)):
            blockers.append('a saved product route')
        if rows('SELECT 1 FROM operations WHERE machine_id=? LIMIT 1',(machine_id,)):
            blockers.append('production order history')
        if rows('SELECT 1 FROM assignments WHERE machine_id=? LIMIT 1',(machine_id,)):
            blockers.append('an approved staff assignment')
        if rows('SELECT 1 FROM users WHERE requested_machine_id=? LIMIT 1',(machine_id,)):
            blockers.append('a staff registration request')
        if rows('''SELECT 1 FROM warehouse_reservations wr
                   JOIN warehouse_slots ws ON ws.id=wr.slot_id
                   WHERE ws.machine_id=? LIMIT 1''',(machine_id,)):
            blockers.append('a warehouse reservation')
        require(not blockers,
                f"Cannot remove {machine['code']} because it is used by " + ', '.join(blockers) + '. Preserve it for traceability or mark it unavailable instead.',
                409)
        event(None,'Machine removed',dict(machine_id=machine_id,code=machine['code'],name=machine['name'],kind=machine['kind']))
        if machine['kind']=='storage':
            db().execute('DELETE FROM warehouse_slots WHERE machine_id=?',(machine_id,))
        db().execute('DELETE FROM machines WHERE id=?',(machine_id,))
        return jsonify(ok=True)

    @app.post('/api/warehouses/<int:warehouse_id>/rows')
    @auth('manager')
    def add_warehouse_row(warehouse_id):
        require(one('SELECT kind FROM machines WHERE id=?',(warehouse_id,))['kind']=='storage','Select a warehouse.')
        count=number(request.json,'bays',1,50,True)
        result=db().execute('SELECT COALESCE(MAX(row_number),0)+1 AS next_row FROM warehouse_slots WHERE machine_id=?',(warehouse_id,)).fetchone()
        row=result['next_row'] if is_postgres else result[0]
        db().executemany('INSERT INTO warehouse_slots(machine_id,row_number,bay_number) VALUES(?,?,?)',[(warehouse_id,row,b) for b in range(1,count+1)])
        event(None,'Warehouse row added',dict(warehouse_id=warehouse_id,row=row,bays=count))
        return jsonify(ok=True),201

    @app.post('/api/warehouse-slots/<int:slot_id>/dispatch')
    @auth('operator')
    def dispatch_slot(slot_id):
        require(g.user['worker_type']=='storage','Only warehouse managers can dispatch stock.',403)
        slot=one('SELECT * FROM warehouse_slots WHERE id=?',(slot_id,))
        require(rows('SELECT 1 FROM assignments WHERE user_id=? AND machine_id=?',(g.user['id'],slot['machine_id'])),'This warehouse is not assigned to you.',403)
        reservation=one('SELECT * FROM warehouse_reservations WHERE slot_id=?',(slot_id,))
        require(not rows("SELECT x.id FROM operations x JOIN lots l ON l.id=x.lot_id WHERE l.order_id=? AND x.status!='complete'",(reservation['order_id'],)), 'Store every lot in this order before dispatching it.',409)
        event(None,'Warehouse stock dispatched',dict(slot_id=slot_id,order_id=reservation['order_id'],reference=text_value(request.json,'reference',200)))
        db().execute('DELETE FROM warehouse_reservations WHERE slot_id=?',(slot_id,))
        return jsonify(ok=True)

    @app.delete('/api/warehouse-slots/<int:slot_id>')
    @auth('manager')
    def delete_warehouse_slot(slot_id):
        slot=one('''SELECT s.*,m.code warehouse_code FROM warehouse_slots s
                    JOIN machines m ON m.id=s.machine_id WHERE s.id=?''',(slot_id,))
        blockers=[]
        if rows('SELECT 1 FROM warehouse_reservations WHERE slot_id=? LIMIT 1',(slot_id,)):
            blockers.append('an active warehouse reservation')
        if rows('SELECT 1 FROM routes WHERE warehouse_slot_id=? LIMIT 1',(slot_id,)) or rows('SELECT 1 FROM route_warehouse_slots WHERE slot_id=? LIMIT 1',(slot_id,)):
            blockers.append('a saved product route')
        if rows('SELECT 1 FROM orders WHERE warehouse_slot_id=? LIMIT 1',(slot_id,)):
            blockers.append('production order history')
        require(not blockers,
                f"Cannot remove address {slot['row_number']}/{slot['bay_number']} because it is used by " + ', '.join(blockers) + '.',
                409)
        event(None,'Warehouse address removed',dict(slot_id=slot_id,warehouse=slot['warehouse_code'],address=f"{slot['row_number']}/{slot['bay_number']}"))
        db().execute('DELETE FROM warehouse_slots WHERE id=?',(slot_id,))
        return jsonify(ok=True)

    @app.post('/api/machines/<int:machine_id>/availability')
    @auth('manager')
    def availability(machine_id):
        one('SELECT id FROM machines WHERE id=?',(machine_id,))
        require(isinstance((request.json or {}).get('available'), bool), 'Availability must be true or false.')
        require(not request.json['available'] or not rows("SELECT 1 FROM machine_faults WHERE machine_id=? AND status='open' LIMIT 1",(machine_id,)),'Maintenance must repair and restart this machine before it can return to service.',409)
        require(not rows("SELECT id FROM operations WHERE machine_id=? AND status='running'",(machine_id,)), 'Finish the active operation before changing availability.',409)
        reason = text_value(request.json,'reason',500)
        db().execute('UPDATE machines SET available=? WHERE id=?',(int(request.json['available']),machine_id))
        event(None,'Machine availability changed',dict(machine_id=machine_id,available=request.json['available'],reason=reason))
        return jsonify(ok=True)

    @app.post('/api/users')
    @auth('manager')
    def add_user():
        data = request.get_json() or {}
        require(data.get('role') in ('operator','client'),'Managers are created only during initial setup.')
        password = secrets.token_urlsafe(15) if data['role']=='client' else text_value(data,'password',128)
        require(len(password)>=12,'Use a password of at least 12 characters.')
        email = text_value(data,'email').lower()
        require('@' in email and '.' in email.split('@')[-1], 'Enter a valid email address.')
        name = text_value(data,'name',120)
        cur = db().execute('INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)',(name,email,generate_password_hash(password),data['role']))
        if data['role']=='operator':
            machine_id = data.get('machine_id')
            worker_type=data.get('worker_type','production')
            require(worker_type in ('production','qc','storage','maintenance'),'Choose a valid job role.')
            db().execute('UPDATE users SET worker_type=? WHERE id=?',(worker_type,cur.lastrowid))
        else:
            send_account_email(email, name, password)
        return jsonify(id=cur.lastrowid),201

    @app.delete('/api/users/<int:user_id>')
    @auth('manager')
    def delete_client(user_id):
        client=one('SELECT * FROM users WHERE id=?',(user_id,))
        require(client['role']=='client','Only client accounts can be removed.',400)
        require(not rows('SELECT 1 FROM orders WHERE client_id=? LIMIT 1',(user_id,)),
                'This client has production orders and must be preserved for traceability.',409)
        event(None,'Client removed',dict(client_id=user_id,name=client['name'],email=client['email']))
        db().execute('DELETE FROM users WHERE id=?',(user_id,))
        return jsonify(ok=True)

    @app.put('/api/users/<int:user_id>/password')
    @auth('manager')
    def reset_user_password(user_id):
        account=one('SELECT id,name,email,role FROM users WHERE id=?',(user_id,))
        require(account['role'] in ('operator','client'),'Only staff and client passwords can be changed.',400)
        password=text_value(request.get_json() or {},'password',128)
        require(len(password)>=12,'Use a password of at least 12 characters.')
        db().execute('UPDATE users SET password=? WHERE id=?',(generate_password_hash(password),user_id))
        event(None,'Account password changed',dict(user_id=user_id,name=account['name'],email=account['email'],role=account['role']))
        return jsonify(ok=True)

    @app.post('/api/operators/<int:user_id>/approve')
    @auth('manager')
    def approve_operator(user_id):
        operator=one("SELECT id,worker_type FROM users WHERE id=? AND role='operator' AND status='pending'",(user_id,))
        db().execute("UPDATE users SET status='approved',requested_machine_id=NULL WHERE id=?",(user_id,))
        return jsonify(ok=True)

    @app.put('/api/operators/<int:user_id>/assignment')
    @auth('manager')
    def update_operator_assignment(user_id):
        data=request.get_json() or {}
        operator=one("SELECT id,name,worker_type FROM users WHERE id=? AND role='operator' AND status='approved'",(user_id,))
        worker_type=data.get('worker_type')
        require(worker_type in ('production','qc','storage','maintenance'),'Choose a valid job role.')
        require(not rows("SELECT 1 FROM operations WHERE operator_id=? AND status='running' LIMIT 1",(user_id,)),
                'This worker has an active operation. Complete or release it before changing the assignment.',409)
        previous_matches=rows('''SELECT m.id,m.code FROM assignments a JOIN machines m ON m.id=a.machine_id
                                 WHERE a.user_id=?''',(user_id,))
        previous=previous_matches[0] if previous_matches else None
        role_changed=worker_type!=operator['worker_type']
        if role_changed:
            db().execute('DELETE FROM assignments WHERE user_id=?',(user_id,))
        db().execute('UPDATE users SET worker_type=?,requested_machine_id=NULL WHERE id=?',(worker_type,user_id))
        event(None,'Staff role changed',dict(user_id=user_id,name=operator['name'],previous_role=operator['worker_type'],
              new_role=worker_type,previous_machine=previous['code'] if previous else None,
              new_machine=previous['code'] if previous and not role_changed else None))
        return jsonify(ok=True)

    @app.post('/api/workers/select-machine')
    @auth('operator')
    def select_worker_machine():
        machine=one('SELECT id,kind,retired FROM machines WHERE id=?',(request.json.get('machine_id'),))
        require(not machine['retired'],'This machine group member is retired.',409)
        require(machine['kind']==g.user['worker_type'],'Choose a machine matching your work role.')
        require(not rows("SELECT 1 FROM operations WHERE operator_id=? AND status='running' LIMIT 1",(g.user['id'],)),'Finish the current operation before changing machines.',409)
        db().execute('DELETE FROM assignments WHERE user_id=?',(g.user['id'],))
        db().execute('INSERT INTO assignments(user_id,machine_id) VALUES(?,?)',(g.user['id'],machine['id']))
        event(None,'Worker selected machine',dict(machine_id=machine['id']))
        return jsonify(ok=True)

    @app.get('/api/shift-schedule')
    @auth('manager')
    def get_shift_schedule():
        try:
            week_start=datetime.strptime(request.args.get('week_start',''),'%Y-%m-%d').date()
        except ValueError:
            raise Invalid('Choose a valid Monday for the roster week.')
        require(week_start.weekday()==0,'The roster week must start on Monday.')
        week_end=week_start+timedelta(days=6)
        schedule=rows('''SELECT s.user_id,u.name worker_name,s.work_date,s.shift_code FROM shift_schedule s
                         JOIN users u ON u.id=s.user_id WHERE s.work_date>=? AND s.work_date<? ORDER BY u.name,s.work_date''',
                      (week_start.isoformat(),week_end.isoformat()))
        return jsonify(week_start=week_start.isoformat(),schedule=schedule)

    @app.post('/api/shift-schedule')
    @auth('manager')
    def save_shift_schedule():
        data=request.get_json() or {}
        try:
            work_date=datetime.strptime(str(data.get('work_date','')),'%Y-%m-%d').date()
            user_id=int(data.get('user_id'))
        except (TypeError,ValueError):
            raise Invalid('Choose a valid worker and work date.')
        require(work_date.weekday()<6,'Weekly shifts can only be scheduled Monday through Saturday.')
        shift_code=data.get('shift_code') or None
        require(shift_code in (None,'off','first','second','night'),'Choose a valid shift or clear the day.')
        worker=one("SELECT id,name FROM users WHERE id=? AND role='operator' AND status='approved' AND worker_type!='maintenance'",(user_id,))
        require(worker is not None,'Choose an approved production, quality or warehouse worker.',404)
        fill_week=bool(data.get('fill_week')) and work_date.weekday()==0
        work_dates=[work_date+timedelta(days=i) for i in range(6)] if fill_week else [work_date]
        for scheduled_date in work_dates:
            if shift_code is None:
                db().execute('DELETE FROM shift_schedule WHERE user_id=? AND work_date=?',(user_id,scheduled_date.isoformat()))
            else:
                db().execute('''INSERT INTO shift_schedule(user_id,work_date,shift_code) VALUES(?,?,?)
                                ON CONFLICT(user_id,work_date) DO UPDATE SET shift_code=excluded.shift_code''',
                             (user_id,scheduled_date.isoformat(),shift_code))
        event(None,'Worker shift scheduled',dict(worker_id=user_id,worker_name=worker['name'],work_dates=[d.isoformat() for d in work_dates],shift_code=shift_code,filled_week=fill_week))
        return jsonify(ok=True)

    @app.post('/api/workers/start-shift')
    @auth('operator')
    def start_worker_shift():
        data=request.get_json() or {}
        shift_code=data.get('shift_code')
        require(shift_code in ('first','second','night'),'Choose a valid shift.')
        local_today=(utcnow()+timedelta(minutes=app.config['FACTORY_UTC_OFFSET_MINUTES'])).date().isoformat()
        scheduled=maybe_one('SELECT shift_code FROM shift_schedule WHERE user_id=? AND work_date=?',(g.user['id'],local_today))
        active=maybe_one("SELECT * FROM shift_logs WHERE user_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",(g.user['id'],))
        require(not scheduled or scheduled['shift_code']==shift_code or (active and active['shift_code']==shift_code),
                'Your manager scheduled a different shift for today. Check your roster.',409)
        machine=one('SELECT id,kind,retired FROM machines WHERE id=?',(data.get('machine_id'),))
        require(machine is not None,'Choose a valid machine.',404)
        require(not machine['retired'],'This machine is retired.',409)
        require(machine['kind']==g.user['worker_type'],'Choose a machine matching your work role.')
        require(not rows("SELECT 1 FROM operations WHERE operator_id=? AND status='running' LIMIT 1",(g.user['id'],)),
                'Finish the current operation before starting or switching shifts.',409)
        if active:
            require(active['shift_code']==shift_code,'End your current shift before choosing a different shift.',409)
            if active['machine_id']==machine['id']:
                return jsonify(ok=True,active_shift=active['id'])
            db().execute('UPDATE shift_logs SET ended_at=? WHERE id=?',(stamp(),active['id']))
            group_key=active['group_key']
        else:
            group_key=secrets.token_hex(16)
        db().execute('DELETE FROM assignments WHERE user_id=?',(g.user['id'],))
        db().execute('INSERT INTO assignments(user_id,machine_id) VALUES(?,?)',(g.user['id'],machine['id']))
        db().execute('INSERT INTO shift_logs(user_id,machine_id,shift_code,group_key,started_at) VALUES(?,?,?,?,?)',
                     (g.user['id'],machine['id'],shift_code,group_key,stamp()))
        event(None,'Worker shift started',dict(machine_id=machine['id'],shift_code=shift_code,group_key=group_key))
        return jsonify(ok=True)

    @app.post('/api/workers/end-shift')
    @auth('operator')
    def end_worker_shift():
        require(not rows("SELECT 1 FROM operations WHERE operator_id=? AND status='running' LIMIT 1",(g.user['id'],)),
                'Complete or report the active operation before ending your shift.',409)
        active=maybe_one("SELECT * FROM shift_logs WHERE user_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",(g.user['id'],))
        require(active is not None,'There is no active shift to end.',409)
        ended=stamp()
        db().execute('UPDATE shift_logs SET ended_at=? WHERE id=?',(ended,active['id']))
        segments=rows('SELECT id,started_at,ended_at FROM shift_logs WHERE group_key=? ORDER BY id',(active['group_key'],))
        total_minutes=sum(max(0,(dt(w['ended_at'] or ended)-dt(w['started_at'])).total_seconds()/60) for w in segments)
        local_zone=timezone(timedelta(minutes=app.config['FACTORY_UTC_OFFSET_MINUTES']))
        local_start=dt(segments[0]['started_at']).astimezone(local_zone)
        shift_end_hour={'first':14,'second':22,'night':6}[active['shift_code']]
        scheduled_end=local_start.replace(hour=shift_end_hour,minute=0,second=0,microsecond=0)
        if active['shift_code']=='night' and local_start.hour>=22:
            scheduled_end+=timedelta(days=1)
        overtime_minutes=max(0,(dt(ended)-scheduled_end.astimezone(timezone.utc)).total_seconds()/60)
        extra_hours=round(overtime_minutes/60,2)
        db().execute('UPDATE shift_logs SET extra_hours=0 WHERE group_key=?',(active['group_key'],))
        db().execute('UPDATE shift_logs SET extra_hours=? WHERE id=?',(extra_hours,active['id']))
        db().execute('DELETE FROM assignments WHERE user_id=?',(g.user['id'],))
        event(None,'Worker shift ended',dict(machine_id=active['machine_id'],shift_code=active['shift_code'],worked_minutes=round(total_minutes),extra_hours=extra_hours))
        return jsonify(ok=True,extra_hours=extra_hours)

    @app.post('/api/workers/report-fault')
    @auth('operator')
    def report_worker_machine_fault():
        require(g.user['worker_type']!='maintenance','Maintenance staff cannot report their own repair queue.',403)
        data=request.get_json() or {}
        machine_id=number(data,'machine_id',1,1000000,True)
        machine=one('SELECT * FROM machines WHERE id=?',(machine_id,))
        require(machine['kind']==g.user['worker_type'] and not machine['retired'],'This machine does not match your work role.',403)
        require(rows('SELECT 1 FROM assignments WHERE user_id=? AND machine_id=?',(g.user['id'],machine_id)),
                'Select this station before reporting a machine fault.',403)
        active=maybe_one("SELECT id FROM shift_logs WHERE user_id=? AND machine_id=? AND ended_at IS NULL",(g.user['id'],machine_id))
        require(active is not None,'Start your shift on this machine before reporting a fault.',409)
        require(machine['available'],'This machine is already reported unavailable.',409)
        reason=text_value(data,'reason',500)
        running=maybe_one("SELECT * FROM operations WHERE machine_id=? AND status='running' ORDER BY id DESC LIMIT 1",(machine_id,))
        if running:
            db().execute("UPDATE operations SET status='hold',actual_end=? WHERE id=?",(stamp(),running['id']))
            db().execute('UPDATE work_sessions SET ended_at=? WHERE operation_id=? AND ended_at IS NULL',(stamp(),running['id']))
        until=stamp(utcnow()+timedelta(minutes=10))
        db().execute('UPDATE machines SET available=0,unavailable_until=? WHERE id=?',(until,machine_id))
        fault=db().execute('INSERT INTO machine_faults(machine_id,operation_id,reported_by,reported_at,reason) VALUES(?,?,?,?,?)',
                           (machine_id,running['id'] if running else None,g.user['id'],stamp(),reason))
        fault_id=fault.lastrowid
        lot_id=running['lot_id'] if running else None
        event(lot_id,'Machine failure reported',dict(machine_id=machine_id,fault_id=fault_id,operation_id=running['id'] if running else None,
              reason=reason,minimum_downtime_minutes=10))
        if running:
            event(lot_id,'Operation paused for maintenance',dict(machine_id=machine_id,operation_id=running['id']))
        notify_maintenance_delay(machine_id,fault_id,reason,[lot_id] if lot_id else [])
        return jsonify(ok=True,fault_id=fault_id)

    @app.post('/api/faults/<int:fault_id>/estimate')
    @auth('operator')
    def fault_repair_estimate(fault_id):
        require(g.user['worker_type']=='maintenance','Only maintenance staff can update repair estimates.',403)
        fault=one("SELECT * FROM machine_faults WHERE id=? AND status='open'",(fault_id,))
        minutes=number(request.json,'repair_minutes',1,10080)
        until=stamp(utcnow()+timedelta(minutes=max(10,minutes)))
        db().execute('UPDATE machine_faults SET repair_minutes=? WHERE id=?',(minutes,fault_id))
        db().execute('UPDATE machines SET unavailable_until=? WHERE id=?',(until,fault['machine_id']))
        event(None,'Repair estimate recorded',dict(fault_id=fault_id,repair_minutes=minutes,unavailable_until=until))
        affected=rows("SELECT x.id,x.lot_id,x.seq FROM operations x JOIN lots l ON l.id=x.lot_id JOIN orders o ON o.id=l.order_id WHERE x.machine_id=? AND x.status IN ('pending','hold') ORDER BY o.created_at,l.id,x.seq,x.id",(fault['machine_id'],))
        prior_updates=rows("SELECT lot_id,details FROM events WHERE kind='Maintenance delay update' AND lot_id IS NOT NULL")
        impacted_lots={item['lot_id'] for item in affected}
        impacted_lots.update(item['lot_id'] for item in prior_updates if json.loads(item['details']).get('fault_id')==fault_id)
        if minutes >= 120:
            machine=one('SELECT * FROM machines WHERE id=?',(fault['machine_id'],))
            peers=rows('SELECT id FROM machines WHERE class_code=? AND kind=? AND available=1 AND retired=0 AND id<>? ORDER BY id',(machine['class_code'],machine['kind'],machine['id']))
            for item in affected:
                if not peers: break
                def load(peer):
                    return sum(op['minutes'] for op in rows("SELECT minutes FROM operations WHERE machine_id=? AND status IN ('pending','running')",(peer['id'],)))
                target=min(peers,key=lambda peer:(load(peer),peer['id']))
                db().execute('UPDATE operations SET machine_id=? WHERE id=?',(target['id'],item['id']))
        all_ops=all_operations()
        for op in all_ops:
            if op['id']==fault['operation_id'] and op['status']=='hold':
                op['status']='pending'
                op['actual_start']=None
                op['actual_end']=None
        schedule=forecast(all_ops,{fault['machine_id']:dt(until)})
        for lot_id in sorted(impacted_lots):
            lot_op_ids=[op['id'] for op in all_ops if op['lot_id']==lot_id]
            last_eta=max((schedule.get(op_id,(None,None))[1] for op_id in lot_op_ids if schedule.get(op_id,(None,None))[1]),default=None)
            event(lot_id,'Maintenance delay update',dict(fault_id=fault_id,machine_code=one('SELECT code FROM machines WHERE id=?',(fault['machine_id'],))['code'],
                machine_name=one('SELECT name FROM machines WHERE id=?',(fault['machine_id'],))['name'],reason=fault['reason'] or 'Unexpected machine maintenance',repair_minutes=minutes,
                unavailable_until=until,eta=last_eta,reassigned=minutes>=120 and bool(peers) if minutes>=120 else False))
        return jsonify(ok=True,unavailable_until=until)

    @app.post('/api/faults/<int:fault_id>/restart')
    @auth('operator')
    def restart_repaired_machine(fault_id):
        require(g.user['worker_type']=='maintenance','Only maintenance staff can restart a machine.',403)
        fault=one("SELECT * FROM machine_faults WHERE id=? AND status='open'",(fault_id,))
        machine=one('SELECT * FROM machines WHERE id=?',(fault['machine_id'],))
        require(fault['repair_minutes'] is not None,'Enter a repair time before restarting this machine.',409)
        if machine['unavailable_until'] and dt(machine['unavailable_until'])>utcnow():
            seconds=max(1,math.ceil((dt(machine['unavailable_until'])-utcnow()).total_seconds()))
            hours,remainder=divmod(seconds,3600)
            minutes,seconds=divmod(remainder,60)
            remaining=' '.join(part for part in (f'{hours}h' if hours else '',f'{minutes}m' if minutes else '',f'{seconds}s' if seconds or not (hours or minutes) else '') if part)
            raise Invalid(f'Machine can be restarted in {remaining}.',409)
        db().execute('UPDATE machines SET available=1,unavailable_until=NULL WHERE id=?',(machine['id'],))
        db().execute("UPDATE machine_faults SET status='resolved',resolved_at=? WHERE id=?",(stamp(),fault_id))
        if fault['operation_id']:
            db().execute("UPDATE operations SET status='pending',actual_start=NULL,actual_end=NULL,operator_id=NULL WHERE id=? AND status='hold'",(fault['operation_id'],))
        event(None,'Machine returned to service',dict(machine_id=machine['id'],fault_id=fault_id))
        return jsonify(ok=True)

    @app.post('/api/routes')
    @auth('manager')
    def add_route():
        data=request.get_json() or {}
        part=one('SELECT * FROM parts WHERE id=?',(data.get('part_id'),))
        steps=data.get('steps')
        require(isinstance(steps,list) and 3<=len(steps)<=30,'A route needs production, QC and storage steps (3–30 total).')
        checked=[]
        for step in steps:
            machine=one('SELECT * FROM machines WHERE id=?',(step.get('machine_id'),))
            require(not machine['retired'],'Retired machines cannot be added to a new route.',409)
            minutes=number(step,'minutes',1,10080)
            checked.append((machine,minutes,number(step,'transition_minutes',0,1440),str(step.get('instructions','')).strip()[:2000],number(step,'min_minutes',1,minutes)))
        require(checked[-1][0]['kind']=='storage' and checked[-2][0]['kind']=='qc' and all(s[0]['kind']=='production' for s in checked[:-2]), 'Route must end with QC then storage, with production steps before them.')
        requested_slots=data.get('warehouse_slot_ids')
        if not isinstance(requested_slots,list):
            requested_slots=[data.get('warehouse_slot_id')]
        slots=available_slots(requested_slots,checked[-1][0]['id'])
        version_result=db().execute('SELECT COALESCE(MAX(version),0)+1 AS next_version FROM routes WHERE part_id=?',(part['id'],)).fetchone()
        version=version_result['next_version'] if is_postgres else version_result[0]
        cur=db().execute('INSERT INTO routes(part_id,version,created_at,warehouse_slot_id) VALUES(?,?,?,?)',(part['id'],version,stamp(),slots[0]['id']))
        db().executemany('INSERT INTO route_warehouse_slots(route_id,slot_id) VALUES(?,?)',[(cur.lastrowid,slot['id']) for slot in slots])
        for seq,(machine,minutes,transition,instructions,min_minutes) in enumerate(checked,1):
            db().execute('INSERT INTO route_steps(route_id,seq,machine_id,minutes,transition_minutes,instructions,min_minutes) VALUES(?,?,?,?,?,?,?)',(cur.lastrowid,seq,machine['id'],minutes,transition if seq>1 else 0,instructions,min_minutes))
        event(None,'Route version created',dict(part=part['code'],version=version,warehouse_slot_ids=[slot['id'] for slot in slots]))
        return jsonify(id=cur.lastrowid,version=version),201

    @app.post('/api/orders')
    @auth('manager')
    def add_order():
        data=request.get_json() or {}
        client=one('SELECT * FROM users WHERE id=?',(data.get('client_id'),))
        require(client['role']=='client','Select a client account.')
        route=one('SELECT * FROM routes WHERE id=?',(data.get('route_id'),))
        quantity=number(data,'quantity',1,1000000,True)
        lot_size=number(data,'lot_size',1,quantity,True)
        require(math.ceil(quantity/lot_size)<=100,'An order can have up to 100 lots.')
        priority=number(data,'priority',1,3,True)
        due=text_value(data,'due_date',10)
        try:
            datetime.strptime(due,'%Y-%m-%d')
        except ValueError:
            raise Invalid('Enter a valid required date.')
        code='ORD-'+secrets.token_hex(3).upper()
        cur=db().execute('INSERT INTO orders(code,client_id,part_id,route_id,quantity,priority,due_date,created_at) VALUES(?,?,?,?,?,?,?,?)',(code,client['id'],route['part_id'],route['id'],quantity,priority,due,stamp()))
        order_id=cur.lastrowid
        steps=rows('SELECT * FROM route_steps WHERE route_id=? ORDER BY seq',(route['id'],))
        require(len(steps)>=3,'This route has no valid steps.')
        requested_slots=data.get('warehouse_slot_ids')
        if not isinstance(requested_slots,list):
            requested_slots=[data.get('warehouse_slot_id')] if data.get('warehouse_slot_id') else [r['slot_id'] for r in rows('SELECT slot_id FROM route_warehouse_slots WHERE route_id=? ORDER BY slot_id',(route['id'],))]
        if not requested_slots:
            requested_slots=[route['warehouse_slot_id']]
        slots=available_slots(requested_slots,steps[-1]['machine_id'])
        db().execute('UPDATE orders SET warehouse_slot_id=? WHERE id=?',(slots[0]['id'],order_id))
        db().executemany('INSERT INTO warehouse_reservations(slot_id,order_id,created_at) VALUES(?,?,?)',[(slot['id'],order_id,stamp()) for slot in slots])
        for i,offset in enumerate(range(0,quantity,lot_size),1):
            lot=db().execute('INSERT INTO lots(order_id,code,token,quantity) VALUES(?,?,?,?)',(order_id,code+f'-L{i:02}',secrets.token_urlsafe(24),min(lot_size,quantity-offset)))
            for step in steps:
                chosen=step['machine_id']
                if step['seq'] < len(steps):
                    base=one('SELECT class_code,kind FROM machines WHERE id=?',(step['machine_id'],))
                    peers=rows('SELECT id FROM machines WHERE class_code=? AND kind=? AND available=1 AND retired=0 ORDER BY id',(base['class_code'],base['kind']))
                    require(peers,'No active machines are available for this production route.',409)
                    if peers:
                        chosen=min(peers,key=lambda m:sum(x['minutes'] for x in rows("SELECT minutes FROM operations WHERE machine_id=? AND status IN ('pending','running')",(m['id'],))))['id']
                db().execute('INSERT INTO operations(lot_id,seq,machine_id,minutes,transition_minutes,instructions,min_minutes) VALUES(?,?,?,?,?,?,?)',(lot.lastrowid,step['seq'],chosen,step['minutes'],step['transition_minutes'],step['instructions'],step['min_minutes']))
            event(lot.lastrowid,'Order created · route confirmed · QR generated',dict(order=code,route_version=route['version'],quantity=min(lot_size,quantity-offset)))
        schedule=forecast(all_operations())
        for op in rows('SELECT x.id FROM operations x JOIN lots l ON l.id=x.lot_id WHERE l.order_id=?',(order_id,)):
            start,end=schedule.get(op['id'],(None,None))
            db().execute('UPDATE operations SET planned_start=?,planned_end=? WHERE id=?',(start,end,op['id']))
        return jsonify(id=order_id,code=code),201

    @app.post('/api/orders/<int:order_id>/approve-eta')
    @auth('manager')
    def approve_eta(order_id):
        orders,_=assemble()
        order=next((o for o in orders if o['id']==order_id),None)
        require(order is not None,'Order not found.',404)
        require(not order['client_approved_at'],'This client request has already been approved.',409)
        approved_at=stamp()
        db().execute('UPDATE orders SET client_approved_at=? WHERE id=?',(approved_at,order_id))
        schedule=forecast(all_operations())
        for op in rows("SELECT x.id FROM operations x JOIN lots l ON l.id=x.lot_id WHERE x.status='pending'"):
            start,end=schedule.get(op['id'],(None,None))
            db().execute('UPDATE operations SET planned_start=?,planned_end=? WHERE id=?',(start,end,op['id']))
        event(order['lots'][0]['id'],'Client order approved',dict(approved_at=approved_at))
        return jsonify(ok=True,approved_at=approved_at)

    @app.get('/api/lots/<int:lot_id>/qr')
    @auth('manager','operator')
    def qr(lot_id):
        lot=one('SELECT * FROM lots WHERE id=?',(lot_id,))
        # Opaque lot identity, never authentication. Works across hostname changes.
        img=qrcode.make('AMMS:'+lot['token'],image_factory=qrcode.image.svg.SvgPathImage,box_size=8,border=4)
        stream=io.BytesIO()
        img.save(stream)
        stream.seek(0)
        response=send_file(stream,mimetype='image/svg+xml')
        response.headers['Cache-Control']='no-store'
        return response

    @app.get('/api/scan')
    @auth('operator','manager')
    def scan():
        value=request.args.get('code','').strip()
        if value.startswith('AMMS:'):
            value=value[5:]
        found=rows('SELECT id FROM lots WHERE token=? OR code=?',(value,value.upper()))
        require(found,'Lot not found. Scan an AMMS label or enter its lot code.',404)
        orders,_=assemble()
        lot=next(l for o in orders for l in o['lots'] if l['id']==found[0]['id'])
        if g.user['role']=='operator':
            allowed={r['machine_id'] for r in rows('SELECT machine_id FROM assignments WHERE user_id=?',(g.user['id'],))}
            require(any(o['machine_id'] in allowed for o in lot['operations']),'This lot does not visit any of your assigned stations.',403)
        return jsonify(lot=lot,events=rows('SELECT e.*,u.name actor FROM events e LEFT JOIN users u ON u.id=e.user_id WHERE lot_id=? ORDER BY e.id DESC',(lot['id'],)))

    @app.post('/api/operations/<int:operation_id>/action')
    @auth('operator')
    def operation_action(operation_id):
        op=one('SELECT x.*,m.kind,m.available,m.class_code FROM operations x JOIN machines m ON m.id=x.machine_id WHERE x.id=?',(operation_id,))
        require(op['kind']==g.user['worker_type'],'This operation requires a different job role.',403)
        require(g.user['worker_type']!='maintenance','Maintenance staff use the machine repair queue.',403)
        require(rows('SELECT 1 FROM assignments WHERE user_id=? AND machine_id=?',(g.user['id'],op['machine_id'])),'Select this station in your workspace before working.',403)
        data=request.get_json() or {}
        action=data.get('action')
        if action=='start':
            require(maybe_one("SELECT id FROM shift_logs WHERE user_id=? AND machine_id=? AND ended_at IS NULL",(g.user['id'],op['machine_id'])) is not None,
                    'Start your shift on this machine before starting production.',409)
            require(op['status']=='pending','This operation has already started or is on hold. Refresh the lot.',409)
            require(op['available'],'This station is unavailable.',409)
            require(not rows("SELECT 1 FROM operations WHERE lot_id=? AND seq<? AND status!='complete'",(op['lot_id'],op['seq'])),'Complete the preceding route steps first.',409)
            require(not rows("SELECT 1 FROM operations WHERE machine_id=? AND status='running'",(op['machine_id'],)),'This station is processing another lot.',409)
            operations=all_operations()
            schedule=forecast(operations)
            queue=sorted([o for o in operations if o['machine_id']==op['machine_id'] and o['status']=='pending' and o['id'] in schedule],key=lambda o:schedule[o['id']][0])
            require(queue and queue[0]['id']==operation_id,'Another lot is scheduled first. Ask the manager to prioritize this lot if needed.',409)
            db().execute("UPDATE operations SET status='running',actual_start=?,operator_id=? WHERE id=?",(stamp(),g.user['id'],operation_id))
            db().execute('INSERT INTO work_sessions(user_id,machine_id,operation_id,started_at) VALUES(?,?,?,?)',(g.user['id'],op['machine_id'],operation_id,stamp()))
            event(op['lot_id'],'Operation started',dict(operation_id=operation_id,machine_id=op['machine_id']))
        elif action=='failed':
            require(op['status']=='running' and op['operator_id']==g.user['id'],'Only your running operation can report a machine failure.',409)
            reason=text_value(data,'reason',500)
            schedule_before_failure=forecast(all_operations())
            waiting_before_failure=rows("SELECT id,lot_id FROM operations WHERE machine_id=? AND status='pending'",(op['machine_id'],))
            waiting_before_failure.sort(key=lambda item:(schedule_before_failure.get(item['id'],('~',None))[0] or '~',item['id']))
            db().execute("UPDATE operations SET status='hold',actual_end=? WHERE id=?",(stamp(),operation_id))
            db().execute('UPDATE machines SET available=0,unavailable_until=? WHERE id=?',(stamp(utcnow()+timedelta(minutes=10)),op['machine_id']))
            db().execute('UPDATE work_sessions SET ended_at=? WHERE operation_id=? AND ended_at IS NULL',(stamp(),operation_id))
            fault=db().execute('INSERT INTO machine_faults(machine_id,operation_id,reported_by,reported_at,reason) VALUES(?,?,?,?,?)',(op['machine_id'],operation_id,g.user['id'],stamp(),reason))
            event(op['lot_id'],'Machine failure reported',dict(operation_id=operation_id,machine_id=op['machine_id'],fault_id=fault.lastrowid,reason=reason,minimum_downtime_minutes=10))
            notify_maintenance_delay(op['machine_id'],fault.lastrowid,reason,[op['lot_id'],*(item['lot_id'] for item in waiting_before_failure)])
            # Move waiting work to the shortest available sister machine, preserving FIFO order through the schedule forecast.
            sisters=rows('SELECT id FROM machines WHERE class_code=? AND available=1 AND retired=0 AND id<>? ORDER BY id',(op['class_code'],op['machine_id']))
            for waiting in waiting_before_failure:
                if not sisters: break
                def remaining_load(machine):
                    active=rows("SELECT minutes,actual_start FROM operations WHERE machine_id=? AND status IN ('pending','running')",(machine['id'],))
                    return sum(max(0,x['minutes']-(utcnow()-dt(x['actual_start'])).total_seconds()/60) if x['actual_start'] else x['minutes'] for x in active)
                chosen=min(sisters,key=remaining_load)
                db().execute('UPDATE operations SET machine_id=? WHERE id=?',(chosen['id'],waiting['id']))
            event(op['lot_id'],'Operation paused for maintenance',dict(machine_id=op['machine_id']))
        elif action=='complete':
            require(op['status']=='running','Only a running operation can be completed.',409)
            require(op['operator_id']==g.user['id'],'The operator who started this operation must complete it.',403)
            elapsed=(utcnow()-dt(op['actual_start'])).total_seconds()/60
            if op['kind']!='storage':
                require(elapsed>=op['min_minutes'],f"Minimum processing time is {op['min_minutes']:g} minutes; {max(0,op['min_minutes']-elapsed):.1f} minutes remain.",409)
            status='complete'
            notes=str(data.get('notes','')).strip()[:2000]
            if op['kind']=='qc':
                require(data.get('result') in ('pass','fail'),'Record a QC pass or fail.')
                require(data['result']=='pass' or notes,'Explain the failed quality check.')
                if data['result']=='fail':
                    status='hold'
                db().execute('INSERT INTO quality(operation_id,result,notes,user_id,created_at) VALUES(?,?,?,?,?)',(operation_id,data['result'],notes,g.user['id'],stamp()))
            if op['kind']=='storage':
                planned=one('SELECT o.id FROM orders o JOIN lots l ON l.order_id=o.id WHERE l.id=?',(op['lot_id'],))
                reserved=rows('''SELECT s.id,s.row_number,s.bay_number FROM warehouse_reservations r
                                 JOIN warehouse_slots s ON s.id=r.slot_id
                                 WHERE r.order_id=? AND s.machine_id=? ORDER BY s.row_number,s.bay_number''',(planned['id'],op['machine_id']))
                if reserved:
                    selected=data.get('warehouse_slot_id')
                    if selected in (None,'') and len(reserved)==1:
                        selected=reserved[0]['id']
                    try:
                        selected=int(selected)
                    except (TypeError,ValueError):
                        raise Invalid('Choose one of this order’s reserved warehouse addresses.')
                    slot=next((s for s in reserved if s['id']==selected),None)
                    require(slot is not None,'Choose one of this order’s reserved warehouse addresses.')
                    location=f"{slot['row_number']}/{slot['bay_number']}"
                else:
                    location=text_value(data,'location',120)
                db().execute('UPDATE lots SET location=? WHERE id=?',(location,op['lot_id']))
                data['location']=location
            db().execute('UPDATE operations SET status=?,actual_end=? WHERE id=?',(status,stamp(),operation_id))
            db().execute('UPDATE work_sessions SET ended_at=? WHERE operation_id=? AND ended_at IS NULL',(stamp(),operation_id))
            event(op['lot_id'],'Quality hold' if status=='hold' else 'Operation completed',dict(operation_id=operation_id,machine_id=op['machine_id'],notes=notes,result=data.get('result'),location=data.get('location')))
        else:
            raise Invalid('Unknown operation action.')
        return jsonify(ok=True)

    @app.post('/api/operations/<int:operation_id>/prioritize')
    @auth('manager')
    def prioritize(operation_id):
        op=one('SELECT * FROM operations WHERE id=?',(operation_id,))
        require(op['status']=='pending','Only waiting operations can be prioritized.',409)
        reason=text_value(request.json or {},'reason',500)
        all_ops=all_operations()
        before=forecast(all_ops)
        queue=sorted([o for o in all_ops if o['machine_id']==op['machine_id'] and o['status']=='pending' and o['id'] in before],key=lambda o:before[o['id']][0])
        require(any(o['id']==operation_id for o in queue),'Resolve the blocked route before prioritizing.',409)
        old=next(i+1 for i,o in enumerate(queue) if o['id']==operation_id)
        # Promote waiting ancestors as well so the requested lot can actually reach the station first.
        rank=-time.time()
        db().execute("UPDATE operations SET rank=? WHERE lot_id=? AND seq<=? AND status='pending'",(rank,op['lot_id'],op['seq']))
        after=forecast(all_operations())
        new_queue=sorted(queue,key=lambda o:after[o['id']][0])
        new=next(i+1 for i,o in enumerate(new_queue) if o['id']==operation_id)
        event(op['lot_id'],'Queue priority override',dict(operation_id=operation_id,machine_id=op['machine_id'],previous_position=old,new_position=new,reason=reason,scope='Selected step and waiting predecessors; active work preserved'))
        return jsonify(ok=True)

    @app.post('/api/operations/<int:operation_id>/release')
    @auth('manager')
    def release(operation_id):
        op=one('SELECT * FROM operations WHERE id=?',(operation_id,))
        require(op['status']=='hold','This operation is not on hold.',409)
        reason=text_value(request.json or {},'reason',500)
        event(op['lot_id'],'QC reinspection authorized',dict(reason=reason,previous_attempt=op))
        db().execute("UPDATE operations SET status='pending',actual_start=NULL,actual_end=NULL,operator_id=NULL WHERE id=?",(operation_id,))
        return jsonify(ok=True)

    return app

def seed_demo(app):
    """Explicit demo setup only; never seeded when serving a real database by default."""
    postgres = app.config['DATABASE'].startswith(('postgres://', 'postgresql://'))
    with closing(connect_database(app.config['DATABASE'])) as db, db:
        count=db.execute('SELECT COUNT(*) AS total FROM users').fetchone()
        if count['total'] if postgres else count[0]:
            return
        for name,email,role in [('Alex Morgan','manager@demo.local','manager'),('Sam Rivera','operator@demo.local','operator'),('Jordan Lee','client@demo.local','client'),('Taylor Brooks','other-client@demo.local','client')]:
            db.execute('INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)',(name,email,generate_password_hash('FactoryDemo123!'),role))
        for code,name,kind in [('M1','Material cutting','production'),('M4','CNC machining','production'),('M7','Surface finishing','production'),('QC','Quality inspection','qc'),('WH','Warehouse','storage')]:
            db.execute('INSERT INTO machines(code,name,kind) VALUES(?,?,?)',(code,name,kind))
        for n in range(1,13):
            db.execute("INSERT INTO machines(code,name,kind) VALUES(?,?,'production') ON CONFLICT (code) DO NOTHING" if postgres else "INSERT OR IGNORE INTO machines(code,name,kind) VALUES(?,?,'production')",(f'M{n}',f'Production machine {n}'))
        db.executemany('INSERT INTO warehouse_slots(machine_id,row_number,bay_number) VALUES(5,?,?)',[(r,b) for r in range(1,4) for b in range(1,11)])
        db.execute("INSERT INTO app_migrations VALUES('expanded_demo_floor') ON CONFLICT (name) DO NOTHING" if postgres else "INSERT OR IGNORE INTO app_migrations VALUES('expanded_demo_floor')")
        db.execute('INSERT INTO assignments VALUES(2,1)')
        db.executemany('INSERT INTO parts(code,name) VALUES(?,?)',[('PC-001','Precision drive gear'),('PC-002','Bearing housing'),('PC-003','Flanged coupling')])
        for part in range(1,4):
            cur=db.execute('INSERT INTO routes(part_id,version,created_at,warehouse_slot_id) VALUES(?,1,?,?)',(part,stamp(),part))
            db.execute('INSERT INTO route_warehouse_slots(route_id,slot_id) VALUES(?,?)',(cur.lastrowid,part))
            for seq,(machine,minutes,instructions) in enumerate([(1,30,'Check material and drawing before cutting.'),(2,50,'Verify tool offset and secure the fixture.'),(3,25,'Inspect surface finish and remove burrs.'),(4,15,'Check dimensions against the approved drawing.'),(5,10,'Label the lot and record the rack or bin.')],1):
                db.execute('INSERT INTO route_steps(route_id,seq,machine_id,minutes,transition_minutes,instructions) VALUES(?,?,?,?,?,?)',(cur.lastrowid,seq,machine,minutes,5 if seq>1 else 0,instructions))
    client=app.test_client()
    csrf=client.get('/api/session').json['csrf']
    login=client.post('/api/login',json=dict(email='manager@demo.local',password='FactoryDemo123!'),headers={'X-CSRF-Token':csrf})
    csrf=login.json['csrf']
    for i in range(3):
        client.post('/api/orders',json=dict(client_id=3 if i<2 else 4,route_id=i+1,quantity=[500,200,300][i],lot_size=[500,200,300][i],priority=2,due_date=(utcnow()+timedelta(days=2+i)).date().isoformat()),headers={'X-CSRF-Token':csrf})

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--demo',action='store_true',help='Create sample data and demo logins if the database is empty.')
    parser.add_argument('--init-manager',action='store_true',help='Create your first manager interactively.')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=5000)
    args=parser.parse_args()
    app=create_app()
    if args.demo:
        seed_demo(app)
    if args.init_manager:
        import getpass
        postgres = app.config['DATABASE'].startswith(('postgres://', 'postgresql://'))
        with closing(connect_database(app.config['DATABASE'])) as db, db:
            count=db.execute('SELECT COUNT(*) AS total FROM users').fetchone()
            if count['total'] if postgres else count[0]:
                raise SystemExit('Users already exist. Create accounts from the manager workspace.')
            name=input('Your name: ').strip()
            email=input('Your email: ').strip().lower()
            password=getpass.getpass('Password (at least 12 characters): ')
            if not name or '@' not in email or len(password)<12:
                raise SystemExit('Name, email and a password of at least 12 characters are required.')
            db.execute('INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)',(name,email,generate_password_hash(password),'manager'))
    from waitress import serve
    print(f'Manufacturing workspace: http://{args.host}:{args.port}',flush=True)
    serve(app,host=args.host,port=args.port)
