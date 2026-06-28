# claude-telegram-skill Improvement Plan

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
