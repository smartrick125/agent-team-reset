---
name: agent-team-reset
description: Rotate task-specific AI chats after a task ends while retaining ongoing team members. Renew persistent chats at configured conversation thresholds, preserving short handoffs. Use for team wrap-up, restart, and moving the whole team to another platform, including 全组收口, 全组开工, and 全组移交.
---

# Reset an agent team

Keep roles and unfinished work across fresh chats without copying the full conversation history. Use a single coordinator to write lifecycle state. Do not change the team's project responsibilities.

## Establish the team

Read the project's role definitions and identify the intended chats in the current host. Confirm ambiguous matches. Never assume every visible chat belongs to the team.

Classify each member with the user or existing role definitions. A `task` member belongs to the current bounded task, such as today's learning guidance. A `persistent` member supports ongoing work, such as publicity across multiple days. Decide by whether tomorrow needs today's full conversation: if a short handoff is enough, the member is `task` even when the role returns tomorrow. Do not infer task scope from a title alone when responsibility is unclear. The next task may have different members. Never archive persistent members just because the day ended.

Persistent members may set `max_user_turns` to a positive integer. One user message counts as one user turn; assistant messages and tool calls do not count. A null or missing threshold disables automatic threshold selection. Ask for the user's preferred threshold without blocking task-member rollover. Never invent a default or claim user turns measure tokens.

The bundled configuration uses 40 user turns. Preserve an existing project threshold. When this configuration is adopted, renew persistent members at 40 observed user turns after handoff and a safe stopping point; retain task members' separate task-completion policy.

Create a project-local config from [the example](references/team.json). Choose any number of roles. Store role handoffs and lifecycle state outside the distributable skill. Chat IDs must come from the host's actual tools. Include the host identity in the state and refuse to operate through a different host.

Optional config keys, all enforced by the script: `roster` writes a full member table (role, title, type, user turns against limit, chat ID, handoff path, sealed, archived) and rewrites it after every saved change, so a wrap-up never needs to read each handoff to know the team; `transcripts` (`format` claude or codex, `dir`) lets `count` read user turns from local transcripts; `keep_history` (default 10 batches) and `max_handoff_lines` (default 80) cap stored history and refuse to seal an oversized handoff. Retired chat IDs keep the latest 50.

Use `python <skill-dir>/scripts/rotate.py --help` for commands. The script checks local state and prints plans. It does not create, message, archive, delete, or clear chats.

## Close the team

1. Treat the user's explicit team wrap-up request as authorization to ask current task members for handoffs and archive those chats. Persistent-member maintenance needs a user request or an existing standing authorization. A request to explain or install this skill does not authorize chat operations. Register actual chat IDs in the current batch.
2. Ask only the members selected for rotation to overwrite a short handoff (at most `max_handoff_lines`) containing unfinished work, relevant decisions, pitfalls, the next action, and file references. The coordinator writes its own handoff when selected. Wait for these members to finish. Do not interrupt active work to manufacture an idle state. Ongoing persistent members do not block task closure.
3. Read each updated handoff and confirm the member has finished. Run `seal` for that role. A hash proves that the file has not changed since confirmation; it does not prove the handoff is accurate or complete.
4. Run `close-plan`. It selects task members and persistent members with observed usage at or above their configured threshold. Execute only the part authorized by the user. Verify current host status, then archive only those IDs with the host's supported archive tool. After each successful operation, record `archived`. Reconcile uncertain results with the host before retrying. If the coordinator is selected, archive it last after saving the other receipts and explaining the restart entry point.

If a member needs approval or has unfinished active work, stop before archiving. Report the specific role and remaining action. Preserve unresolved project requests.

## Rebuild the team

1. Require an explicit team start or restart request. Query the host to reconcile archive receipts. Begin a new unique batch only after old task members are archived. Preserve ongoing persistent members with their existing chat IDs and usage. Update task membership for the new task; do not silently remove or reclassify a live persistent member.
2. Run `open-plan`. Create only missing chats in the intended project, using configured titles and startup prompts. Start fresh chats rather than forks. Prompt each member to read project rules and its own handoff, then read other files as needed.
3. Save each returned chat ID with `register` before creating the next chat. If a create result is uncertain, search the host for the batch marker before retrying. Wait for each new member to acknowledge its role or request input. Hand state ownership to the new coordinator.

Do not start unrelated project work during reconstruction. Follow host requirements for displaying created chats.

## Renew a persistent member

When `transcripts` is configured, run `count`: it records every active member's user turns and marks members at their threshold as due. Otherwise read the count from the host and record it with `observe --role ROLE --thread ID --user-turns N`. Claude counts deduplicate resumed transcript files and skip tool results and system reminders. Codex counts deduplicate message IDs whose source metadata includes `user.text`; execution starts, forwarded tasks and injected context do not count. A `sessions` source also checks its sibling `archived_sessions`. Missing source metadata, legacy user events, unidentified messages or malformed records make the count unknown; use `observe` only with a verified complete cumulative count. Counts must not decrease. A lower transcript count preserves the stored value and warns that it needs an audit before threshold plans are used; `observe` cannot lower an inflated legacy value. Correcting such state requires a separately authorized, backed-up repair, never an automatic reset. If the host cannot provide a complete count, report that limitation and do not infer a count from a truncated summary.

Run `maintenance-plan` to select only persistent members at their configured threshold. A threshold schedules handoff at the next safe stopping point; it does not authorize interrupting work. Confirm the handoff, seal it, archive through the host, and record the receipt. Then run `renew --role ROLE --thread OLD_ID` to prepare that role for recreation. `open-plan` returns that missing role while retaining other members. Register its new ID and read its handoff. The new chat starts with an unknown count until observed.

## Hand the team to another host

Use this when one platform runs low on quota and the whole team moves; two hosts never maintain the team at once.

1. Require an explicit 全组移交 or platform-switch request. Run `handover-plan`: it selects every live member regardless of lifecycle or threshold and reports which handoffs are sealed. Registering or renewing chats is refused from now on.
2. Ask every listed member to overwrite its handoff, confirm it, `seal` it, archive the chat through the host, and record `archived`.
3. Run `handed-over --to NEW_HOST`. It refuses while any member is live. Afterwards the old state accepts only `status`, and the roster shows the handover.
4. On the new host, copy the config with the new `host` (and `transcripts`), use a new state file, `begin` a batch, and run `open-plan`. Each new member reads the roster and its own handoff. Keep handoffs in the project folder both hosts can read, never in a host-private directory.

## Respect host limits

For Codex, use available native thread tools to list, message, wait, create, and archive chats. For another host, inspect that host's supported tools before acting. Codex thread tools do not manage Claude chats. If the host cannot archive or create chats, save the verified handoffs and report the precise manual step. Do not modify private application databases or invent an API.

Archiving removes a chat from the active list. It does not erase messages in an open chat. A fresh chat provides a fresh conversation. Permanent deletion is outside this skill's default scope.

This skill runs on explicit requests. It is not a background task-completion watcher. Report separately what was saved, what was actually archived, and what was actually created. Never report a generated plan as a completed host operation or promise a fixed token saving.
