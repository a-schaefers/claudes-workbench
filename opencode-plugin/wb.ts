// Which `wb` the plugin runs, and with what environment (#1311, 2026-11-25). OpenCode's service can
// start from a desktop login or a unit with its own PATH and none of the user's WB_* variables, so
// `wb setup --opencode` writes both into the config entry's options: `wb` (the clone's bin/wb) and
// `env` (the WB_* paths set where setup ran). Without them (an `opencode plugin add` install) it
// runs `wb` by name from the service's own PATH, as Claude Code's mod does (`findWb`, #1200).
// Pure: no I/O until called, and the file test comes in, so tests need no wb.
import { statSync } from 'node:fs'
import { delimiter, isAbsolute, join } from 'node:path'

export type Options = Readonly<Record<string, unknown>> | undefined

export interface Found {
  readonly wb: string       // the path it runs, or the name when none was found
  readonly found: boolean
  readonly from: 'options' | 'PATH' | 'none'
}

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
                       exec: (path: string) => boolean = isExec): Found {
  const given = options?.wb
  if (typeof given === 'string' && given) {
    // an absolute path the options name is the one: never a different wb found on PATH instead
    return { wb: given, found: isAbsolute(given) && exec(given), from: 'options' }
  }
  for (const dir of (env.PATH ?? '').split(delimiter)) {
    if (dir && exec(join(dir, 'wb'))) return { wb: join(dir, 'wb'), found: true, from: 'PATH' }
  }
  return { wb: 'wb', found: false, from: 'none' }
}

// wb's state directory as wb reads it (store.home): WB_HOME, else ~/.local/state/workbench
export function wbHome(env: Record<string, string | undefined>): string {
  return env.WB_HOME || join(env.HOME ?? '~', '.local', 'state', 'workbench')
}
