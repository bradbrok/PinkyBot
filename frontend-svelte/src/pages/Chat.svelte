<script>
    import { onMount, onDestroy, tick } from 'svelte';
    import { _ } from 'svelte-i18n';
    import Modal from '../components/Modal.svelte';
    import ChatSidebar from '../components/ChatSidebar.svelte';
    import ChatMessage from '../components/ChatMessage.svelte';
    import ChatInput from '../components/ChatInput.svelte';
    import { api } from '../lib/api.js';
    import {
        parseBrokerMessage, groupByAgent, sortMessages,
        latestAssistantTimestamp, userContentMatches,
        deriveMessageKey, isHeartbeatMessage,
    } from '../lib/chatUtils.js';

    export let params = {};

    // ── State ──────────────────────────────────────────────

    let agentsList = [];
    let sessionsList = [];
    let activeSession = null;
    let activeAgent = null;
    let renamingSession = null;
    let renameValue = '';
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
    let streamingStats = null;
    let sessionMeta = null;
    let sessionMetaInterval = null;

    // Chat search
    let chatSearchQuery = '';
    let chatSearchResults = [];
    let chatSearchOpen = false;

    // Reply-to / quote
    let replyTo = null;

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
    let showNewSessionModal = false;
    let creatingSession = false;
    let newSessionAgent = '';
    let newSessionName = '';
    let newSessionError = '';
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
    let sessionCache = {};

    // ── Reactive Declarations ──────────────────────────────

    $: agentSessions = groupByAgent(agentsList, sessionsList);
    $: activeSessionRecord = sessionsList.find((session) => session.id === activeSession) || null;
    $: messages = sortMessages([...persistedMessages, ...localMessages]);
    $: activeMainSession = activeAgent ? `${activeAgent}-main` : null;
    $: isStreamingSession = !!activeAgent && (activeSession === activeMainSession || (activeSessionRecord && activeSessionRecord._from_store === false));
    $: canUseStreamingChat = !!activeAgent && isStreamingSession;
    $: canUseLegacySessionChat = !!activeSessionRecord && !activeSessionRecord._from_store && !canUseStreamingChat;
    $: canSendMessage = !!activeSession && (canUseStreamingChat || canUseLegacySessionChat);

    // ── Local Message Helpers ──────────────────────────────

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

    // ── Session Cache ──────────────────────────────────────

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
                totalMessages, loadedPersistedCount, hasMore,
                currentHistorySource: { ...currentHistorySource },
                infoMessages, infoSession, infoModel, infoContext, infoContextPct,
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

    // ── Scroll Management ──────────────────────────────────

    function isNearBottom() {
        if (!messagesContainer) return true;
        return messagesContainer.scrollHeight - messagesContainer.scrollTop - messagesContainer.clientHeight < 80;
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

        if (forceBottom) { scrollToBottom(); return; }
        if (!snapshot) return;
        if (snapshot.mode === 'prepend') {
            messagesContainer.scrollTop = messagesContainer.scrollHeight - snapshot.height + snapshot.top;
            return;
        }
        if (snapshot.nearBottom) { scrollToBottom(); return; }
        messagesContainer.scrollTop = snapshot.top;
    }

    function scrollToBottom() {
        if (messagesContainer) messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }

    // ── Session & Chat Data ────────────────────────────────

    async function refreshSessions() {
        try {
            const [agentsData, sessData, convsData] = await Promise.all([
                api('GET', '/agents'),
                api('GET', '/sessions'),
                api('GET', '/conversations'),
            ]);

            agentsList = agentsData.agents || [];
            const agentNames = agentsList.map((a) => a.name);

            // Fetch streaming sub-sessions per agent
            const streamingSessionsByAgent = {};
            await Promise.all(agentNames.map(async (name) => {
                try {
                    const data = await api('GET', `/agents/${name}/streaming-sessions`);
                    streamingSessionsByAgent[name] = data.sessions || [];
                } catch {
                    streamingSessionsByAgent[name] = [];
                }
            }));

            const sessIds = new Set((sessData || []).map((s) => s.id));
            const convSessions = (convsData.conversations || [])
                .filter((c) => !sessIds.has(c.session_id))
                .map((c) => ({
                    id: c.session_id,
                    state: 'streaming',
                    model: 'streaming',
                    message_count: c.message_count,
                    last_active: c.last_message_at,
                    agent_name: c.session_id.split('-')[0],
                    session_type: 'streaming',
                    _from_store: true,
                }));

            // Build live streaming lookup and merge into existing records
            const liveStreamingById = {};
            for (const [agentName, sessions] of Object.entries(streamingSessionsByAgent)) {
                for (const ss of sessions) {
                    liveStreamingById[`${agentName}-${ss.label}`] = {
                        agentName, label: ss.label, connected: ss.connected,
                    };
                }
            }

            const allSessionIds = new Set([...sessIds, ...convSessions.map((s) => s.id)]);
            const merged = [...sessData, ...convSessions].map((s) => {
                const live = liveStreamingById[s.id];
                if (live) {
                    return { ...s, _from_store: false, _streaming_label: live.label, state: live.connected ? 'streaming' : 'disconnected' };
                }
                return s;
            });

            // Add streaming sessions not yet in the list
            const streamingSessions = [];
            for (const [sessionId, live] of Object.entries(liveStreamingById)) {
                if (!allSessionIds.has(sessionId)) {
                    streamingSessions.push({
                        id: sessionId,
                        state: live.connected ? 'streaming' : 'disconnected',
                        model: 'streaming',
                        message_count: 0,
                        last_active: null,
                        agent_name: live.agentName,
                        session_type: live.label === 'main' ? 'main' : 'streaming',
                        _from_store: false,
                        _streaming_label: live.label,
                    });
                }
            }

            sessionsList = [...merged, ...streamingSessions];
            connected = true;
        } catch {
            connected = false;
        }
    }

    function getConversationTargets(sessionId, agentName) {
        const mainSessionId = agentName ? `${agentName}-main` : null;
        const sessionRecord = sessionsList.find((s) => s.id === sessionId);
        const isStreamingSub = sessionRecord && sessionRecord._from_store === false && sessionId !== mainSessionId;
        return {
            preferred: sessionId,
            fallback: isStreamingSub ? null : (mainSessionId && mainSessionId !== sessionId ? mainSessionId : null),
        };
    }

    async function refreshChat({ preserveScroll = true } = {}) {
        if (!activeSession) return;

        const requestSeq = ++chatRefreshSeq;
        const sessionId = activeSession;
        const agentName = activeAgent || sessionId.split('-')[0];
        const sessionRecord = sessionsList.find((s) => s.id === sessionId) || null;
        const { preferred, fallback } = getConversationTargets(sessionId, agentName);

        let nextPersisted = [];
        let nextTotal = 0;
        let nextHasMore = false;
        let nextSource = { kind: null, sessionId: null };
        let loadedFromConversation = false;

        // Try preferred conversation history
        try {
            const history = await api('GET', `/conversations/${preferred}/history?limit=${PAGE_SIZE}`);
            if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;
            if ((history.total || 0) > 0 || preferred === fallback || sessionRecord?._from_store) {
                nextPersisted = sortMessages(history.messages || []);
                nextTotal = history.total || nextPersisted.length;
                nextHasMore = nextTotal > nextPersisted.length;
                nextSource = { kind: 'conversation', sessionId: preferred };
                loadedFromConversation = true;
            }
        } catch { /* fall through */ }

        // Try fallback
        if (!loadedFromConversation && fallback) {
            try {
                const history = await api('GET', `/conversations/${fallback}/history?limit=${PAGE_SIZE}`);
                if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;
                if ((history.total || 0) > 0) {
                    nextPersisted = sortMessages(history.messages || []);
                    nextTotal = history.total || nextPersisted.length;
                    nextHasMore = nextTotal > nextPersisted.length;
                    nextSource = { kind: 'conversation', sessionId: fallback };
                    loadedFromConversation = true;
                }
            } catch { /* fall through */ }
        }

        // Fall back to in-memory session history
        if (!loadedFromConversation) {
            try {
                const history = await api('GET', `/sessions/${sessionId}/history?limit=${PAGE_SIZE}`);
                if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;
                nextPersisted = sortMessages(history.messages || []);
                nextTotal = nextPersisted.length;
                nextSource = { kind: 'session', sessionId };
            } catch {
                if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;
                nextPersisted = [];
                nextTotal = 0;
                nextSource = { kind: null, sessionId: null };
            }
        }

        // Merge session events (agent-level covers all sessions + lifecycle events)
        try {
            const eventsData = await api('GET', `/agents/${agentName}/session-events?limit=20`);
            if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;
            const eventMessages = (eventsData.events || []).map(e => {
                const labels = {
                    context_restart: '\u21BB Context restarted',
                    session_resumed: '\u25B6 Session resumed',
                    session_resume: '\u25B6 Session resumed',
                    idle_sleep: '\uD83D\uDCA4 Idle sleep',
                    compact: '\u2298 Context compacted',
                    archive: '\u25A3 Archived',
                    wake: '\u2600 Wake',
                    agent_started: '\u25CF Agent started',
                    agent_stopped: '\u25CB Agent stopped',
                    session_start: '\u25CF Session started',
                    session_end: '\u25A0 Session ended',
                };
                const meta = typeof e.metadata === 'string' ? JSON.parse(e.metadata || '{}') : (e.metadata || {});
                const detail = e.detail || meta.reason || meta.summary || '';
                return {
                    role: 'system',
                    content: (labels[e.event_type] || `\u25CF ${e.event_type}`) + (detail ? ` \u2014 ${detail}` : ''),
                    timestamp: e.created_at,
                    metadata: { session_event: true, event_type: e.event_type },
                };
            });
            if (eventMessages.length > 0) {
                nextPersisted = sortMessages([...nextPersisted, ...eventMessages]);
            }
        } catch { /* non-critical */ }

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

        // Resolve pending reply
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

        // Fetch agent info + streaming context
        try {
            const agentData = await api('GET', `/agents/${agentName}`);
            if (requestSeq !== chatRefreshSeq || sessionId !== activeSession) return;
            if (agentData.model && !selectedModel) selectedModel = agentData.model;
            if (agentData.restart_threshold_pct != null) contextNudgePct = agentData.restart_threshold_pct;
            if (agentData.model) infoModel = agentData.model;
        } catch { /* non-critical */ }

        let gotStreamingContext = false;
        try {
            const refreshLabel = activeSessionRecord?._streaming_label || sessionId?.split('-').slice(1).join('-') || 'main';
            const streamStatus = await api('GET', `/agents/${agentName}/streaming/status?label=${encodeURIComponent(refreshLabel)}`);
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
        } catch { /* fallback below */ }

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
        } catch { /* best effort */ }
        loadingOlder = false;
    }

    function handleMessagesScroll() {
        if (!messagesContainer) return;
        if (messagesContainer.scrollTop < 200 && hasMore && !loadingOlder) loadOlderMessages();
    }

    // ── Polling ────────────────────────────────────────────

    function startChatPolling() {
        stopChatPolling();
        const pollSessionId = activeSession;
        chatPollInterval = setInterval(() => {
            if (pollSessionId !== activeSession) return;
            refreshChat();
        }, 3000);
    }

    function stopChatPolling() {
        if (chatPollInterval) { clearInterval(chatPollInterval); chatPollInterval = null; }
    }

    function stopActivityPolling() {
        if (activityPollInterval) { clearInterval(activityPollInterval); activityPollInterval = null; }
    }

    // ── Session Meta Polling ──────────────────────────────

    function formatUptime(seconds) {
        if (seconds == null) return '--';
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        if (h > 0) return `${h}h ${m}m`;
        if (m > 0) return `${m}m`;
        return `${Math.floor(seconds)}s`;
    }

    async function fetchSessionMeta() {
        if (!activeAgent || !activeSession) { sessionMeta = null; return; }
        try {
            const label = activeSessionRecord?._streaming_label || activeSession?.split('-').slice(1).join('-') || 'main';
            sessionMeta = await api('GET', `/agents/${activeAgent}/session-meta?label=${encodeURIComponent(label)}`);
        } catch { sessionMeta = null; }
    }

    function startSessionMetaPolling() {
        stopSessionMetaPolling();
        fetchSessionMeta();
        sessionMetaInterval = setInterval(fetchSessionMeta, 5000);
    }

    function stopSessionMetaPolling() {
        if (sessionMetaInterval) { clearInterval(sessionMetaInterval); sessionMetaInterval = null; }
    }

    $: if (showSessionInfo && activeAgent && activeSession) {
        startSessionMetaPolling();
    } else {
        stopSessionMetaPolling();
        if (!showSessionInfo) sessionMeta = null;
    }

    function startActivityPolling() {
        stopActivityPolling();
        const pollSessionId = activeSession;
        const pollAgentName = activeAgent || (activeSession ? activeSession.split('-')[0] : null);
        if (!pollAgentName) return;
        const pollRecord = sessionsList.find((s) => s.id === pollSessionId);
        const pollLabel = pollRecord?._streaming_label || pollSessionId?.split('-').slice(1).join('-') || 'main';

        activityPollInterval = setInterval(async () => {
            if (pollSessionId !== activeSession) return;
            try {
                const status = await api('GET', `/agents/${pollAgentName}/streaming/status?label=${encodeURIComponent(pollLabel)}`);
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
            } catch { /* transient */ }
        }, 1000);
    }

    // ── Session Selection ──────────────────────────────────

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

    // ── Send Message ───────────────────────────────────────

    async function sendMessage() {
        if (!canSendMessage || !messageInput.trim() || sending) return;

        let text = messageInput.trim();
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
            if (pendingReply) { pendingReply = null; thinking = false; thinkingActivity = ''; thinkingContent = ''; activityLog = []; }
            pendingReplyTimer = null;
        }, 60000);

        captureScroll('append');
        localMessages = [...localMessages, buildLocalMessage({ role: 'user', content: text, _localKind: 'pending-user', _sentAt: sentAt })];
        await restoreScroll({ forceBottom: true });

        try {
            if (canUseStreamingChat) {
                const sessionLabel = activeSessionRecord?._streaming_label || activeSession?.split('-').slice(1).join('-') || 'main';
                const sessionParam = sessionLabel !== 'main' ? `?session=${encodeURIComponent(sessionLabel)}` : '';
                await api('POST', `/agents/${activeAgent}/chat${sessionParam}`, { content: text });
                sending = false;
                await refreshChat();
            } else if (canUseLegacySessionChat) {
                const data = await api('POST', `/sessions/${sessionId}/message`, { content: text });
                localMessages = [...localMessages, buildLocalMessage({
                    role: 'assistant', content: data.content, duration_ms: data.duration_ms,
                    _localKind: 'pending-assistant', _sentAt: Date.now() / 1000,
                })];
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

    // ── File Upload ────────────────────────────────────────

    async function handleFileUpload(file) {
        if (!file || !activeAgent) return;
        const formData = new FormData();
        formData.append('file', file);
        sending = true;
        addLocalMessage({ role: 'user', content: `Uploading: ${file.name} (${(file.size / 1024).toFixed(1)} KB)` });
        await tick();
        scrollToBottom();
        try {
            const resp = await fetch(`/agents/${activeAgent}/upload`, { method: 'POST', body: formData });
            if (!resp.ok) throw new Error(await resp.text());
            const data = await resp.json();
            addLocalMessage({ role: 'system', content: `File uploaded: ${data.filename} (${data.size} bytes) \u2192 ${data.path}` });
        } catch (e) {
            addLocalMessage({ role: 'system', content: `Upload failed: ${e.message}` });
        }
        sending = false;
        await tick();
        scrollToBottom();
    }

    // ── Agent Actions ──────────────────────────────────────

    async function stopActiveAgent() {
        if (!activeAgent) return;
        try {
            await api('POST', `/agents/${activeAgent}/stop`);
            addLocalMessage({ role: 'system', content: `${activeAgent} force-stopped.` });
        } catch (e) {
            console.error('Stop failed:', e);
        }
    }

    async function contextRestart() {
        if (!activeSession || restarting) return;
        restarting = true;
        try {
            const savePrompt = 'Your session is about to be restarted. Save your current state now:\n\n'
                + '1. Use your save_my_context or set wake context tool to persist what you were working on\n'
                + '2. Include: current task, key context, any blockers, and what to do next\n'
                + '3. Confirm when saved\n\n'
                + 'This is a context restart \u2014 your conversation will reset but your saved state will carry over.';
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
        try { await api('POST', `/conversations/${sessionId}/checkpoint`, { type, detail }); } catch { /* best effort */ }
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
                addLocalMessage({ role: 'system', content: `Model changed to ${selectedModel} \u2014 session restarted for new context window (${result.old_turns} turns saved)` });
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
        try { await api('PUT', `/agents/${activeAgent}`, { restart_threshold_pct: contextNudgePct }); } catch (e) { alert(`Failed to update nudge: ${e.message}`); }
        savingNudge = false;
    }

    // ── Sub-Session Management ─────────────────────────────

    function normalizeSessionLabel(value) {
        return String(value || '')
            .trim()
            .replace(/\s+/g, '-')
            .replace(/[^A-Za-z0-9._-]/g, '-')
            .replace(/-+/g, '-')
            .replace(/^[-.]+|[-.]+$/g, '');
    }

    function validateSessionLabel(value) {
        const label = normalizeSessionLabel(value);
        if (!label) return { label: '', error: 'Session name cannot be empty.' };
        if (label === 'main') return { label, error: 'Session name "main" is reserved.' };
        return { label, error: '' };
    }

    async function spawnAgentSession(agentName) {
        newSessionAgent = agentName;
        newSessionName = `chat-${Date.now().toString(36)}`;
        newSessionError = '';
        showNewSessionModal = true;
        await tick();
    }

    async function submitNewSession() {
        if (!newSessionAgent || creatingSession) return;

        const { label, error } = validateSessionLabel(newSessionName);
        if (error) {
            newSessionError = error;
            return;
        }

        const agentName = newSessionAgent;
        const newSessionId = `${agentName}-${label}`;
        creatingSession = true;
        newSessionError = '';
        try {
            await api('POST', `/agents/${agentName}/streaming-sessions?label=${encodeURIComponent(label)}`);
            showNewSessionModal = false;
            await refreshSessions();
            await selectSession(newSessionId, agentName);
        } catch (e) {
            newSessionError = e.message || 'Failed to create session.';
        } finally {
            creatingSession = false;
        }
    }

    function startRename(agentName, label) {
        if (label === 'main') return;
        renamingSession = { agentName, label };
        renameValue = label;
    }

    async function finishRename() {
        if (!renamingSession) return;
        const { agentName, label } = renamingSession;
        const { label: newLabel, error } = validateSessionLabel(renameValue);
        renamingSession = null;
        if (!newLabel || newLabel === label) return;
        if (error) {
            addLocalMessage({ role: 'system', content: `Rename failed: ${error}` });
            return;
        }
        try {
            await api('PATCH', `/agents/${agentName}/streaming-sessions/${encodeURIComponent(label)}`, { label: newLabel });
            if (activeSession === `${agentName}-${label}`) activeSession = `${agentName}-${newLabel}`;
            await refreshSessions();
        } catch (e) {
            addLocalMessage({ role: 'system', content: `Rename failed: ${e.message}` });
        }
    }

    // ── Search ─────────────────────────────────────────────

    async function searchChats() {
        if (!chatSearchQuery.trim()) return;
        try {
            const results = await api('GET', `/conversations/search?q=${encodeURIComponent(chatSearchQuery)}`);
            chatSearchResults = results.results || [];
            chatSearchOpen = true;
        } catch { chatSearchResults = []; }
    }

    // ── Forward ────────────────────────────────────────────

    async function openForwardModal(msg) {
        forwardMessage = msg;
        forwardContext = '';
        forwardChips = [];
        forwardSearch = '';
        forwarding = false;
        try {
            const data = await api('GET', '/agents');
            forwardAgents = (data.agents || []).map(a => a.name).filter(n => n !== activeAgent);
        } catch {
            forwardAgents = [];
        }
        showForwardModal = true;
    }

    function addForwardChip(name) {
        if (!forwardChips.includes(name)) forwardChips = [...forwardChips, name];
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
            for (const target of targets) await api('POST', `/agents/${target}/forward`, { content: body });
            addLocalMessage({ role: 'system', content: `Forwarded to ${targets.join(', ')}` });
            showForwardModal = false;
        } catch (e) {
            console.error('Forward failed:', e);
        }
        forwarding = false;
    }

    // ── Sidebar Events ─────────────────────────────────────

    function handleSidebarEvent(type) {
        return (e) => {
            const d = e.detail;
            switch (type) {
                case 'selectSession': selectSession(d.id, d.agentName); break;
                case 'spawnSession': spawnAgentSession(d.agentName); break;
                case 'startRename': startRename(d.agentName, d.label); break;
                case 'finishRename': finishRename(); break;
                case 'cancelRename': renamingSession = null; break;
                case 'searchChats': searchChats(); break;
            }
        };
    }

    function handleGlobalClick() {
        if (restartDropdownOpen) restartDropdownOpen = false;
    }

    // ── Lifecycle ──────────────────────────────────────────

    onMount(async () => {
        await refreshSessions();
        refreshInterval = setInterval(refreshSessions, 10000);
        document.addEventListener('click', handleGlobalClick);
        if (params?.agent && !activeSession) {
            const mainSessionId = `${params.agent}-main`;
            if (sessionsList.some(s => s.id === mainSessionId)) selectSession(mainSessionId, params.agent);
        }
    });

    onDestroy(() => {
        clearInterval(refreshInterval);
        stopChatPolling();
        stopActivityPolling();
        stopSessionMetaPolling();
        document.removeEventListener('click', handleGlobalClick);
    });
</script>

<!-- ── Layout ───────────────────────────────────────────── -->

<div class="main">
    <div class="sidebar" class:collapsed={sidebarCollapsed}>
        <ChatSidebar
            {agentsList}
            {agentSessions}
            {activeSession}
            {renamingSession}
            bind:renameValue
            bind:chatSearchQuery
            on:selectSession={handleSidebarEvent('selectSession')}
            on:spawnSession={handleSidebarEvent('spawnSession')}
            on:startRename={handleSidebarEvent('startRename')}
            on:finishRename={handleSidebarEvent('finishRename')}
            on:cancelRename={handleSidebarEvent('cancelRename')}
            on:searchChats={handleSidebarEvent('searchChats')}
        />
    </div>

    <div class="chat-area">
        {#if !activeSession}
            <div class="empty-state">{$_('chat.select_agent')}</div>
        {:else}
            <!-- Header bar -->
            <div class="chat-info">
                <button class="sidebar-toggle-btn" on:click={() => sidebarCollapsed = !sidebarCollapsed} title={sidebarCollapsed ? 'Show agents' : 'Hide agents'}>
                    <span class="material-symbols-outlined">{sidebarCollapsed ? 'menu' : 'close'}</span>
                </button>
                <span class="info-context" class:warning={infoContextPct >= contextNudgePct}>{$_('chat.context')}: <strong>{infoContext}</strong></span>
                <span>{$_('chat.messages')}: <strong>{infoMessages}</strong></span>
                <span>{$_('chat.session')}: <strong>{infoSession}</strong></span>
                <div class="chat-actions">
                    <button class="btn-action" on:click={() => showSettings = !showSettings}>{$_('chat.model')}</button>
                    <button class="btn-action" on:click={() => showSessionInfo = !showSessionInfo}>info</button>
                    <div class="restart-group">
                        <button class="btn-restart" class:restarting on:click={contextRestart} disabled={restarting}>{restarting ? $_('chat.restarting') : $_('chat.context_restart')}</button>
                        <button class="btn-restart-chevron" class:open={restartDropdownOpen} on:click|stopPropagation={() => restartDropdownOpen = !restartDropdownOpen} disabled={restarting}>&#x25BE;</button>
                        {#if restartDropdownOpen}
                            <div class="restart-dropdown" on:click|stopPropagation={() => restartDropdownOpen = false}>
                                <button class="restart-dropdown-item" class:active-action={compacting} on:click={compactContext} disabled={compacting}>
                                    <span class="dropdown-icon">&oslash;</span> {compacting ? $_('chat.compacting') : $_('chat.compact')}
                                    <span class="dropdown-hint">Summarize old context</span>
                                </button>
                                <button class="restart-dropdown-item restart-dropdown-danger" class:active-action={archiving} on:click={archiveSession} disabled={archiving}>
                                    <span class="dropdown-icon">&#x25A3;</span> {archiving ? $_('chat.archiving') : $_('chat.archive')}
                                    <span class="dropdown-hint">Save memory + fresh start</span>
                                </button>
                            </div>
                        {/if}
                    </div>
                    <button class="btn-action btn-stop-chat" on:click={stopActiveAgent} title="Force stop agent" aria-label="Force stop agent">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
                        stop
                    </button>
                </div>
            </div>

            <!-- Settings panel -->
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

            <!-- Session info panel -->
            {#if showSessionInfo}
                <div class="session-info-panel">
                    <div class="session-info-row">
                        <span class="session-info-label">Session</span>
                        <span class="session-info-value session-id-chip" title={infoSession} on:click={() => navigator.clipboard?.writeText(infoSession)}>{infoSession.length > 24 ? infoSession.slice(0, 12) + '\u2026' + infoSession.slice(-8) : infoSession}</span>
                    </div>
                    <div class="session-info-row">
                        <span class="session-info-label">Context</span>
                        <span class="session-info-value" class:session-info-warn={infoContextPct >= contextNudgePct}>{sessionMeta ? `${sessionMeta.context_pct}%` : infoContext}</span>
                    </div>
                    <div class="session-info-row">
                        <span class="session-info-label">Model</span>
                        <span class="session-info-value">{sessionMeta?.model || infoModel}</span>
                    </div>
                    {#if sessionMeta}
                        <div class="session-info-row">
                            <span class="session-info-label">Connected</span>
                            <span class="session-info-value"><span class="session-status-dot" class:connected={sessionMeta.connected} class:disconnected={!sessionMeta.connected}></span>{sessionMeta.connected ? 'Yes' : 'No'}</span>
                        </div>
                        <div class="session-info-row">
                            <span class="session-info-label">Provider</span>
                            <span class="session-info-value session-provider-badge">{sessionMeta.provider || '--'}</span>
                        </div>
                        <div class="session-info-row">
                            <span class="session-info-label">Cost</span>
                            <span class="session-info-value">${(sessionMeta.cost_usd || 0).toFixed(4)}</span>
                        </div>
                        <div class="session-info-row">
                            <span class="session-info-label">Turns</span>
                            <span class="session-info-value">{sessionMeta.turns ?? '--'}</span>
                        </div>
                        <div class="session-info-row">
                            <span class="session-info-label">Uptime</span>
                            <span class="session-info-value">{formatUptime(sessionMeta.uptime_seconds)}</span>
                        </div>
                    {/if}
                    {#if streamingStats}
                        {#if !sessionMeta}
                            <div class="session-info-row"><span class="session-info-label">Turns</span><span class="session-info-value">{streamingStats.turns || 0}</span></div>
                            <div class="session-info-row"><span class="session-info-label">Cost</span><span class="session-info-value">${(streamingStats.cost_usd || 0).toFixed(2)}</span></div>
                        {/if}
                        {#if streamingStats.messages_sent > 0}
                            <div class="session-info-row"><span class="session-info-label">Msgs out</span><span class="session-info-value">{streamingStats.messages_sent}</span></div>
                        {/if}
                        {#if streamingStats.errors > 0}
                            <div class="session-info-row"><span class="session-info-label">Errors</span><span class="session-info-value session-info-warn">{streamingStats.errors}</span></div>
                        {/if}
                        {#if streamingStats.auto_restarts > 0}
                            <div class="session-info-row"><span class="session-info-label">Auto-restarts</span><span class="session-info-value">{streamingStats.auto_restarts}</span></div>
                        {/if}
                        {#if streamingStats.totalTokens}
                            <div class="session-info-row"><span class="session-info-label">Tokens</span><span class="session-info-value">{(streamingStats.totalTokens / 1000).toFixed(1)}k / {(streamingStats.maxTokens / 1000).toFixed(0)}k</span></div>
                        {/if}
                    {/if}
                    {#if activeSessionRecord?.sdk_session_id}
                        <div class="session-info-row">
                            <span class="session-info-label">Resume ID</span>
                            <span class="session-info-value session-id-chip" title={activeSessionRecord.sdk_session_id} on:click={() => navigator.clipboard?.writeText(activeSessionRecord.sdk_session_id)}>{activeSessionRecord.sdk_session_id.slice(0, 16)}&hellip;</span>
                        </div>
                    {/if}
                    {#if activeSessionRecord?.restart_count > 0}
                        <div class="session-info-row"><span class="session-info-label">Restarts</span><span class="session-info-value">{activeSessionRecord.restart_count}</span></div>
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
                                                <div class="breakdown-segment" style="width: {pct}%; --seg-hue: {(i * 47 + 200) % 360}" title="{cat.name}: {(cat.tokens / 1000).toFixed(1)}k ({pct.toFixed(1)}%)"></div>
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

            <!-- Messages -->
            <div class="messages" bind:this={messagesContainer} on:scroll={handleMessagesScroll}>
                {#if loadingOlder}
                    <div class="loading-older">{$_('chat.loading_older')}</div>
                {/if}
                {#if hasMore && !loadingOlder}
                    <div class="loading-older"><button class="btn-load-more" on:click={loadOlderMessages}>{$_('chat.load_older')}</button></div>
                {/if}
                {#each messages as msg, index (deriveMessageKey(msg, index))}
                    {#if isHeartbeatMessage(msg)}
                        {@const hbTime = msg.content?.match(/\d{2}:\d{2}/)?.[0] || ''}
                        <div class="heartbeat-indicator">
                            ♥ Heartbeat{hbTime ? ` · ${hbTime}` : ''}
                        </div>
                    {:else}
                        <ChatMessage
                            {msg}
                            {index}
                            on:reply={e => { replyTo = e.detail; }}
                            on:forward={e => openForwardModal(e.detail)}
                        />
                    {/if}
                {/each}
                {#if thinking || agentWorking}
                    <div class="thinking-bubble">
                        <div class="thinking-dots-row">
                            <span class="dot"></span><span class="dot"></span><span class="dot"></span>
                        </div>
                        {#if activityLog.length > 0}
                            <div class="thinking-log">
                                {#each activityLog as entry, i}
                                    <div class="thinking-log-entry" class:current={i === activityLog.length - 1}>{entry}</div>
                                {/each}
                            </div>
                        {:else if thinkingActivity}
                            <div class="thinking-log"><div class="thinking-log-entry current">{thinkingActivity}</div></div>
                        {/if}
                        {#if thinkingContent}
                            <div class="thinking-reasoning">
                                <span class="thinking-reasoning-label">thinking</span>
                                <span class="thinking-reasoning-text">{thinkingContent.length > 200 ? thinkingContent.slice(0, 200) + '\u2026' : thinkingContent}</span>
                            </div>
                        {/if}
                    </div>
                {/if}
            </div>

            <!-- Input -->
            <ChatInput
                bind:messageInput
                {sending}
                {canSendMessage}
                {activeSession}
                {activeAgent}
                {replyTo}
                on:send={sendMessage}
                on:clearReply={() => { replyTo = null; }}
                on:upload={e => handleFileUpload(e.detail)}
            />
        {/if}
    </div>
</div>

<!-- Forward Modal -->
<Modal bind:show={showNewSessionModal} title={newSessionAgent ? `New Session for ${newSessionAgent}` : 'New Session'} width="420px">
    <div class="new-session-modal">
        <label class="new-session-label" for="new-session-name">Session name</label>
        <input
            id="new-session-name"
            class="new-session-input"
            type="text"
            bind:value={newSessionName}
            placeholder="chat-worker"
            on:input={() => { newSessionError = ''; }}
            on:keydown={(e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    submitNewSession();
                }
            }}
            autofocus
        />
        <div class="new-session-hint">
            Label: <span>{normalizeSessionLabel(newSessionName) || 'enter-a-name'}</span>
        </div>
        {#if newSessionError}
            <div class="new-session-error">{newSessionError}</div>
        {/if}
    </div>
    <div slot="footer" class="new-session-actions">
        <button class="new-session-cancel" on:click={() => { showNewSessionModal = false; }}>Cancel</button>
        <button class="new-session-create" on:click={submitNewSession} disabled={creatingSession}>
            {creatingSession ? 'Creating...' : 'Create Session'}
        </button>
    </div>
</Modal>

<Modal bind:show={showForwardModal} title="Forward Message" maxWidth="500px">
    <div class="forward-modal">
        <div class="forward-to">
            <span class="forward-to-label">To:</span>
            <div class="forward-to-field">
                {#each forwardChips as chip}
                    <span class="forward-chip">{chip} <button class="forward-chip-x" on:click={() => removeForwardChip(chip)}>x</button></span>
                {/each}
                <input class="forward-to-input" type="text" bind:value={forwardSearch}
                    placeholder={forwardChips.length === 0 ? 'Type agent name...' : ''}
                    on:keydown={(e) => {
                        if (e.key === 'Backspace' && !forwardSearch && forwardChips.length > 0) removeForwardChip(forwardChips[forwardChips.length - 1]);
                        if (e.key === 'Enter' && forwardSuggestions.length > 0) { e.preventDefault(); addForwardChip(forwardSuggestions[0]); }
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
        <div class="forward-context-wrap">
            <textarea class="forward-context" bind:value={forwardContext} placeholder="Add instructions or context (optional)..." rows="3"></textarea>
        </div>
        <div class="forward-preview">
            <div class="forward-preview-label">Message</div>
            <div class="forward-preview-content">{forwardMessage?.content?.slice(0, 300)}{(forwardMessage?.content?.length || 0) > 300 ? '...' : ''}</div>
        </div>
        <div class="forward-actions">
            <button class="forward-cancel" on:click={() => showForwardModal = false}>Cancel</button>
            <button class="forward-send" on:click={sendForward} disabled={forwarding || forwardChips.length === 0}>{forwarding ? 'Sending...' : 'Forward'}</button>
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
                        {#if ts}<span class="search-modal-time" title={ts.toLocaleString()}>{ts.toLocaleDateString()} {ts.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>{/if}
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

    /* Sidebar shell */
    .sidebar { width: 260px; display: flex; flex-direction: column; background: var(--surface-1); }
    .sidebar.collapsed { display: none; }

    /* Chat area */
    .chat-area { flex: 1; display: flex; flex-direction: column; background: var(--app-bg); }
    .empty-state { flex: 1; display: flex; align-items: center; justify-content: center; font-family: var(--font-grotesk); color: var(--text-muted); font-size: 0.9rem; }

    /* Header bar */
    .chat-info { padding: 0.6rem 1.5rem; background: var(--surface-1); font-family: var(--font-grotesk); font-size: 0.72rem; color: var(--text-muted); display: flex; align-items: center; gap: 1.5rem; }
    .chat-info span { display: flex; align-items: center; gap: 0.3rem; }
    .sidebar-toggle-btn { background: none; border: none; cursor: pointer; color: var(--text-muted); padding: 0.15rem; display: flex; align-items: center; border-radius: var(--radius); transition: all 0.1s; }
    .sidebar-toggle-btn:hover { background: var(--surface-2); color: var(--text-primary); }
    .sidebar-toggle-btn .material-symbols-outlined { font-size: 18px; }
    .info-context.warning { color: var(--danger-outline); font-weight: 700; }
    .chat-actions { display: flex; gap: 0.3rem; margin-left: auto; align-items: center; }
    .btn-action { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; padding: 0.25rem 0.6rem; background: var(--surface-2); color: var(--text-muted); border: none; border-radius: var(--radius-lg); cursor: pointer; text-transform: uppercase; letter-spacing: 0.04em; transition: all 0.1s; }
    .btn-action:hover { color: var(--text-primary); background: var(--surface-3); }
    .btn-stop-chat { display: flex; align-items: center; gap: 0.3rem; }
    .btn-stop-chat:hover { color: var(--red); }
    .btn-action:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-action.active-action { color: var(--accent); background: var(--accent-soft); animation: pulse 1s infinite; }

    /* Restart group */
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
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }

    /* Settings bar */
    .settings-bar { padding: 0.5rem 1.5rem; background: var(--surface-2); font-family: var(--font-grotesk); font-size: 0.7rem; display: flex; gap: 2rem; align-items: center; border-radius: 0 0 var(--radius-lg) var(--radius-lg); }
    .setting-item { display: flex; align-items: center; gap: 0.5rem; color: var(--text-secondary); }
    .setting-item select, .setting-item input { font-family: var(--font-body); font-size: 0.7rem; padding: 0.25rem 0.4rem; border: none; border-radius: var(--radius-lg); background: var(--input-bg); color: var(--text-primary); }
    .setting-item input[type="number"] { width: 4rem; }

    /* Session info panel */
    .session-info-panel { padding: 0.5rem 1.5rem; background: var(--surface-2); font-family: var(--font-grotesk); font-size: 0.68rem; display: flex; flex-wrap: wrap; gap: 0.4rem 1.5rem; align-items: center; border-top: 1px solid var(--border); }
    .session-info-row { display: flex; align-items: center; gap: 0.35rem; }
    .session-info-label { color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.6rem; }
    .session-info-value { color: var(--text-secondary); font-family: var(--font-mono); font-size: 0.65rem; }
    .session-info-warn { color: var(--danger-outline); font-weight: 700; }
    .session-id-chip { cursor: pointer; background: var(--surface-3); padding: 0.1rem 0.35rem; border-radius: var(--radius); transition: background 0.1s; }
    .session-id-chip:hover { background: var(--primary-container); color: var(--on-primary-container); }
    .session-status-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 0.3rem; vertical-align: middle; }
    .session-status-dot.connected { background: var(--green, #4caf50); box-shadow: 0 0 4px var(--green, #4caf50); }
    .session-status-dot.disconnected { background: var(--red, #f44336); box-shadow: 0 0 4px var(--red, #f44336); }
    .session-provider-badge { background: var(--surface-3); padding: 0.1rem 0.35rem; border-radius: var(--radius); text-transform: lowercase; letter-spacing: 0.02em; }
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

    /* Messages area */
    .messages { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 1.5rem 2rem; display: flex; flex-direction: column; gap: 1rem; min-width: 0; }
    .loading-older { text-align: center; padding: 0.5rem; font-size: 0.8rem; color: var(--text-muted); font-family: var(--font-grotesk); }
    .btn-load-more { background: none; border: 1px solid var(--border); padding: 0.3rem 1rem; border-radius: var(--radius); cursor: pointer; font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted); }
    .btn-load-more:hover { background: var(--surface-1); color: var(--text-primary); }

    /* Heartbeat indicator */
    .heartbeat-indicator { text-align: center; font-family: var(--font-grotesk); font-size: 0.6rem; color: var(--text-muted); opacity: 0.5; padding: 0.15rem 0; letter-spacing: 0.04em; }

    /* Thinking bubble */
    .thinking-bubble { align-self: flex-start; background: var(--surface-1); box-shadow: 4px 4px 0px var(--shadow-color); border-radius: var(--radius-lg); padding: 0.85rem 1.2rem; display: flex; flex-direction: column; gap: 0.4rem; }
    .thinking-reasoning { display: flex; gap: 0.4rem; margin-top: 0.4rem; align-items: flex-start; }
    .thinking-reasoning-label { font-size: 0.6rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-subtle); white-space: nowrap; padding-top: 1px; }
    .thinking-reasoning-text { font-size: 0.72rem; color: var(--text-muted); font-style: italic; line-height: 1.4; opacity: 0.8; }
    .thinking-dots-row { display: flex; align-items: center; gap: 5px; height: 18px; }
    .thinking-dots-row .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); opacity: 0.5; animation: dot-bounce 1.2s ease-in-out infinite; }
    .thinking-dots-row .dot:nth-child(2) { animation-delay: 0.2s; }
    .thinking-dots-row .dot:nth-child(3) { animation-delay: 0.4s; }
    @keyframes dot-bounce { 0%, 60%, 100% { transform: translateY(0); opacity: 0.4; } 30% { transform: translateY(-6px); opacity: 1; } }
    .thinking-log { display: flex; flex-direction: column; gap: 0.15rem; margin-top: 0.1rem; max-width: 300px; }
    .thinking-log-entry { font-family: var(--font-grotesk); font-size: 0.67rem; color: var(--text-subtle); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; line-height: 1.4; transition: color 0.2s; }
    .thinking-log-entry.current { color: var(--text-muted); font-weight: 600; }

    /* Search modal */
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

    /* New session modal */
    .new-session-modal { display: flex; flex-direction: column; gap: 0.65rem; }
    .new-session-label { font-family: var(--font-grotesk); font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-secondary); }
    .new-session-input {
        width: 100%;
        padding: 0.7rem 0.8rem;
        border-radius: var(--radius-lg);
        border: 1px solid var(--border);
        background: var(--surface-2);
        color: var(--text-primary);
        font-family: var(--font-body);
        font-size: 0.92rem;
    }
    .new-session-input:focus { outline: 2px solid var(--primary-container); outline-offset: -1px; border-color: var(--primary-container); }
    .new-session-hint { font-family: var(--font-grotesk); font-size: 0.68rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }
    .new-session-hint span { color: var(--text-primary); text-transform: none; letter-spacing: 0; margin-left: 0.25rem; }
    .new-session-error {
        padding: 0.65rem 0.75rem;
        border-radius: var(--radius-lg);
        background: color-mix(in srgb, var(--red) 14%, transparent);
        color: var(--danger-outline);
        font-family: var(--font-body);
        font-size: 0.82rem;
    }
    .new-session-actions { display: flex; justify-content: flex-end; gap: 0.5rem; width: 100%; }
    .new-session-cancel, .new-session-create {
        border: none;
        border-radius: var(--radius-lg);
        padding: 0.55rem 0.9rem;
        font-family: var(--font-grotesk);
        font-size: 0.68rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        cursor: pointer;
    }
    .new-session-cancel { background: var(--surface-2); color: var(--text-secondary); }
    .new-session-cancel:hover { background: var(--surface-3); color: var(--text-primary); }
    .new-session-create { background: var(--primary-container); color: var(--on-primary-container); }
    .new-session-create:hover { background: var(--primary); color: #fff; }
    .new-session-create:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Forward modal */
    .forward-modal { display: flex; flex-direction: column; gap: 0.8rem; }
    .forward-to { position: relative; }
    .forward-to-label { font-family: var(--font-grotesk); font-size: 0.65rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.04em; margin-right: 0.4rem; }
    .forward-to-field { display: flex; flex-wrap: wrap; align-items: center; gap: 0.3rem; padding: 0.4rem 0.6rem; border: 1px solid var(--border); border-radius: var(--radius); background: var(--input-bg); min-height: 2.2rem; margin-top: 0.3rem; }
    .forward-to-field:focus-within { outline: 2px solid var(--primary-container); outline-offset: -2px; }
    .forward-to-input { border: none; outline: none; background: none; flex: 1; min-width: 80px; font-family: var(--font-body); font-size: 0.8rem; color: var(--text-primary); }
    .forward-chip { display: inline-flex; align-items: center; gap: 0.2rem; padding: 0.15rem 0.5rem; background: var(--primary-container); color: var(--on-primary-container); border-radius: var(--radius); font-family: var(--font-grotesk); font-size: 0.65rem; font-weight: 600; }
    .forward-chip-x { background: none; border: none; cursor: pointer; font-size: 0.7rem; color: inherit; opacity: 0.6; padding: 0 0.15rem; }
    .forward-chip-x:hover { opacity: 1; }
    .forward-suggestions { position: absolute; top: 100%; left: 0; right: 0; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); z-index: 50; max-height: 150px; overflow-y: auto; margin-top: 0.2rem; }
    .forward-suggestion { display: block; width: 100%; padding: 0.4rem 0.6rem; background: none; border: none; text-align: left; font-family: var(--font-grotesk); font-size: 0.7rem; cursor: pointer; color: var(--text-primary); }
    .forward-suggestion:hover { background: var(--surface-3); }
    .forward-context-wrap { }
    .forward-context { width: 100%; font-family: var(--font-body); font-size: 0.8rem; padding: 0.5rem; border: 1px solid var(--border); border-radius: var(--radius); background: var(--input-bg); color: var(--text-primary); resize: vertical; }
    .forward-preview { background: var(--surface-1); border-radius: var(--radius); padding: 0.5rem 0.8rem; }
    .forward-preview-label { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.3rem; }
    .forward-preview-content { font-size: 0.8rem; color: var(--text-secondary); line-height: 1.5; max-height: 100px; overflow: hidden; }
    .forward-actions { display: flex; justify-content: flex-end; gap: 0.5rem; }
    .forward-cancel { font-family: var(--font-grotesk); font-size: 0.7rem; padding: 0.4rem 1rem; background: var(--surface-2); border: none; border-radius: var(--radius); cursor: pointer; color: var(--text-muted); }
    .forward-send { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; padding: 0.4rem 1rem; background: var(--primary-container); color: var(--on-primary-container); border: none; border-radius: var(--radius); cursor: pointer; }
    .forward-send:disabled { opacity: 0.4; cursor: not-allowed; }

    @media (max-width: 768px) {
        .sidebar { position: fixed; left: 0; top: 0; bottom: 0; z-index: 100; width: 280px; }
    }
</style>
