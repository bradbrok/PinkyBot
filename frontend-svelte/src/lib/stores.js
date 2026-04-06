import { writable } from 'svelte/store';

export const agents = writable([]);
export const currentAgent = writable(null);
export const toastMessage = writable(null);

export function toast(msg, type = 'success') {
    toastMessage.set({ message: msg, type });
}
