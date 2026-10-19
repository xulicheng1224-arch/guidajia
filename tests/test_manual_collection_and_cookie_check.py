import asyncio
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend import app as server, collection_batches as batches, collection_queue, collector, domain as d, session_verification, store as s, worker
from tests.test_collection_batches import clock
from tests.test_system import client, product, record


def register(browser):
    browser.post('/api/access/register', json={'email':'cookie-check@fake.example','password':'FixtureUser123'}).raise_for_status()
    return browser.get('/api/state').json()['access']['user']['id']


def test_random_admin_url_only_uses_user_cookie_without_copying_admin_data(client, monkeypatch):
    first = product(client, name='admin-private-one', url='https://item.taobao.com/item.htm?id=123')
    second = product(client, name='admin-private-two', url='https://detail.tmall.com/item.htm?id=456&skuId=789')
    product(client, url='https://item.jd.com/999.html')
    product(client, name='admin-reference', url='')
    deleted = product(client, url='https://item.taobao.com/item.htm?id=333')
    s.update(deleted['id'], {}, deleted_at=s.now())
    invalid = product(client, url='https://item.taobao.com/item.htm?id=444')
    s.update(invalid['id'], {'url':'https://example.com/not-a-product'})
    record(first, price=76543)
    collector.session_state.save(collector.SESSION, {'cookies':[{'name':'unb','value':'fixture-admin-secret','domain':'.taobao.com'}],'origins':[]})
    original_cookie = collector.SESSION.read_bytes()
    choices = []
    def choose(urls):
        choices.append(set(urls))
        return second['url']
    monkeypatch.setattr(session_verification.secrets, 'choice', choose)
    monkeypatch.setattr(worker.random, 'uniform', lambda *_: 0)
    with TestClient(server.app) as browser:
        owner = register(browser)
        own = product(browser, name='user-own', url='https://item.taobao.com/item.htm?id=888')
        assert browser.post('/api/session/verify',json={}).status_code == 409
        browser.post('/api/session/import',json={'content':'unb=fixture-user-secret'}).raise_for_status()
        response = browser.post('/api/session/verify',json={'product_id':own['id']})
        assert response.status_code == 200, response.text
        assert response.json()['product_id'] is None
        again = browser.post('/api/session/verify',json={'product_id':first['id']})
        assert again.json()['job_id'] == response.json()['job_id']
        assert choices == [{first['url'],second['url']}]
        assert all(value not in response.text for value in (first['id'],second['id'],first['name'],second['name']))
        calls = []
        async def collect(p):
            assert s.TENANT.get() == owner
            assert p['url'] == second['url'] and p['name'] == 'Cookie 校验'
            assert p['id'] == session_verification.CHECK_PRODUCT_ID
            state = json.loads(collector.runtime().SESSION.read_text())
            assert state['cookies'][0]['value'] == 'fixture-user-secret'
            calls.append(p['url'])
            return {'status':'success','price':12345,'stock':'unknown','sku_verified':True}
        monkeypatch.setattr(collector, 'collect', collect)
        async def exercise():
            with s.tenant_scope(owner):
                s.set_settings({'global_paused':True,'request_gap':0,'circuit_until':(d.utcnow()+timedelta(hours=1)).isoformat()})
                collector.runtime().LOGIN['status'] = 'expired'
                pool = worker.CollectionPool()
                try:
                    assert pool.dispatch(capacity=0) == 0
                    assert pool.dispatch() == 1
                    await asyncio.gather(*pool.tasks)
                    assert session_verification.read(collector.runtime().SESSION)['state'] == 'passed'
                    assert [p['id'] for p in s.entities('product')] == [own['id']]
                    assert d.records() == []
                    with s.connection() as db:
                        assert db.execute('SELECT count(*) FROM forecasts').fetchone()[0] == 0
                finally:
                    await pool.close()
        asyncio.run(exercise())
        jobs = browser.get('/api/jobs').json()
        assert len(jobs) == 1 and jobs[0]['status'] == 'success'
        for secret in (first['id'],second['id'],'admin-private','fixture-admin-secret'):
            assert secret not in json.dumps(jobs)
        feed = browser.get('/api/collection/results',params={'after':'["",""]'}).json()['items']
        assert feed[0]['product_name'] == 'Cookie 校验' and not feed[0]['product_available']
        assert calls == [second['url']]
    assert collector.SESSION.read_bytes() == original_cookie
    assert d.records(first['id'])[0]['price'] == 76543


def test_user_cannot_supply_a_fallback_when_admin_has_no_valid_taobao_links(client):
    product(client, url='https://item.jd.com/999.html')
    with TestClient(server.app) as browser:
        register(browser)
        own = product(browser)
        browser.post('/api/session/import',json={'content':'unb=fixture-user'}).raise_for_status()
        response = browser.post('/api/session/verify',json={'product_id':own['id'],'url':own['url']})
        assert response.status_code == 409 and '管理员暂未配置' in response.json()['detail']
        assert browser.get('/api/jobs').json() == []


def test_admin_still_selects_own_target(client):
    p = product(client)
    collector.session_state.save(collector.SESSION, {'cookies':[{'name':'unb','value':'fixture-admin','domain':'.taobao.com'}],'origins':[]})
    assert client.post('/api/session/verify',json={}).status_code == 404
    response = client.post('/api/session/verify',json={'product_id':p['id']})
    assert response.status_code == 200 and response.json()['product_id'] == p['id']
    row = client.get('/api/jobs').json()[0]
    assert row['product_id'] == p['id'] and 'verification_target' not in json.loads(row['context'])


def test_manual_collection_bypasses_older_paused_scheduled_jobs_and_never_resumes_timer(client, monkeypatch):
    products = [product(client,url=f'https://item.taobao.com/item.htm?id={100+i}',paused=False) for i in range(3)]
    s.set_settings({'crawler_start_gap':0,'global_paused':True,'request_gap':0,'max_retries':0})
    before = {r['product_id']:r['id'] for r in client.get('/api/jobs').json()}
    response = client.post(f'/api/product/{products[-1]["id"]}/crawl',json={})
    assert response.json()['job_id'] == before[products[-1]['id']]
    queued = client.get('/api/jobs').json()
    manual = next(row for row in queued if row['product_id'] == products[-1]['id'])
    assert manual['queue']['reason'] == 'queued' and manual['queue']['position'] == 1
    assert all(row['queue']['reason'] == 'paused' for row in queued if row is not manual)
    assert collection_queue.explain()['reason'] == 'queued'
    calls = []
    async def collect(p):
        calls.append(p['id'])
        return {'status':'price_masked','price':None}
    monkeypatch.setattr(collector,'collect',collect)
    monkeypatch.setattr(worker.random,'uniform',lambda *_:0)
    async def exercise():
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch() == 1
            await asyncio.gather(*pool.tasks)
            assert calls == [products[-1]['id']]
            assert s.settings()['global_paused'] is True
            worker.schedule()
            assert pool.dispatch() == 0
            assert d.health()['waiting'] == 2
            s.set_settings({'global_paused':False})
            assert pool.dispatch() == 2
            await asyncio.gather(*pool.tasks)
            assert set(calls) == {p['id'] for p in products}
        finally: await pool.close()
    asyncio.run(exercise())


def test_one_click_keeps_five_slots_and_eight_second_waves_while_timer_stopped(client, monkeypatch):
    at = clock(monkeypatch)
    for i in range(8):product(client,url=f'https://item.taobao.com/item.htm?id={1000+i}')
    s.set_settings({'crawler_start_gap':0,'global_paused':True,'max_retries':0,'circuit_failures':100})
    result = client.post('/api/collection/all',json={}).json()
    assert result['added'] == 8 and result['batch']['batch_size'] == 5
    calls = []
    async def collect(p):
        calls.append(p['id'])
        await asyncio.sleep(.01)
        return {'status':'price_masked','price':None}
    monkeypatch.setattr(collector,'collect',collect)
    async def exercise():
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch(capacity=0) == 0
            assert pool.dispatch() == 5
            await asyncio.gather(*pool.tasks)
            assert len(calls) == 5
            at[0] += timedelta(seconds=7)
            assert pool.dispatch() == 0
            at[0] += timedelta(seconds=1)
            assert pool.dispatch() == 3
            await asyncio.gather(*pool.tasks)
            assert len(calls) == 8 and s.settings()['global_paused'] is True
            assert d.health()['queued'] == 0
        finally:await pool.close()
    asyncio.run(exercise())
