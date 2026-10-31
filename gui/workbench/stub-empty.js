// Browser stub for Node-only modules that are imported but never used at runtime.
const fail = () => { throw new Error('not available in the browser') }
export const readFile = fail, writeFile = fail, readdir = fail, stat = fail, access = fail, mkdir = fail
export default {}
