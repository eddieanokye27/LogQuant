"""Ordinary shop API. No knowledge of experiments or fault categories."""
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI()

def emit(**fields):
    path = Path('/evidence/application.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(),
                                 'service': 'application', **fields}) + '\n')

def connect():
    return psycopg.connect(host='db', dbname='shop', user='shop_app',
        password=os.environ['APP_PASSWORD'], connect_timeout=2,
        application_name='shop', options='-c statement_timeout=1500 -c lock_timeout=800')

@app.middleware('http')
async def logging(request, call_next):
    started = time.monotonic()
    rid = uuid.uuid4().hex
    extra = {}
    try:
        response = await call_next(request)
    except Exception as exc:
        extra = {'exception_type': type(exc).__name__, 'message': str(exc)}
        response = JSONResponse({'detail': 'Operation failed'}, status_code=500)
    emit(severity='ERROR' if response.status_code >= 500 else 'INFO', event='request',
         request_id=rid, method=request.method, route=request.url.path,
         status=response.status_code, duration_ms=round((time.monotonic()-started)*1000, 3), **extra)
    response.headers['X-Request-ID'] = rid
    return response

@app.get('/live')
def live():
    return {'ok': True}

@app.get('/ready')
def ready():
    with connect() as conn:
        conn.execute('SELECT 1').fetchone()
    return {'ok': True}

@app.get('/products')
def products():
    with connect() as conn:
        rows = conn.execute('SELECT id, name, price, pack_size FROM products ORDER BY id').fetchall()
    return {'products': rows}

@app.get('/products/{product_id}/quote')
def quote(product_id: int):
    with connect() as conn:
        row = conn.execute('SELECT price, pack_size FROM products WHERE id=%s', (product_id,)).fetchone()
    if row is None:
        return JSONResponse({'detail': 'Not found'}, status_code=404)
    return {'unit_price': float(row[0]) / row[1]}

class Order(BaseModel):
    product_id: int = Field(ge=1, le=10)
    quantity: int = Field(ge=1, le=5)

@app.post('/orders')
def order(body: Order):
    with connect() as conn:
        row = conn.execute('INSERT INTO orders(product_id, quantity) VALUES (%s,%s) RETURNING id',
                           (body.product_id, body.quantity)).fetchone()
    return {'id': row[0]}
