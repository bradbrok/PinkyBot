/**
 * Chat utility functions — pure helpers shared by Chat page and its sub-components.
 */

/**
 * Parse broker metadata header from user messages.
 * DM format:    [platform | dm | sender | chat_id | timestamp tz | msg_id:123]\ncontent
 * Group format: [platform | group | display | sender | chat_id | timestamp tz | msg_id:123]\ncontent
 * Legacy format: [platform | sender | chat_id | timestamp tz | msg_id:123]\ncontent
 */
export function parseBrokerMessage(text) {
    const match = String(text || '').match(/^\[([^\]]+)\]\n?([\s\S]*)$/);
    if (!match) return { meta: null, content: text };
    const parts = match[1].split('|').map((s) => s.trim());
    if (parts.length < 3) return { meta: null, content: text };

    const meta = { platform: parts[0] };
    if (parts[1] === 'dm') {
        meta.type = 'dm';
        meta.sender = parts[2] || '';
        meta.chatId = parts[3] || '';
        meta.timestamp = parts[4] || '';
        if (parts[5]) meta.msgId = parts[5].replace('msg_id:', '');
    } else if (parts[1] === 'group') {
        meta.type = 'group';
        meta.groupName = parts[2] || '';
        meta.sender = parts[3] || '';
        meta.chatId = parts[4] || '';
        meta.timestamp = parts[5] || '';
        if (parts[6]) meta.msgId = parts[6].replace('msg_id:', '');
    } else {
        meta.type = 'dm';
        meta.sender = parts[1];
        meta.chatId = parts[2];
        if (parts.length >= 4) meta.timestamp = parts[3];
        if (parts.length >= 5) meta.msgId = parts[4].replace('msg_id:', '');
    }
    return { meta, content: match[2] || '' };
}

/**
 * Filter out auto-generated standalone session IDs (daemon-internal like "pinky-3776b989b4e").
 */
export function isStandaloneSessionId(id) {
    if (!id) return false;
    return /^pinky-[0-9a-f]{8,}$/i.test(id);
}

/**
 * Group sessions by agent, sort main first, filter noise.
 */
export function groupByAgent(agents, sessions) {
    const agentNames = new Set(agents.map((a) => a.name));
    const groups = {};
    const seenIds = new Set();
    const orphans = [];

    for (const s of sessions) {
        if (isStandaloneSessionId(s.id)) continue;
        if (seenIds.has(s.id)) continue;
        seenIds.add(s.id);

        const owner = s.agent_name || '';
        if (owner && agentNames.has(owner)) {
            if (!groups[owner]) groups[owner] = [];
            groups[owner].push(s);
            continue;
        }

        let matched = false;
        for (const aName of agentNames) {
            if (s.id.startsWith(`${aName}-`) || s.id === aName) {
                if (!groups[aName]) groups[aName] = [];
                groups[aName].push(s);
                matched = true;
                break;
            }
        }
        if (!matched) orphans.push(s);
    }

    for (const aName of Object.keys(groups)) {
        groups[aName].sort((a, b) => {
            const aIsMain = (a.session_type || '') === 'main' || a.id === `${aName}-main`;
            const bIsMain = (b.session_type || '') === 'main' || b.id === `${aName}-main`;
            if (aIsMain && !bIsMain) return -1;
            if (!aIsMain && bIsMain) return 1;
            return a.id.localeCompare(b.id);
        });
    }

    return { groups, orphans };
}

/**
 * Format a unix timestamp into a human-readable chat time.
 */
export function formatMsgTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const now = new Date();
    const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (d.toDateString() === now.toDateString()) return time;
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    if (d.toDateString() === yesterday.toDateString()) return `yesterday ${time}`;
    return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
}

/**
 * Sort messages by timestamp, then local order.
 */
export function sortMessages(list) {
    return [...list].sort((a, b) => {
        const aTs = Number(a.timestamp || a._localTimestamp || 0);
        const bTs = Number(b.timestamp || b._localTimestamp || 0);
        if (aTs !== bTs) return aTs - bTs;
        return Number(a._localOrder || 0) - Number(b._localOrder || 0);
    });
}

/**
 * Find the latest assistant message timestamp in a list.
 */
export function latestAssistantTimestamp(list) {
    return list.reduce((latest, msg) => {
        if (msg.role !== 'assistant') return latest;
        return Math.max(latest, Number(msg.timestamp || msg._localTimestamp || 0));
    }, 0);
}

/**
 * Check if a message's content matches a given text (handles broker-wrapped messages).
 */
export function userContentMatches(message, text) {
    if (message.role !== 'user') return false;
    if (String(message.content || '') === text) return true;
    const parsed = parseBrokerMessage(message.content);
    return String(parsed.content || '') === text;
}

/**
 * Derive a stable key for a message (for Svelte {#each} keying).
 */
export function deriveMessageKey(msg, index) {
    return msg.id
        || msg.message_id
        || msg._localId
        || `${msg.role}-${msg.timestamp || msg._localTimestamp || 0}-${index}`;
}

/**
 * Detect heartbeat/system messages that should be hidden.
 */
export function isHeartbeatMessage(msg) {
    if (!msg?.content) return false;
    const c = typeof msg.content === 'string' ? msg.content : '';
    return c.includes('HEARTBEAT_OK')
        || c.startsWith('Heartbeat, check to see')
        || c.startsWith('Heartbeat. Call send_heartbeat');
}
