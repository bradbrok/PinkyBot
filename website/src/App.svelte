<script>
  import { onMount } from 'svelte';
  import Router from 'svelte-spa-router';
  import Landing from './pages/Landing.svelte';
  import Login from './pages/Login.svelte';
  import Presentations from './pages/Presentations.svelte';
  import Fleet from './pages/Fleet.svelte';
  import Docs from './pages/Docs.svelte';
  import { init as initTheme } from './lib/theme.svelte.js';

  onMount(() => {
    initTheme();
  });

  const routes = {
    '/': Landing,
    '/login': Login,
    '/presentations': Presentations,
    '/fleet': Fleet,
    '/docs': Docs,
  };
</script>

<Router {routes} />
