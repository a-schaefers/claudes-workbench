import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'workbench',
  component: 'Pane',
  requestId: 'workbench',
  props: {
    title: 'Workbench',
    isFocused: false,
    bodyColumns: 50,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 20 },
    view: {},
  },
} as const

test('with no tasks the pane says how to start one, on every surface', async ($, on) => {
  on('clock.now', () => ({ value: 1_800_000_000_000 }))
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /No delegated tasks/ }))?.text)
      .toBe('No delegated tasks.')
    await ui.unmount()
  }
})

test('/workbench opens the pane and counts open tasks', async ($, on) => {
  let opened = ''
  on('command.register', () => ({ value: { command: 'workbench' } }))
  on('ui.open', (_, e) => {
    opened = e.id
    return { value: { isPlaced: true } }
  })
  on('env.get', () => ({ value: '/nonexistent' }))
  on('clock.now', () => ({ value: 1_800_000_000_000 }))
  on('clock.every', () => ({ value: { cancel: () => {} } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const out = await $.command.run({ command: 'workbench', args: '' })
  expect(opened).toBe('workbench')
  expect(JSON.stringify(out)).toContain('no open tasks')
})
