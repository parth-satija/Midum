// Browser stub: the font-ligature addon bundles a Node-only code path that is never executed in a browser.
const noop = () => {}
const ch = { hasSubscribers: false, subscribe: noop, unsubscribe: noop, publish: noop, bindStore: noop, unbindStore: noop, runStores: (_d, fn, ...a) => fn?.(...a) }
export const channel = () => ch
export const tracingChannel = () => ({ ...ch, traceSync: (fn, _c, t, ...a) => fn.apply(t, a), tracePromise: (fn, _c, t, ...a) => fn.apply(t, a), traceCallback: (fn, _p, _c, t, ...a) => fn.apply(t, a) })
export const subscribe = noop
export const unsubscribe = noop
export const hasSubscribers = () => false
export default { channel, tracingChannel, subscribe, unsubscribe, hasSubscribers }
