<script>
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

<Layout>
    <Router {routes} />
</Layout>
