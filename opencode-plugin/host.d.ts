// The part of OpenCode's plugin API this plugin uses, declared structurally so the plugin imports
// nothing from @opencode/* and needs no install to type-check (#1311, 2026-11-25). Each shape is
// read from OpenCode 2.4.1's source (packages/…); OpenCode checks a module only by its default
// export's shape (core/src/plugin/module.ts `Module`: an id and a setup function; the TUI's
// tui/src/plugin/context.tsx `isPlugin`), so these are the whole contract. A new use adds its
// shape here, with its source.

// plugin/src/promise/registration.ts
export interface Registration {
  readonly dispose: () => Promise<void>
}

// schema/src/rpc.ts `PortableDefinition`, its JSON Schema form
export interface RpcDefinition {
  readonly id: string
  readonly methods: Readonly<Record<string, { readonly input?: object; readonly output?: object }>>
  readonly events: Readonly<Record<string, { readonly schema: object }>>
}

// plugin/src/promise/rpc.ts `RpcDomain.register`: a handler gets the input and returns the output
export type RpcHandlers<D extends RpcDefinition> = {
  readonly [Name in keyof D['methods']]: (input: unknown, context: { readonly signal: AbortSignal }) => Promise<unknown>
}

// plugin/src/promise/plugin.ts `Context`: `options` is the config entry's `options`
// (core/src/plugin/module.ts: `plugin.effect({ ...host, options: operation.options })`)
export interface ServerContext {
  readonly options: Readonly<Record<string, unknown>>
  readonly rpc: {
    readonly register: <D extends RpcDefinition>(definition: D, handlers: RpcHandlers<D>) => Promise<Registration>
  }
  readonly agent: { readonly transform: Transform<AgentEditor> }
  readonly skill: {
    readonly transform: Transform<SkillEditor>
    // client `skill.list` (client/src/promise/generated/types.ts `SkillListOutput`)
    readonly list: () => Promise<{ readonly data: readonly Pick<SkillInfo, 'id' | 'path'>[] }>
  }
  // plugin/src/promise/session.ts `SessionDomain.hook`, keyed by the hook's name as OpenCode's
  // `Hooks<Spec>` is: a hook this plugin takes is a key of `SessionHooks`
  readonly session: {
    readonly hook: <K extends keyof SessionHooks>(name: K, callback: (input: SessionHooks[K]) => Promise<void> | void) => Promise<Registration>
  }
  // plugin/src/promise/event.ts `EventDomain` (client/src/shared-events.ts `SubscribeOptions`)
  readonly event: {
    readonly subscribe: (options?: { readonly types?: readonly string[]; readonly signal?: AbortSignal }) =>
      AsyncIterable<{ readonly type: string }>
  }
}

// plugin/src/promise/registration.ts `Transform`: the callback edits the domain's list, and runs
// again on every rebuild (core/src/state.ts: "Reads rebuild by applying every registered transform
// in order")
export type Transform<Editor> = (callback: (editor: Editor) => void) => Promise<Registration>

// ai/src/schema/messages.ts `SystemPart`
export interface SystemPart {
  readonly type: 'text'
  readonly text: string
}

// The session hooks this plugin takes, each name's input (plugin/src/promise/session.ts): a new
// one is a key here, or added from its own module (`declare module './host.d.ts' { interface
// SessionHooks { … } }`), so the slices that add hooks don't edit one line
export interface SessionHooks {
  readonly context: SessionContextHook
}

// plugin/src/promise/session.ts `SessionContext`: before each model request; `system` is the
// request's system prompt, in order, which the hook may edit (2.3.2 and 2.4.1 alike)
export interface SessionContextHook {
  readonly sessionID: string
  readonly agent: string
  system: SystemPart[]
}

// schema/src/permission.ts, a rule of an agent's `permissions`
export interface PermissionRule {
  readonly action: string
  readonly resource: string
  readonly effect: 'allow' | 'deny' | 'ask'
}

// schema/src/agent.ts `Info`, the fields this plugin sets
export interface AgentInfo {
  name: string
  description?: string
  mode: 'subagent' | 'primary' | 'all'
  hidden: boolean
  permissions: PermissionRule[]
}

// plugin/src/promise/agent.ts `AgentEditor`: `update` of an id it lacks starts from
// `Agent.Info.default` (core/src/agent.ts)
export interface AgentEditor {
  get(id: string): AgentInfo | undefined
  update(id: string, update: (agent: AgentInfo) => void): void
  remove(id: string): void
}

// schema/src/skill.ts `Info`
export interface SkillInfo {
  readonly id: string
  readonly name: string
  readonly description?: string
  readonly autoinvoke?: boolean
  readonly path: string          // a file, or `skill://<id>/SKILL.md` for one held in memory
  readonly content: string
}

// plugin/src/promise/skill.ts `SkillEditor`: `add` replaces the skill of the same id
export interface SkillEditor {
  list(): readonly SkillInfo[]
  get(id: string): SkillInfo | undefined
  add(skill: SkillInfo): void
  remove(id: string): void
}

// plugin/src/promise/plugin.ts `Plugin`
export interface ServerPlugin {
  readonly id: string
  readonly setup: (context: ServerContext) => Promise<(() => Promise<void> | void) | void>
}

// client/src/promise/rpc.ts `RpcClient`: each method, called with its input
export type RpcClient<D extends RpcDefinition> = {
  readonly [Name in keyof D['methods']]: (input?: unknown) => Promise<unknown>
}

// plugin/src/tui/context.ts `Context` (`client: OpenCodeClient`, whose `rpc` is
// client/src/promise/rpc.ts `RpcApi`; `ui.toast.show`, `ToastOptions`)
export interface TuiContext {
  readonly options: Readonly<Record<string, unknown>> | undefined
  readonly client: { readonly rpc: <D extends RpcDefinition>(definition: D) => RpcClient<D> }
  readonly ui: {
    readonly toast: {
      show(options: { readonly message: string; readonly title?: string; readonly variant?: 'info' | 'success' | 'warning' | 'error' }): void
    }
  }
}

// plugin/src/tui/plugin.ts `Definition`
export interface TuiPlugin {
  readonly id: string
  readonly setup: (context: TuiContext) => Promise<(() => Promise<void> | void) | void>
}
