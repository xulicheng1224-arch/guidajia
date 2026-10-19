import asyncio
import json
import sqlite3
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import accounts, app as server, collection_batches, collection_queue, collector, daily_prices, domain as d, full_backup, official_api as api, prediction_data as pd, price_sources, store as s, worker
from backend.product_urls import identity, normalize_url
from tests.test_system import client, product


def configured(platform="taobao", method=None):
    value = {**price_sources.PROFILES[platform], "enabled": True, "app_key": "fixture-app-key", "app_secret": "fixture-app-secret"}
    if method:
        value["method"] = method
    if value["method"] == "taobao.item.sku.get":
        value["token"] = "fixture-session-token"
    return value


def enable(platform="taobao", method=None):
    return price_sources.save({"mode": "api", "providers": {platform: configured(platform, method)}})


def tb_response(price="99.99"):
    return {"tbk_item_info_get_response": {"results": {"n_tbk_item": [{"num_iid": "123", "zk_final_price": price, "reserve_price": "129.99", "title": "测试显卡"}]}}}


def jd_response(sku=123, price="99.99", query=False):
    row = {"skuId": sku, "goodsName": "京东显卡", "unitPrice": price, "priceInfo": {"price": price, "lowestCouponPrice": "9.99"}}
    return {"jd_union_open_goods_" + ("query_response" if query else "promotiongoodsinfo_query_responce"): {"code": "0", "queryResult" if query else "result": json.dumps({"code": 200, "data": [row]})}}


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


def test_defaults_validation_secret_preservation_and_no_generic_setting_bypass(client):
    default = client.get('/api/price-source').json()
    assert default['mode'] == 'crawler'
    assert not any(v['enabled'] for v in default['providers'].values())
    for body in ({'mode':'both'}, {'mode':'api'}, {'providers': {'other': {}}}, {'providers': {'taobao': {'enabled':True}}}, {'providers': {'taobao': {'enabled':'true'}}}, {'providers': {'taobao': {'method':'taobao.item.delete'}}}, {'providers': {'taobao': {'endpoint':'https://evil.example'}}}):
        assert client.put('/api/price-source',json=body).status_code == 422
    assert client.get('/api/price-source').json() == default
    value = configured()
    response = client.put('/api/price-source',json={'mode':'api','providers':{'taobao':value}})
    assert response.status_code == 200
    assert value['app_secret'] not in response.text
    assert value['app_secret'] not in client.get('/api/state').text
    revision = response.json()['revision']
    assert client.put('/api/price-source',json={'providers':{'taobao':{'app_secret':'__saved__'}}}).json()['revision'] == revision
    assert s.settings()['price_apis']['taobao']['app_secret'] == value['app_secret']
    for field in ('price_source','price_apis','price_source_revision'):
        assert client.patch('/api/settings',json={field:'anything'}).status_code == 422
    assert client.put('/api/price-source',json={'mode':'crawler'}).json()['mode'] == 'crawler'
    assert s.settings()['price_apis']['taobao']['app_secret'] == value['app_secret']


def test_credentials_and_source_are_account_isolated_api_has_no_cookie_requirement(client, monkeypatch):
    enable()
    with TestClient(server.app) as other:
        assert other.post('/api/access/register',json={'email':'api-user@fake.example','password':'FixtureUser123'}).status_code == 200
        assert other.get('/api/price-source').json()['mode'] == 'crawler'
        assert not other.get('/api/price-source').json()['providers']['taobao']['configured']
        owner = other.get('/api/state').json()['access']['user']['id']
        p=product(other, paused=False)
        with s.tenant_scope(owner):
            assert not worker.account_cookie_ready()
            enable()
            s.set_settings({'request_gap':0,'max_retries':0})
            collector.runtime().LOGIN['status']='expired'
            assert worker.permitted(s.settings())
            assert collection_queue.explain()['reason'] not in ('cookie_missing','session_expired')
            async def fetch(*_):
                return api.parse(tb_response(),identity(p['url']),configured())
            async def browser(*_):
                raise AssertionError('API mode started a browser')
            monkeypatch.setattr(api,'collect',fetch)
            monkeypatch.setattr(collector,'collect',browser)
            monkeypatch.setattr(worker.random,'uniform',lambda *_:0)
            async def run():
                pool=worker.CollectionPool()
                assert pool.dispatch()==1
                await asyncio.gather(*pool.tasks)
                await pool.close()
            asyncio.run(run())
            assert d.records(p['id'])[0]['source']=='api'
        assert other.post('/api/session/verify',json={'product_id':p['id']}).status_code==409
    assert not s.entities('product')


def test_official_md5_signature_and_request_parameters():
    assert api.signature({'foo':'1','bar':'2','foo_bar':'3','foobar':'4','sign':'ignore','empty':''},'secret')=='4B4AC0F2D69BA521FFDE55A2BBEE3025'
    for platform,url,method in [('taobao','https://item.taobao.com/item.htm?id=123',None),('taobao','https://item.taobao.com/item.htm?id=123&skuId=456','taobao.item.sku.get'),('jd','https://item.jd.com/123.html',None),('jd','https://item.jd.com/123.html','jd.union.open.goods.query')]:
        config=configured(platform,method)
        params,error=api.parameters(identity(url),config)
        assert error is None and 'app_secret' not in params
        assert params['sign']==api.signature(params,config['app_secret'])
        if platform=='jd':
            business=json.loads(params['360buy_param_json'])
            assert business == ({'goodsReqDTO':{'skuIds':[123],'pageIndex':1,'pageSize':1}} if method else {'skuIds':'123'})
        elif method:
            assert params['sku_id']=='456' and params['num_iid']=='123' and params['session']==config['token']


@pytest.mark.parametrize('query',[False,True])
def test_jd_price_identity_and_not_lowest_coupon(query):
    config=configured('jd','jd.union.open.goods.query' if query else None)
    result=api.parse(jd_response(query=query),identity('https://item.jd.com/123.html'),config)
    assert result['price']==9999 and result['sku_verified']
    assert result['item_id']==result['selected_sku_id']=='123'
    assert pd.identity(result)=='jd:123:123:regular'
    assert api.parse(jd_response(sku=999,query=query),identity('https://item.jd.com/123.html'),config)['status']=='sku_mismatch'


def test_taobao_item_and_sku_scope_are_never_confused():
    info=identity('https://item.taobao.com/item.htm?id=123')
    result=api.parse(tb_response(),info,configured())
    assert result['price']==9999 and result['original_price']==12999
    assert result['api_granularity']=='item' and pd.identity(result) is None
    _,error=api.parameters(identity('https://item.taobao.com/item.htm?id=123&skuId=456'),configured())
    assert error=='api_sku_unsupported'
    config=configured(method='taobao.item.sku.get')
    body={'item_sku_get_response':{'sku':{'num_iid':123,'sku_id':456,'price':'99.99','quantity':2,'properties_name':'1:2:型号:黑色'}}}
    result=api.parse(body,identity('https://item.taobao.com/item.htm?id=123&skuId=456'),config)
    assert result['price']==9999 and result['selected_options']==['黑色']
    assert pd.identity(result)=='123:456:regular'
    body['item_sku_get_response']['sku']['num_iid']=999
    assert api.parse(body,identity('https://item.taobao.com/item.htm?id=123&skuId=456'),config)['status']=='item_mismatch'


@pytest.mark.parametrize('value',['10-20','99 ~ 100',None,True,'0','NaN','-1'])
def test_invalid_or_range_prices_are_none(value):
    result=api.parse(tb_response(value),identity('https://item.taobao.com/item.htm?id=123'),configured())
    assert result['price'] is None and result['status']=='price_missing'


def test_api_errors_redact_credentials_and_decode_nested_error():
    config=configured()
    result=api.parse({'error_response':{'code':25,'sub_msg':f"sign error {config['app_secret']} {config['app_key']} https://example.com/?sign=private"}},identity('https://item.taobao.com/item.htm?id=123'),config)
    assert result['status']=='api_auth_error' and result['price'] is None
    assert config['app_secret'] not in json.dumps(result) and config['app_key'] not in json.dumps(result)
    assert 'sign=private' not in json.dumps(result)
    jd={'jd_union_open_goods_promotiongoodsinfo_query_response':{'result':json.dumps({'code':403,'message':'permission denied'})}}
    assert api.parse(jd,identity('https://item.jd.com/123.html'),configured('jd'))['status']=='api_permission_error'


def test_http_gateway_stream_redirect_bounds_and_timeout(client,monkeypatch):
    enable()
    p=product(client)
    async_client=httpx.AsyncClient
    requests=[]
    response={'status':200,'body':tb_response()}
    def handler(request):
        requests.append(request)
        params=parse_qs(request.content.decode())
        assert 'app_secret' not in params and request.url==api.GATEWAYS['taobao']
        return httpx.Response(response['status'],json=response['body'],headers={'Location':'https://evil.example'})
    monkeypatch.setattr(api.httpx,'AsyncClient',lambda **kwargs:async_client(transport=httpx.MockTransport(handler),**kwargs))
    async def run():
        assert (await api.collect(p,s.settings()))['price']==9999
        response['status']=302
        result=await api.collect(p,s.settings())
        assert result['status']=='api_http_error' and len(requests)==2
        response['status']=200
        response['body']={'data':'x'*(api.MAX_RESPONSE_BYTES+1)}
        assert (await api.collect(p,s.settings()))['status']=='api_response_invalid'
        cancelled=[]
        async def wait(*_):
            try: await asyncio.Event().wait()
            finally: cancelled.append(True)
        monkeypatch.setattr(api,'request_json',wait)
        monkeypatch.setattr(api,'TIMEOUT_SECONDS',0.02)
        result=await api.collect(p,s.settings())
        assert result['status']=='timeout' and cancelled
    asyncio.run(run())


@pytest.mark.parametrize('first',['crawler','api'])
def test_source_switch_drains_old_branch_and_never_overlaps(client,monkeypatch,first):
    if first=='api':enable()
    products=[product(client,url=f'https://item.taobao.com/item.htm?id={123+i}',paused=False) for i in range(2)]
    s.set_settings({'max_concurrency':1,'max_retries':0,'request_gap':0})
    monkeypatch.setattr(worker.random,'uniform',lambda *_:0)
    async def run():
        release=asyncio.Event()
        entered=[]
        active=[]
        async def fetch(source,*_):
            assert not active
            active.append(source)
            entered.append(source)
            try:
                if len(entered)==1: await release.wait()
                return {'price':None,'status':'price_missing','stock':'unknown'}
            finally:active.pop()
        async def browser(*args):return await fetch('crawler',*args)
        async def official(*args):return await fetch('api',*args)
        monkeypatch.setattr(collector,'collect',browser)
        monkeypatch.setattr(api,'collect',official)
        pool=worker.CollectionPool()
        try:
            assert pool.dispatch()==1
            await until(lambda:len(entered)==1)
            enable() if first=='crawler' else price_sources.save({'mode':'crawler'})
            assert pool.dispatch()==0
            assert collection_queue.explain()['reason']=='source_switch'
            release.set()
            await asyncio.gather(*pool.tasks)
            assert s.settings()['failures']==0  # Old branch cannot trip the new circuit.
            assert pool.dispatch()==1
            await asyncio.gather(*pool.tasks)
            assert entered==[first,'api' if first=='crawler' else 'crawler']
            assert {r['source'] for p in products for r in d.records(p['id'])}=={'crawler','api'}
        finally:await pool.close()
    asyncio.run(run())


def test_api_jd_display_daily_override_training_and_platform_isolation(client):
    p=product(client,url='https://item.m.jd.com/product/123.html?tracking=ignored')
    assert p['url']=='https://item.jd.com/123.html'
    observed=d.utcnow()-timedelta(hours=1)
    stamp=pd.day(observed).isoformat()
    result=api.parse(jd_response(),identity(p['url']),configured('jd'))
    d.save_observation(p['id'],result,source='api',observed_at=observed.isoformat())
    row=d.records(p['id'])[0]
    assert pd.usable(row,s.settings()) and pd.identity(row)=='jd:123:123:regular'
    assert not pd.matches_product(row,{'url':'https://item.taobao.com/item.htm?id=123&skuId=123'})
    report=daily_prices.report(p,[row],'regular')
    assert report['sku_id']=='123' and report['rows'][0]['price']==9999
    response=client.post(f'/api/product/{p["id"]}/daily-prices',json={'day':stamp,'basis':'regular','price':88.88,'reason':'核对人工价格','item_id':'123','sku_id':'123','version':0})
    assert response.status_code==200,response.text
    row=d.records(p['id'])[0]
    assert row['price']==8888 and row['raw_price']==9999 and pd.usable(row,s.settings())
    assert client.get('/api/state').json()['products'][0]['current_price']==8888
    detail=client.get(f'/api/product/{p["id"]}')
    assert detail.status_code==200 and detail.json()['records'][0]['source']=='api'
    assert d.health()['total']==1 and d.health()['success']==1


def test_schedule_batches_skip_unsupported_sources_and_share_limits(client,monkeypatch):
    tb=product(client,paused=False)
    jd=product(client,url='https://item.jd.com/999.html',paused=False)
    for p in (tb,jd):s.update(p['id'],{'last_scheduled':None},audited=False)
    with s.connection() as db:db.execute('DELETE FROM jobs')
    worker.schedule()
    assert d.health()['queued']==1
    with s.connection() as db:db.execute('DELETE FROM jobs')
    enable('jd')
    worker.schedule()
    with s.connection() as db:
        rows=list(db.execute('SELECT product_id FROM jobs'))
        assert [row[0] for row in rows]==[jd['id']]
        db.execute('DELETE FROM jobs')
    batch=collection_batches.enqueue_all()
    assert batch['skipped_source']==1 and batch['added']==1
    assert batch['batch']['batch_size']==5 and batch['batch']['gap_seconds']==8
    s.set_settings({'global_paused':True,'request_gap':0})
    monkeypatch.setattr(worker.random,'uniform',lambda *_:0)
    async def fetch(*_):return api.failure('api_auth_error','jd')
    monkeypatch.setattr(api,'collect',fetch)
    async def run():
        pool=worker.CollectionPool()
        assert pool.dispatch(capacity=0)==0
        assert pool.dispatch()==1
        await asyncio.gather(*pool.tasks)
        assert s.settings()['global_paused'] is True
        await pool.close()
    asyncio.run(run())


def test_backup_redacts_simple_copy_and_full_backup_restores_api_configuration(client,tmp_path):
    enable()
    simple=worker.backup()
    with sqlite3.connect(s.data_dir()/'backups'/simple) as db:
        settings={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM settings')}
    assert settings['price_apis']=={} and settings['price_source']=='crawler'
    assert b'fixture-app-secret' not in (s.data_dir()/'backups'/simple).read_bytes()
    password='OfficialApiBackup123!'
    result=full_backup.create(password)
    source=s.DATA/'system-backups'/result['name']
    assert b'fixture-app-secret' not in source.read_bytes()
    folder=tmp_path/'checked'
    folder.mkdir()
    restored,_=full_backup.unpack(source,password,folder)
    with sqlite3.connect(restored/'monitor.sqlite3') as db:
        values={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM settings')}
    assert values['price_apis']['taobao']['app_secret']=='fixture-app-secret'
    assert values['price_source']=='api'


def test_api_five_slots_auth_failure_no_retry_or_browser_fallback(client,monkeypatch):
    enable()
    for i in range(6):product(client,url=f'https://item.taobao.com/item.htm?id={123+i}',paused=False)
    s.set_settings({'request_gap':0,'max_retries':3,'circuit_failures':100})
    monkeypatch.setattr(worker.random,'uniform',lambda *_:0)
    called=[]
    async def run():
        release=asyncio.Event()
        async def fetch(p,config):
            called.append(p['id'])
            if len(called)==5:release.set()
            await release.wait()
            return api.failure('api_auth_error','taobao',error_code='401')
        async def browser(*_):raise AssertionError('No browser fallback is allowed')
        monkeypatch.setattr(api,'collect',fetch)
        monkeypatch.setattr(collector,'collect',browser)
        pool=worker.CollectionPool()
        try:
            assert pool.dispatch()==5
            await asyncio.gather(*pool.tasks)
            assert len(called)==5 and pool.dispatch()==1
            await asyncio.gather(*pool.tasks)
        finally:await pool.close()
    asyncio.run(run())
    assert len(set(called))==6
    with s.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE attempts=1 AND error='api_auth_error'").fetchone()[0]==6
    assert collector.LOGIN['status']!='expired'


@pytest.mark.parametrize('url',['http://item.jd.com/123.html','https://item.jd.com.evil.example/123.html','https://user:secret@item.jd.com/123.html','https://item.jd.com:123/123.html','https://item.jd.com/not-an-id.html'])
def test_jd_url_validation(url):
    with pytest.raises(ValueError):normalize_url(url)
