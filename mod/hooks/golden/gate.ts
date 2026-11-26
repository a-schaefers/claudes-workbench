// Kernel.Gate's gateOf: one simple command's words, as a host's parser read them, and its
// redirects' files in; the gate it meets out. Each outcome, the wrappers and options that hid a
// command (#560, #1005), #1364's rule (only a command's own words: a heredoc's body never arrives
// as words, so `python3 -` is no send), the send's arguments read, and words that name a property
// of every JavaScript object (the mod's old tables crashed on them or took them for wrappers).
import type { Golden } from './run.js'

const noSend = { slug: '', title: '', titled: false, bodyFile: '', filed: false, draft: false, anyway: false, lack: '' }
const g = (at: number, word: string, gate: string, more: Record<string, unknown> = {}) =>
  ({ at, word, gate, delegates: false, args: [], dirs: [], writes: [], send: noSend, ...more })
const sent = (more: Record<string, unknown>) => ({ ...noSend, ...more })

export default {
  module: 'Kernel.Gate',
  cases: [
    { fn: 'gateOf', in: { words: [], writes: [] }, out: g(0, '', ''), why: 'no words: no gate' },
    { fn: 'gateOf', in: { words: ['ls', '-la'], writes: [] }, out: g(0, 'ls', ''), why: 'most commands meet no gate' },
    { fn: 'gateOf', in: { words: ['cd', '--', '-x'], writes: [] }, out: g(0, 'cd', 'cd', { args: ['-x'] }), why: 'cd --: the next word is the target' },
    { fn: 'gateOf', in: { words: ['pushd', '-L'], writes: [] }, out: g(0, 'pushd', 'cd'), why: 'only flags: home, no target' },
    { fn: 'gateOf', in: { words: ['CD', '-'], writes: [] }, out: g(0, 'cd', 'cd', { args: ['-'] }), why: 'a word is lowercased; - is the last directory' },
    { fn: 'gateOf', in: { words: ['wb', 'patch', 'send', 'fix', '--title', 'A fix', '--body-file=b.md', '--draft'], writes: [] },
      out: g(0, 'wb', 'send', { args: ['fix', '--title', 'A fix', '--body-file=b.md', '--draft'], send: sent({ slug: 'fix', title: 'A fix', titled: true, bodyFile: 'b.md', filed: true, draft: true }) }),
      why: 'wb patch send, its slug, title, body file and draft read' },
    { fn: 'gateOf', in: { words: ['python3', '/src/bin/wb', 'patch', 'send', 'fix', '--title', '$T', '--body-file', 'b'], writes: [] },
      out: g(0, 'python3', 'send', { args: ['fix', '--title', '$T', '--body-file', 'b'], send: sent({ slug: 'fix', title: '$T', titled: true, bodyFile: 'b', filed: true, lack: 'shell' }) }),
      why: 'Python running …/wb is wb (the review of #1005); a $ only the shell knows can’t be shown' },
    { fn: 'gateOf', in: { words: ['python3', '-m', 'workbench', 'patch', 'send', '--', '-odd', '--title'], writes: [] },
      out: g(0, 'python3', 'send', { args: ['--', '-odd', '--title'], send: sent({ slug: '-odd', lack: 'title' }) }), why: 'after --, the slug; nothing more is read' },
    { fn: 'gateOf', in: { words: ['wb', 'patch', 'send', '--title', 'T', '--body-file'], writes: [] },
      out: g(0, 'wb', 'send', { args: ['--title', 'T', '--body-file'], send: sent({ title: 'T', titled: true, lack: 'slug' }) }), why: 'a --body-file with no value is none, and there is no slug' },
    { fn: 'gateOf', in: { words: ['wb', 'patch', 'zip'], writes: [] }, out: g(0, 'wb', ''), why: 'wb patch zip writes a file the user may email: nothing outward (#1291)' },
    { fn: 'gateOf', in: { words: ['sudo', '-u', 'me', 'git', '-C', '/up', 'push'], writes: [] }, out: g(3, 'git', 'outward', { dirs: ['/up'] }), why: 'past sudo and its valued option; git’s -C named (#1005)' },
    { fn: 'gateOf', in: { words: ['GIT', '-c', 'a=b', 'PUSH'], writes: [] }, out: g(0, 'git', 'outward'), why: 'macOS finds GIT as git; -c takes a value' },
    { fn: 'gateOf', in: { words: ['git', 'pull'], writes: [] }, out: g(0, 'git', ''), why: 'a pull sends nothing' },
    { fn: 'gateOf', in: { words: ['gh', '-R', 'a/b', 'issue', 'create'], writes: [] }, out: g(0, 'gh', 'outward'), why: 'gh issue create, past -R' },
    { fn: 'gateOf', in: { words: ['gh', 'pr', 'view'], writes: [] }, out: g(0, 'gh', ''), why: 'gh pr view reads' },
    { fn: 'gateOf', in: { words: ['gh', 'api', 'repos/x', '-f', 'a=1', '-X', 'GET'], writes: [] }, out: g(0, 'gh', ''), why: 'gh api with fields and an explicit GET: a query, no write' },
    { fn: 'gateOf', in: { words: ['gh', 'api', 'repos/x', '--raw-field=a'], writes: [] }, out: g(0, 'gh', 'outward'), why: 'gh api with a field: POST by default' },
    { fn: 'gateOf', in: { words: ['gh', 'api', '--method=delete', 'x'], writes: [] }, out: g(0, 'gh', 'outward'), why: 'a method other than GET or HEAD writes' },
    { fn: 'gateOf', in: { words: ['gh', 'constructor', 'x'], writes: [] }, out: g(0, 'gh', ''), why: 'a group named like a property of every object: no gate, never a throw' },
    { fn: 'gateOf', in: { words: ['toString', 'git', 'push'], writes: [] }, out: g(0, 'tostring', ''), why: 'only the listed wrappers run the rest: toString is a command of its own' },
    { fn: 'gateOf', in: { words: ['wb', 'route', 'significant', '--api', 'x'], writes: [] }, out: g(0, 'wb', 'route', { args: ['significant', '--api', 'x'] }), why: 'wb route: its own arguments for the dry run' },
    { fn: 'gateOf', in: { words: ['timeout', '60', 'wb', 'start', 't', '--mod', 'm'], writes: [] },
      out: g(2, 'wb', 'start', { delegates: true, args: ['--start', 't', '--mod', 'm'] }), why: 'a start with an abbreviated --model, past timeout’s duration; it hands work out too' },
    { fn: 'gateOf', in: { words: ['wb', 'start', 't'], writes: [] }, out: g(0, 'wb', '', { delegates: true }), why: 'a start without --model hands work out and meets no gate' },
    { fn: 'gateOf', in: { words: ['WB', 'start', 't'], writes: [] }, out: g(0, 'wb', ''), why: 'delegation reads the word as written: WB is no wb there' },
    { fn: 'gateOf', in: { words: ['wb', 'settings', '--json', 'set', 'cap', '9'], writes: [] }, out: g(0, 'wb', 'settings', { args: ['--settings', '--json', 'cap', '9'] }), why: 'settings’ own --json before set (the review of #1336)' },
    { fn: 'gateOf', in: { words: ['wb', 'settings', 'get', 'set'], writes: [] }, out: g(0, 'wb', ''), why: 'set must be the first word after settings’ options' },
    { fn: 'gateOf', in: { words: ['python3', '-'], writes: [] }, out: g(0, 'python3', ''), why: 'an interpreter fed a heredoc: its body is no word, so nothing it mentions counts (#1364)' },
    { fn: 'gateOf', in: { words: ['VAR=x', 'if', '!', 'tee', '-a', 'one', '-', 'two'], writes: ['out'] },
      out: g(3, 'tee', '', { writes: [{ to: 'out', names: [] }, { to: 'one', names: [] }, { to: 'two', names: [] }] }), why: 'past an assignment and reserved words: the redirect’s file, then tee’s' },
    { fn: 'gateOf', in: { words: ['cp', '-t', '/state', 'a/route-ok.json', 'b'], writes: [] }, out: g(0, 'cp', '', { writes: [{ to: '/state', names: ['a/route-ok.json', 'b'] }] }), why: 'a copy into -t’s directory names its sources' },
    { fn: 'gateOf', in: { words: ['mv', '--suffix', '.bak', 'x'], writes: [] }, out: g(0, 'mv', ''), why: 'one operand, its value option skipped: no destination' },
    { fn: 'gateOf', in: { words: ['dd', 'if=a', 'of=/state/route-ok.json'], writes: [] }, out: g(0, 'dd', '', { writes: [{ to: '/state/route-ok.json', names: [] }] }), why: 'dd writes of=' },
  ],
} satisfies Golden
