// telegram-bridge plugin for opencode.
//
// opencode has no external-command hooks (unlike Claude Code / Codex), so this thin
// TypeScript plugin translates opencode's plugin events into the SAME stdin-JSON
// contract that scripts/tg.py already implements for the other agents, then shells
// back into it. All real logic stays in tg.py — this file only maps events.
//
// Installed by `python3 scripts/tg.py install opencode`, which copies this file into
// $OPENCODE_CONFIG_DIR/plugin/ and replaces __TG_PY__ with the absolute path to tg.py.
// The always-listen instructions go into AGENTS.md instead of here, because opencode
// has no session-start context injection — the agent itself runs `tg listen`.
//
// The `import type` is elided at runtime (Bun strips types), so the plugin needs no
// installed dependency.
import type { Plugin } from "@opencode-ai/plugin"

const TG_PY = "__TG_PY__"

type Json = Record<string, unknown>

// Feed a synthesized hook payload to `tg.py hook <event>` on stdin. stdout is ignored:
// the sessionstart handler prints additionalContext JSON that opencode can't consume
// (we deliver those instructions via AGENTS.md instead). Never throws — a notifier
// failure must not break the session.
async function runHook(event: string, payload: Json, directory: string): Promise<void> {
  try {
    const proc = Bun.spawn(["python3", TG_PY, "hook", event], {
      env: { ...process.env, TG_CWD: directory, TG_AGENT: "opencode" },
      stdin: "pipe",
      stdout: "ignore",
      stderr: "ignore",
    })
    proc.stdin.write(JSON.stringify(payload))
    proc.stdin.end()
    await proc.exited
  } catch {
    /* swallow: best-effort notifier */
  }
}

// Best-effort fetch of the last assistant message text for the idle mirror. The SDK
// message shape varies across versions, so this is defensive; on any miss the mirror
// falls back to a generic line.
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
  return {
    // Bus events: announce on session start, mirror on idle (Stop analogue).
    event: async ({ event }: { event: any }) => {
      if (event?.type === "session.created") {
        const id = event?.properties?.info?.id ?? ""
        await runHook("sessionstart", { cwd: directory, session_id: id }, directory)
      } else if (event?.type === "session.idle") {
        const id = event?.properties?.sessionID ?? ""
        const last = await lastAssistantText(client, id)
        await runHook(
          "stop",
          { cwd: directory, session_id: id, last_assistant_message: last },
          directory,
        )
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
