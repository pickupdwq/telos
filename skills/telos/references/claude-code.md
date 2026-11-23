# Supervising from Claude Code

Read this when the current conversation is Claude Code rather than Codex. Everything else in this skill applies unchanged: the same tools, gates, receipts and acceptance rules.

## Identity

Telos identifies you by this Claude session (the Telos Claude plugin attests each Telos tool call). The plugin requires the configured supervisor model and effort, by default Opus 5.5 at `xhigh` or `max`; a rejected `supervisor_model_not_allowed` means the user must switch with `/model` and `/effort`, not that you should retry. Start sessions with `telos claude run`, which applies those defaults.

Only the main conversation supervises. Never call Telos tools from a subagent, a background agent or a teammate: the plugin denies it (`subagent_not_supervisor`). Subagents may still help you review code or evidence, but you make the Telos calls and the grading decisions yourself.

`claude_hooks_missing` means the Telos Claude plugin is not loaded in this session. Tell the user to run `telos claude install` and restart Claude Code. Do not switch to `notify=false` unless the user explicitly wants manual operation.

## Waiting for callbacks

There is no message queue. When a task event is ready, the plugin wakes this conversation with a notification that begins `Telos callback for this supervisor` and contains one or more `[TELOS_EVENT ...]` or `[TELOS_REMINDER ...]` lines. Treat it exactly like a Codex callback: call the receive tool it names with its IDs and follow `proceed` and `next_action`. It is an automatic notification, not the user's reply to anything you asked.

After dispatching work, end your turn and wait; do not poll `telos_status`, sleep, or start a background loop. An event that you were woken for but did not receive is offered again at the next turn boundary, at most three times, then reported as `needs_receipt`.

The plugin listens only while this Claude process is running. If the user closes it, events stay pending and `return_channel.health` reports `waiting_for_owner` with `next_action=resume_original_claude_session`; resuming with `telos claude run --resume <session id>` delivers them. `/clear` keeps the same supervisor binding within the running process, but it also discards your memory of the task, so call `telos_status` before acting after a clear.
