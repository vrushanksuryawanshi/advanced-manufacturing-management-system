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
 kind TEXT NOT NULL CHECK(kind IN ('production','qc','storage')), available INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS assignments(user_id INTEGER REFERENCES users(id), machine_id INTEGER REFERENCES machines(id), PRIMARY KEY(user_id,machine_id));
CREATE TABLE IF NOT EXISTS parts(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routes(id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES parts(id), version INTEGER NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(part_id,version));
CREATE TABLE IF NOT EXISTS route_steps(id INTEGER PRIMARY KEY, route_id INTEGER NOT NULL REFERENCES routes(id), seq INTEGER NOT NULL,
 machine_id INTEGER NOT NULL REFERENCES machines(id), minutes REAL NOT NULL, transition_minutes REAL NOT NULL, instructions TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, client_id INTEGER NOT NULL REFERENCES users(id),
 part_id INTEGER NOT NULL REFERENCES parts(id), route_id INTEGER NOT NULL REFERENCES routes(id), quantity INTEGER NOT NULL,
 priority INTEGER NOT NULL, due_date TEXT NOT NULL, created_at TEXT NOT NULL, approved_eta TEXT);
CREATE TABLE IF NOT EXISTS lots(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), code TEXT UNIQUE NOT NULL,
 token TEXT UNIQUE NOT NULL, quantity INTEGER NOT NULL, location TEXT);
CREATE TABLE IF NOT EXISTS operations(id INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL REFERENCES lots(id), seq INTEGER NOT NULL,
 machine_id INTEGER NOT NULL REFERENCES machines(id), minutes REAL NOT NULL, transition_minutes REAL NOT NULL, instructions TEXT NOT NULL,
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
 order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_migrations(name TEXT PRIMARY KEY);
"""

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
    instance.mkdir(exist_ok=True)
    secret_path = instance / 'session-key'
    if not secret_path.exists():
        secret_path.write_text(secrets.token_hex(32), encoding='utf8')
    app.config.update(SECRET_KEY=os.environ.get('SECRET_KEY') or secret_path.read_text(),
                      DATABASE=str(database or instance / 'manufacturing.db'), TESTING=testing,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == '1',
                      FACTORY_UTC_OFFSET_MINUTES=int(os.environ.get('FACTORY_UTC_OFFSET_MINUTES', '330')),
                      SMTP_HOST=os.environ.get('SMTP_HOST'), SMTP_PORT=int(os.environ.get('SMTP_PORT', '587')),
                      SMTP_USER=os.environ.get('SMTP_USER'), SMTP_PASSWORD=os.environ.get('SMTP_PASSWORD'),
                      SMTP_FROM=os.environ.get('SMTP_FROM') or os.environ.get('SMTP_USER'),
                      MAX_CONTENT_LENGTH=1024 * 1024, PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    with closing(sqlite3.connect(app.config['DATABASE'])) as conn, conn:
        conn.executescript(SCHEMA)
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

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=15)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
        return g.db

    def rows(sql, args=()):
        return [dict(r) for r in db().execute(sql, args)]

    def one(sql, args=()):
        result = db().execute(sql, args).fetchone()
        require(result is not None, 'Record not found.', 404)
        return dict(result)

    def event(lot, kind, details):
        db().execute('INSERT INTO events(lot_id,user_id,kind,details,created_at) VALUES(?,?,?,?,?)',
                     (lot, g.user['id'], kind, json.dumps(details), stamp()))

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
        machine_id=data.get('machine_id')
        worker_type=data.get('worker_type','production')
        require(worker_type in ('production','qc','storage'),'Choose operator, quality checker, or warehouse manager.')
        machine=one('SELECT kind FROM machines WHERE id=?',(machine_id,))
        require(machine['kind']==worker_type,'Choose a station matching your job role.')
        db().execute("INSERT INTO users(name,email,password,role,status,requested_machine_id,worker_type) VALUES(?,?,?,'operator','pending',?,?)", (name,email,generate_password_hash(password),machine_id,worker_type))
        return jsonify(ok=True, message='Registration submitted. A manager must approve your account before you can sign in.'), 201

    @app.get('/api/registration-machines')
    def registration_machines():
        worker_type=request.args.get('worker_type','production')
        require(worker_type in ('production','qc','storage'),'Choose a valid job role.')
        return jsonify(machines=rows('SELECT id,code,name,kind FROM machines WHERE kind=? ORDER BY id',(worker_type,)))

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    def all_operations():
        return rows('''SELECT x.*, m.code machine_code,m.name machine_name,m.kind,m.available,
          l.code lot_code,l.quantity,l.order_id,l.location,o.code order_code,o.part_id,o.route_id,o.priority,o.created_at,o.client_id,
          o.warehouse_slot_id,ws.row_number || '/' || ws.bay_number warehouse_address,
          p.code part_code,p.name part_name FROM operations x JOIN machines m ON m.id=x.machine_id
          JOIN lots l ON l.id=x.lot_id JOIN orders o ON o.id=l.order_id JOIN parts p ON p.id=o.part_id
          LEFT JOIN warehouse_slots ws ON ws.id=o.warehouse_slot_id''')

    def warehouse_state():
        return rows('''SELECT s.*,s.row_number || '/' || s.bay_number address,m.code warehouse_code,
          r.order_id,o.code order_code,
          CASE WHEN r.order_id IS NULL THEN 'available'
            WHEN EXISTS(SELECT 1 FROM lots l WHERE l.order_id=r.order_id AND l.location IS NOT NULL) THEN 'occupied'
            ELSE 'reserved' END status
          FROM warehouse_slots s JOIN machines m ON m.id=s.machine_id
          LEFT JOIN warehouse_reservations r ON r.slot_id=s.id LEFT JOIN orders o ON o.id=r.order_id
          ORDER BY s.machine_id,s.row_number,s.bay_number''')

    def available_slot(slot_id, warehouse_id):
        slot=one('SELECT * FROM warehouse_slots WHERE id=?',(slot_id,))
        require(slot['machine_id']==warehouse_id,'Choose an address in the route warehouse.')
        require(not rows('SELECT 1 FROM warehouse_reservations WHERE slot_id=?',(slot_id,)), 'That warehouse address has already been reserved. Refresh and select another available address.',409)
        return slot

    def forecast(ops):
        """Serial schedule generation; continuous capacity, one operation per machine.
        Held/unavailable predecessors block their descendants. Active work is never preempted.
        """
        now = utcnow()
        available = {}
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
        pending = [o for o in ops if o['status'] == 'pending' and o['available']]
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
            etas = [l['eta'] for l in order['lots']]
            order['eta'] = max(etas) if all(etas) else None
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
                safe = {k:order[k] for k in ('id','code','part_code','part_name','quantity','due_date','progress','status','approved_eta')}
                safe['stages'] = sorted(set(l['stage'] for l in order['lots']))
                safe_orders.append(safe)
            return jsonify(orders=safe_orders, server_time=stamp())
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
            return jsonify(machines=machines, assignments=assigned, operations=[o for o in ops if o['machine_id'] in assigned and o['status']!='complete'],
                           warehouse_slots=[s for s in warehouse_state() if s['machine_id'] in assigned], server_time=stamp())
        routes = rows('SELECT * FROM routes ORDER BY id DESC')
        for route in routes:
            route['steps'] = rows('SELECT * FROM route_steps WHERE route_id=? ORDER BY seq', (route['id'],))
        return jsonify(orders=orders, machines=machines, parts=rows('SELECT * FROM parts ORDER BY code'), routes=routes,
                       warehouse_slots=warehouse_state(),
                       users=rows('SELECT id,name,email,role,status,requested_machine_id,worker_type FROM users ORDER BY name'),
                       assignments=rows('SELECT * FROM assignments'),
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
        cur = db().execute('INSERT INTO machines(code,name,kind) VALUES(?,?,?)', (text_value(data,'code',30),text_value(data,'name',120),data['kind']))
        if data['kind']=='storage':
            db().executemany('INSERT INTO warehouse_slots(machine_id,row_number,bay_number) VALUES(?,?,?)',[(cur.lastrowid,r,b) for r in range(1,4) for b in range(1,11)])
        return jsonify(id=cur.lastrowid), 201

    @app.post('/api/warehouses/<int:warehouse_id>/rows')
    @auth('manager')
    def add_warehouse_row(warehouse_id):
        require(one('SELECT kind FROM machines WHERE id=?',(warehouse_id,))['kind']=='storage','Select a warehouse.')
        count=number(request.json,'bays',1,50,True)
        row=db().execute('SELECT COALESCE(MAX(row_number),0)+1 FROM warehouse_slots WHERE machine_id=?',(warehouse_id,)).fetchone()[0]
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

    @app.post('/api/machines/<int:machine_id>/availability')
    @auth('manager')
    def availability(machine_id):
        one('SELECT id FROM machines WHERE id=?',(machine_id,))
        require(isinstance((request.json or {}).get('available'), bool), 'Availability must be true or false.')
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
            require(worker_type in ('production','qc','storage'),'Choose a valid job role.')
            require(one('SELECT kind FROM machines WHERE id=?',(machine_id,))['kind']==worker_type,'Choose a station matching the job role.')
            db().execute('UPDATE users SET worker_type=? WHERE id=?',(worker_type,cur.lastrowid))
            db().execute('INSERT INTO assignments VALUES(?,?)',(cur.lastrowid,machine_id))
        else:
            send_account_email(email, name, password)
        return jsonify(id=cur.lastrowid),201

    @app.post('/api/operators/<int:user_id>/approve')
    @auth('manager')
    def approve_operator(user_id):
        data=request.get_json() or {}
        operator=one("SELECT id,worker_type FROM users WHERE id=? AND role='operator' AND status='pending'",(user_id,))
        machine_id=data.get('machine_id')
        require(one('SELECT kind FROM machines WHERE id=?',(machine_id,))['kind']==operator['worker_type'],'Choose a station matching this staff member’s job role.')
        db().execute("INSERT INTO assignments(user_id,machine_id) VALUES(?,?)",(operator['id'],machine_id))
        db().execute("UPDATE users SET status='approved' WHERE id=?",(user_id,))
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
            checked.append((machine,number(step,'minutes',0.1,10080),number(step,'transition_minutes',0,1440),str(step.get('instructions','')).strip()[:2000]))
        require(checked[-1][0]['kind']=='storage' and checked[-2][0]['kind']=='qc' and all(s[0]['kind']=='production' for s in checked[:-2]), 'Route must end with QC then storage, with production steps before them.')
        slot=available_slot(data.get('warehouse_slot_id'),checked[-1][0]['id'])
        version=db().execute('SELECT COALESCE(MAX(version),0)+1 FROM routes WHERE part_id=?',(part['id'],)).fetchone()[0]
        cur=db().execute('INSERT INTO routes(part_id,version,created_at,warehouse_slot_id) VALUES(?,?,?,?)',(part['id'],version,stamp(),slot['id']))
        for seq,(machine,minutes,transition,instructions) in enumerate(checked,1):
            db().execute('INSERT INTO route_steps(route_id,seq,machine_id,minutes,transition_minutes,instructions) VALUES(?,?,?,?,?,?)',(cur.lastrowid,seq,machine['id'],minutes,transition if seq>1 else 0,instructions))
        event(None,'Route version created',dict(part=part['code'],version=version))
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
        slot=available_slot(data.get('warehouse_slot_id') or route['warehouse_slot_id'],steps[-1]['machine_id'])
        db().execute('UPDATE orders SET warehouse_slot_id=? WHERE id=?',(slot['id'],order_id))
        db().execute('INSERT INTO warehouse_reservations(slot_id,order_id,created_at) VALUES(?,?,?)',(slot['id'],order_id,stamp()))
        for i,offset in enumerate(range(0,quantity,lot_size),1):
            lot=db().execute('INSERT INTO lots(order_id,code,token,quantity) VALUES(?,?,?,?)',(order_id,code+f'-L{i:02}',secrets.token_urlsafe(24),min(lot_size,quantity-offset)))
            for step in steps:
                db().execute('INSERT INTO operations(lot_id,seq,machine_id,minutes,transition_minutes,instructions) VALUES(?,?,?,?,?,?)',(lot.lastrowid,step['seq'],step['machine_id'],step['minutes'],step['transition_minutes'],step['instructions']))
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
        require(order['eta'] is not None,'Resolve the hold or unavailable station before approving an ETA.',409)
        db().execute('UPDATE orders SET approved_eta=? WHERE id=?',(order['eta'],order_id))
        event(order['lots'][0]['id'],'Client ETA approved',dict(eta=order['eta']))
        return jsonify(ok=True)

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
        op=one('SELECT x.*,m.kind,m.available FROM operations x JOIN machines m ON m.id=x.machine_id WHERE x.id=?',(operation_id,))
        require(op['kind']==g.user['worker_type'],'This operation requires a different job role.',403)
        require(rows('SELECT 1 FROM assignments WHERE user_id=? AND machine_id=?',(g.user['id'],op['machine_id'])),'This station is not assigned to you.',403)
        data=request.get_json() or {}
        action=data.get('action')
        if action=='start':
            require(op['status']=='pending','This operation has already started or is on hold. Refresh the lot.',409)
            require(op['available'],'This station is unavailable.',409)
            require(not rows("SELECT 1 FROM operations WHERE lot_id=? AND seq<? AND status!='complete'",(op['lot_id'],op['seq'])),'Complete the preceding route steps first.',409)
            require(not rows("SELECT 1 FROM operations WHERE machine_id=? AND status='running'",(op['machine_id'],)),'This station is processing another lot.',409)
            operations=all_operations()
            schedule=forecast(operations)
            queue=sorted([o for o in operations if o['machine_id']==op['machine_id'] and o['status']=='pending' and o['id'] in schedule],key=lambda o:schedule[o['id']][0])
            require(queue and queue[0]['id']==operation_id,'Another lot is scheduled first. Ask the manager to prioritize this lot if needed.',409)
            db().execute("UPDATE operations SET status='running',actual_start=?,operator_id=? WHERE id=?",(stamp(),g.user['id'],operation_id))
            event(op['lot_id'],'Operation started',dict(operation_id=operation_id,machine_id=op['machine_id']))
        elif action=='complete':
            require(op['status']=='running','Only a running operation can be completed.',409)
            require(op['operator_id']==g.user['id'],'The operator who started this operation must complete it.',403)
            status='complete'
            notes=str(data.get('notes','')).strip()[:2000]
            if op['kind']=='qc':
                require(data.get('result') in ('pass','fail'),'Record a QC pass or fail.')
                require(data['result']=='pass' or notes,'Explain the failed quality check.')
                if data['result']=='fail':
                    status='hold'
                db().execute('INSERT INTO quality(operation_id,result,notes,user_id,created_at) VALUES(?,?,?,?,?)',(operation_id,data['result'],notes,g.user['id'],stamp()))
            if op['kind']=='storage':
                planned=one('SELECT o.id,o.warehouse_slot_id FROM orders o JOIN lots l ON l.order_id=o.id WHERE l.id=?',(op['lot_id'],))
                if planned['warehouse_slot_id']:
                    slot=one('SELECT row_number,bay_number FROM warehouse_slots WHERE id=?',(planned['warehouse_slot_id'],))
                    require(rows('SELECT 1 FROM warehouse_reservations WHERE slot_id=? AND order_id=?',(planned['warehouse_slot_id'],planned['id'])),'The warehouse reservation is no longer available.',409)
                    location=f"{slot['row_number']}/{slot['bay_number']}"
                else:
                    location=text_value(data,'location',120)
                db().execute('UPDATE lots SET location=? WHERE id=?',(location,op['lot_id']))
                data['location']=location
            db().execute('UPDATE operations SET status=?,actual_end=? WHERE id=?',(status,stamp(),operation_id))
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
    with closing(sqlite3.connect(app.config['DATABASE'])) as db, db:
        if db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
            return
        for name,email,role in [('Alex Morgan','manager@demo.local','manager'),('Sam Rivera','operator@demo.local','operator'),('Jordan Lee','client@demo.local','client'),('Taylor Brooks','other-client@demo.local','client')]:
            db.execute('INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)',(name,email,generate_password_hash('FactoryDemo123!'),role))
        for code,name,kind in [('M1','Material cutting','production'),('M4','CNC machining','production'),('M7','Surface finishing','production'),('QC','Quality inspection','qc'),('WH','Warehouse','storage')]:
            db.execute('INSERT INTO machines(code,name,kind) VALUES(?,?,?)',(code,name,kind))
        for n in range(1,13):
            db.execute("INSERT OR IGNORE INTO machines(code,name,kind) VALUES(?,?,'production')",(f'M{n}',f'Production machine {n}'))
        db.executemany('INSERT INTO warehouse_slots(machine_id,row_number,bay_number) VALUES(5,?,?)',[(r,b) for r in range(1,4) for b in range(1,11)])
        db.execute("INSERT OR IGNORE INTO app_migrations VALUES('expanded_demo_floor')")
        db.execute('INSERT INTO assignments VALUES(2,1)')
        db.executemany('INSERT INTO parts(code,name) VALUES(?,?)',[('PC-001','Precision drive gear'),('PC-002','Bearing housing'),('PC-003','Flanged coupling')])
        for part in range(1,4):
            cur=db.execute('INSERT INTO routes(part_id,version,created_at,warehouse_slot_id) VALUES(?,1,?,?)',(part,stamp(),part))
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
        with closing(sqlite3.connect(app.config['DATABASE'])) as db, db:
            if db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
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
