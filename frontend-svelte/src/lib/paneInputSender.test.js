import assert from 'node:assert/strict';
import test from 'node:test';

import { createPaneInputSender } from './paneInputSender.js';

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

test('rapid text then Enter starts a cumulative keepalive submit immediately', async () => {
    const calls = [];
    const requests = [];
    const sender = createPaneInputSender({
        send(body, options) {
            const request = deferred();
            calls.push({ body, options });
            requests.push(request);
            return request.promise;
        },
    });

    sender.enqueue({ text: 'rapid' });
    sender.enqueue({ key: 'Enter' });

    assert.equal(calls.length, 2, 'Enter is not promise-chained behind text');
    assert.equal(calls[0].options.keepalive, false);
    assert.equal(calls[1].options.keepalive, true);
    assert.deepEqual(calls[1].body.events, [
        { seq: 1, text: 'rapid', key: '' },
        { seq: 2, text: '', key: 'Enter' },
    ]);

    sender.dispose();
    requests[1].resolve({ acked_seq: 2 });
    requests[0].resolve({ acked_seq: 1 });
    await Promise.all(requests.map((request) => request.promise));
});

test('an out-of-order acknowledgement prunes cumulative retries monotonically', async () => {
    const calls = [];
    const requests = [];
    const sender = createPaneInputSender({
        send(body, options) {
            const request = deferred();
            calls.push({ body, options });
            requests.push(request);
            return request.promise;
        },
    });

    sender.enqueue({ text: 'a' });
    sender.enqueue({ text: 'b' });
    requests[1].resolve({ acked_seq: 2 });
    await requests[1].promise;
    await Promise.resolve();

    sender.enqueue({ key: 'Enter' });
    assert.deepEqual(calls[2].body.events, [
        { seq: 3, text: '', key: 'Enter' },
    ]);

    requests[0].resolve({ acked_seq: 1 });
    requests[2].resolve({ acked_seq: 3 });
    await Promise.all(requests.map((request) => request.promise));
});

test('a failed final submit retries its unacknowledged cumulative batch', async () => {
    const calls = [];
    const sender = createPaneInputSender({
        send(body, options) {
            calls.push({ body, options });
            if (body.events.at(-1).key === 'Enter' && calls.length < 4) {
                return Promise.reject(new Error('transient network failure'));
            }
            return Promise.resolve({ acked_seq: body.events.at(-1).seq });
        },
    });

    sender.enqueue({ text: 'answer' });
    await Promise.resolve();
    sender.enqueue({ key: 'Enter' });
    await new Promise((resolve) => setTimeout(resolve, 0));

    const submits = calls.filter((call) => call.body.events.at(-1).key === 'Enter');
    assert.equal(submits.length, 3);
    assert.ok(submits.every((call) => call.options.keepalive));
});

test('a late old success cannot clear an exhausted Enter failure', async () => {
    const oldText = deferred();
    const callbacks = [];
    let callCount = 0;
    const sender = createPaneInputSender({
        send(body) {
            callCount += 1;
            if (callCount === 1) return oldText.promise;
            assert.equal(body.events.at(-1).key, 'Enter');
            return Promise.reject(new Error('submit exhausted'));
        },
        onError: () => callbacks.push('error'),
        onSuccess: () => callbacks.push('success'),
    });

    sender.enqueue({ text: 'visible' });
    sender.enqueue({ key: 'Enter' });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(callCount, 4, 'original Enter plus two bounded retries');
    assert.deepEqual(callbacks, ['error']);

    oldText.resolve({ acked_seq: 1 });
    await oldText.promise;
    await Promise.resolve();
    assert.deepEqual(callbacks, ['error']);
});

test('a late old failure is suppressed after a newer Enter succeeds', async () => {
    const oldText = deferred();
    const submit = deferred();
    const callbacks = [];
    let callCount = 0;
    const sender = createPaneInputSender({
        send() {
            callCount += 1;
            return callCount === 1 ? oldText.promise : submit.promise;
        },
        onError: () => callbacks.push('error'),
        onSuccess: () => callbacks.push('success'),
    });

    sender.enqueue({ text: 'answer' });
    sender.enqueue({ key: 'Enter' });
    submit.resolve({ acked_seq: 2 });
    await submit.promise;
    await Promise.resolve();
    assert.deepEqual(callbacks, ['success']);

    oldText.reject(new Error('stale prefix failure'));
    await Promise.resolve();
    await Promise.resolve();
    assert.deepEqual(callbacks, ['success']);
});
