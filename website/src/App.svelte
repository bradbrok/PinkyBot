<script>
  import Router from 'svelte-spa-router';
  import Landing from './pages/Landing.svelte';
  import Login from './pages/Login.svelte';
  import Presentations from './pages/Presentations.svelte';
  import Fleet from './pages/Fleet.svelte';

  const routes = {
    '/': Landing,
    '/login': Login,
    '/presentations': Presentations,
    '/fleet': Fleet,
  };
</script>

<Router {routes} />
