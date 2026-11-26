// The plugin's RPC contract (OpenCode's `./rpc` part: packages/plugin's docs, build/plugins/rpc):
// the server part implements it, the TUI part and any client call it. JSON Schema, so it needs no
// validator package. Data only: importing it does nothing (#1311, 2026-11-25).
export const Workbench = {
  id: 'workbench',
  methods: {
    // which wb the server part runs (the config entry's options, else the package's own bin/wb,
    // else PATH) and wb's state dir
    status: {
      input: { type: 'object', properties: {}, additionalProperties: false },
      output: {
        type: 'object',
        properties: {
          wb: { type: 'string' },
          found: { type: 'boolean' },
          from: { type: 'string', enum: ['options', 'package', 'PATH', 'none'] },
          home: { type: 'string' },
        },
        required: ['wb', 'found', 'from', 'home'],
        additionalProperties: false,
      },
    },
  },
  events: {},
} as const

export interface Status {
  readonly wb: string
  readonly found: boolean
  readonly from: 'options' | 'package' | 'PATH' | 'none'
  readonly home: string
}
