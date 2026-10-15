import assert from 'node:assert/strict'
import test from 'node:test'

const buildInfoModule = await import('./build-info.mjs').catch(() => ({}))
const resolveBuildInfo = buildInfoModule.resolveBuildInfo
const createBuildInfoPlugin = buildInfoModule.createBuildInfoPlugin
const implementationAvailable = typeof resolveBuildInfo === 'function'
const pluginAvailable = typeof createBuildInfoPlugin === 'function'

test('exports the build information resolver and Vite plugin factory', () => {
  assert.equal(typeof resolveBuildInfo, 'function')
  assert.equal(typeof createBuildInfoPlugin, 'function')
})

test('the Vite plugin exposes build information through an importable virtual module', { skip: !pluginAvailable }, async () => {
  const plugin = createBuildInfoPlugin({
    version: 'v1.2.0',
    lastUpdate: '2026-10-15',
  })

  const resolvedId = plugin.resolveId('virtual:build-info')
  assert.equal(resolvedId, '\0virtual:build-info')
  const source = plugin.load(resolvedId)
  const module = await import(`data:text/javascript,${encodeURIComponent(source)}`)
  assert.deepEqual(module.default, {
    version: 'v1.2.0',
    lastUpdate: '2026-10-15',
  })
})

test('uses the nearest release tag and the tagged commit date', { skip: !implementationAvailable }, () => {
  const runGit = (args) => {
    if (args[0] === 'describe') return 'v1.1.0\n'
    if (args[0] === 'log' && args.at(-1) === 'v1.1.0') return '2026-10-15\n'
    throw new Error(`Unexpected Git command: ${args.join(' ')}`)
  }

  assert.deepEqual(resolveBuildInfo({ env: {}, runGit }), {
    version: 'v1.1.0',
    lastUpdate: '2026-10-15',
  })
})

test('shows a development build with the HEAD date when no release tag exists', { skip: !implementationAvailable }, () => {
  const runGit = (args) => {
    if (args[0] === 'describe') throw new Error('No matching tag')
    if (args[0] === 'log' && args.at(-1) === 'HEAD') return '2026-10-14\n'
    throw new Error(`Unexpected Git command: ${args.join(' ')}`)
  }

  assert.deepEqual(resolveBuildInfo({ env: {}, runGit }), {
    version: '开发版',
    lastUpdate: '2026-10-14',
  })
})

test('falls back to the build date when Git metadata is unavailable', { skip: !implementationAvailable }, () => {
  const runGit = () => {
    throw new Error('Git is unavailable')
  }
  const now = () => new Date('2026-10-13T08:30:00+08:00')

  assert.deepEqual(resolveBuildInfo({ env: {}, runGit, now }), {
    version: '开发版',
    lastUpdate: '2026-10-13',
  })
})

test('environment variables override individual resolved fields', { skip: !implementationAvailable }, () => {
  const runGit = (args) => {
    if (args[0] === 'describe') return 'v1.1.0'
    if (args[0] === 'log' && args.at(-1) === 'v1.1.0') return '2026-10-12'
    throw new Error(`Unexpected Git command: ${args.join(' ')}`)
  }

  assert.deepEqual(
    resolveBuildInfo({
      env: { VITE_APP_VERSION: 'v2.0.0' },
      runGit,
    }),
    {
      version: 'v2.0.0',
      lastUpdate: '2026-10-12',
    },
  )
})

test('complete environment overrides work without Git', { skip: !implementationAvailable }, () => {
  const runGit = () => {
    throw new Error('Git must not be required for complete overrides')
  }

  assert.deepEqual(
    resolveBuildInfo({
      env: {
        VITE_APP_VERSION: 'v3.2.1',
        VITE_APP_LAST_UPDATE: '2026-10-11',
      },
      runGit,
    }),
    {
      version: 'v3.2.1',
      lastUpdate: '2026-10-11',
    },
  )
})
