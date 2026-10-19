"""Manual commands bypass timer/session/circuit pauses, without resuming timers."""

import asyncio
from datetime import timedelta

import pytest

from backend import collection_queue, collector, crawl_pacing, domain as d, store as s, worker
from tests.test_collection_batches import clock
from tests.test_system import client, product


@pytest.mark.parametrize("login", ["expired", "opening", "waiting"])
@pytest.mark.parametrize("role", ["admin", "user"])
def test_manual_runs_despite_all_pause_states(client, monkeypatch, login, role):
    if role == "user":
        client.post('/api/access/register', json={'email':'manual@fake.example','password':'FixtureUser123'}).raise_for_status()
    owner = client.get('/api/state').json()['access']['user']['id']
    scheduled = product(client, paused=False, url='https://item.taobao.com/item.htm?id=100')
    chosen = product(client, paused=True, url='https://item.taobao.com/item.htm?id=200')
    calls = []
    async def collect(p):
        assert s.TENANT.get() == owner
        calls.append(p['id'])
        return {'price':None,'status':'blocked','stock':'unknown'}
    monkeypatch.setattr(collector, 'collect', collect)
    async def exercise():
        with s.tenant_scope(owner):
            s.set_settings({'global_paused':True,'crawler_start_gap':0,'request_gap':0,'max_retries':0,
                'circuit_until':(d.utcnow()+timedelta(hours=1)).isoformat()})
            collector.runtime().LOGIN.update(status=login,issue='verification_required')
            # A missing own Cookie must produce a result, never an endless queue.
            response = client.post(f'/api/product/{chosen["id"]}/crawl',json={})
            assert response.status_code == 200, response.text
            queue = collection_queue.explain()
            assert queue['reason'] == 'queued'
            pool = worker.CollectionPool()
            try:
                assert pool.dispatch() == 1
                await asyncio.gather(*pool.tasks)
                assert calls == [chosen['id']]
                assert s.settings()['global_paused'] is True
                assert s.get(chosen['id'])['paused'] is True
                assert pool.dispatch() == 0
                with s.connection() as db:
                    assert db.execute("SELECT status FROM jobs WHERE product_id=?", (scheduled['id'],)).fetchone()[0] == 'queued'
            finally:
                await pool.close()
    asyncio.run(exercise())


def test_manual_has_priority_over_older_scheduled_jobs_without_global_pause(client, monkeypatch):
    scheduled = product(client, paused=False, url='https://item.taobao.com/item.htm?id=300')
    chosen = product(client, paused=False, url='https://item.taobao.com/item.htm?id=400')
    s.set_settings({'crawler_start_gap':0,'request_gap':0,'max_retries':0,'max_concurrency':1})
    client.post(f'/api/product/{chosen["id"]}/crawl',json={}).raise_for_status()
    calls = []
    async def collect(p):
        calls.append(p['id'])
        return {'price':None,'status':'price_masked'}
    monkeypatch.setattr(collector,'collect',collect)
    async def exercise():
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch() == 1
            await asyncio.gather(*pool.tasks)
            assert calls == [chosen['id']]
            assert pool.dispatch() == 1
            await asyncio.gather(*pool.tasks)
            assert calls == [chosen['id'],scheduled['id']]
        finally:
            await pool.close()
    asyncio.run(exercise())


def test_failed_manual_wave_never_pauses_remaining_manual_jobs(client, monkeypatch):
    at = clock(monkeypatch)
    for i in range(8):
        product(client, paused=True, url=f'https://item.taobao.com/item.htm?id={500+i}')
    s.set_settings({'global_paused':True,'crawler_start_gap':.001,'request_gap':0,'max_retries':3,
        'circuit_until':(d.utcnow()+timedelta(hours=1)).isoformat()})
    collector.LOGIN.update(status='expired',issue='verification_required')
    client.post('/api/collection/all',json={}).raise_for_status()
    calls = []
    async def collect(p):
        calls.append(p['id'])
        return {'price':None,'status':'blocked','stock':'unknown'}
    monkeypatch.setattr(collector,'collect',collect)
    async def exercise():
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch(capacity=0) == 0
            assert pool.dispatch() == 5
            await asyncio.gather(*pool.tasks)
            assert len(calls) == 5
            assert pool.dispatch() == 0
            at[0] += timedelta(seconds=8)
            assert pool.dispatch() == 3
            await asyncio.gather(*pool.tasks)
            assert len(calls) == len(set(calls)) == 8
            assert d.health()['queued'] == 0
            assert s.settings()['global_paused'] is True
        finally:
            await pool.close()
    asyncio.run(exercise())


def test_manual_success_keeps_timer_circuit_and_counters(client, monkeypatch):
    chosen = product(client, paused=True)
    until = (d.utcnow()+timedelta(hours=1)).isoformat()
    s.set_settings({'global_paused':True,'crawler_start_gap':0,'request_gap':300,'failures':4,'circuit_until':until})
    async def collect(_):
        return {'price':10000,'status':'success','stock':'in_stock'}
    monkeypatch.setattr(collector,'collect',collect)
    client.post(f'/api/product/{chosen["id"]}/crawl',json={}).raise_for_status()
    async def exercise():
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch() == 1
            # Manual work must not hold a slot for the automatic request_gap.
            async with asyncio.timeout(2):
                await asyncio.gather(*pool.tasks)
            assert s.settings()['circuit_until'] == until
            assert s.settings()['failures'] == 4
            assert s.settings()['global_paused'] is True
        finally:
            await pool.close()
    asyncio.run(exercise())


def test_running_scheduled_job_is_promoted_while_waiting_for_start(client, monkeypatch):
    chosen = product(client, paused=False)
    s.set_settings({'crawler_start_gap':1,'request_gap':0,'max_retries':0})
    calls = []
    async def exercise():
        entered, proceed = asyncio.Event(), asyncio.Event()
        async def wait(job, gap, ready):
            entered.set()
            await proceed.wait()
            return 0 if ready() else None
        async def collect(p):
            calls.append(p['id'])
            return {'price':None,'status':'price_masked'}
        monkeypatch.setattr(crawl_pacing,'wait_turn',wait)
        monkeypatch.setattr(collector,'collect',collect)
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch() == 1
            await entered.wait()
            s.set_settings({'global_paused':True,'circuit_until':(d.utcnow()+timedelta(hours=1)).isoformat()})
            collector.LOGIN.update(status='expired',issue='verification_required')
            response = client.post(f'/api/product/{chosen["id"]}/crawl',json={})
            assert response.status_code == 200
            proceed.set()
            await asyncio.gather(*pool.tasks)
            assert calls == [chosen['id']]
            rows = client.get('/api/jobs').json()
            assert len(rows) == 1 and rows[0]['status'] == 'failed' and rows[0]['attempts'] == 1
        finally:
            await pool.close()
    asyncio.run(exercise())


def test_missing_user_cookie_finishes_as_failure_without_using_admin_cookie(client, monkeypatch):
    collector.accept_session({'cookies':[{'name':'unb','value':'fixture-admin','domain':'.taobao.com'}],'origins':[]},'fixture')
    client.post('/api/access/register',json={'email':'no-cookie@fake.example','password':'FixtureUser123'}).raise_for_status()
    owner = client.get('/api/state').json()['access']['user']['id']
    chosen = product(client, paused=True)
    async def no_process(*args, **kwargs):
        pytest.fail('A user without an own Cookie must fail before launching a browser')
    monkeypatch.setattr(collector.asyncio,'create_subprocess_exec',no_process)
    client.post(f'/api/product/{chosen["id"]}/crawl',json={}).raise_for_status()
    async def exercise():
        with s.tenant_scope(owner):
            s.set_settings({'global_paused':True,'crawler_start_gap':0,'request_gap':0,'max_retries':0})
            pool = worker.CollectionPool()
            try:
                assert pool.dispatch() == 1
                async with asyncio.timeout(2):
                    await asyncio.gather(*pool.tasks)
                rows = client.get('/api/jobs').json()
                assert rows[0]['status'] == 'failed' and rows[0]['error'] == 'login_or_redirect'
                assert not collector.runtime().SESSION.exists()
            finally:
                await pool.close()
    asyncio.run(exercise())
