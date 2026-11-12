"""Administrative control. All returned information belongs to evaluation only."""
import os
import hashlib
import json
from pathlib import Path
import platform
from importlib.metadata import version
import time
import uuid
import httpx
import psycopg
from psycopg import sql
from fixture.core import CATEGORIES, now

class Control:
    def __init__(self):
        self.lock = None
        self.lock_pid = None
        self.changed_password = None

    def admin(self):
        return psycopg.connect(host='db', dbname='shop', user='postgres',
            password=os.environ['ADMIN_PASSWORD'], connect_timeout=2, autocommit=True,
            options='-c statement_timeout=2500 -c lock_timeout=1000', application_name='maintenance')

    def docker(self, action):
        # Only the database in this Compose project is addressable.
        with httpx.Client(transport=httpx.HTTPTransport(uds='/var/run/docker.sock'),
                          base_url='http://docker', timeout=30) as client:
            response = client.get('/containers/json', params={'all':'true', 'filters':
                '{"label":["com.docker.compose.project=logquant-fixture","com.docker.compose.service=db"]}'})
            response.raise_for_status()
            containers = response.json()
            if len(containers) != 1:
                raise RuntimeError('Expected exactly one fixture database container')
            cid = containers[0]['Id']
            if action == 'state':
                return containers[0]['State']
            if action not in ('start','stop'):
                raise ValueError('Unsupported database control')
            response = client.post(f'/containers/{cid}/{action}', params={'t': '3'})
            if response.status_code != 304:  # Already in requested state.
                response.raise_for_status()

    def wait_database(self):
        deadline = time.monotonic() + 30
        while True:
            try:
                with self.admin() as conn:
                    conn.execute('SELECT 1')
                return
            except psycopg.Error:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.25)

    def runtime(self):
        dependencies = {}
        for line in Path('requirements.lock').read_text().splitlines():
            if not line or line.startswith('#'): continue
            name, expected = line.split('==')
            actual = version(name)
            if actual != expected: raise RuntimeError('Installed dependency differs from lock: '+name)
            dependencies[name] = actual
        environment = json.loads(Path('environment.lock.json').read_text())
        if environment['dependency_lock_sha256'] != hashlib.sha256(Path('requirements.lock').read_bytes()).hexdigest():
            raise RuntimeError('Environment identity differs from installed dependency lock')
        with self.admin() as conn:
            database = conn.execute('SELECT version()').fetchone()[0]
        return {'python': platform.python_version(), 'postgresql': database,
                'dependency_lock_sha256': hashlib.sha256(Path('requirements.lock').read_bytes()).hexdigest(),
                'installed_dependencies': dependencies,
                'packages': {name: version(name) for name in ('fastapi','uvicorn','psycopg','httpx','starlette','anyio','pydantic')}}

    def reset(self):
        if self.lock:
            self.lock.close()
            self.lock = None
        self.docker('start')
        self.wait_database()
        with self.admin() as conn:
            # Recover lock holders left by a killed harness, before touching tables.
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='fixture_control' AND pid<>pg_backend_pid()")
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname='shop_app'").fetchone()
            if not exists:
                conn.execute('CREATE ROLE shop_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION')
            conn.execute(sql.SQL('ALTER ROLE shop_app PASSWORD {}').format(sql.Literal(os.environ['APP_PASSWORD'])))
            conn.execute('CREATE TABLE IF NOT EXISTS products(id integer PRIMARY KEY, name text NOT NULL, price numeric NOT NULL, pack_size integer NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS orders(id bigserial PRIMARY KEY, product_id integer REFERENCES products(id), quantity integer NOT NULL)')
            conn.execute('TRUNCATE orders, products RESTART IDENTITY')
            for i in range(1,11):
                conn.execute('INSERT INTO products VALUES (%s,%s,%s,%s)', (i,f'Item {i}',i*12,6))
            conn.execute('GRANT USAGE ON SCHEMA public TO shop_app')
            conn.execute('GRANT SELECT ON products TO shop_app')
            conn.execute('GRANT SELECT, INSERT ON orders TO shop_app')
            conn.execute('GRANT USAGE, SELECT ON SEQUENCE orders_id_seq TO shop_app')

    def inject(self, category):
        if category == CATEGORIES[0]:
            self.docker('stop')
            return
        if category == CATEGORIES[4]:
            self.lock = self.admin()
            self.lock.execute("SET application_name='fixture_control'")
            self.lock.execute('BEGIN')
            self.lock.execute('LOCK TABLE orders IN ACCESS EXCLUSIVE MODE')
            self.lock_pid = self.lock.execute('SELECT pg_backend_pid()').fetchone()[0]
            return
        with self.admin() as conn:
            if category == CATEGORIES[1]:
                self.changed_password = uuid.uuid4().hex
                conn.execute(sql.SQL('ALTER ROLE shop_app PASSWORD {}').format(sql.Literal(self.changed_password)))
            elif category == CATEGORIES[2]:
                conn.execute('UPDATE products SET pack_size=0 WHERE id=1')
            elif category == CATEGORIES[3]:
                conn.execute('REVOKE INSERT ON orders FROM shop_app')
            else:
                raise ValueError('Unknown fault')

    def recover(self, category):
        if category == CATEGORIES[0]:
            self.docker('start')
            self.wait_database()
        elif category == CATEGORIES[4]:
            if self.lock:
                self.lock.close()
                self.lock = None
        elif category:
            with self.admin() as conn:
                if category == CATEGORIES[1]:
                    conn.execute(sql.SQL('ALTER ROLE shop_app PASSWORD {}').format(sql.Literal(os.environ['APP_PASSWORD'])))
                elif category == CATEGORIES[2]:
                    conn.execute('UPDATE products SET pack_size=6 WHERE id=1')
                elif category == CATEGORIES[3]:
                    conn.execute('GRANT INSERT ON orders TO shop_app')

    def state(self, category):
        if category == CATEGORIES[0]:
            return {'stopped': self.docker('state') == 'exited'}
        if category == CATEGORIES[1]:
            # An independent connection with the unchanged application password must fail.
            try:
                with psycopg.connect(host='db', dbname='shop', user='shop_app',
                        password=os.environ['APP_PASSWORD'], connect_timeout=2):
                    pass
                return {'authentication_rejected': False}
            except psycopg.OperationalError as exc:
                return {'authentication_rejected': 'password authentication failed' in str(exc)}
        with self.admin() as conn:
            if category == CATEGORIES[3]:
                # HTTP suite proves fresh authentication/reads; this is an independent privilege observation.
                return {'insert_denied': not conn.execute(
                    "SELECT has_table_privilege('shop_app','orders','INSERT')").fetchone()[0]}
            if category == CATEGORIES[2]:
                return {'zero_pack_size': conn.execute('SELECT pack_size=0 FROM products WHERE id=1').fetchone()[0]}
            if category == CATEGORIES[4]:
                row = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name='shop' AND %s=ANY(pg_blocking_pids(pid)))", (self.lock_pid,)).fetchone()
                return {'blocked_by_holder': row[0]}
        return {}
