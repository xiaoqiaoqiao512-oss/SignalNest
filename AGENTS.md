# SignalNest agent context

The project is SignalNest, a single-user, self-hosted campus information agent.

## Local operational memory

Before asking the deployer for production connection details, read
`.signalnest-local/operations.md` if it exists. The deployer has asked that agents
reuse this information instead of asking for it again. This local directory is
ignored by Git; do not copy its connection metadata into public reports.

For a request limited to inspection or auditing, use read-only diagnostics.
Check the Windows keepalive task and `wsl.exe --list --running` before executing
Linux commands: entering a stopped distribution could start it and hide a failure.
Do not trigger crawling, retries, policy updates or SMTP sends just to inspect
status. Follow the user's existing authorization for any requested changes.

Preserve the distinction between production records, isolated query snapshots,
offline counterfactual evaluations and fresh public-page checks. An empty mail
queue does not establish that the notification policy has no false negatives.
