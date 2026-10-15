import type { Plugin } from 'vite'

export interface BuildInfo {
  version: string
  lastUpdate: string
}

export interface ResolveBuildInfoOptions {
  cwd?: string
  env?: Record<string, string | undefined>
  now?: () => Date
  runGit?: (args: string[]) => string
}

export function resolveBuildInfo(options?: ResolveBuildInfoOptions): BuildInfo
export function createBuildInfoPlugin(buildInfo: BuildInfo): Plugin

