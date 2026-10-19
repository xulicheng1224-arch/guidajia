import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const source = await readFile(new URL('../frontend/request-state.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022}}).outputText;
const {createQueuedRefresh} = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
const deferred = () => {let resolve, reject; const promise = new Promise((a, b) => {resolve = a; reject = b;}); return {promise, resolve, reject};};

test('refresh requested during an old read waits for and publishes a fresh read without overlap', async () => {
  const old = deferred(), fresh = deferred();
  let reads = 0, active = 0, peak = 0;
  const published = [];
  const refresh = createQueuedRefresh(async () => {
    peak = Math.max(peak, ++active);
    try {return await (++reads === 1 ? old.promise : fresh.promise);}
    finally {active--;}
  }, value => published.push(value));
  const first = refresh();
  await Promise.resolve();
  const afterWrite = refresh();
  assert.equal(first, afterWrite);
  old.resolve('old');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads, 2);
  let finished = false;
  afterWrite.then(() => finished = true);
  await Promise.resolve();
  assert.equal(finished, false);
  fresh.resolve('fresh');
  await afterWrite;
  assert.deepEqual(published, ['old', 'fresh']);
  assert.equal(peak, 1);
});

test('bursts coalesce and synchronous read failure does not lock future refreshes', async () => {
  let reads = 0;
  const published = [];
  const refresh = createQueuedRefresh(() => {
    if (++reads === 1) throw new Error('fixture failure');
    return Promise.resolve(reads);
  }, value => published.push(value));
  await assert.rejects(refresh(), /fixture failure/);
  await Promise.all([refresh(), refresh(), refresh()]);
  assert.equal(reads, 2);
  assert.deepEqual(published, [2]);
});

test('failed asynchronous reads preserve the previous value and can recover', async () => {
  let fail = false, value = 1;
  const published = [];
  const refresh = createQueuedRefresh(async () => {
    if (fail) throw new Error('network unavailable');
    return value;
  }, result => published.push(result));
  await refresh();
  fail = true;
  await assert.rejects(refresh(), /network unavailable/);
  assert.deepEqual(published, [1]);
  fail = false;
  value = 2;
  await refresh();
  assert.deepEqual(published, [1, 2]);
});
