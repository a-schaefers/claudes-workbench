// Which `wb` the plugin runs, and with what environment (#1311, 2026-11-25). OpenCode's service can
// start from a desktop login or a unit with its own PATH and none of the user's WB_* variables, so
// `wb setup --opencode` writes both into the config entry's options: `wb` (the clone's bin/wb) and
// `env` (the WB_* paths set where setup ran). Without them (an `opencode plugin add` install) it
// runs the wb its own package brings, bin/wb beside opencode-plugin/ (OWN: the Git install's, in
// OpenCode's package cache, or a clone's), and only without one `wb` by name from the service's
// own PATH, as Claude Code's mod does (`findWb`, #1200). The package's own before PATH's: the
// plugin and the wb it talks to come from the same commit (#1311's review, 2026-11-25: the Git
// install brought no wb, and the plugin could do nothing without a clone).
// Pure: no I/O until called, and the file test comes in, so tests need no wb.
import { statSync } from 'node:fs'
import { delimiter, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export type Options = Readonly<Record<string, unknown>> | undefined

export interface Found {
  readonly wb: string       // the path it runs, or the name when none was found
  readonly found: boolean
  readonly from: 'options' | 'package' | 'PATH' | 'none'
}

// the wb this package brings: the root package's bin/wb (package.json's `files`), one up from here
export const OWN = fileURLToPath(new URL('../bin/wb', import.meta.url))

export const isExec = (path: string): boolean => {
  const st = statSync(path, { throwIfNoEntry: false })
  return !!st && st.isFile() && (process.platform === 'win32' || (st.mode & 0o111) !== 0)
}

// The options' `env`, its string values only: what the plugin adds to wb's environment
export function optionEnv(options: Options): Record<string, string> {
  const env = options?.env
  if (!env || typeof env !== 'object') return {}
  return Object.fromEntries(Object.entries(env).filter((e): e is [string, string] => typeof e[1] === 'string'))
}

// The environment `wb` runs in: OpenCode's own, then the options' WB_* paths over it
export const wbEnv = (options: Options, base: Record<string, string | undefined> = process.env) =>
  ({ ...base, ...optionEnv(options) })

export function findWb(options: Options, env: Record<string, string | undefined> = process.env,
                       exec: (path: string) => boolean = isExec, own: string = OWN): Found {
  const given = options?.wb
  if (typeof given === 'string' && given) {
    // an absolute path the options name is the one: never a different wb found on PATH instead
    return { wb: given, found: isAbsolute(given) && exec(given), from: 'options' }
  }
  if (own && exec(own)) return { wb: own, found: true, from: 'package' }
  for (const dir of (env.PATH ?? '').split(delimiter)) {
    if (dir && exec(join(dir, 'wb'))) return { wb: join(dir, 'wb'), found: true, from: 'PATH' }
  }
  return { wb: 'wb', found: false, from: 'none' }
}

// wb's state directory as wb reads it (store.home): WB_HOME, a `~` and a relative one expanded
// as store.home does (store.PATH_VARS allows both: a service's own WB_HOME=~/wb was reported as
// the literal `~/wb`, #1311's review), else ~/.local/state/workbench
export function wbHome(env: Record<string, string | undefined>, cwd: string = process.cwd()): string {
  const home = env.HOME ?? '~'
  const v = env.WB_HOME
  if (!v) return join(home, '.local', 'state', 'workbench')
  const expanded = v === '~' ? home : v.startsWith('~/') ? join(home, v.slice(2)) : v
  return resolve(cwd, expanded)
}
