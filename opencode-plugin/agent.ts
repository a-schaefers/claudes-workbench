// The Workbench agent and the workbench skills (#1335): the procedure in OpenCode's own places
// (the host-shape decision, #1296). The agent's prompt is SKILL.md as skill.ts renders it, added
// to OpenCode's own system prompt by the session `context` hook on each request the Workbench
// agent makes (`withPrompt`), so it holds while the agent is selected and is gone once the session
// switches away. Not Agent.Info's `instructions`: OpenCode 2.4.1 has no such field (its Plan agent
// sends its text from the same hook; the field is newer, core/src/instructions/agent.ts), and
// `system` would replace OpenCode's own prompt, its tools' guidance with it (seen live,
// 2026-11-25: `instructions` set, nothing reached the model). The `workbench` skill holds the
// same text under the id of the Claude Code skill OpenCode also scans in ~/.claude/skills
// (config/plugin/compatibility.ts), so OpenCode lists one `workbench`, this one (`keepOurs`):
// Claude Code's is SKILL.md itself, whose Bash and Claude lines a driver here would misread.
// Each reference part is a `workbench-<name>` skill. All are `autoinvoke: false`: no other
// agent's list offers them (skill/instructions.ts), and the Workbench agent loads one by the id
// its prompt names.
//
// The agent's permissions are OpenCode's default and the question tool, as Build's. The gates
// (#1314) add no rule here: they decide on OpenCode's permission hook (consent.ts, `permission.hook`
// "evaluate"), which every call's permission passes, whatever agent's rules let it. The procedure's
// typed wb tools (tools.ts) need no rule either. The mode (#1315) is this agent itself: upkeep.ts
// reads a session's.
import type { AgentEditor, Registration, ServerContext, SessionContextHook, SkillEditor } from './host.d.ts'
import { SKILL_ID, readSkill, render, type Rendered, type Where } from './skill.ts'
import { OWN, findWb, wbEnv, type Options } from './wb.ts'
import { dirname, join } from 'node:path'
import { statSync } from 'node:fs'

export const AGENT = 'workbench'
export const NAME = 'Workbench'
export const DESCRIPTION = 'Hands each coding request to OpenCode agents through wb, on the GPU or an API model: '
  + 'you write the spec, review the diff and accept or send it back.'

// workbench's code: the package's own (a clone, or the Git install in OpenCode's package cache),
// else the clone the options' wb runs from (wb setup --opencode's entry loads a copy of
// opencode-plugin/ alone, ocplugin.make_copy, so skill/ isn't beside it)
export function skillRoot(options: Options, own: string = OWN): string | undefined {
  const given = typeof options?.wb === 'string' && options.wb ? dirname(dirname(options.wb)) : undefined
  return [dirname(dirname(own)), given].find((root): root is string =>
    !!root && !!statSync(join(root, 'skill', 'workbench', 'SKILL.md'), { throwIfNoEntry: false })?.isFile())
}

export function where(root: string, options: Options, base = process.env): Where {
  const found = findWb(options, wbEnv(options, base))
  return {
    root,
    wb: found.found ? found.wb : join(root, 'bin', 'wb'),
    onPath: found.from === 'PATH',
    docs: !!statSync(join(root, 'ARCHITECTURE.md'), { throwIfNoEntry: false })?.isFile(),
    wakes: options?.wake !== 'off',  // notices.ts's own reading of the option
  }
}

export function defineAgent(editor: AgentEditor): void {
  editor.update(AGENT, (agent) => {
    agent.name = NAME
    agent.description = DESCRIPTION
    agent.mode = 'primary'
    agent.permissions.push({ action: 'question', resource: '*', effect: 'allow' })
  })
}

// OpenCode's own words for an agent's instructions (core/src/instructions/agent.ts), so a model
// tells them from the user's and drops them on leaving the agent
export const intro = `You are the ${NAME} agent. Follow these instructions while you are the ${NAME} agent:\n\n`

// The context hook's edit: the prompt, last in the system prompt, on the Workbench agent's
// requests only
export function withPrompt(input: SessionContextHook, prompt: string): void {
  if (input.agent === AGENT) input.system.push({ type: 'text', text: intro + prompt })
}

export function defineSkills(editor: SkillEditor, rendered: Rendered): void {
  for (const part of [rendered.skill, ...rendered.parts]) {
    // add replaces by id: the ~/.claude/skills/workbench OpenCode found goes
    editor.add({ ...part, autoinvoke: false, path: `skill://${part.id}/SKILL.md` })  // OURS for `workbench`
  }
}

export const OURS = `skill://${SKILL_ID}/SKILL.md`

// OpenCode applies its config's skills after every plugin's (core/src/plugin/internal.ts `post`:
// "the instance's explicit choices win"), the ~/.claude/skills copy among them, so it replaced
// this one (seen live, 2026-11-25: `skill.list` gave ~/.claude/skills/workbench/SKILL.md). Each
// rebuild applies the transforms in the order they were registered (core/src/state.ts), so on a
// `skill.updated` whose `workbench` isn't ours the transform registers again, last (live: the
// activation's own update, a second after setup, brought it back). It stops there: ours stays
// last, and the next rebuild keeps it.
export async function keepOurs(ctx: Pick<ServerContext, 'skill' | 'event'>, apply: (editor: SkillEditor) => void,
                               first: Registration, signal: AbortSignal): Promise<void> {
  let reg = first
  for await (const event of ctx.event.subscribe({ types: ['skill.updated'], signal })) {
    if (event.type !== 'skill.updated' || signal.aborted) continue
    const now = (await ctx.skill.list()).data.find((s) => s.id === SKILL_ID)
    if (!now || now.path === OURS) continue
    await reg.dispose()
    reg = await ctx.skill.transform(apply)
  }
}

export interface Registered {
  readonly rendered: Rendered
  readonly stop: () => void
}

// The server part's setup calls this; nothing without a skill/workbench/ to render
export async function registerAgent(ctx: ServerContext): Promise<Registered | undefined> {
  const root = skillRoot(ctx.options)
  const texts = root ? readSkill(root) : undefined
  if (!root || !texts) return undefined
  const rendered = render(texts, where(root, ctx.options))
  const apply = (editor: SkillEditor) => defineSkills(editor, rendered)
  const stop = new AbortController()
  const first = await ctx.skill.transform(apply)
  void keepOurs(ctx, apply, first, stop.signal).catch(() => undefined) // the event stream gone: ours stays as it is
  await ctx.agent.transform(defineAgent)
  await ctx.session.hook('context', (input) => withPrompt(input, rendered.prompt))
  return { rendered, stop: () => stop.abort() }
}
