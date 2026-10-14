import assert from 'node:assert/strict';
import test from 'node:test';

import { parseBrokerMessage } from './chatUtils.js';

for (const type of ['dm', 'group']) {
    for (const threaded of [false, true]) {
        for (const [field, id] of [
            ['slack_user_id', 'U0ABC123'],
            ['slack_bot_id', 'B0XYZ'],
        ]) {
            test(`Slack ${type} ${field} preserves parsed fields; threaded=${threaded}`, () => {
                const msgId = '1711584001.000200';
                const sender = 'Sample Sender';
                const prefix = type === 'dm'
                    ? `slack | dm | ${sender} | D0123`
                    : `slack | group | #sample | ${sender} | C0123`;
                const thread = threaded
                    ? ' | thread_root_ts:1711584000.000100 | is_thread_reply:true'
                    : '';
                const existing = `${prefix} | 1970-01-01 00:00:00 UTC | msg_id:${msgId}${thread}`;
                const before = parseBrokerMessage(`[${existing}]\nhello`);
                const after = parseBrokerMessage(`[${existing} | ${field}:${id}]\nhello`);

                assert.deepEqual(after, before);
                assert.equal(after.meta.platform, 'slack');
                assert.equal(after.meta.type, type);
                assert.equal(after.meta.msgId, msgId);
                assert.equal(after.meta.sender, sender);
                assert.equal(after.meta.chatId, type === 'dm' ? 'D0123' : 'C0123');
                assert.equal(after.content, 'hello');
            });
        }
    }
}
