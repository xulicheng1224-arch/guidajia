import asyncio
from types import SimpleNamespace

import pytest

import crawler
from collection_limits import COLLECT_TIMEOUT_SECONDS
from crawler_diagnostics import PageDiagnostics
from backend import collector, official_api, worker, store as s
from diagnostics import describe
from tests.test_system import client
from tests.test_system import product


def test_shared_120_second_limit_and_render_setting(client):
    assert COLLECT_TIMEOUT_SECONDS == collector.COLLECT_TIMEOUT_SECONDS == official_api.TIMEOUT_SECONDS == 120
    assert client.get('/api/state').json()['health']['timeout_seconds'] == 120
    assert client.patch('/api/settings',json={'wait_seconds':120}).status_code == 200
    assert client.patch('/api/settings',json={'wait_seconds':121}).status_code == 422


def test_price_wait_races_late_verification_and_cancels_waiter():
    async def run():
        cancelled = asyncio.Event()
        async def wait_for(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        page = SimpleNamespace(locator=lambda _: SimpleNamespace(first=SimpleNamespace(wait_for=wait_for)))
        monitor = SimpleNamespace(interrupted=asyncio.Event())
        task = asyncio.create_task(crawler.wait_price_region(page,monitor,120))
        await asyncio.sleep(.01)
        monitor.interrupted.set()
        assert await asyncio.wait_for(task,1) is False
        assert cancelled.is_set()
    asyncio.run(run())


def test_price_wait_uses_requested_budget_and_accepts_delayed_visible_node():
    async def run():
        async def wait_for(**kwargs):
            assert kwargs['timeout'] == 114000
            await asyncio.sleep(.01)
        async def settle(page):
            return False
        page = SimpleNamespace(locator=lambda _: SimpleNamespace(first=SimpleNamespace(wait_for=wait_for)))
        monitor = SimpleNamespace(interrupted=asyncio.Event(),settle=settle)
        assert await crawler.wait_price_region(page,monitor,114)
    asyncio.run(run())


def test_page_evidence_does_not_keep_sensitive_resource_or_script_text():
    async def evaluate(_):
        return {'title':'','document_ready_state':'complete','body_text_length':0,
                'dom_element_count':20,'script_count':2,'iframe_count':0,'page_signals':[]}
    page = SimpleNamespace(on=lambda *args:None,evaluate=evaluate)
    evidence = PageDiagnostics(page)
    request = SimpleNamespace(url='https://assets.example/script.js?token=PRIVATE',resource_type='script',failure='net::ERR_CONNECTION_RESET PRIVATE')
    evidence.failed(request)
    evidence.script_error('ReferenceError: PRIVATE_TOKEN is not defined')
    result = {'status':'price_region_missing'}
    asyncio.run(evidence.capture(result))
    info = describe(result)
    assert info['render_state'] == 'blank'
    assert info['request_failures'] == [{'host':'assets.example','resource_type':'script','code':'ERR_CONNECTION_RESET'}]
    assert info['script_error_types'] == ['ReferenceError']
    assert '空白' in info['message']
    assert 'PRIVATE' not in str(info)


def test_deadline_result_preserves_render_diagnostics():
    progress = {'diagnostics':{'stage':'price_region','render_state':'blank','body_text_length':0,
                 'document_ready_state':'complete','request_failures':[{'host':'assets.example','code':'ERR_FAILED'}]}}
    result = collector.timeout_result_with_progress(progress)
    assert result['status'] == 'timeout' and result['diagnostics']['timeout_seconds'] == 120
    assert result['diagnostics']['render_state'] == 'blank'
    assert result['diagnostics']['body_text_length'] == 0


def test_retry_shares_task_budget_instead_of_starting_another_full_timeout(client,monkeypatch):
    chosen=product(client,paused=False)
    s.set_settings({'crawler_start_gap':0,'request_gap':.1,'max_retries':3})
    monkeypatch.setattr(collector,'COLLECT_TIMEOUT_SECONDS',.06)
    calls=[]
    async def collect(_):
        calls.append(True)
        await asyncio.sleep(.04)
        return {'status':'price_region_missing','price':None,'diagnostics':{'render_state':'blank','body_text_length':0}}
    monkeypatch.setattr(collector,'collect',collect)
    with s.connection() as db:
        job=dict(db.execute('SELECT * FROM jobs WHERE product_id=?',(chosen['id'],)).fetchone())
    asyncio.run(worker.run_job(job))
    row=client.get('/api/jobs').json()[0]
    assert len(calls)==1 and row['attempts']==1 and row['error']=='timeout'
    assert row['diagnostics']['render_state']=='blank'
