import { writable } from 'svelte/store';

export const agents = writable([]);
export const currentAgent = writable(null);
export const toastMessage = writable(null);
