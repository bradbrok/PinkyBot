<script>
    import { onMount, onDestroy, tick } from 'svelte';
    import { _ } from 'svelte-i18n';
    import Modal from '../components/Modal.svelte';
    import { api } from '../lib/api.js';
    import { escapeHtml, renderMarkdown, timeAgo } from '../lib/utils.js';

    export let params = {};

    /**
     * Parse broker metadata header from user messages.
     * DM format:    [platform | dm | sender | chat_id | timestamp tz | msg_id:123]\ncontent
     * Group format: [platform | group | display | sender | chat_id | timestamp tz | msg_id:123]\ncontent
     * Legacy format: [platform | sender | chat_id | timestamp tz | msg_id:123]\ncontent
     * Returns { meta: {platform, type, sender, chatId, timestamp, msgId, groupName} | null, content: string }
     */
    function parseBrokerMessage(text) {
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

    function groupByAgent(agents, sessions) {
        const agentNames = new Set(agents.map((a) => a.name));
        const groups = {};
        const orphans = [];
        for (const s of sessions) {
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
        return { groups, orphans };
    }

    function sortMessages(list) {
        return [...list].sort((a, b) => {
            const aTs = Number(a.timestamp || a._localTimestamp || 0);
            const bTs = Number(b.timestamp || b._localTimestamp || 0);
            if (aTs !== bTs) return aTs - bTs;
            return Number(a._localOrder || 0) - Number(b._localOrder || 0);
        });
    }

    function latestAssistantTimestamp(list) {
        return list.reduce((latest, msg) => {
            if (msg.role !== 'assistant') return latest;
            return Math.max(latest, Number(msg.timestamp || msg._localTimestamp || 0));
        }, 0);
    }

    function userContentMatches(message, text) {
        if (message.role !== 'user') return false;
        if (String(message.content || '') === text) return true;
        const parsed = parseBrokerMessage(message.content);
        return String(parsed.content || '') === text;
    }

    function deriveMessageKey(msg, index) {
        return msg.id
            || msg.message_id
            || msg._localId
            || `${msg.role}-${msg.timestamp || msg._localTimestamp || 0}-${index}`;
    }

    function isHeartbeatMessage(msg) {
        if (!msg?.content) return false;
        const c = typeof msg.content === 'string' ? msg.content : '';
        return c.includes('HEARTBEAT_OK') ||
               c.startsWith('Heartbeat, check to see') ||
               c.startsWith('Heartbeat. Call send_heartbeat');
    }

    let agentsList = [];
    let sessionsList = [];
    let activeSession = null;
    let activeAgent = null;
    let activeSessionRecord = null;
    let messages = [];
    let persistedMessages = [];
    let localMessages = [];
    let messageInput = '';
    let sending = false;
    let thinking = false;
    let thinkingActivity = '';
    let thinkingContent = '';
    let activityLog = [];
    let agentWorking = false;
    let activityPollInterval = null;
    let wasWorking = false;
    let connected = true;

    let infoModel = '--';
    let infoContext = '0%';
    let infoContextPct = 0;
    let infoMessages = 0;
    let infoSession = '--';

    let sidebarCollapsed = false;
    let messagesContainer;
    let refreshInterval;
    let restarting = false;
    let compacting = false;
    let archiving = false;
    let restartDropdownOpen = false;
    let streamingStats = null; // Extended stats from streaming status

    // Chat search
    let chatSearchQuery = '';
    let chatSearchResults = [];
    let chatSearchOpen = false;

    // Reply-to / quote
    let replyTo = null; // { id, role, content, msgId }

    function setReplyTo(msg) {
        const parsed = msg.role === 'user' ? parseBrokerMessage(msg.content) : null;
        const content = parsed?.content || msg.content || '';
        const msgId = parsed?.meta?.msgId || msg.id || msg.message_id || '';
        replyTo = { id: deriveMessageKey(msg, 0), role: msg.role, content: content.slice(0, 200), msgId };
    }

    function clearReply() { replyTo = null; }

    async function searchChats() {
        if (!chatSearchQuery.trim()) return;
        try {
            const results = await api('GET', `/conversations/search?q=${encodeURIComponent(chatSearchQuery)}`);
            chatSearchResults = results.results || [];
            chatSearchOpen = true;
        } catch { chatSearchResults = []; }
    }

    const PAGE_SIZE = 100;
    let hasMore = false;
    let totalMessages = 0;
    let loadedPersistedCount = 0;
    let loadingOlder = false;

    let showSettings = false;
    let showSessionInfo = false;
    let showForwardModal = false;
    let forwardMessage = null;
    let forwardAgents = [];
    let forwardSelected = {};
    let forwardContext = '';
    let forwarding = false;
    let forwardSearch = '';
    let forwardChips = [];
    let selectedModel = '';
    let contextNudgePct = 80;
    let savingModel = false;
    let savingNudge = false;

    const availableModels = [
        { value: 'claude-sonnet-4-6', label: 'Sonnet 4.6 (1M)' },
        { value: 'claude-opus-4-6', label: 'Opus 4.6 (1M)' },
        { value: 'claude-sonnet-4-5-20250514', label: 'Sonnet 4.5' },
        { value: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5' },
    ];

    let chatPollInterval;
    let chatRefreshSeq = 0;
    let sessionSwitchSeq = 0;
    let currentHistorySource = { kind: null, sessionId: null };
    let pendingReply = null;
    let pendingReplyTimer = null;
    let scrollSnapshot = null;
    let localMessageOrder = 0;
    let fileInput;
    let sessionCache = {};

    $: agentSessions = groupByAgent(agentsList, sessionsList);
    $: activeSessionRecord = sessionsList.find((session) => session.id === activeSession) || null;
    $: messages = sortMessages([...persistedMessages, ...localMessages]);
    $: activeMainSession = activeAgent ? `${activeAgent}-main` : null;
    $: canUseStreamingChat = !!activeAgent && activeSession === activeMainSession;
    $: canUseLegacySessionChat = !!activeSessionRecord && !activeSessionRecord._from_store && !canUseStreamingChat;
    $: canSendMessage = !!activeSession && (canUseStreamingChat || canUseLegacySessionChat);
    // messagePlaceholder: resolved in template using $_

    function buildLocalMessage(message) {
        return {
            ...message,
            _localId: `local-${Date.now()}-${++localMessageOrder}`,
            _localTimestamp: message.timestamp || Date.now() / 1000,
            _localOrder: localMessageOrder,
        };
    }

    function addLocalMessage(message) {
        localMessages = [...localMessages, buildLocalMessage(message)];
    }

    function applyCachedSessionState(sessionId) {
        const cached = sessionCache[sessionId];
        if (!cached) return false;
        persistedMessages = cached.persistedMessages || [];
        localMessages = cached.localMessages || [];
        totalMessages = cached.totalMessages || persistedMessages.length;
        loadedPersistedCount = cached.loadedPersistedCount || persistedMessages.length;
        hasMore = !!cached.hasMore;
        currentHistorySource = cached.currentHistorySource || { kind: null, sessionId: null };
        infoMessages = cached.infoMessages ?? totalMessages;
        infoSession = cached.infoSession || sessionId;
        infoModel = cached.infoModel ?? infoModel;
        infoContext = cached.infoContext ?? infoContext;
        infoContextPct = cached.infoContextPct ?? infoContextPct;
        return true;
    }

    function cacheCurrentSessionState(sessionId = activeSession) {
        if (!sessionId) return;
        sessionCache = {
            ...sessionCache,
            [sessionId]: {
                persistedMessages: [...persistedMessages],
                localMessages: [...localMessages],
                totalMessages,
                loadedPersistedCount,
                hasMore,
                currentHistorySource: { ...currentHistorySource },
                infoMessages,
                infoSession,
                infoModel,
                infoContext,
                infoContextPct,
            },
        };
    }

    function reconcileLocalMessages(nextPersisted) {
        localMessages = localMessages.filter((item) => {
            if (item._localKind === 'pending-user') {
                return !nextPersisted.some((msg) => {
                    const ts = Number(msg.timestamp || 0);
                    return userContentMatches(msg, item.content) && (!ts || ts >= item._sentAt - 60);
                });
            }

            if (item._localKind === 'pending-assistant') {
                return !nextPersisted.some((msg) => {
                    if (msg.role !== 'assistant') return false;
                    if (String(msg.content || '') !== String(item.content || '')) return false;
                    const ts = Number(msg.timestamp || 0);
                    return !ts || ts >= item._sentAt - 60;
                });
            }

            return true;
        });
    }

    function isNearBottom() {
        if (!messagesContainer) return true;
        const remaining = messagesContainer.scrollHeight - messagesContainer.scrollTop - messagesContainer.clientHeight;
        return remaining < 80;
    }

    function captureScroll(mode = 'refresh') {
        if (!messagesContainer) return;
        scrollSnapshot = {
            mode,
            top: messagesContainer.scrollTop,
            height: messagesContainer.scrollHeight,
            nearBottom: isNearBottom(),
        };
    }

    async function restoreScroll({ forceBottom = false } = {}) {
        await tick();
        if (!messagesContainer) return;

        const snapshot = scrollSnapshot;
        scrollSnapshot = null;

        if (forceBottom) {
            scrollToBottom();
            return;
        }

        if (!snapshot) return;

        if (snapshot.mode === 'prepend') {
            messagesContainer.scrollTop = messagesContainer.scrollHeight - snapshot.height + snapshot.top;
            return;
        }

        if (snapshot.nearBottom) {
            scrollToBottom();
            return;
        }

        messagesContainer.scrollTop = snapshot.top;
    }

    function scrollToBottom() {
        if (messagesContainer) messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }

    async function refreshSessions() {
        try {
            const [agentsData, sessData, convsData] = await Promise.all([
                api('GET', '/agents'),
                api('GET', '/sessions'),
                api('GET', '/conversations'),
            ]);

            agentsList = agentsData.agents || [];
            const sessIds = new Set((sessData || []).map((session) => session.id));
            const convSessions = (convsData.conversations || [])
                .filter((conversation) => !sessIds.has(conversation.session_id))
                .map((conversation) => ({
                    id: conversation.session_id,
                    state: 'streaming',
                    model: 'streaming',
                    message_count: conversation.message_count,
                    last_active: conversation.last_message_at,
                    agent_name: conversation.session_id.split('-')[0],
                    session_type: 'streaming',
                    _from_store: true,
                }));

            sessionsList = [...sessData, ...convSessions];
            connected = true;
        } catch {
            connected = false;
        }
    }

    function getConversationTargets(sessionId, agentName) {
        const mainSessionId = agentName ? `${agentName}-main` : null;
        return {
            preferred: sessionId,
            fallback: mainSessionId && mainSessionId !== sessionId ? mainSessionId : null,
        };
    }

    async function refreshChat({ preserveScroll = true } = {}) {
        if (!activeSession) return;

        const requestSeq = ++chatRefreshSeq;
        const sessionId = activeSession;
        const agentName = activeAgent || sessionId.split('-')[0];
        const sessionRecord = sessionsList.find((session) => session.id === sessionId) || null;
        const { preferred, fallback } = getConversationTargets(sessionId, agentName);

        let nextPersisted = [];
        let nextTotal = 0;
        let nextHasMore = false;
        let nextSource = { kind: null, sessionId: null };
        let loadedFromConversation = false;

        try {
            const preferredHistory = await api('GET', `/conversations/${preferred}/history?limit=${PAGE_SIZE}`);
            if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;

            if (
                (preferredHistory.total || 0) > 0
                || preferred === fallback
                || sessionRecord?._from_store
            ) {
                nextPersisted = sortMessages(preferredHistory.messages || []);
                nextTotal = preferredHistory.total || nextPersisted.length;
                nextHasMore = nextTotal > nextPersisted.length;
                nextSource = { kind: 'conversation', sessionId: preferred };
                loadedFromConversation = true;
            }
        } catch {
            // Best effort. Fall through to fallback/session history.
        }

        if (!loadedFromConversation && fallback) {
            try {
                const fallbackHistory = await api('GET', `/conversations/${fallback}/history?limit=${PAGE_SIZE}`);
                if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;

                if ((fallbackHistory.total || 0) > 0) {
                    nextPersisted = sortMessages(fallbackHistory.messages || []);
                    nextTotal = fallbackHistory.total || nextPersisted.length;
                    nextHasMore = nextTotal > nextPersisted.length;
                    nextSource = { kind: 'conversation', sessionId: fallback };
                    loadedFromConversation = true;
                }
            } catch {
                // Fall back to in-memory history below.
            }
        }

        if (!loadedFromConversation) {
            try {
                const history = await api('GET', `/sessions/${sessionId}/history?limit=${PAGE_SIZE}`);
                if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;

                nextPersisted = sortMessages(history.messages || []);
                nextTotal = nextPersisted.length;
                nextHasMore = false;
                nextSource = { kind: 'session', sessionId };
            } catch {
                if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;
                nextPersisted = [];
                nextTotal = 0;
                nextHasMore = false;
                nextSource = { kind: null, sessionId: null };
            }
        }

        if (preserveScroll) captureScroll('refresh');

        persistedMessages = nextPersisted;
        totalMessages = nextTotal;
        loadedPersistedCount = nextPersisted.length;
        hasMore = nextHasMore;
        infoMessages = nextTotal;
        infoSession = sessionId;
        currentHistorySource = nextSource;
        reconcileLocalMessages(nextPersisted);
        cacheCurrentSessionState(sessionId);

        if (pendingReply && pendingReply.sessionId === sessionId) {
            const latestAssistant = latestAssistantTimestamp(nextPersisted);
            const threshold = Math.max(pendingReply.priorAssistantTs || 0, pendingReply.sentAt - 2);
            if (latestAssistant > threshold) {
                pendingReply = null;
                thinking = false;
                thinkingActivity = '';
                thinkingContent = '';
                activityLog = [];
            }
        }

        await restoreScroll();

        try {
            const agentData = await api('GET', `/agents/${agentName}`);
            if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;
            if (agentData.model && !selectedModel) selectedModel = agentData.model;
            if (agentData.restart_threshold_pct != null) contextNudgePct = agentData.restart_threshold_pct;
            if (agentData.model) infoModel = agentData.model;
        } catch {
            // Ignore non-critical info refresh failures.
        }

        let gotStreamingContext = false;
        try {
            const streamStatus = await api('GET', `/agents/${agentName}/streaming/status`);
            if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;

            if (streamStatus.connected) {
                const ctx = streamStatus.context || {};
                if (ctx.percentage != null) {
                    infoContext = `${ctx.percentage}%`;
                    infoContextPct = ctx.percentage;
                    gotStreamingContext = true;
                }
                streamingStats = {
                    ...streamStatus.stats,
                    totalTokens: ctx.total_tokens,
                    maxTokens: ctx.max_tokens,
                    categories: ctx.categories || [],
                    mcpTools: ctx.mcp_tools || [],
                };
            } else {
                streamingStats = null;
            }
        } catch {
            // Ignore. Fallback below.
        }

        if (!gotStreamingContext) {
            try {
                const context = await api('GET', `/sessions/${sessionId}/context`);
                if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;
                infoContext = `${context.context_used_pct}%`;
                infoContextPct = context.context_used_pct || 0;
            } catch {
                if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;
                infoContext = '--';
                infoContextPct = 0;
            }
        }
    }

    async function loadOlderMessages() {
        if (!activeSession || loadingOlder || !hasMore || currentHistorySource.kind !== 'conversation') return;

        loadingOlder = true;
        try {
            const older = await api('GET', `/conversations/${currentHistorySource.sessionId}/history?limit=${PAGE_SIZE}&offset=${loadedPersistedCount}`);
            const olderMessages = sortMessages(older.messages || []);

            if (olderMessages.length > 0) {
                captureScroll('prepend');
                persistedMessages = [...olderMessages, ...persistedMessages];
                loadedPersistedCount += olderMessages.length;
                hasMore = older.has_more || (totalMessages > loadedPersistedCount);
                await restoreScroll();
            } else {
                hasMore = false;
            }
        } catch {
            // Best effort.
        }
        loadingOlder = false;
    }

    function handleMessagesScroll() {
        if (!messagesContainer) return;
        if (messagesContainer.scrollTop < 200 && hasMore && !loadingOlder) {
            loadOlderMessages();
        }
    }

    function startChatPolling() {
        stopChatPolling();
        const pollSessionId = activeSession;
        chatPollInterval = setInterval(() => {
            if (pollSessionId !== activeSession) return;
            refreshChat();
        }, 3000);
    }

    function stopChatPolling() {
        if (chatPollInterval) {
            clearInterval(chatPollInterval);
            chatPollInterval = null;
        }
    }

    function stopActivityPolling() {
        if (activityPollInterval) {
            clearInterval(activityPollInterval);
            activityPollInterval = null;
        }
    }

    function startActivityPolling() {
        stopActivityPolling();
        const pollSessionId = activeSession;
        const pollAgentName = activeAgent || (activeSession ? activeSession.split('-')[0] : null);
        if (!pollAgentName) return;

        activityPollInterval = setInterval(async () => {
            if (pollSessionId !== activeSession) return;
            try {
                const status = await api('GET', `/agents/${pollAgentName}/streaming/status`);
                if (pollSessionId !== activeSession) return;

                const activity = status?.stats?.current_activity || '';
                const log = status?.stats?.activity_log || [];
                const currentThinking = status?.stats?.current_thinking || '';
                const isWorking = !!activity || !!currentThinking;

                if (!thinking) {
                    agentWorking = isWorking;
                    if (isWorking) {
                        thinkingActivity = activity;
                        thinkingContent = currentThinking;
                        activityLog = log;
                    } else {
                        thinkingActivity = '';
                        thinkingContent = '';
                        activityLog = [];
                    }
                } else if (status?.stats) {
                    thinkingActivity = activity;
                    thinkingContent = currentThinking;
                    activityLog = log;
                }

                if (wasWorking && !isWorking && !thinking) {
                    agentWorking = false;
                    thinkingActivity = '';
                    thinkingContent = '';
                    activityLog = [];
                    await refreshChat();
                }
                wasWorking = isWorking;
            } catch {
                // Ignore transient polling failures.
            }
        }, 1000);
    }

    async function selectSession(id, agentName) {
        const previousSessionId = activeSession;
        if (previousSessionId) cacheCurrentSessionState(previousSessionId);

        stopChatPolling();
        stopActivityPolling();
        chatRefreshSeq += 1;
        const switchSeq = ++sessionSwitchSeq;
        activeSession = id;
        activeAgent = agentName || null;
        if (!applyCachedSessionState(id)) {
            persistedMessages = [];
            localMessages = [];
            totalMessages = 0;
            loadedPersistedCount = 0;
            hasMore = false;
            currentHistorySource = { kind: null, sessionId: null };
        }
        pendingReply = null;
        thinking = false;
        thinkingActivity = '';
        thinkingContent = '';
        activityLog = [];
        agentWorking = false;
        wasWorking = false;
        if (window.innerWidth <= 768) sidebarCollapsed = true;
        await refreshChat({ preserveScroll: false });
        if (switchSeq !== sessionSwitchSeq || activeSession !== id) return;
        await tick();
        scrollToBottom();
        startChatPolling();
        startActivityPolling();
    }

    async function sendMessage() {
        if (!canSendMessage || !messageInput.trim() || sending) return;

        let text = messageInput.trim();
        // Prepend reply context if quoting a message
        if (replyTo) {
            const quotedSnippet = replyTo.content.slice(0, 150).replace(/\n/g, ' ');
            const refLine = replyTo.msgId ? `[replying to msg_id:${replyTo.msgId}]` : `[replying to ${replyTo.role} message]`;
            text = `${refLine}\n> ${quotedSnippet}\n\n${text}`;
            replyTo = null;
        }
        const sentAt = Date.now() / 1000;
        const sessionId = activeSession;
        const priorAssistantTs = latestAssistantTimestamp(persistedMessages);
        messageInput = '';
        sending = true;
        thinking = true;
        thinkingActivity = '';
        thinkingContent = '';
        activityLog = [];
        pendingReply = { sessionId, sentAt, priorAssistantTs };
        if (pendingReplyTimer) clearTimeout(pendingReplyTimer);
        pendingReplyTimer = setTimeout(() => {
            if (pendingReply) {
                pendingReply = null;
                thinking = false;
                thinkingActivity = '';
                thinkingContent = '';
                activityLog = [];
            }
            pendingReplyTimer = null;
        }, 60000);

        captureScroll('append');
        localMessages = [
            ...localMessages,
            buildLocalMessage({
                role: 'user',
                content: text,
                _localKind: 'pending-user',
                _sentAt: sentAt,
            }),
        ];
        await restoreScroll({ forceBottom: true });

        try {
            if (canUseStreamingChat) {
                await api('POST', `/agents/${activeAgent}/chat`, { content: text });
                // Re-enable input immediately — response arrives via streaming poll
                sending = false;
                await refreshChat();
            } else if (canUseLegacySessionChat) {
                const data = await api('POST', `/sessions/${sessionId}/message`, { content: text });
                localMessages = [
                    ...localMessages,
                    buildLocalMessage({
                        role: 'assistant',
                        content: data.content,
                        duration_ms: data.duration_ms,
                        _localKind: 'pending-assistant',
                        _sentAt: Date.now() / 1000,
                    }),
                ];
                pendingReply = null;
                thinking = false;
                thinkingActivity = '';
                thinkingContent = '';
                activityLog = [];
                await refreshChat();
            }
        } catch (e) {
            pendingReply = null;
            thinking = false;
            thinkingActivity = '';
            thinkingContent = '';
            activityLog = [];
            localMessages = localMessages.filter((msg) => !(msg._localKind === 'pending-user' && msg.content === text && msg._sentAt === sentAt));
            addLocalMessage({ role: 'system', content: `Error: ${e.message}` });
        } finally {
            sending = false;
            if (pendingReplyTimer) { clearTimeout(pendingReplyTimer); pendingReplyTimer = null; }
        }
    }

    function handleKeydown(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    }

    async function handleFileUpload() {
        if (!fileInput?.files?.[0] || !activeAgent) return;
        const file = fileInput.files[0];
        const formData = new FormData();
        formData.append('file', file);

        sending = true;
        addLocalMessage({ role: 'user', content: `Uploading: ${file.name} (${(file.size / 1024).toFixed(1)} KB)` });
        await tick();
        scrollToBottom();

        try {
            const resp = await fetch(`/agents/${activeAgent}/upload`, {
                method: 'POST',
                body: formData,
            });
            if (!resp.ok) throw new Error(await resp.text());
            const data = await resp.json();
            addLocalMessage({ role: 'system', content: `File uploaded: ${data.filename} (${data.size} bytes) → ${data.path}` });
        } catch (e) {
            addLocalMessage({ role: 'system', content: `Upload failed: ${e.message}` });
        }

        sending = false;
        fileInput.value = '';
        await tick();
        scrollToBottom();
    }

    async function stopActiveAgent() {
        if (!activeAgent) return;
        try {
            await api('POST', `/agents/${activeAgent}/stop`);
            addLocalMessage({ role: 'system', content: `${activeAgent} force-stopped.` });
        } catch (e) {
            console.error('Stop failed:', e);
        }
    }

    async function openForwardModal(msg) {
        forwardMessage = msg;
        forwardContext = '';
        forwardChips = [];
        forwardSearch = '';
        forwarding = false;
        try {
            const data = await api('GET', '/agents');
            forwardAgents = (data.agents || [])
                .map(a => a.name)
                .filter(n => n !== activeAgent);
        } catch {
            forwardAgents = [];
        }
        showForwardModal = true;
    }

    function addForwardChip(name) {
        if (!forwardChips.includes(name)) {
            forwardChips = [...forwardChips, name];
        }
        forwardSearch = '';
    }

    function removeForwardChip(name) {
        forwardChips = forwardChips.filter(n => n !== name);
    }

    $: forwardSuggestions = forwardSearch.trim()
        ? forwardAgents.filter(a => a.toLowerCase().includes(forwardSearch.toLowerCase()) && !forwardChips.includes(a))
        : [];

    async function sendForward() {
        if (forwarding) return;
        const targets = forwardChips;
        if (targets.length === 0) return;
        forwarding = true;
        const prefix = forwardContext.trim() ? `${forwardContext.trim()}\n\n` : '';
        const body = `${prefix}[Forwarded from ${activeAgent}]\n${forwardMessage.content}`;
        try {
            for (const target of targets) {
                await api('POST', `/agents/${target}/forward`, { content: body });
            }
            addLocalMessage({ role: 'system', content: `Forwarded to ${targets.join(', ')}` });
            showForwardModal = false;
        } catch (e) {
            console.error('Forward failed:', e);
        }
        forwarding = false;
    }

    async function contextRestart() {
        if (!activeSession || restarting) return;
        restarting = true;

        try {
            const savePrompt = 'Your session is about to be restarted. Save your current state now:\n\n'
                + '1. Use your save_my_context or set wake context tool to persist what you were working on\n'
                + '2. Include: current task, key context, any blockers, and what to do next\n'
                + '3. Confirm when saved\n\n'
                + 'This is a context restart — your conversation will reset but your saved state will carry over.';

            const saveResult = await api('POST', `/sessions/${activeSession}/message`, { content: savePrompt });
            addLocalMessage({ role: 'assistant', content: saveResult.content, duration_ms: saveResult.duration_ms, _localKind: 'pending-assistant', _sentAt: Date.now() / 1000 });

            await api('POST', `/sessions/${activeSession}/restart`);
            await logCheckpoint('context-restart', 'Context restarted via UI');

            const wakePrompt = 'Session was restarted via context restart (UI). Check your wake context or saved context for continuation state. Pick up where you left off.';
            const wakeResult = await api('POST', `/sessions/${activeSession}/message`, { content: wakePrompt });
            addLocalMessage({ role: 'assistant', content: wakeResult.content, duration_ms: wakeResult.duration_ms, _localKind: 'pending-assistant', _sentAt: Date.now() / 1000 });

            await refreshChat();
            await refreshSessions();
        } catch (e) {
            addLocalMessage({ role: 'system', content: `Restart failed: ${e.message}` });
        }

        restarting = false;
    }

    async function logCheckpoint(type, detail) {
        const sessionId = currentHistorySource.sessionId || activeSession;
        if (!sessionId) return;
        try {
            await api('POST', `/conversations/${sessionId}/checkpoint`, { type, detail });
        } catch {
            // Best effort.
        }
    }

    async function compactContext() {
        if (!activeAgent || compacting) return;
        compacting = true;
        try {
            await api('POST', `/agents/${activeAgent}/streaming/compact`);
            await logCheckpoint('compact', 'Context compacted');
            await refreshChat();
        } catch (e) {
            addLocalMessage({ role: 'system', content: `Compact failed: ${e.message}` });
        }
        compacting = false;
    }

    async function archiveSession() {
        if (!activeAgent || archiving) return;
        if (!confirm('Archive this session? The agent will save its memory, then get a fresh context.')) return;
        archiving = true;
        try {
            const result = await api('POST', `/agents/${activeAgent}/streaming/archive`);
            await logCheckpoint('archive', `Archived. ${result.old_turns} turns. Session: ${result.old_session_id}`);
            await refreshChat();
            await refreshSessions();
        } catch (e) {
            addLocalMessage({ role: 'system', content: `Archive failed: ${e.message}` });
        }
        archiving = false;
    }

    async function saveModel() {
        if (!activeAgent || !selectedModel) return;
        savingModel = true;
        try {
            const result = await api('POST', `/agents/${activeAgent}/streaming/model`, { model: selectedModel });
            if (result.restarted) {
                addLocalMessage({ role: 'system', content: `Model changed to ${selectedModel} — session restarted for new context window (${result.old_turns} turns saved)` });
                await refreshChat();
                await refreshSessions();
            } else {
                addLocalMessage({ role: 'system', content: `Model switched to ${selectedModel} (mid-session, no restart needed)` });
            }
        } catch {
            try {
                await api('PUT', `/agents/${activeAgent}`, { model: selectedModel });
                addLocalMessage({ role: 'system', content: `Model set to ${selectedModel} (takes effect on next session)` });
            } catch (e) {
                alert(`Failed to update model: ${e.message}`);
            }
        }
        savingModel = false;
    }

    async function saveNudge() {
        if (!activeAgent) return;
        savingNudge = true;
        try {
            await api('PUT', `/agents/${activeAgent}`, { restart_threshold_pct: contextNudgePct });
        } catch (e) {
            alert(`Failed to update nudge: ${e.message}`);
        }
        savingNudge = false;
    }

    async function spawnAgentSession(agentName) {
        try {
            await api('POST', `/agents/${agentName}/streaming-sessions?label=chat`);
            await refreshSessions();
            if (activeSession !== `${agentName}-main`) {
                await selectSession(`${agentName}-main`, agentName);
            }
            addLocalMessage({ role: 'system', content: `Created auxiliary session ${agentName}-chat. Web chat remains on the main session.` });
        } catch (e) {
            addLocalMessage({ role: 'system', content: `New session failed: ${e.message}` });
        }
    }

    function handleGlobalClick() {
        if (restartDropdownOpen) restartDropdownOpen = false;
    }

    onMount(async () => {
        await refreshSessions();
        refreshInterval = setInterval(refreshSessions, 10000);
        document.addEventListener('click', handleGlobalClick);

        // Auto-select agent from route param (e.g. /chat/barsik)
        if (params?.agent && !activeSession) {
            const targetAgent = params.agent;
            const mainSessionId = `${targetAgent}-main`;
            const hasSession = sessionsList.some(s => s.id === mainSessionId);
            if (hasSession) {
                selectSession(mainSessionId, targetAgent);
            }
        }
    });

    onDestroy(() => {
        clearInterval(refreshInterval);
        stopChatPolling();
        stopActivityPolling();
        document.removeEventListener('click', handleGlobalClick);
    });
</script>

<div class="main">
    <button class="sidebar-toggle" on:click={() => sidebarCollapsed = !sidebarCollapsed}>
        {sidebarCollapsed ? $_('chat.show_agents') : $_('chat.hide_agents')}
    </button>
    <div class="sidebar" class:collapsed={sidebarCollapsed}>
        <div class="sidebar-header">{$_('chat.agents')}</div>
        <div class="sidebar-search">
            <input type="text" class="sidebar-search-input" placeholder="Search chats..." bind:value={chatSearchQuery} on:keydown={(e) => e.key === 'Enter' && searchChats()}>
        </div>
        <div class="session-list">
            {#each agentsList as agent}
                {@const aSessions = agentSessions.groups[agent.name] || []}
                <div class="agent-group">
                    <div class="agent-group-header" on:click={() => { if (aSessions.length > 0) selectSession(aSessions[0].id, agent.name); }}>
                        <span class="chat-working-dot" class:working={agent.working_status === 'working'} class:offline={agent.working_status === 'offline'} title={agent.working_status || 'idle'}></span>
                        <span class="agent-name-text">{agent.display_name || agent.name}</span>
                        <span class="agent-status-tag" class:status-working={agent.working_status === 'working'} class:status-offline={agent.working_status === 'offline'}>{agent.working_status === 'working' ? 'working' : agent.working_status === 'offline' ? 'offline' : 'idle'}</span>
                        <span class="agent-model">{(agent.model || '').replace(/^claude-/i, '')}</span>
                        <button class="btn-new" on:click|stopPropagation={() => spawnAgentSession(agent.name)}>+</button>
                    </div>
                    {#each aSessions as s}
                        {@const isMain = (s.session_type || '') === 'main'}
                        {@const label = s.id.replace(new RegExp(`^${agent.name}-`), '').replace(/-?main$/, '') || 'main'}
                        <div
                            class="session-item"
                            class:active={activeSession === s.id}
                            class:main-session={isMain}
                            on:click={() => selectSession(s.id, agent.name)}
                        >
                            <span class="session-label">{label}</span>
                            <span class="session-count">{s.message_count}</span>
                        </div>
                    {/each}
                </div>
            {/each}
            {#if agentSessions.orphans.length > 0}
                <div class="agent-group">
                    <div class="agent-group-header"><span style="color:var(--gray-mid)">{$_('chat.standalone')}</span></div>
                    {#each agentSessions.orphans as s}
                        <div class="session-item" class:active={activeSession === s.id} on:click={() => selectSession(s.id, null)}>
                            <span class="session-label">{s.id}</span>
                            <span class="session-count">{s.message_count}</span>
                        </div>
                    {/each}
                </div>
            {/if}
        </div>
    </div>

    <div class="chat-area">
        {#if !activeSession}
            <div class="empty-state">{$_('chat.select_agent')}</div>
        {:else}
            <div class="chat-info">
                <span class="info-context" class:warning={infoContextPct >= contextNudgePct}>{$_('chat.context')}: <strong>{infoContext}</strong></span>
                <span>{$_('chat.messages')}: <strong>{infoMessages}</strong></span>
                <span>{$_('chat.session')}: <strong>{infoSession}</strong></span>
                <div class="chat-actions">
                    <button class="btn-action" on:click={() => showSettings = !showSettings}>{$_('chat.model')}</button>
                    <button class="btn-action" on:click={() => showSessionInfo = !showSessionInfo}>info</button>
                    <div class="restart-group">
                        <button class="btn-restart" class:restarting on:click={contextRestart} disabled={restarting}>{restarting ? $_('chat.restarting') : $_('chat.context_restart')}</button>
                        <button class="btn-restart-chevron" class:open={restartDropdownOpen} on:click|stopPropagation={() => restartDropdownOpen = !restartDropdownOpen} disabled={restarting}>▾</button>
                        {#if restartDropdownOpen}
                            <!-- svelte-ignore a11y-click-events-have-key-events -->
                            <div class="restart-dropdown" on:click|stopPropagation={() => restartDropdownOpen = false}>
                                <button class="restart-dropdown-item" class:active-action={compacting} on:click={compactContext} disabled={compacting}>
                                    <span class="dropdown-icon">⊘</span> {compacting ? $_('chat.compacting') : $_('chat.compact')}
                                    <span class="dropdown-hint">Summarize old context</span>
                                </button>
                                <button class="restart-dropdown-item restart-dropdown-danger" class:active-action={archiving} on:click={archiveSession} disabled={archiving}>
                                    <span class="dropdown-icon">▣</span> {archiving ? $_('chat.archiving') : $_('chat.archive')}
                                    <span class="dropdown-hint">Save memory + fresh start</span>
                                </button>
                            </div>
                        {/if}
                    </div>
                    <button class="btn-action btn-stop-chat" on:click={stopActiveAgent} title="Force stop agent">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
                        stop
                    </button>
                </div>
            </div>
            {#if showSettings}
                <div class="settings-bar">
                    <label class="setting-item">
                        <span>{$_('chat.model')}</span>
                        {#if !selectedModel || selectedModel.startsWith('claude-')}
                        <select bind:value={selectedModel} on:change={saveModel} disabled={savingModel}>
                            {#each availableModels as m}
                                <option value={m.value}>{m.label}</option>
                            {/each}
                        </select>
                        {:else}
                        <span style="font-size:0.7rem;color:var(--text-primary);background:var(--surface-2);padding:0.25rem 0.4rem;border-radius:var(--radius-lg)">{selectedModel}</span>
                        {/if}
                    </label>
                    <label class="setting-item">
                        <span>{$_('chat.context_nudge')}</span>
                        <input type="number" min="10" max="95" step="5" bind:value={contextNudgePct} on:change={saveNudge} disabled={savingNudge}>
                    </label>
                </div>
            {/if}
            {#if showSessionInfo}
                <div class="session-info-panel">
                    <div class="session-info-row">
                        <span class="session-info-label">Session</span>
                        <span
                            class="session-info-value session-id-chip"
                            title={infoSession}
                            on:click={() => navigator.clipboard?.writeText(infoSession)}
                        >{infoSession.length > 24 ? infoSession.slice(0, 12) + '…' + infoSession.slice(-8) : infoSession}</span>
                    </div>
                    <div class="session-info-row">
                        <span class="session-info-label">Context</span>
                        <span class="session-info-value" class:session-info-warn={infoContextPct >= contextNudgePct}>{infoContext}</span>
                    </div>
                    <div class="session-info-row">
                        <span class="session-info-label">Model</span>
                        <span class="session-info-value">{infoModel}</span>
                    </div>
                    {#if streamingStats}
                        <div class="session-info-row">
                            <span class="session-info-label">Turns</span>
                            <span class="session-info-value">{streamingStats.turns || 0}</span>
                        </div>
                        <div class="session-info-row">
                            <span class="session-info-label">Cost</span>
                            <span class="session-info-value">${(streamingStats.cost_usd || 0).toFixed(2)}</span>
                        </div>
                        {#if streamingStats.messages_sent > 0}
                            <div class="session-info-row">
                                <span class="session-info-label">Msgs out</span>
                                <span class="session-info-value">{streamingStats.messages_sent}</span>
                            </div>
                        {/if}
                        {#if streamingStats.errors > 0}
                            <div class="session-info-row">
                                <span class="session-info-label">Errors</span>
                                <span class="session-info-value session-info-warn">{streamingStats.errors}</span>
                            </div>
                        {/if}
                        {#if streamingStats.auto_restarts > 0}
                            <div class="session-info-row">
                                <span class="session-info-label">Auto-restarts</span>
                                <span class="session-info-value">{streamingStats.auto_restarts}</span>
                            </div>
                        {/if}
                        {#if streamingStats.totalTokens}
                            <div class="session-info-row">
                                <span class="session-info-label">Tokens</span>
                                <span class="session-info-value">{(streamingStats.totalTokens / 1000).toFixed(1)}k / {(streamingStats.maxTokens / 1000).toFixed(0)}k</span>
                            </div>
                        {/if}
                    {/if}
                    {#if activeSessionRecord?.sdk_session_id}
                        <div class="session-info-row">
                            <span class="session-info-label">Resume ID</span>
                            <span
                                class="session-info-value session-id-chip"
                                title={activeSessionRecord.sdk_session_id}
                                on:click={() => navigator.clipboard?.writeText(activeSessionRecord.sdk_session_id)}
                            >{activeSessionRecord.sdk_session_id.slice(0, 16)}…</span>
                        </div>
                    {/if}
                    {#if activeSessionRecord?.restart_count > 0}
                        <div class="session-info-row">
                            <span class="session-info-label">Restarts</span>
                            <span class="session-info-value">{activeSessionRecord.restart_count}</span>
                        </div>
                    {/if}
                    {#if streamingStats?.categories?.length > 0 && streamingStats.maxTokens > 0}
                        {@const barCategories = streamingStats.categories.filter(c => c.tokens > 0 && c.name !== 'Free space')}
                        {@const totalUsed = barCategories.reduce((s, c) => s + c.tokens, 0)}
                        {@const maxT = streamingStats.maxTokens}
                        <div class="session-info-breakdown">
                            <div class="breakdown-bar-container">
                                <div class="breakdown-bar">
                                    <div class="breakdown-bar-fill">
                                        {#each barCategories as cat, i}
                                            {@const pct = (cat.tokens / maxT) * 100}
                                            {#if pct > 0.3}
                                                <div
                                                    class="breakdown-segment"
                                                    style="width: {pct}%; --seg-hue: {(i * 47 + 200) % 360}"
                                                    title="{cat.name}: {(cat.tokens / 1000).toFixed(1)}k ({pct.toFixed(1)}%)"
                                                ></div>
                                            {/if}
                                        {/each}
                                    </div>
                                    <div class="breakdown-nudge-line" style="left: {contextNudgePct}%" title="Restart nudge: {contextNudgePct}%"></div>
                                </div>
                                <span class="breakdown-bar-label">{((totalUsed / maxT) * 100).toFixed(0)}% used</span>
                            </div>
                            <div class="breakdown-legend">
                                {#each barCategories as cat, i}
                                    {@const pct = (cat.tokens / maxT) * 100}
                                    {#if pct > 0.3}
                                        <span class="breakdown-legend-item" title="{cat.tokens.toLocaleString()} tokens">
                                            <span class="legend-dot" style="--seg-hue: {(i * 47 + 200) % 360}"></span>
                                            {cat.name} <strong>{(cat.tokens / 1000).toFixed(1)}k</strong>
                                        </span>
                                    {/if}
                                {/each}
                            </div>
                        </div>
                    {/if}
                </div>
            {/if}
            <div class="messages" bind:this={messagesContainer} on:scroll={handleMessagesScroll}>
                {#if loadingOlder}
                    <div class="loading-older">{$_('chat.loading_older')}</div>
                {/if}
                {#if hasMore && !loadingOlder}
                    <div class="loading-older"><button class="btn-load-more" on:click={loadOlderMessages}>{$_('chat.load_older')}</button></div>
                {/if}
                {#each messages as msg, index (deriveMessageKey(msg, index))}
                    {#if !isHeartbeatMessage(msg)}
                    {@const parsed = msg.role === 'user' ? parseBrokerMessage(msg.content) : null}
                    <div class="message {msg.role}">
                        {#if msg.role === 'user'}
                            {#if parsed?.meta}
                                <div class="broker-content">{@html escapeHtml(parsed.content)}</div>
                                <details class="broker-meta">
                                    <summary>{parsed.meta.sender} {parsed.meta.type === 'group' ? `in ${parsed.meta.groupName}` : ''} via {parsed.meta.platform}</summary>
                                    <div class="broker-meta-detail">
                                        <span>▾ {parsed.meta.type} via {parsed.meta.platform}</span>
                                        {#if parsed.meta.timestamp}<span>Time: {parsed.meta.timestamp}</span>{/if}
                                        <span>Chat: {parsed.meta.sender} ({parsed.meta.chatId})</span>
                                        {#if parsed.meta.groupName}<span>Group: {parsed.meta.groupName}</span>{/if}
                                        {#if parsed.meta.msgId}<span>Msg ID: {parsed.meta.msgId}</span>{/if}
                                    </div>
                                </details>
                            {:else}
                                {@html escapeHtml(msg.content)}
                            {/if}
                        {:else if msg.role === 'system' && msg.metadata?.checkpoint}
                            <div class="checkpoint-divider checkpoint-{msg.metadata.checkpoint}">
                                <span class="checkpoint-icon">{msg.metadata.checkpoint === 'context-restart' ? '↻' : msg.metadata.checkpoint === 'compact' ? '⊘' : msg.metadata.checkpoint === 'archive' ? '▣' : '●'}</span>
                                {msg.content}
                            </div>
                        {:else if msg.role === 'system' && msg.metadata?.reaction}
                            <div class="reaction-row">
                                <span class="reaction-emoji">{msg.metadata.emoji}</span>
                                <span class="reaction-label">reacted</span>
                            </div>
                        {:else if msg.role === 'system'}
                            <div class="system-timeline-row">── {msg.content} ──</div>
                        {:else}
                            {#if msg.metadata?.thinking?.length}
                                <details class="thinking-meta">
                                    <summary class="thinking-summary">↳ thinking ({msg.metadata.thinking.length} block{msg.metadata.thinking.length > 1 ? 's' : ''})</summary>
                                    <div class="thinking-blocks">
                                        {#each msg.metadata.thinking as t}
                                            <div class="thinking-block">{t}</div>
                                        {/each}
                                    </div>
                                </details>
                            {/if}
                            {@html renderMarkdown(msg.content)}
                            {#if msg.metadata?.tool_uses?.length}
                                <details class="tool-meta">
                                    <summary>{msg.metadata.tool_uses.length} tool{msg.metadata.tool_uses.length > 1 ? 's' : ''} used</summary>
                                    <div class="tool-list">
                                        {#each msg.metadata.tool_uses as tu}
                                            <div class="tool-item" class:tool-error={tu.error}>
                                                <span class="tool-name">{tu.tool}</span>
                                                {#if tu.input && typeof tu.input === 'object'}
                                                    <span class="tool-input">{Object.entries(tu.input).map(([k,v]) => `${k}: ${String(v).slice(0,60)}`).join(', ')}</span>
                                                {/if}
                                            </div>
                                        {/each}
                                    </div>
                                </details>
                            {/if}
                            {#if msg.metadata?.cost_usd}
                                <div class="meta">${msg.metadata.cost_usd.toFixed(4)}</div>
                            {/if}
                            {#if msg.duration_ms}
                                <div class="meta">{(msg.duration_ms / 1000).toFixed(1)}s</div>
                            {/if}
                        {/if}
                        {#if msg.role !== 'system'}
                            <div class="msg-actions">
                                <button class="msg-action-btn" title="Copy" on:click|stopPropagation={() => navigator.clipboard?.writeText(msg.content)}>
                                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                                </button>
                                <button class="msg-action-btn" title="Reply" on:click|stopPropagation={() => setReplyTo(msg)}>
                                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/></svg>
                                </button>
                                <button class="msg-action-btn" title="Forward to agent" on:click|stopPropagation={() => openForwardModal(msg)}>
                                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 17 20 12 15 7"/><path d="M4 18v-2a4 4 0 0 1 4-4h12"/></svg>
                                </button>
                            </div>
                        {/if}
                    </div>
                    {/if}
                {/each}
                {#if thinking || agentWorking}
                    <div class="thinking-bubble">
                        <div class="thinking-dots-row">
                            <span class="dot"></span>
                            <span class="dot"></span>
                            <span class="dot"></span>
                        </div>
                        {#if activityLog.length > 0}
                            <div class="thinking-log">
                                {#each activityLog as entry, i}
                                    <div class="thinking-log-entry" class:current={i === activityLog.length - 1}>
                                        {entry}
                                    </div>
                                {/each}
                            </div>
                        {:else if thinkingActivity}
                            <div class="thinking-log">
                                <div class="thinking-log-entry current">{thinkingActivity}</div>
                            </div>
                        {/if}
                        {#if thinkingContent}
                            <div class="thinking-reasoning">
                                <span class="thinking-reasoning-label">thinking</span>
                                <span class="thinking-reasoning-text">{thinkingContent.length > 200 ? thinkingContent.slice(0, 200) + '…' : thinkingContent}</span>
                            </div>
                        {/if}
                    </div>
                {/if}
            </div>
            {#if replyTo}
                <div class="reply-bar">
                    <span class="reply-bar-label">↩ replying to {replyTo.role}</span>
                    <span class="reply-bar-content">{replyTo.content.slice(0, 100)}{replyTo.content.length > 100 ? '…' : ''}</span>
                    <button class="reply-bar-close" on:click={clearReply}>✕</button>
                </div>
            {/if}
            <div class="input-area">
                <input type="file" bind:this={fileInput} on:change={handleFileUpload} style="display:none">
                <button class="btn-upload" on:click={() => fileInput.click()} disabled={sending || !activeAgent} title={$_('chat.upload_file')}>
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                </button>
                <input type="text" bind:value={messageInput} placeholder={!activeSession ? $_('chat.select_agent') : canSendMessage ? $_('chat.type_message') : $_('chat.main_session_only')} on:keydown={handleKeydown} disabled={sending || !canSendMessage}>
                <button class="btn-send" on:click={sendMessage} disabled={sending || !canSendMessage}>
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
                </button>
            </div>
        {/if}
    </div>
</div>

<!-- Forward Modal -->
<Modal bind:show={showForwardModal} title="Forward Message" maxWidth="500px">
    <div class="forward-modal">
        <!-- To: field with chips -->
        <div class="forward-to">
            <span class="forward-to-label">To:</span>
            <div class="forward-to-field">
                {#each forwardChips as chip}
                    <span class="forward-chip">
                        {chip}
                        <button class="forward-chip-x" on:click={() => removeForwardChip(chip)}>x</button>
                    </span>
                {/each}
                <input
                    class="forward-to-input"
                    type="text"
                    bind:value={forwardSearch}
                    placeholder={forwardChips.length === 0 ? 'Type agent name...' : ''}
                    on:keydown={(e) => {
                        if (e.key === 'Backspace' && !forwardSearch && forwardChips.length > 0) {
                            removeForwardChip(forwardChips[forwardChips.length - 1]);
                        }
                        if (e.key === 'Enter' && forwardSuggestions.length > 0) {
                            e.preventDefault();
                            addForwardChip(forwardSuggestions[0]);
                        }
                    }}
                />
            </div>
            {#if forwardSuggestions.length > 0}
                <div class="forward-suggestions">
                    {#each forwardSuggestions as s}
                        <button class="forward-suggestion" on:click={() => addForwardChip(s)}>{s}</button>
                    {/each}
                </div>
            {/if}
        </div>

        <!-- Context -->
        <div class="forward-context-wrap">
            <textarea class="forward-context" bind:value={forwardContext} placeholder="Add instructions or context (optional)..." rows="3"></textarea>
        </div>

        <!-- Message preview -->
        <div class="forward-preview">
            <div class="forward-preview-label">Message</div>
            <div class="forward-preview-content">{forwardMessage?.content?.slice(0, 300)}{(forwardMessage?.content?.length || 0) > 300 ? '...' : ''}</div>
        </div>

        <div class="forward-actions">
            <button class="forward-cancel" on:click={() => showForwardModal = false}>Cancel</button>
            <button class="forward-send" on:click={sendForward} disabled={forwarding || forwardChips.length === 0}>
                {forwarding ? 'Sending...' : 'Forward'}
            </button>
        </div>
    </div>
</Modal>

<!-- Search Results Modal -->
<Modal bind:show={chatSearchOpen} title='Search: "{chatSearchQuery}"' maxWidth="700px">
    <div class="search-modal-body">
        {#if chatSearchResults.length === 0}
            <div class="search-modal-empty">No results found.</div>
        {:else}
            <div class="search-modal-count">{chatSearchResults.length} result{chatSearchResults.length !== 1 ? 's' : ''}</div>
            {#each chatSearchResults as r}
                {@const agentName = (r.session_id || '').split('-')[0]}
                {@const ts = r.timestamp ? new Date(r.timestamp * 1000) : null}
                <div class="search-modal-item" on:click={() => { selectSession(r.session_id, agentName || null); chatSearchOpen = false; }}>
                    <div class="search-modal-item-header">
                        <span class="search-modal-agent">{agentName || 'unknown'}</span>
                        <span class="search-modal-role badge-{r.role}">{r.role}</span>
                        {#if ts}
                            <span class="search-modal-time" title={ts.toLocaleString()}>{ts.toLocaleDateString()} {ts.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                        {/if}
                        <span class="search-modal-session">{r.session_id}</span>
                    </div>
                    <div class="search-modal-content">{(r.content || '').slice(0, 300)}</div>
                </div>
            {/each}
        {/if}
    </div>
</Modal>

<style>
    .main { display: flex; flex: 1; overflow: hidden; height: 100vh; height: 100dvh; }

    /* Session sidebar (inside main content) */
    .sidebar { width: 260px; display: flex; flex-direction: column; background: var(--surface-1); }
    .sidebar.collapsed { display: none; }
    .sidebar-header { padding: 0.8rem 1rem; background: var(--surface-2); font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; display: flex; justify-content: space-between; align-items: center; }
    .sidebar-search { padding: 0.3rem 0.5rem; }
    .sidebar-search-input { width: 100%; font-family: var(--font-body); font-size: 0.75rem; padding: 0.35rem 0.5rem; border: none; border-radius: var(--radius-lg); background: var(--surface-2); color: var(--text-primary); }
    .sidebar-search-input:focus { outline: 2px solid var(--primary-container); outline-offset: -2px; background: var(--input-focus-bg); }
    .search-modal-body { max-height: 70vh; overflow-y: auto; }
    .search-modal-empty { padding: 2rem; text-align: center; color: var(--text-muted); font-family: var(--font-grotesk); }
    .search-modal-count { padding: 0.3rem 0.5rem; font-family: var(--font-grotesk); font-size: 0.65rem; color: var(--text-muted); border-bottom: 1px solid var(--border); }
    .search-modal-item { padding: 0.6rem 0.8rem; border-bottom: 1px solid var(--border); cursor: pointer; transition: background 0.1s; }
    .search-modal-item:hover { background: var(--surface-2); }
    .search-modal-item-header { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.3rem; }
    .search-modal-agent { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--accent); }
    .search-modal-role { font-family: var(--font-grotesk); font-size: 0.55rem; font-weight: 600; text-transform: uppercase; padding: 0.1rem 0.35rem; border-radius: var(--radius); }
    .search-modal-role.badge-user { background: var(--primary-container); color: var(--on-primary-container); }
    .search-modal-role.badge-assistant { background: var(--surface-3); color: var(--text-secondary); }
    .search-modal-time { font-family: var(--font-mono); font-size: 0.6rem; color: var(--text-muted); }
    .search-modal-session { font-family: var(--font-mono); font-size: 0.55rem; color: var(--text-muted); opacity: 0.6; margin-left: auto; }
    .search-modal-content { font-size: 0.8rem; color: var(--text-secondary); line-height: 1.5; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; }
    .session-list { flex: 1; overflow-y: auto; padding: 0.3rem; }
    .agent-group { margin-bottom: 0.5rem; }
    .agent-group-header { padding: 0.3rem 0.6rem; font-family: var(--font-grotesk); font-size: 0.72rem; font-weight: 700; text-transform: uppercase; color: var(--text-secondary); cursor: pointer; display: flex; align-items: center; gap: 0.4rem; border-radius: var(--radius-lg); }
    .agent-group-header:hover { background: var(--surface-2); }
    .agent-name-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .agent-model { font-size: 0.58rem; color: var(--text-muted); font-weight: 400; font-family: var(--font-body); letter-spacing: 0.02em; flex-shrink: 0; }
    .btn-new { font-family: var(--font-grotesk); font-size: 0.65rem; font-weight: 700; padding: 0.15rem 0.45rem; background: var(--primary-container); color: var(--on-primary-container); border: none; border-radius: var(--radius); cursor: pointer; text-transform: uppercase; flex-shrink: 0; }
    .btn-new:hover { background: var(--primary); color: #fff; }
    .session-item { padding: 0.2rem 0.6rem 0.2rem 1rem; cursor: pointer; font-family: var(--font-grotesk); font-size: 0.68rem; border-radius: var(--radius-lg); margin-bottom: 1px; transition: background 0.1s; display: flex; align-items: center; justify-content: space-between; gap: 0.4rem; }
    .session-item:hover { background: var(--surface-2); }
    .session-item.active { background: var(--primary-container); color: var(--on-primary-container); }
    .session-item.main-session { border-left: 2px solid var(--yellow); }
    .session-label { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
    .session-count { font-size: 0.6rem; color: var(--text-muted); flex-shrink: 0; }
    .session-item.active .session-count { color: var(--on-primary-container); opacity: 0.65; }

    /* Chat area */
    .chat-area { flex: 1; display: flex; flex-direction: column; background: var(--app-bg); }
    .chat-info { padding: 0.6rem 1.5rem; background: var(--surface-1); font-family: var(--font-grotesk); font-size: 0.72rem; color: var(--text-muted); display: flex; align-items: center; gap: 1.5rem; }
    .chat-info span { display: flex; align-items: center; gap: 0.3rem; }
    .info-context.warning { color: var(--danger-outline); font-weight: 700; }
    .chat-actions { display: flex; gap: 0.3rem; margin-left: auto; align-items: center; }
    .btn-action { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; padding: 0.25rem 0.6rem; background: var(--surface-2); color: var(--text-muted); border: none; border-radius: var(--radius-lg); cursor: pointer; text-transform: uppercase; letter-spacing: 0.04em; transition: all 0.1s; }
    .btn-action:hover { color: var(--text-primary); background: var(--surface-3); }
    .btn-stop-chat { display: flex; align-items: center; gap: 0.3rem; }
    .btn-stop-chat:hover { color: var(--red); }
    .btn-action:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-action.active-action { color: var(--accent); background: var(--accent-soft); animation: pulse 1s infinite; }
    .btn-restart { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; padding: 0.25rem 0.6rem; background: var(--surface-2); color: var(--text-muted); border: none; border-radius: var(--radius-lg) 0 0 var(--radius-lg); cursor: pointer; text-transform: uppercase; letter-spacing: 0.04em; transition: all 0.1s; }
    .btn-restart:hover { color: var(--primary-container); background: var(--surface-inverse); }
    .btn-restart:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-restart.restarting { color: var(--accent); background: var(--accent-soft); animation: pulse 1s infinite; }
    .restart-group { position: relative; display: flex; align-items: stretch; }
    .btn-restart-chevron { font-family: var(--font-grotesk); font-size: 0.55rem; padding: 0.25rem 0.35rem; background: var(--surface-2); color: var(--text-muted); border: none; border-left: 1px solid var(--border); border-radius: 0 var(--radius-lg) var(--radius-lg) 0; cursor: pointer; transition: all 0.1s; }
    .btn-restart-chevron:hover { color: var(--text-primary); background: var(--surface-3); }
    .btn-restart-chevron.open { background: var(--surface-3); color: var(--text-primary); }
    .btn-restart-chevron:disabled { opacity: 0.4; cursor: not-allowed; }
    .restart-dropdown { position: absolute; top: 100%; right: 0; margin-top: 0.25rem; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-lg); box-shadow: 0 4px 12px rgba(0,0,0,0.3); z-index: 50; min-width: 200px; overflow: hidden; }
    .restart-dropdown-item { display: flex; align-items: center; gap: 0.4rem; width: 100%; padding: 0.5rem 0.75rem; font-family: var(--font-grotesk); font-size: 0.65rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; background: none; border: none; color: var(--text-secondary); cursor: pointer; transition: all 0.1s; text-align: left; flex-wrap: wrap; }
    .restart-dropdown-item:hover { background: var(--surface-3); color: var(--text-primary); }
    .restart-dropdown-item:disabled { opacity: 0.4; cursor: not-allowed; }
    .restart-dropdown-item.active-action { color: var(--accent); }
    .restart-dropdown-danger { color: var(--danger-outline); }
    .restart-dropdown-danger:hover { background: var(--red); color: #fff; }
    .dropdown-icon { font-size: 0.75rem; flex-shrink: 0; }
    .dropdown-hint { width: 100%; font-size: 0.55rem; font-weight: 400; color: var(--text-muted); text-transform: none; letter-spacing: 0; margin-top: -0.1rem; }
    .settings-bar { padding: 0.5rem 1.5rem; background: var(--surface-2); font-family: var(--font-grotesk); font-size: 0.7rem; display: flex; gap: 2rem; align-items: center; border-radius: 0 0 var(--radius-lg) var(--radius-lg); }
    .setting-item { display: flex; align-items: center; gap: 0.5rem; color: var(--text-secondary); }
    .setting-item select, .setting-item input { font-family: var(--font-body); font-size: 0.7rem; padding: 0.25rem 0.4rem; border: none; border-radius: var(--radius-lg); background: var(--input-bg); color: var(--text-primary); }
    .setting-item input[type="number"] { width: 4rem; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
    .session-info-panel { padding: 0.5rem 1.5rem; background: var(--surface-2); font-family: var(--font-grotesk); font-size: 0.68rem; display: flex; flex-wrap: wrap; gap: 0.4rem 1.5rem; align-items: center; border-top: 1px solid var(--border); }
    .session-info-row { display: flex; align-items: center; gap: 0.35rem; }
    .session-info-label { color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.6rem; }
    .session-info-value { color: var(--text-secondary); font-family: var(--font-mono); font-size: 0.65rem; }
    .session-info-warn { color: var(--danger-outline); font-weight: 700; }
    .session-id-chip { cursor: pointer; background: var(--surface-3); padding: 0.1rem 0.35rem; border-radius: var(--radius); transition: background 0.1s; }
    .session-id-chip:hover { background: var(--primary-container); color: var(--on-primary-container); }
    .session-info-breakdown { width: 100%; display: flex; flex-direction: column; gap: 0.3rem; margin-top: 0.2rem; padding-top: 0.3rem; border-top: 1px solid var(--border); }
    .breakdown-bar-container { display: flex; align-items: center; gap: 0.5rem; }
    .breakdown-bar { flex: 1; height: 8px; background: var(--surface-3); border-radius: 4px; position: relative; overflow: visible; }
    .breakdown-bar-fill { display: flex; height: 100%; border-radius: 4px; overflow: hidden; }
    .breakdown-segment { height: 100%; background: hsl(var(--seg-hue), 55%, 55%); transition: opacity 0.15s; min-width: 2px; }
    .breakdown-segment:hover { opacity: 0.75; }
    .breakdown-nudge-line { position: absolute; top: -2px; bottom: -2px; width: 0; border-right: 2px dashed var(--red, #ef4444); pointer-events: auto; cursor: default; z-index: 1; }
    .breakdown-bar-label { font-family: var(--font-mono); font-size: 0.58rem; color: var(--text-muted); white-space: nowrap; flex-shrink: 0; }
    .breakdown-legend { display: flex; flex-wrap: wrap; gap: 0.15rem 0.6rem; }
    .breakdown-legend-item { font-family: var(--font-mono); font-size: 0.55rem; color: var(--text-muted); display: flex; align-items: center; gap: 0.2rem; cursor: default; }
    .breakdown-legend-item strong { color: var(--text-secondary); font-weight: 600; }
    .legend-dot { width: 6px; height: 6px; border-radius: 2px; background: hsl(var(--seg-hue), 55%, 55%); flex-shrink: 0; }

    /* Messages */
    .messages { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 1.5rem 2rem; display: flex; flex-direction: column; gap: 1rem; min-width: 0; }
    .loading-older { text-align: center; padding: 0.5rem; font-size: 0.8rem; color: var(--text-muted); font-family: var(--font-grotesk); }
    .btn-load-more { background: none; border: 1px solid var(--border); padding: 0.3rem 1rem; border-radius: var(--radius); cursor: pointer; font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted); }
    .btn-load-more:hover { background: var(--surface-1); color: var(--text-primary); }
    .message { max-width: 75%; min-width: 0; padding: 1rem 1.2rem; line-height: 1.6; font-size: 0.95rem; overflow-wrap: break-word; word-break: break-word; border-radius: var(--radius-lg); }
    .message.user { align-self: flex-end; background: var(--primary-container); color: var(--on-primary-container); box-shadow: 4px 4px 0px rgba(0,0,0,0.1); }
    .message.assistant { align-self: flex-start; background: var(--surface-1); box-shadow: 4px 4px 0px var(--shadow-color); }
    .message .meta { font-family: var(--font-grotesk); font-size: 0.65rem; color: var(--text-muted); margin-top: 0.5rem; }
    .thinking-meta { margin-bottom: 0.5rem; font-family: var(--font-grotesk); font-size: 0.65rem; }
    .thinking-summary { color: var(--text-muted); cursor: pointer; user-select: none; letter-spacing: 0.03em; }
    .thinking-summary:hover { color: var(--text-secondary); }
    .thinking-blocks { margin-top: 0.4rem; display: flex; flex-direction: column; gap: 0.4rem; border-left: 2px solid var(--surface-3); padding-left: 0.6rem; }
    .thinking-block { font-family: var(--font-body); font-size: 0.75rem; color: var(--text-muted); line-height: 1.55; white-space: pre-wrap; }
    .tool-meta { margin-top: 0.5rem; font-family: var(--font-grotesk); font-size: 0.65rem; }
    .tool-meta summary { color: var(--text-muted); cursor: pointer; user-select: none; }
    .tool-meta summary:hover { color: var(--text-primary); }
    .tool-list { display: flex; flex-direction: column; gap: 0.2rem; margin-top: 0.3rem; }
    .tool-item { display: flex; gap: 0.4rem; align-items: baseline; color: var(--text-secondary); font-size: 0.6rem; }
    .tool-name { font-weight: 700; color: var(--tone-neutral-text); background: var(--tone-neutral-bg); padding: 0 0.3rem; border-radius: var(--radius); }
    .tool-input { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 400px; }
    .tool-error .tool-name { background: var(--tone-error-bg); color: var(--tone-error-text); }
    .broker-meta { margin-top: 0.4rem; font-family: var(--font-grotesk); font-size: 0.65rem; }
    .broker-meta summary { color: var(--on-primary-container); opacity: 0.5; cursor: pointer; user-select: none; }
    .broker-meta summary:hover { opacity: 0.8; }
    .broker-meta-detail { display: flex; flex-direction: column; gap: 0.15rem; margin-top: 0.3rem; color: var(--on-primary-container); opacity: 0.6; font-size: 0.6rem; }
    .message { position: relative; }
    .msg-actions { display: flex; gap: 0.3rem; margin-top: 0.3rem; opacity: 0; transition: opacity 0.15s; }
    .message:hover .msg-actions { opacity: 1; }
    .msg-action-btn { background: var(--surface-2); border: none; border-radius: var(--radius); cursor: pointer; padding: 0.2rem 0.35rem; color: var(--text-muted); display: flex; align-items: center; transition: all 0.1s; }
    .msg-action-btn:hover { background: var(--primary-container); color: var(--on-primary-container); }
    .reply-bar { display: flex; align-items: center; gap: 0.5rem; padding: 0.4rem 2rem; background: var(--surface-2); border-left: 3px solid var(--accent); font-family: var(--font-grotesk); font-size: 0.7rem; }
    .reply-bar-label { font-weight: 700; text-transform: uppercase; font-size: 0.6rem; color: var(--accent); flex-shrink: 0; }
    .reply-bar-content { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); }
    .reply-bar-close { background: none; border: none; color: var(--text-muted); cursor: pointer; font-size: 0.8rem; padding: 0.1rem 0.3rem; flex-shrink: 0; }
    .reply-bar-close:hover { color: var(--text-primary); }
    .message.system { align-self: center; font-family: var(--font-grotesk); font-size: 0.75rem; color: var(--text-muted); padding: 0.5rem; }
    .system-timeline-row { text-align: center; color: var(--text-muted); font-family: var(--font-grotesk); font-size: 0.7rem; letter-spacing: 0.04em; padding: 0.1rem 0; }
    .reaction-row { display: flex; align-items: center; gap: 0.3rem; font-family: var(--font-grotesk); font-size: 0.75rem; }
    .reaction-emoji { font-size: 1rem; }
    .reaction-label { color: var(--text-muted); font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.04em; }
    .chat-working-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--green); flex-shrink: 0; display: inline-block; margin-right: 0.3rem; vertical-align: middle; }
    .chat-working-dot.working { background: var(--green); box-shadow: 0 0 5px rgba(74,222,128,0.5); animation: working-pulse 1.5s ease-in-out infinite; }
    .chat-working-dot.offline { background: var(--text-muted); opacity: 0.5; }
    .agent-status-tag { font-size: 0.5rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--green); flex-shrink: 0; }
    .agent-status-tag.status-working { color: var(--green); }
    .agent-status-tag.status-offline { color: var(--text-muted); opacity: 0.5; }
    @keyframes working-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
    .checkpoint-divider { display: flex; align-items: center; gap: 0.5rem; width: 100%; padding: 0.4rem 1rem; font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; border-radius: var(--radius-lg); margin: 0.5rem 0; }
    .checkpoint-icon { font-size: 0.9rem; }
    .checkpoint-context-restart { color: var(--accent-contrast); background: var(--accent-soft); }
    .checkpoint-compact { color: var(--tone-info-text); background: var(--tone-info-bg); }
    .checkpoint-archive { color: var(--tone-error-text); background: var(--tone-error-bg); }
    .empty-state { flex: 1; display: flex; align-items: center; justify-content: center; font-family: var(--font-grotesk); color: var(--text-muted); font-size: 0.9rem; }
    .thinking-bubble { align-self: flex-start; background: var(--surface-1); box-shadow: 4px 4px 0px var(--shadow-color); border-radius: var(--radius-lg); padding: 0.85rem 1.2rem; display: flex; flex-direction: column; gap: 0.4rem; }
    .thinking-reasoning { display: flex; gap: 0.4rem; margin-top: 0.4rem; align-items: flex-start; }
    .thinking-reasoning-label { font-size: 0.6rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-subtle); white-space: nowrap; padding-top: 1px; }
    .thinking-reasoning-text { font-size: 0.72rem; color: var(--text-muted); font-style: italic; line-height: 1.4; opacity: 0.8; }
    .thinking-dots-row { display: flex; align-items: center; gap: 5px; height: 18px; }
    .thinking-dots-row .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); opacity: 0.5; animation: dot-bounce 1.2s ease-in-out infinite; }
    .thinking-dots-row .dot:nth-child(2) { animation-delay: 0.2s; }
    .thinking-dots-row .dot:nth-child(3) { animation-delay: 0.4s; }
    @keyframes dot-bounce { 0%, 60%, 100% { transform: translateY(0); opacity: 0.4; } 30% { transform: translateY(-6px); opacity: 1; } }
    .thinking-activity { font-family: var(--font-grotesk); font-size: 0.68rem; color: var(--text-muted); max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .thinking-log { display: flex; flex-direction: column; gap: 0.15rem; margin-top: 0.1rem; max-width: 300px; }
    .thinking-log-entry { font-family: var(--font-grotesk); font-size: 0.67rem; color: var(--text-subtle); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; line-height: 1.4; transition: color 0.2s; }
    .thinking-log-entry.current { color: var(--text-muted); font-weight: 600; }

    /* Markdown in messages */
    .message :global(code) { font-family: monospace; font-size: 0.82em; padding: 0.2em 0.5em; border-radius: var(--radius); word-break: break-word; }
    .message.assistant :global(code) { background: var(--code-inline-bg); color: var(--text-primary); }
    .message.user :global(code) { background: rgba(0,0,0,0.12); color: var(--on-primary-container); }
    .message :global(pre) { margin: 0.8rem 0; padding: 1.2rem 1.4rem; overflow-x: auto; font-family: monospace; font-size: 0.82rem; line-height: 1.6; position: relative; border-radius: var(--radius-lg); }
    .message.assistant :global(pre) { background: var(--code-pre-bg); color: var(--code-pre-text); border-left: 4px solid var(--accent); }
    .message.user :global(pre) { background: rgba(0,0,0,0.2); color: var(--on-primary-container); border-left: 4px solid rgba(0,0,0,0.2); }
    .message :global(pre code) { background: none !important; padding: 0 !important; color: inherit !important; font-size: inherit; }
    .message :global(pre .lang-label) { position: absolute; top: 0; right: 0; font-size: 0.65rem; padding: 0.2rem 0.6rem; background: var(--accent); color: var(--accent-contrast); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; border-radius: 0 var(--radius-lg) 0 var(--radius); }
    .message :global(strong) { font-weight: 700; }
    .message :global(em) { font-style: italic; }
    .message :global(ul), .message :global(ol) { margin: 0.5rem 0; padding-left: 1.5rem; }
    .message :global(li) { margin-bottom: 0.3rem; line-height: 1.5; }
    .message :global(p) { margin-bottom: 0.5rem; }
    .message :global(p:last-child) { margin-bottom: 0; }
    .message :global(blockquote) { border-left: 3px solid var(--accent); padding-left: 0.8rem; margin: 0.5rem 0; color: var(--text-secondary); font-style: italic; }
    .message :global(a) { color: var(--link-chip-text); background: var(--link-chip-bg); padding: 0 0.2em; border-radius: var(--radius); }
    .message :global(table) { border-collapse: collapse; margin: 0.8rem 0; font-size: 0.88rem; width: 100%; }
    .message :global(thead th) { font-family: var(--font-grotesk); font-weight: 700; text-align: left; padding: 0.5rem 0.8rem; background: var(--surface-2); font-size: 0.82rem; text-transform: uppercase; }
    .message :global(tbody td) { padding: 0.4rem 0.8rem; }
    .message :global(tbody tr:nth-child(even) td) { background: var(--surface-1); }
    .message :global(tbody tr:hover td) { background: var(--hover-accent); }

    /* Input area */
    .input-area { padding: 0.6rem 1rem; padding-bottom: calc(0.6rem + env(safe-area-inset-bottom, 0px)); background: var(--surface-1); display: flex; gap: 0.5rem; align-items: center; }
    .input-area input { flex: 1; font-family: var(--font-body); font-size: 1rem; padding: 0.7rem 1rem; border: none; border-radius: var(--radius-lg); outline: none; background: var(--input-bg); color: var(--text-primary); }
    .input-area input:focus { outline: 2px solid var(--primary-container); outline-offset: -2px; background: var(--input-focus-bg); }
    .btn-upload { background: none; border: none; cursor: pointer; padding: 0.5rem; display: flex; align-items: center; justify-content: center; color: var(--text-muted); transition: color 0.15s; border-radius: var(--radius-md); }
    .btn-upload:hover { color: var(--text-primary); background: var(--surface-2); }
    .btn-upload:disabled { color: var(--text-muted); opacity: 0.4; cursor: not-allowed; }
    .btn-send { background: var(--primary-container); border: none; cursor: pointer; padding: 0.55rem; display: flex; align-items: center; justify-content: center; color: var(--on-primary-container); border-radius: var(--radius-md); transition: all 0.1s; }
    .btn-send:hover { background: var(--primary); }
    .btn-send:active { transform: scale(0.95); }
    .btn-send:disabled { background: var(--surface-2); color: var(--text-muted); cursor: not-allowed; }

    .sidebar-toggle { display: none; width: 100%; padding: 0.5rem; font-family: var(--font-grotesk); font-size: 0.7rem; text-align: center; background: var(--surface-2); border: none; cursor: pointer; text-transform: uppercase; color: var(--text-muted); }

    /* Forward modal */
    .forward-modal { display: flex; flex-direction: column; gap: 0.8rem; }
    .forward-to { position: relative; }
    .forward-to-label { font-family: var(--font-grotesk); font-size: 0.65rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.04em; margin-right: 0.4rem; }
    .forward-to-field { display: flex; flex-wrap: wrap; align-items: center; gap: 0.3rem; padding: 0.4rem 0.6rem; border: 1px solid var(--border); border-radius: var(--radius); background: var(--input-bg); min-height: 2.2rem; margin-top: 0.3rem; }
    .forward-to-field:focus-within { outline: 2px solid var(--primary-container); outline-offset: -2px; }
    .forward-chip { display: inline-flex; align-items: center; gap: 0.25rem; background: var(--primary-container); color: var(--on-primary-container); font-family: var(--font-grotesk); font-size: 0.72rem; font-weight: 600; padding: 0.15rem 0.5rem; border-radius: 999px; }
    .forward-chip-x { background: none; border: none; color: var(--on-primary-container); cursor: pointer; font-size: 0.7rem; padding: 0 0.1rem; opacity: 0.6; }
    .forward-chip-x:hover { opacity: 1; }
    .forward-to-input { flex: 1; min-width: 80px; border: none; background: transparent; font-family: var(--font-body); font-size: 0.85rem; color: var(--text-primary); outline: none; }
    .forward-suggestions { position: absolute; top: 100%; left: 0; right: 0; z-index: 10; margin-top: 2px; background: var(--surface-1); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 4px 12px rgba(0,0,0,0.3); overflow: hidden; }
    .forward-suggestion { display: block; width: 100%; text-align: left; padding: 0.45rem 0.8rem; font-family: var(--font-grotesk); font-size: 0.8rem; background: none; border: none; color: var(--text-primary); cursor: pointer; }
    .forward-suggestion:hover { background: var(--surface-2); }
    .forward-preview { background: var(--surface-2); border-radius: var(--radius); padding: 0.6rem 0.8rem; }
    .forward-preview-label { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.3rem; letter-spacing: 0.04em; }
    .forward-preview-content { font-size: 0.8rem; color: var(--text-secondary); white-space: pre-wrap; word-break: break-word; max-height: 120px; overflow-y: auto; }
    .forward-context { width: 100%; font-family: var(--font-body); font-size: 0.85rem; padding: 0.5rem 0.7rem; border: 1px solid var(--border); border-radius: var(--radius); background: var(--input-bg); color: var(--text-primary); resize: vertical; }
    .forward-context:focus { outline: 2px solid var(--primary-container); outline-offset: -2px; }
    .forward-actions { display: flex; justify-content: flex-end; gap: 0.5rem; }
    .forward-cancel { font-family: var(--font-grotesk); font-size: 0.75rem; padding: 0.4rem 1rem; background: var(--surface-2); color: var(--text-muted); border: none; border-radius: var(--radius); cursor: pointer; }
    .forward-cancel:hover { color: var(--text-primary); }
    .forward-send { font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; padding: 0.4rem 1.2rem; background: var(--primary-container); color: var(--on-primary-container); border: none; border-radius: var(--radius); cursor: pointer; text-transform: uppercase; }
    .forward-send:hover { background: var(--primary); }
    .forward-send:disabled { opacity: 0.4; cursor: not-allowed; }

    @media (max-width: 768px) {
        .main { flex-direction: column; height: 100dvh; overflow: hidden; }
        .sidebar { width: 100%; max-height: 40vh; overflow-y: auto; flex-shrink: 0; }
        .sidebar.collapsed { max-height: 0; overflow: hidden; display: flex; }
        .sidebar-toggle { display: block; flex-shrink: 0; }
        .chat-area { flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
        .messages { flex: 1; overflow-y: auto; padding: 1rem; min-height: 0; }
        .input-area { flex-shrink: 0; padding: 0.5rem 0.8rem; padding-bottom: calc(0.5rem + env(safe-area-inset-bottom, 0px)); z-index: 10; gap: 0.4rem; }
        .input-area input { font-size: 16px; }
        .chat-info { padding: 0.5rem 1rem; gap: 0.8rem; flex-wrap: wrap; font-size: 0.65rem; flex-shrink: 0; }
        .chat-actions { gap: 0.3rem; flex-wrap: wrap; }
        .settings-bar { padding: 0.5rem 1rem; gap: 1rem; flex-wrap: wrap; font-size: 0.65rem; flex-shrink: 0; }
    }
</style>
