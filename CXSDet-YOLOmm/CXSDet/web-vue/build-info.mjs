import { execFileSync } from 'node:child_process'

const releaseTagPattern = 'v[0-9]*.[0-9]*.[0-9]*'

function clean(value) {
  return String(value ?? '').trim()
}

function formatLocalDate(date) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function executeGit(args, cwd) {
  return execFileSync('git', args, {
    cwd,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'ignore'],
  })
}

export function resolveBuildInfo(options = {}) {
  const cwd = options.cwd ?? process.cwd()
  const env = options.env ?? process.env
  const now = options.now ?? (() => new Date())
  const runGit = options.runGit ?? ((args) => executeGit(args, cwd))
  const versionOverride = clean(env.VITE_APP_VERSION)
  const dateOverride = clean(env.VITE_APP_LAST_UPDATE)

  if (versionOverride && dateOverride) {
    return {
      version: versionOverride,
      lastUpdate: dateOverride,
    }
  }

  let releaseTag = ''
  try {
    releaseTag = clean(
      runGit(['describe', '--tags', '--match', releaseTagPattern, '--abbrev=0']),
    )
  }
  catch {
    releaseTag = ''
  }

  let gitDate = ''
  try {
    gitDate = clean(runGit(['log', '-1', '--format=%cs', releaseTag || 'HEAD']))
  }
  catch {
    gitDate = ''
  }

  return {
    version: versionOverride || releaseTag || '开发版',
    lastUpdate: dateOverride || gitDate || formatLocalDate(now()),
  }
}

export function createBuildInfoPlugin(buildInfo) {
  const publicId = 'virtual:build-info'
  const resolvedId = `\0${publicId}`
  const source = `export default ${JSON.stringify(buildInfo)};`

  return {
    name: 'cxsdet-build-info',
    resolveId(id) {
      return id === publicId ? resolvedId : null
    },
    load(id) {
      return id === resolvedId ? source : null
    },
  }
}

