const SUBMIT_KEY = 'Enter';

function newClientId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `pane-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/**
 * Deliver pane input immediately while preserving order at the daemon.
 *
 * Every request repeats all events that have not yet been acknowledged. This
 * makes a later request (especially Enter) a cumulative retry of any slower
 * text requests ahead of it. The daemon de-duplicates events by client/seq.
 * Enter requests use fetch keepalive so a submission already initiated by the
 * keypress may finish after the dashboard tab closes.
 */
export function createPaneInputSender({ send, onError = () => {}, onSuccess = () => {} }) {
    const clientId = newClientId();
    let nextSeq = 0;
    let ackedSeq = 0;
    let pending = [];
    let uiActive = true;

    function acceptAck(result) {
        const ack = Number(result?.acked_seq || 0);
        if (Number.isInteger(ack) && ack > ackedSeq) {
            ackedSeq = ack;
            pending = pending.filter((event) => event.seq > ackedSeq);
        }
        // A stale prefix acknowledgement must not clear a failure for a newer
        // event (notably an exhausted Enter) that is still pending.
        if (uiActive && pending.length === 0) onSuccess();
    }

    function transmit(events, keepalive, retriesLeft = 0) {
        const requestMaxSeq = events.at(-1)?.seq || 0;
        let request;
        try {
            // Call send synchronously: Enter must start its keepalive request in
            // the key event handler, not from a promise queued behind text.
            request = send({ client_id: clientId, events }, { keepalive });
        } catch (error) {
            request = Promise.reject(error);
        }
        return Promise.resolve(request)
            .then(acceptAck)
            .catch((error) => {
                if (retriesLeft > 0) {
                    const retryEvents = pending.map((event) => ({ ...event }));
                    if (retryEvents.length) return transmit(retryEvents, true, retriesLeft - 1);
                }
                // A newer cumulative request may already have applied every
                // event in this failed request. Its late rejection is stale,
                // not a current send failure.
                if (requestMaxSeq <= ackedSeq) return undefined;
                if (uiActive) onError(error);
                return undefined;
            });
    }

    function enqueue(payload) {
        const event = {
            seq: ++nextSeq,
            text: payload.text || '',
            key: payload.key || '',
        };
        pending.push(event);
        const events = pending.map((item) => ({ ...item }));
        const submitting = event.key === SUBMIT_KEY;
        void transmit(events, submitting, submitting ? 2 : 0);
    }

    function dispose() {
        // Requests were started synchronously and target the pane captured by
        // the component's send callback. Let them finish; only silence stale UI.
        uiActive = false;
    }

    return { enqueue, dispose };
}
