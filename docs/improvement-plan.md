# claude-telegram-skill Improvement Plan

## Hook adapter conformance matrix for Codex and opencode [2605.codex-hooks]

- Source idea reference: `/Users/azalio/gitroot/azalio/azalio-obsidian/azalio/sources/articles/hooks-in-codex-what-they-are-and-how-to-use.md`
- Benefit hypothesis: a conformance matrix with fixture-level hook invocations would catch adapter regressions before users discover frozen Codex turns, dropped opencode replies, or broken notification routing.
- Confidence: high.
- Reasoning: the bridge now serves Claude Code, Codex, and opencode through thin adapters over one Python runtime; architecture already calls out sharp inbound-behavior differences and version-sensitive Codex hook trust.
- Why not already tried: current tests mock Telegram and validate install/handlers, but the architecture explicitly says they do not run the real Codex/opencode hook runtimes.
- Implementation layer: `scripts/tg.py`, `codex/hooks.json`, `opencode/plugin/telegram-bridge.ts`, `hooks/hooks.json`, and `tests/test_e2e.py`.
- Missing capability: a repeatable compatibility check that proves each adapter emits the expected stdin contract, environment variables, non-blocking behavior, and user-visible routing outcome.
- Architecture evidence: `docs/ARCHITECTURE.md` documents one shared `scripts/tg.py` core, per-agent adapters, `TG_AGENT` behavior, Codex turn-boundary reinjection, opencode `session.promptAsync`, and Known Risks/Gaps around blocking `tg listen`, Codex hook trust, and missing real runtime coverage.

### Proposed Changes

- Add a table-driven fixture suite for `sessionstart`, `stop`, `userprompt`, and `notification` across Claude Code, Codex, and opencode adapter payloads.
- Assert that Codex and opencode never invoke blocking `tg listen`, and that Codex `Stop` replies produce the expected `hookSpecificOutput.additionalContext` / blocking reason shape.
- Add a lightweight generated-conformance document under `docs/` that lists supported hook events, expected payload keys, allowed side effects, and validation command.
- Keep tests offline by stubbing Telegram API and process spawning; do not require a live bot token or an installed Codex/opencode binary.

## Policy-coded inbound reply gating and audit [2606.lelu]

- Source idea reference: `/Users/azalio/gitroot/azalio/azalio-obsidian/azalio/sources/articles/github-lelu-ai-lelu-open-source-authorization-engine-for-ai-agents-confidence-aware-f4944ffd4137.md`
- Benefit hypothesis: adding explicit local policy decisions for inbound Telegram replies would reduce the chance that a routed reply becomes an unreviewed high-risk agent prompt, while preserving the bridge's dependency-free runtime.
- Confidence: medium.
- Reasoning: the project already enforces `user_id` allow-listing and reply-to routing, but it does not classify or audit the content/action risk of delivered replies; a small deny-first local policy layer fits the single-file Python boundary better than integrating a full authorization service.
- Why not already tried: the current safety model is identity and session attribution; architecture does not describe policy-as-code, per-message risk audit, or human-review nudges beyond dropping plain/unknown replies.
- Implementation layer: `scripts/tg.py`, `config.example.json`, `skills/telegram/SKILL.md`, and `tests/test_e2e.py`.
- Missing capability: deterministic inbound decision records (`allow`, `nudge_confirm`, `deny`) before writing a reply into a session inbox.
- Architecture evidence: `docs/ARCHITECTURE.md` says inbound routing accepts only configured `user_id` replies tied to `sent.map`, writes to `inbox.jsonl` before offset advancement, and intentionally drops plain messages because there is no safe session target.

### Proposed Changes

- Add optional config keys for inbound policy: blocked literal patterns, confirmation-required patterns, max message length, and audit log retention.
- Record each inbound decision to a local JSONL audit file under `TG_STATE_DIR`, excluding bot tokens and without sending the content to external services.
- For confirmation-required replies, send a nudge asking the user to reply with an explicit confirmation token before the original text is delivered.
- Cover allow/deny/confirm flows with offline tests, including crash-safe behavior that does not advance Telegram offset before the decision is durably recorded.
