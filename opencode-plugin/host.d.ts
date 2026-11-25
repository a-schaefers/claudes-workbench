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
