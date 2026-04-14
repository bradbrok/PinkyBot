<script>
import { onMount, onDestroy } from 'svelte';
import Router from 'svelte-spa-router';
import Layout from './components/Layout.svelte';
import Dashboard from './pages/Dashboard.svelte';
import Chat from './pages/Chat.svelte';
import Agents from './pages/Agents.svelte';
// Fleet consolidated into Agents page
import Memories from './pages/Memories.svelte';
import Research from './pages/Research.svelte';
import Tasks from './pages/Tasks.svelte';
import Settings from './pages/Settings.svelte';
import People from './pages/People.svelte';
import Presentations from './pages/Presentations.svelte';
import ProjectHub from './pages/ProjectHub.svelte';
import KnowledgeBase from './pages/KnowledgeBase.svelte';
import Landing from './pages/Landing.svelte';
import Onboarding from './pages/Onboarding.svelte';
import Analytics from './pages/Analytics.svelte';
import Login from './pages/Login.svelte';
import Setup from './pages/Setup.svelte';

function detectPage() {
    const path = window.location.pathname || '/';
    const hash = window.location.hash || '';
    return {
        auth: path === '/login' ? 'login' : path === '/setup' ? 'setup' : '',
        landing: path === '/landing' || hash === '#/landing',
    };
}

// Initialize eagerly (before first render) to prevent Layout from mounting on public pages
let { auth: authPage, landing: isLanding } = detectPage();

function updateAuthPage() {
    const detected = detectPage();
    authPage = detected.auth;
    isLanding = detected.landing;
}

onMount(() => {
    updateAuthPage();
    window.addEventListener('popstate', updateAuthPage);
    window.addEventListener('hashchange', updateAuthPage);
});

onDestroy(() => {
    window.removeEventListener('popstate', updateAuthPage);
    window.removeEventListener('hashchange', updateAuthPage);
});

const routes = {
    '/': Dashboard,
    '/dashboard': Dashboard,
    '/chat': Chat,
    '/chat/:agent': Chat,
    '/agents': Agents,
    '/fleet': Agents, // redirect: Fleet consolidated into Agents
    '/memories': Memories,
    '/people': People,
    '/research': Research,
    '/tasks': Tasks,
    '/analytics': Analytics,
    '/settings': Settings,
    '/presentations': Presentations,
    '/knowledge-base': KnowledgeBase,
    '/projects/:id': ProjectHub,
    '/landing': Landing,
    '/onboarding': Onboarding,
};
</script>

{#if authPage === 'login'}
    <Login />
{:else if authPage === 'setup'}
    <Setup />
{:else if isLanding}
    <Landing />
{:else}
    <Layout>
        <Router {routes} />
    </Layout>
{/if}
