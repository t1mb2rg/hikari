# Codex Engineering backend verification

Hikari can select `HIKARI_ENGINEERING_BACKEND=codex` while keeping the existing
Claude backend as the default. `HIKARI_ENGINEERING_CODEX_MODEL` optionally overrides
the model; when empty, the existing Codex model/provider configuration is used.
Conversation model configuration remains separate. The dashboard exposes both.

The adapter copies only vetted model-provider settings from the local Codex config.
It does not inherit desktop session identity, permission profiles, plugins or hooks.
Saved bearer credentials are passed through an invocation environment variable,
never exposed in argv or copied into project files. No credential values are
returned by the dashboard.

## Gate 1: FAIL, 2026-09-10

The initial non-interactive text smoke test returned the expected marker and a
valid Codex session. The first real Worker task did not create its requested file:
the actual child session was read-only despite the legacy sandbox arguments.
Codex truthfully reported that it could not perform the work, but its process
exited successfully; the initial adapter/Worker combination incorrectly accepted
this as task completion.

Evidence session: `codex:01a086fd-a356-7d10-bfc2-0e295528fec9`.
Hikari session: `7dc321e5942a4629a8ec5cbba9160c26`.
No requested file existed. This failure record remains unchanged.

## Repair

- Explicitly select the current named `:workspace` or `:read-only` permission
  profile and do not combine it with legacy sandbox options.
- Remove inherited desktop session and permission environment variables.
- Require a structured final report with `completed`, `blocked` or `failed` status
  and validation information. A successful model turn is not sufficient by itself.
- Preserve blocked results as blocked so the maintainer loop does not automatically
  retry work prevented by a permission/environment boundary.
- Keep process deadlines, stream grounded command/file activity, and terminate the
  owned child process tree on timeout.

## Gate 2: PASS, 2026-09-10

A fresh isolated Git repository and Hikari EngineeringSession were created.
The real Worker selected Codex, requested one documentation file, and retained
ownership of the commit. Codex created `CODEX_BACKEND_GATE.md` containing exactly
`HIKARI_CODEX_WORKER_GATE_PASS` plus a newline.

- Hikari session: `5b1630982c204ae79985c1f38318920c`.
- Codex session: `codex:01a08705-9fb3-77a1-b0e4-43a993a44b29`.
- Branch: `hikari/engineering/5b1630982c204ae79985c1f38318920c`.
- Commit: `4d547116a6c1` (one file, one insertion).
- Requested file content matched exactly; worktree was clean after Hikari committed.
- The actual Codex turn context recorded `workspace-write`, network disabled and
  approval policy `never`.

This verifies the local Worker -> Codex -> file edit -> Hikari commit path.
It does not claim QQ ingress, GitHub publication, production activation, or every
future engineering request has passed a physical gate.

References checked during implementation:
- https://developers.openai.com/codex/noninteractive
- https://developers.openai.com/codex/permissions
- https://developers.openai.com/codex/config-reference
