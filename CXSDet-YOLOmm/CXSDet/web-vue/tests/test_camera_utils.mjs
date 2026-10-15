import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'
import ts from '../node_modules/typescript/lib/typescript.js'


const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const sourcePath = path.join(projectRoot, 'src', 'utils', 'camera.ts')
assert.ok(fs.existsSync(sourcePath), 'Camera permission helper is not implemented')

const source = fs.readFileSync(sourcePath, 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2020,
    target: ts.ScriptTarget.ES2020
  }
}).outputText
const camera = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

assert.match(camera.getCameraErrorMessage({ name: 'NotAllowedError' }), /地址栏.*允许/)
assert.match(camera.getCameraErrorMessage({ name: 'NotFoundError' }), /未检测到/)
assert.match(camera.getCameraErrorMessage({ name: 'NotReadableError' }), /占用/)
assert.equal(camera.PREFERRED_CAMERA_CONSTRAINTS.audio, false)
assert.equal(camera.PREFERRED_CAMERA_CONSTRAINTS.video.facingMode.ideal, 'environment')

console.log('PASS: camera errors are actionable and preferred constraints allow desktop fallback.')
