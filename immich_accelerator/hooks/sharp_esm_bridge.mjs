// ESM face of heic_decode_shim.js for `import sharp from 'sharp'` (#191).
// The shim's resolve hook sends that import here with the importer's own
// CommonJS Sharp entry in the query string; this module requires it and
// exports the shim's shared wrapper around it.
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { wrappedSharp } = require('./heic_decode_shim.js');
const entry = new URL(import.meta.url).searchParams.get('sharp');

export default wrappedSharp(require(entry));
