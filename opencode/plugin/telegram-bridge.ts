// telegram-bridge plugin for opencode.
//
// opencode has no external-command hooks (unlike Claude Code / Codex) and its shell
// tool has NO background mode — a blocking `tg listen` would freeze the turn. So inbound
// is plugin-driven: a process-lifetime poll loop pulls Telegram messages from tg.py and
// injects them into the idle session via the SDK (`session.promptAsync`, non-blocking).
// Outbound (announce, idle-mirror, permission notify) is delegated to the same tg.py hook
// handlers that serve Claude Code and Codex, so all real logic stays in tg.py.
//
// Installed by `python3 scripts/tg.py install opencode`, which copies this file into
// $OPENCODE_CONFIG_DIR/plugin/ and replaces __TG_PY__ with the absolute path to tg.py.
// The always-listen instructions go into AGENTS.md (opencode has no session-start
// injection); they tell the agent to acknowledge in Telegram and NOT to run a listener.
//
// `import type` is elided at runtime (Bun strips types), so the plugin needs no dependency.
import type { Plugin } from "@opencode-ai/plugin"

const TG_PY = "__TG_PY__"

type Json = Record<string, unknown>

// Routing key is the project directory (same as Claude Code keys by cwd), so the agent's
// own `tg send` from its shell (cwd = directory) and the plugin's tg.py calls share a key.
function tgEnv(directory: string): Record<string, string> {
  return { ...(process.env as Record<string, string>), TG_CWD: directory, TG_AGENT: "opencode" }
}

// Feed a synthesized hook payload to `tg.py hook <event>` on stdin. stdout is ignored:
// the sessionstart handler prints additionalContext that opencode can't consume (we
// deliver those instructions via AGENTS.md). Never throws.
async function runHook(event: string, payload: Json, directory: string): Promise<void> {
  try {
    const proc = Bun.spawn(["python3", TG_PY, "hook", event], {
      env: tgEnv(directory),
      stdin: "pipe",
      stdout: "ignore",
      stderr: "ignore",
    })
    proc.stdin.write(JSON.stringify(payload))
    proc.stdin.end()
    await proc.exited
  } catch {
    /* best-effort notifier */
  }
}

// One non-blocking receive cycle (lock -> pump -> claim). Returns the message text routed
// to this project, or null when there is none (tg.py exits 3) or on error. tg.py dedupes
// by Telegram update_id, so the same message is never returned twice.
async function recvMessage(directory: string): Promise<string | null> {
  try {
    const proc = Bun.spawn(["python3", TG_PY, "recv", "0"], {
      env: tgEnv(directory),
      stdout: "pipe",
      stderr: "ignore",
    })
    const text = (await new Response(proc.stdout).text()).trim()
    const code = await proc.exited
    return code === 0 && text ? text : null
  } catch {
    return null
  }
}

// Best-effort last assistant message for the idle mirror; defensive across SDK versions.
async function lastAssistantText(client: any, id: string): Promise<string> {
  try {
    const res = await client.session.messages({ path: { id } })
    const msgs: any[] = res?.data ?? res ?? []
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i]
      const info = m?.info ?? m
      if (info?.role !== "assistant") continue
      const parts: any[] = m?.parts ?? info?.parts ?? []
      const text = parts
        .filter((p) => p?.type === "text")
        .map((p) => p?.text ?? "")
        .join("")
        .trim()
      if (text) return text
    }
  } catch {
    /* best effort */
  }
  return ""
}

export const TelegramBridge: Plugin = async ({ directory, client }) => {
  const idle = new Set<string>() // sessions safe to inject into (idle, not mid-turn)
  const pending = new Map<string, string[]>() // claimed-but-not-yet-injected (BusyError re-queue)
  let injecting = false // serialize injects so we never start two turns at once

  // Inject a Telegram message as a user prompt. promptAsync returns 204 immediately
  // (non-blocking); injecting into a busy session throws BusyError, which we catch.
  async function inject(id: string, text: string): Promise<boolean> {
    try {
      await client.session.promptAsync({
        path: { id },
        query: { directory },
        body: { parts: [{ type: "text", text: `Telegram message from the user:\n${text}` }] },
      })
      return true
    } catch {
      return false // BusyError or transient — caller re-queues
    }
  }

  // Deliver at most one pending/new message to an idle session. Gated so a message is
  // never lost (claimed text is buffered until an inject succeeds) and a turn is never
  // started while another inject is in flight or the session is busy.
  async function deliver(id: string): Promise<void> {
    if (injecting || !idle.has(id)) return
    injecting = true
    try {
      const buf = pending.get(id) ?? []
      let text = buf.shift() ?? null
      if (!text) text = await recvMessage(directory)
      if (!text) return
      idle.delete(id) // injecting starts a turn -> treat as busy until next session.idle
      const ok = await inject(id, text)
      if (!ok) {
        buf.unshift(text) // re-queue and retry on the next idle
        idle.add(id)
      }
      if (buf.length) pending.set(id, buf)
      else pending.delete(id)
    } finally {
      injecting = false
    }
  }

  // Single process-lifetime poll loop (the plugin body runs once per opencode process).
  // A globalThis guard prevents a second loop if the module is somehow loaded twice.
  const g = globalThis as any
  if (!g.__tgBridgePollStarted) {
    g.__tgBridgePollStarted = true
    const timer = setInterval(() => {
      for (const id of [...idle]) void deliver(id)
    }, 1500)
    if (timer && typeof (timer as any).unref === "function") (timer as any).unref()
  }

  return {
    event: async ({ event }: { event: any }) => {
      const t = event?.type
      if (t === "session.created") {
        const id = event?.properties?.info?.id ?? ""
        if (id) {
          await runHook("sessionstart", { cwd: directory, session_id: id }, directory)
          idle.add(id)
        }
      } else if (t === "session.idle") {
        const id = event?.properties?.sessionID ?? ""
        if (!id) return
        const last = await lastAssistantText(client, id)
        await runHook("stop", { cwd: directory, session_id: id, last_assistant_message: last }, directory)
        idle.add(id) // turn finished -> safe to inject
        await deliver(id)
      } else if (t === "session.status") {
        // turn-start signal -> pause injection (belt-and-suspenders with BusyError catch)
        const id = event?.properties?.sessionID ?? event?.properties?.sessionId ?? ""
        const st = event?.properties?.status?.type ?? event?.properties?.type
        if (id && st === "busy") idle.delete(id)
      } else if (t === "session.deleted") {
        const id = event?.properties?.info?.id ?? event?.properties?.sessionID ?? ""
        if (id) {
          idle.delete(id)
          pending.delete(id)
        }
      }
    },
    // UserPromptSubmit analogue: cancels the idle auto-mirror when you type locally.
    "chat.message": async (input: any) => {
      await runHook("userprompt", { cwd: directory, session_id: input?.sessionID ?? "" }, directory)
    },
    // Notification analogue: only mirrors while away mode is on (same gate as Claude).
    "permission.ask": async (input: any) => {
      const msg = input?.title || input?.metadata?.title || input?.type || "Agent needs your approval"
      await runHook("notification", { cwd: directory, message: String(msg) }, directory)
    },
  }
}
