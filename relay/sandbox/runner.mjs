// Code Mode runner: runs one snippet in a QuickJS (WebAssembly) interpreter with no I/O of its own.
// Started by the relay per execute() call as `node --permission --allow-fs-read=<this dir>`, with an empty
// environment, so the Node process itself can't open files, sockets or child processes either.
//
// Protocol (one JSON object per line):
//   stdin  first line : {"code": "...", "methods": ["boxes.list", ...], "limits": {...}}
//   stdout            : {"call": {"id": 1, "method": "boxes.list", "args": {...}}}
//   stdin             : {"id": 1, "ok": true, "value": ...} | {"id": 1, "ok": false, "error": "..."}
//   stdout last line  : {"done": {"ok": true, "value": ..., "logs": [...]}} | {"done": {"ok": false, "error": "..."}}
// The relay owns every method: each call is checked and answered there, never here.
import { newQuickJSWASMModuleFromVariant } from "quickjs-emscripten-core"
import variant from "@jitl/quickjs-wasmfile-release-sync"
import readline from "node:readline"

const rl = readline.createInterface({ input: process.stdin })
const lines = rl[Symbol.asyncIterator]()
const out = (o) => process.stdout.write(JSON.stringify(o) + "\n")
const done = (o) => { out({ done: o }); process.exit(0) }

const first = await lines.next()
if (first.done) done({ ok: false, error: "no input" })
const job = JSON.parse(first.value)
const L = { memoryBytes: 32 << 20, stackBytes: 512 << 10, cpuMs: 10000, maxCalls: 50, maxLogs: 200,
  maxOutBytes: 256 << 10, ...job.limits }

const pending = new Map()
let nextId = 0
;(async () => {
  for await (const line of { [Symbol.asyncIterator]: () => lines }) {
    let m
    try { m = JSON.parse(line) } catch { continue }
    const p = pending.get(m.id)
    if (p) { pending.delete(m.id); p(m) }
  }
})()

const QuickJS = await newQuickJSWASMModuleFromVariant(variant)
const rt = QuickJS.newRuntime()
rt.setMemoryLimit(L.memoryBytes)
rt.setMaxStackSize(L.stackBytes)
// CPU budget counts interpreter time only; time spent waiting on the relay is not charged.
let cpu = 0, slice = Date.now()
rt.setInterruptHandler(() => cpu + (Date.now() - slice) > L.cpuMs)
const vm = rt.newContext()

const logs = []
const log = vm.newFunction("__log", (...hs) => {
  if (logs.length < L.maxLogs) logs.push(hs.map((h) => { const v = vm.dump(h); return typeof v === "string" ? v : JSON.stringify(v) }).join(" ").slice(0, 2000))
})
vm.setProp(vm.global, "__log", log); log.dispose()

let calls = 0
const host = vm.newFunction("__call", (mh, ah) => {
  const method = vm.getString(mh)
  const argsJson = vm.getString(ah)
  const d = vm.newPromise()
  if (!job.methods.includes(method)) {
    const e = vm.newError(`unknown method ${method}`); d.reject(e); e.dispose()
  } else if (++calls > L.maxCalls) {
    const e = vm.newError(`call limit reached (${L.maxCalls} per run)`); d.reject(e); e.dispose()
  } else {
    const id = ++nextId
    pending.set(id, (m) => {
      if (!d.alive) return
      if (m.ok) {
        const r = vm.evalCode(`(${JSON.stringify(JSON.stringify(m.value ?? null))})`)
        const v = vm.unwrapResult(r); const parsed = vm.evalCode(`JSON.parse`); const fn = vm.unwrapResult(parsed)
        const res = vm.unwrapResult(vm.callFunction(fn, vm.undefined, v)); d.resolve(res)
        res.dispose(); fn.dispose(); v.dispose()
      } else {
        const e = vm.newError(String(m.error || "failed")); d.reject(e); e.dispose()
      }
      d.settled.then(pump)
    })
    out({ call: { id, method, args: JSON.parse(argsJson) } })
  }
  return d.handle
})
vm.setProp(vm.global, "__call", host); host.dispose()

// The snippet sees one object per namespace (boxes.list(...), kanban.list(...)) plus console.log.
const prelude = `
(() => {
  const call = globalThis.__call, lg = globalThis.__log;
  globalThis.console = Object.freeze({ log: (...a) => lg(...a), error: (...a) => lg(...a) });
  const ns = {};
  for (const m of ${JSON.stringify(job.methods)}) {
    const [n, fn] = m.split(".");
    (ns[n] ||= {})[fn] = (args = {}) => call(m, JSON.stringify(args ?? {}));
  }
  for (const k of Object.keys(ns)) globalThis[k] = Object.freeze(ns[k]);
  delete globalThis.__call; delete globalThis.__log;
})();
`
const pre = vm.evalCode(prelude)
if (pre.error) { const e = vm.dump(pre.error); pre.error.dispose(); done({ ok: false, error: "setup failed: " + JSON.stringify(e) }) }
pre.value.dispose()

slice = Date.now()
const res = vm.evalCode(`(async () => {\n${job.code}\n})()`, "snippet.js")
cpu += Date.now() - slice
if (res.error) {
  const e = vm.dump(res.error); res.error.dispose()
  finish({ ok: false, error: describe(e) })
}
const promise = res.value

function describe(e) {
  if (e && typeof e === "object") return `${e.name || "Error"}: ${e.message || JSON.stringify(e)}`
  return String(e)
}

function pump() {
  slice = Date.now()
  const r = rt.executePendingJobs()
  cpu += Date.now() - slice
  if (r.error) { const e = vm.dump(r.error); r.error.dispose(); finish({ ok: false, error: describe(e) }) }
  else r.dispose?.()
  check()
}

function check() {
  const st = vm.getPromiseState(promise)
  if (st.type === "pending") {
    if (pending.size === 0) finish({ ok: false, error: "the snippet stopped without returning (an await that never ends)" })
    return
  }
  if (st.type === "fulfilled") {
    const v = st.notAPromise ? vm.dump(promise) : vm.dump(st.value); st.value?.dispose?.()
    const text = JSON.stringify(v ?? null)
    if (text.length > L.maxOutBytes) finish({ ok: false, error: `result over ${L.maxOutBytes} bytes: return less (filter in the snippet)` })
    else finish({ ok: true, value: v ?? null })
  } else {
    const e = vm.dump(st.error); st.error?.dispose?.()
    finish({ ok: false, error: describe(e) })
  }
}

function finish(o) {
  if (cpu > L.cpuMs && !o.ok) o.error = `time limit reached (${L.cpuMs} ms of CPU)`
  done({ ...o, logs, calls })
}

pump()
