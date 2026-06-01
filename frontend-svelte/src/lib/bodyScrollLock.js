/**
 * Reference-counted body scroll lock — shared across all modals.
 *
 * A module-level counter (not per-component state) is required because modals
 * stack (e.g. a confirm dialog opened over a base modal). The body must only
 * unlock once the LAST open modal closes, so we lock on the first acquire and
 * restore the original overflow on the final release.
 */
let lockCount = 0;
let prevOverflow = '';

function isBrowser() {
    return typeof document !== 'undefined';
}

export function lockBodyScroll() {
    if (!isBrowser()) return;
    if (lockCount === 0) {
        prevOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
    }
    lockCount += 1;
}

export function unlockBodyScroll() {
    if (!isBrowser() || lockCount === 0) return;
    lockCount -= 1;
    if (lockCount === 0) {
        document.body.style.overflow = prevOverflow;
    }
}
