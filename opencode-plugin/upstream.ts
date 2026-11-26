// The upstream gate on OpenCode's path (#1296's parity row 14, 2026-11-26): what the Claude Code
// mod's consent dialog holds (register.tsx's sendRefusal: a gh write, a `git push`, a GitHub MCP
// tool's write, aimed at workbench upstream), asked here in the session's form (consent.ts's Asker),
// one form a call. Before it, only `wb patch send` asked on OpenCode (workbench_patch_send), and the
// rendered procedure had the driver ask in words: a rule the model may misread, never the user's
// own click.
// - Where: `tool.hook("execute.before")`, once a call, before the call's own permission ask, with
//   the whole input (a shell call's `command` and `workdir`, an MCP tool's arguments); a throw
//   refuses the call, its words what the model reads. Not the permission hook: OpenCode's scan
//   hands it a line it can't read (a `gh issue create --body "$(cat <<'EOF' …)"`, the common form)
//   as one opaque text, wb has no refusal of its own behind a gh write or a git push (as it has
//   behind wb route and wb patch send), and the hook runs again on a pending ask's re-evaluation
//   (core/src/permission.ts), which would ask twice.
// - What: the line read as the mod reads it (mod/hooks/shell.ts's upstreamAim, one copy for both
//   hosts: bash -c, eval, a fed shell, an interpreter's code that runs commands; the kernel's gateOf
//   per simple command), aimed as the mod aims it (a gated directory, by real path too; a plugins
//   directory whose checkout's origin is upstream; the upstream's name in the line, unless that
//   runs in a developer's own clone of upstream). The gated directories are the mod's (gatedWhere),
//   OpenCode's package caches added as a plugins directory. A `wb patch send` there is refused with
//   the tool that shows its title and body (gates.ts's SEND_VIA_TOOL), as the permission hook's
//   gate refuses one it can read.
// - The form: `Send it` lets that one call through, nothing remembered (a yes to one push is no yes
//   to the next); `Don't send`, a dismissal, or no TUI attached in time (`opencode run`, the
//   service alone) refuses it: the gates fail closed, and OpenCode's autoaccept never answers a
//   form (tui/src/routes/session/index.tsx). Away on the web chat, chat.ts's page line says a form
//   waits (`form.created`), and the form waits for the user, as patch_send's does.
// Nothing runs at import.
import {
  absolutePath, gatedWhere, githubAimsUpstream, inside, originIs, shownCommand, shownInput, upstreamAim, type Where,
} from '../mod/hooks/shell.ts'
import type { Asker, Before } from './consent.ts'
import { NOT_SENT, SEND_ASK, SEND_VIA_TOOL, places, type Env } from './gates.ts'

export type Row = Record<string, unknown>

// OpenCode's tool info on execute.before (core/src/tool.ts `beforeExecute`): an MCP tool's
// namespace is its server's name, `name` its own (core/src/tool/mcp.ts)
export interface ToolInfo { readonly name?: string; readonly options?: { readonly namespace?: string } }
export type UpstreamBefore = Before & { readonly info?: ToolInfo }

export interface UpstreamDeps {
  readonly env: () => Env                         // wb's environment (wbEnv: the plugin's options over the service's)
  readonly dir: (session: string) => Promise<string>
  readonly read: (path: string) => string | undefined
  readonly real: (path: string) => string
  readonly origin: (dir: string) => Promise<string> // `git -C <dir> remote get-url origin`, '' when it fails
  readonly asker: Pick<Asker, 'ask'>
  // whether the signed-in GitHub user can push to the slug (`gh api repos/<slug> --jq
  // .permissions.push`), false when gh can't say: none, no gate is lifted
  readonly canPush?: (slug: string) => Promise<boolean>
}

// A no, or gh's silence, is asked again only after this (a yes holds for the server's life), so a
// user's gated calls don't each wait on gh (the mod's PUSH_RECHECK_MS)
export const PUSH_RECHECK_MS = 10 * 60_000

export const NO_FORM_SENT = 'workbench: not sent: no OpenCode window is open on this session to ask the user in ' +
  '(the form shows in the terminal UI), so nothing was asked and nothing was sent. Tell them; once one is open, ' +
  'try again, or the user runs it in a terminal of their own.'

// The GitHub tool's own name for an MCP call, else '': by its namespace (the server's name holds
// `github`, as the mod's GITHUB_MCP matches it), else (no info) by the `<server>_<name>` it is called
const GITHUB_TOOL = /^[^_]*github[^_]*_(.+)$/i
export function githubTool(tool: string, info?: ToolInfo): string {
  const ns = info?.options?.namespace
  if (typeof ns === 'string' && typeof info?.name === 'string') return /github/i.test(ns) ? info.name : ''
  return GITHUB_TOOL.exec(tool)?.[1] ?? ''
}

// OpenCode's package caches (ocplugin.package_caches: its Git install of this plugin lives there)
export function packageCaches(env: Env): string[] {
  const home = env.HOME ?? ''
  return [...new Set([...(env.XDG_CACHE_HOME ? [`${env.XDG_CACHE_HOME}/opencode/npm`] : []), `${home}/.cache/opencode/npm`])]
}

export class Upstream {
  private readonly pushable = new Map<string, { ok: boolean; at: number }>()
  constructor(private d: UpstreamDeps, private now: () => number = Date.now) {}

  // Upstream's maintainer or a collaborator (they can push to it): their own driver's writes there
  // are their own work, never a send upstream, so never asked. The gate is for a user's driver
  // reaching the maintainer's repository; it held the owner's own issue comments until he answered
  // (2026-11-26: "it never should halt our work"). The mod's canPush.
  private async maintainer(slug: string): Promise<boolean> {
    if (!this.d.canPush) return false
    const now = this.now(), had = this.pushable.get(slug)
    if (had && (had.ok || now - had.at < PUSH_RECHECK_MS)) return had.ok
    const ok = await this.d.canPush(slug).catch(() => false)
    this.pushable.set(slug, { ok, at: now })
    return ok
  }

  async before(e: UpstreamBefore): Promise<void> {
    const why = await this.refusal(e)
    if (why) throw new Error(why)
  }

  // Why the call is refused, or '' (no send upstream, or the user's yes in the form)
  async refusal(e: UpstreamBefore): Promise<string> {
    const input = (e.input ?? {}) as Row
    if (e.tool === 'shell' || e.tool === 'bash') {
      return typeof input.command === 'string'
        ? this.shell(e.sessionID, input.command, typeof input.workdir === 'string' ? input.workdir : '') : ''
    }
    const name = githubTool(e.tool, e.info)
    return name ? this.mcp(e.sessionID, e.tool, name, input) : ''
  }

  private where(env: Env, cwd: string): Where & { home: string; root: string } {
    const home = env.HOME ?? ''
    const root = places(env, process.cwd()).home // as consent.ts reads $WB_HOME
    let patches: Row = {}
    try {
      const got = JSON.parse(this.d.read(`${root}/patches.json`) ?? '{}')
      if (got && typeof got === 'object' && !Array.isArray(got)) patches = got
    } catch { /* none: the defaults */ }
    return {
      home, root,
      ...gatedWhere({ patches, home, cwd, root, source: env.WB_SOURCE, claudeConfig: env.CLAUDE_CONFIG_DIR,
        pluginCache: env.CLAUDE_CODE_PLUGIN_CACHE_DIR, plugins: packageCaches(env) }),
    }
  }

  // a developer's own clone of upstream (its origin upstream), never a gated directory nor a plugins one
  private async developer(dir: string, g: Where): Promise<boolean> {
    if ([...g.dirs, ...g.plugins].some(d => inside(dir, d))) return false
    return originIs(await this.d.origin(dir), g.slugs)
  }

  private async shell(session: string, command: string, workdir: string): Promise<string> {
    if (!/\b(?:gh|git|wb|workbench)\b/i.test(command)) return '' // most calls: no parse
    const env = this.d.env()
    const base = await this.d.dir(session)
    const home = env.HOME ?? ''
    const cwd = workdir ? absolutePath(workdir, home, base) : base
    const g = this.where(env, cwd)
    let aim = upstreamAim(command, cwd, home, g.slugs, g.dirs, g.root, g.plugins)
    if (!aim.why && aim.outAt.length) { // a symlinked spelling of a gated directory: by real path
      const dirs = g.dirs.map(this.d.real)
      for (const p of aim.outAt) {
        const real = this.d.real(p)
        if (real !== p && dirs.some(d => inside(real, d))) { aim = { ...aim, why: 'dir', at: p }; break }
      }
    }
    if (!aim.why) return ''
    if (aim.why === 'send') return SEND_VIA_TOOL
    if (aim.why === 'plugin' && !aim.named && !originIs(await this.d.origin(aim.at), g.slugs)) return ''
    if (aim.why === 'slug' && await this.developer(aim.at, g)) return ''
    if (await this.maintainer(g.slug)) return ''
    return this.ask(session, `The driver wants to send this to GitHub (${g.slug}), publicly: ${shownCommand(command, aim)}`)
  }

  private async mcp(session: string, tool: string, name: string, input: Row): Promise<string> {
    const env = this.d.env()
    const cwd = await this.d.dir(session)
    const g = this.where(env, cwd)
    if (!githubAimsUpstream(name, input, g.slugs)) return ''
    if (await this.developer(cwd, g)) return ''
    if (await this.maintainer(g.slug)) return ''
    return this.ask(session, `The driver wants to send this to GitHub (${g.slug}), publicly: ${tool} ${shownInput(input)}`)
  }

  private async ask(session: string, what: string): Promise<string> {
    const a = await this.d.asker.ask(session, { title: 'Workbench', question: `${what}\n\nSend it?`, ...SEND_ASK })
    if (a === 'none') return NO_FORM_SENT
    return a === 'yes' ? '' : NOT_SENT
  }
}
