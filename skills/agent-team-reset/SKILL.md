---
name: agent-team-reset
description: Rotate task-specific AI chats after a task ends while retaining ongoing team members. Renew persistent chats at configured conversation thresholds, preserving short handoffs. Use for team wrap-up, restart, and moving the whole team to another platform, including 全组收口, 全组开工, and 全组移交.
---

# Reset an agent team

Keep roles and unfinished work across fresh chats without copying the full conversation history. Use a single coordinator to write lifecycle state. Do not change the team's project responsibilities.

## Establish the team

Read the project's role definitions and identify the intended chats in the current host. Confirm ambiguous matches. Never assume every visible chat belongs to the team.

Classify each member with the user or existing role definitions. A `task` member belongs to the current bounded task, such as today's learning guidance. A `persistent` member supports ongoing work, such as publicity across multiple days. Decide by whether tomorrow needs today's full conversation: if a short handoff is enough, the member is `task` even when the role returns tomorrow. Do not infer task scope from a title alone when responsibility is unclear. The next task may have different members. Never archive persistent members just because the day ended.

Classify persistent members explicitly with `persistent_class`: `maintenance` rotates at 30 user turns and `regular` at 20. Do not infer the class from the chat title. If `max_user_turns` is also present, it must equal the class limit. One user message counts as one user turn; assistant messages and tool calls do not count. Counts do not measure tokens. Task members keep their task-completion policy.

An unclassified persistent member without `max_user_turns` uses the regular 20-turn limit. Unclassified legacy members with an explicit `max_user_turns` retain that value, including a disabled null threshold, until an explicit migration. To adopt the new policy, edit member classification and threshold fields in the project config, then run `policy` with the current batch. You may also correct or remove the optional top-level `tier` description, which has no execution effect. The command preserves chat IDs, counts, handoffs, lifecycle, and membership. It refuses other config changes.

Create a project-local config from [the example](references/team.json). Choose any number of roles. Store role handoffs and lifecycle state outside the distributable skill. Chat IDs must come from the host's actual tools. Include the host identity in the state and refuse to operate through a different host.

Optional config keys, all enforced by the script: `roster` writes a full member table (role, title, type, user turns against limit, chat ID, handoff path, sealed, archived) and rewrites it after every saved change, so a wrap-up never needs to read each handoff to know the team; `transcripts` (`format` claude or codex, `dir`) lets `count` read user turns from local transcripts; `keep_history` (default 10 batches) and `max_handoff_lines` (default 80) cap stored history and refuse to seal an oversized handoff. Retired chat IDs keep the latest 50.

Use `python <skill-dir>/scripts/rotate.py --help` for commands. The script checks local state and prints plans. It does not create, message, archive, delete, or clear chats. Only explicit `cleanup --apply` deletes eligible local handoff files.

## Check the configured roster

Run `check --refresh-counts` for the current batch to read current transcript counts without saving shared state or rewriting the roster. The configuration defines expected members, including roles that have never registered a chat. The output contains `mode`, `missing`, `due`, `count_status`, and one next action per affected role. Unknown sources and a count below the stored value produce an `audit` action and block automatic rotation for that role. Do not treat unknown as zero. Only the lifecycle-state coordinator persists verified cumulative counts with `count` or `observe`. Plain `check` uses already recorded values. These are explicit checks, not a background monitor.

To connect a previously unconfigured project to local logs, edit only the top-level `transcripts` source and run `configure-source` with the current batch. It validates the format and existing directory, preserving all chat IDs, counts, handoffs, and membership. It cannot change other config fields. A configured directory does not prove every chat has a complete identifiable transcript. `count` reports unknown sources instead of inventing zero.

In `running` mode, a role with no chat gets a `create` plan. An archived role gets `renew` before replacement. Reconcile actual host status before creating anything, and register each real new ID immediately. To compare recorded IDs with the host, save a verified active-ID snapshot as a JSON string list and pass `--live-threads PATH` to `check`. A recorded chat absent from that snapshot gets `reconcile`, not an invented archive receipt or an immediate duplicate chat.

At a persistent member's limit, stop at a safe point and overwrite the role's core handoff with unfinished tasks, decisions, pitfalls, file references, and the next action. Confirm the content, `seal` it, and only then archive through the host and record `archived`. `renew --role ROLE --thread OLD_ID` requires that receipt. It retains the sealed handoff for the successor. Use `open-plan` to create only the missing configured role, register its new ID, and obtain its read confirmation. Never interrupt active work or invent host receipts.

`mode` is explicit: `running`, `closing`, `closed`, or `handing_over`. An empty running roster still needs members. An intentionally stopped roster does not. In `closing` and `closed`, `check` never plans replacements and `open-plan` returns an empty list. Only an explicit new start with `begin` and a new batch reopens the team.

## Close the entire team

For an authorized whole-team stop, run `close-team` before archiving anyone. It persists `closing` first, then plans a core handoff and archive for every live configured member, regardless of class or usage. Save and confirm each current core handoff, `seal`, archive through the host, and record `archived`. The last receipt changes the mode to `closed`. Repeat checks do not rebuild this team. Preserve unfinished work in the handoffs for the next authorized start.

`close-plan` retains its separate task-wrap-up meaning: task members and persistent members at their threshold. It does not declare a whole-team stop.

## Consume and bound handoff files

Keep one configured handoff file per role, overwritten rather than appended. Use a dedicated `handoffs/` directory under the project config directory. Keep role reports elsewhere. The line cap still applies, and core unfinished work must survive every rewrite.

After reading the inherited sealed file, the actual new chat confirms its new ID and the sealed hash. Record `read-receipt --role ROLE --thread NEW_ID --sha256 HASH`. The command requires a registered active successor and unchanged inherited content. It records evidence supplied by the host, not proof that the script read on the chat's behalf. Before the receipt, sealing a replacement handoff is refused.

Periodically run `cleanup` to preview consumed handoff files. Add `--apply` only when deletion is authorized. Use `--role ROLE` to inspect a single role. Cleanup requires a receipt matching the current new chat and unchanged sealed hash. It refuses links, directories, paths outside the dedicated `handoffs/` directory, and config, state, or roster files. It never deletes role reports or unconsumed core handoffs. Rerunning cleanup does not delete a subsequently updated file. After consumption, later core handoffs overwrite the same role path, and `seal` starts a new handoff cycle.

For legacy report paths, first copy each handoff into its new dedicated path without deleting the report. Change only the config's `handoff` fields and run `migrate-handoffs`. Old and new files must have identical content, matching any saved seal. IDs, counts, archive receipts, and lifecycle stay intact. `policy` cannot migrate paths.

## Keep unfinished work assigned

Apply these rules when handing off an already authorized task, including work carried across chat rotation. Use the project's own roles and workflow. A review owner, repair owner, and coordinator describe responsibilities, not required chat names or additional team members.

1. Read the latest issue state and evidence before acting. Record one current owner and one concrete next action for each issue. Unreviewed feedback belongs to the review owner. A confirmed problem awaiting a fix belongs to the repair owner. A completed fix awaiting independent review belongs to the review owner. Do not treat an empty feedback list as an issue.
2. If you own the next action, perform it within the authorized scope. Do not wait for a later participant to act first. When your action is complete, save the evidence and transfer ownership through the project's workflow before notifying the recipient. Preserve the issue ID and event ID when retrying the same transfer.
3. If you cannot perform your action, record the specific blocker, evidence, attempted steps, and the decision or access needed. Route that blocker to the project's coordinator rather than waiting on the next review or repair stage. Keep the interrupted stage so work can resume there. Blocking and resuming do not count as failed repair reviews. Follow the project's configured review limit separately.
4. Persisting state and delivering a message are separate operations. Use supported host tools only within the user's messaging authorization. Reconcile uncertain delivery with the recipient's actual chat before retrying. Report missing tools or authorization as a blocker, never as a completed transfer.
5. Carry the current owner, next action, and unresolved blockers into the rotation handoff. After recreation, reconcile the latest project state before resuming so an older handoff cannot reclaim work already transferred. These rules do not install a watcher or start unrelated work.

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

When `transcripts` is configured, run `count`: it records every active member's user turns and marks members at their threshold as due. Otherwise read the count from the host and record it with `observe --role ROLE --thread ID --user-turns N`. Claude counts deduplicate message UUIDs within the active chat's `sessionId` or matching transcript filename and skip tool results and system reminders. A reused title never imports a retired chat's count into its successor. If the active session cannot be identified, the count stays unknown. Codex counts deduplicate message IDs whose source metadata includes `user.text`; execution starts, forwarded tasks and injected context do not count. A `sessions` source also checks its sibling `archived_sessions`. Missing source metadata, legacy user events, unidentified messages or malformed records make the count unknown; use `observe` only with a verified complete cumulative count. Counts must not decrease. A lower transcript count preserves the stored value and warns that it needs an audit before threshold plans are used; `observe` cannot lower an inflated legacy value. Correcting such state requires a separately authorized, backed-up repair, never an automatic reset. If the host cannot provide a complete count, report that limitation and do not infer a count from a truncated summary.

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
