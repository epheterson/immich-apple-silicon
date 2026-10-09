// ESM face of heic_decode_shim.js for `import sharp from 'sharp'` (#191).
// The shim's resolve hook sends that import here with the URL Node resolved
// for it; this module imports that URL (a plain URL, so the hook passes it
// through) and exports the shim's wrapper around it.
import { createRequire } from 'node:module';

const { wrappedSharp } = createRequire(import.meta.url)('./heic_decode_shim.js');
const real = await import(new URL(import.meta.url).searchParams.get('real'));

export default wrappedSharp(real.default);
