<script>
import { onMount, onDestroy } from 'svelte';
import Router from 'svelte-spa-router';
import Layout from './components/Layout.svelte';
import Dashboard from './pages/Dashboard.svelte';
import Chat from './pages/Chat.svelte';
import Agents from './pages/Agents.svelte';
import Fleet from './pages/Fleet.svelte';
import Memories from './pages/Memories.svelte';
import Research from './pages/Research.svelte';
import Tasks from './pages/Tasks.svelte';
import Settings from './pages/Settings.svelte';
import Landing from './pages/Landing.svelte';
import Login from './pages/Login.svelte';
import Setup from './pages/Setup.svelte';

let authPage = '';

function updateAuthPage() {
    const path = window.location.pathname || '/';
    authPage = path === '/login' ? 'login' : path === '/setup' ? 'setup' : '';
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
    '/agents': Agents,
    '/fleet': Fleet,
    '/memories': Memories,
    '/research': Research,
    '/tasks': Tasks,
    '/settings': Settings,
    '/landing': Landing,
};
</script>

{#if authPage === 'login'}
    <Login />
{:else if authPage === 'setup'}
    <Setup />
{:else}
    <Layout>
        <Router {routes} />
    </Layout>
{/if}
