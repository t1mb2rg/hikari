# Codex command sandbox boundary — 2026-09-10

The Engineering Codex adapter now requires a custom restricted filesystem
profile. It does not fall back to a built-in profile when the requested boundary
cannot be enforced. On the audited native Windows installation, its default
`unelevated` sandbox cannot enforce restricted reads, so the updated adapter
returns `77` / `[codex:blocked]` before starting a model task.

## Concrete audit evidence

Installed client: **codex-cli 0.153.4**. The audit assigned the existing synthetic
`work/growth-physical-gate/source` repository as the sandbox working directory and
tried to read only the existing, harmless `hikari-correctness/README.md` outside
that directory. Both built-ins succeeded:

| Profile | Exit | Observed result |
|---|---:|---|
| `:workspace` | 0 | `BENIGN_OUTSIDE_READ=True` |
| `:read-only` | 0 | `BENIGN_OUTSIDE_READ=True` |

Neither result was a credential-read test. It demonstrates that these profiles
did not enforce repository-only reads. Secret-name environment filters do not
prevent reading a credential file through the filesystem.

Both a custom root-deny/minimal-read profile and a simpler profile with denied
credential directories were rejected by the same installed unelevated sandbox:

```text
windows sandbox failed: Restricted read-only access requires the elevated Windows sandbox backend
```

An existing sandbox setup marker and sandbox accounts were observed through
metadata only. No account, firewall, ACL, local policy, or elevated setup changes
were requested or performed. The elevated mode was not exercised by this audit.

## Implemented profile

`CodexEngineeringBackend` builds the `hikari-engineering` profile entirely in
trusted host code and passes it as **one inline TOML table** through an ephemeral
`-c permissions.hikari-engineering={...}` override. This avoids the installed
CLI's dotted-key parsing issue for quoted special keys such as `":minimal"`.
No global Codex configuration is written.

The profile:

- extends `:workspace` for an authorized maintainer turn or `:read-only` for a
  read-only turn, preserving inherited Git/Codex metadata write protections;
- sets `:root="deny"`, `:minimal="read"`, and the actual workspace root to its
  authorized read/write mode;
- keeps workspace `.git` and `.codex` read-only;
- disables command network access and denies shared host temp access;
- adds read-only paths for the executing Python installation/virtual environment,
  selected Codex executable directory, discovered Git/PowerShell executable
  directories, and actual Git metadata directories obtained from `git rev-parse`;
- never grants the source checkout itself merely because a worktree needs its
  shared `.git` metadata;
- explicitly denies the user's `.codex`, `.ssh`, `.aws`, `.azure`, `.kube`,
  `.gnupg`, `.git-credentials`, `.netrc`, `.npmrc`, `.pypirc`, and `.config/gh`, the
  configured `CODEX_HOME`, and known Windows GitHub CLI credential directories;
- repeats exact denials for `CODEX_HOME/auth.json`, `config.toml`, and
  `.sandbox-secrets`, including when an installed runtime needs a narrower
  readable subtree;
- enumerates existing workspace dotenv paths through filenames only, including
  ignored files, and denies them by exact path. Exact `.env.example`, `.env.sample`,
  and `.env.template` names remain available only when Git reports a tracked
  regular file. Untracked templates and real `.env`/`.env.*`/`*.env` files remain
  denied; the root `.env` path is denied even before it exists;
- scans at most 32 directory levels and 100,000 entries, refusing execution if
  depth/entry limits or inspection errors prevent a complete snapshot. Symlinks
  and Windows junctions are denied without traversing their targets. Git metadata
  is handled by its separate read-only allowlist rather than recursively scanning
  object storage.

Runtime paths and Git metadata come from the trusted host, never the natural
language task or generated candidate. Normal tracked template editing and project
tests require no changes merely to accommodate a falsely broad dotenv denial.
Dotenv enumeration describes the pre-execution snapshot; it does not authorize
reading external runtime secrets or automatically trust newly untracked templates.

Each backend run creates a new `.hikari-tmp-<UUID>` directory directly inside its
isolated worktree. Both the parent process environment and the explicit child
`shell_environment_policy.set` map `TEMP`, `TMP`, and `TMPDIR` to that directory.
Only this host-created directory receives temporary write access, including for
an otherwise read-only project turn. `:tmpdir` therefore refers to the private
directory; `:slash_tmp` remains denied. Ordinary `tempfile`/pytest behavior works
without granting the shared OS temporary directory.

Before returning success, failure, or preflight refusal, the host checks the
directory's original filesystem identity, resolved containment, and every child
for symlinks/junctions, then removes only that run's directory. A substituted
directory or linked escape is not followed or deleted; it changes the result to
blocked so Worker cannot commit the residue. Preexisting directories are never
reused or cleaned. Tempfiles are removed before Worker inspects the diff and
commits. An abrupt host-process crash may leave an isolated scratch directory for
the existing Engineering recovery boundary; this is not permission to delete
arbitrary preexisting files.

The main invocation keeps `--ignore-user-config`, `--ignore-rules`, explicit
`approval_policy="never"`, structured completion validation, bounded execution,
Codex-specific resume IDs, and existing model/provider routing. It also explicitly
sets `features.apps=false`, `web_search="disabled"`, core-only child shell
environment inheritance, no shell profile loading, and canonical exclusion
filters for key/token/secret/password/credential environment names. The exact
provider `env_key` is excluded too, even for a name such as `SESSION_AUTH`. The parent
Codex process can still authenticate using the existing provider route; credential
values are never placed in the command line or candidate files.

Hikari still owns staging, commits, pushes, PR publication, and deployment. The
generated command profile grants no Git metadata writes or network publication
permission to the model-assigned command stage.

Command `PATH` starts with the host's actual Python executable directory and
`HIKARI_RUNTIME_PYTHON` names that interpreter. A host virtual environment also
provides its exact `VIRTUAL_ENV`. These fixed runtime values are passed explicitly
alongside the private temp variables; inherited model credentials remain excluded.
This preserves the validated Hikari Python during ordinary task-local checks.

## Explicit operator setting and preflight

`EngineeringBackendConfig.codex_sandbox` is loaded from
`HIKARI_ENGINEERING_CODEX_SANDBOX`, accepts only `unelevated` or `elevated`, and
defaults to `unelevated`. It is exported by `apply_to_environment`. On Windows,
the adapter passes exactly that selected mode. It never selects elevated mode
automatically and never invokes a setup/install/account-management command.
Selecting elevated mode is an operator decision; its platform setup can itself
require machine security configuration and has not been verified by this gate.

Before launching `codex exec`, the adapter runs `codex sandbox` with the same
profile, selected Windows mode, and managed restrictions. Its only command is
the trusted Python executable printing a fixed marker. It reads no task content
or credentials. Nonzero exit, missing marker, timeout, or an unavailable sandbox
blocks the task with return code `77`; it does not launch a broader profile or a
second backend. The preflight has a maximum 20-second deadline bounded further
by the configured backend deadline. The model stage retains its own configured
timeout. Both the preflight and model stages terminate their owned process trees
on timeout before returning a failure.

## Updated backend physical probe

The updated `CodexEngineeringBackend.run()` was exercised in read-only mode with
an **empty scratch `CODEX_HOME`**, no authentication keys, the existing synthetic
repository, and a 10-second deadline. The actual result was:

```json
{
  "returncode": 77,
  "stdout": "",
  "backend_session_id": "",
  "events": 0,
  "model_task_executed": false
}
```

Its error was:

```text
[codex:blocked] Restricted read-only access requires the elevated Windows sandbox backend; unelevated mode cannot enforce this profile. No model task was executed. Configure a supported sandbox explicitly; no broader fallback was used.
```

The local evidence is retained at
`work/codex-strict-gate/result.json` and the repeated final verification
`work/codex-strict-gate/result-v2.json`, outside the repository. No production
service or policy was changed, and no actual credentials were read for this probe.
This is a verified refusal gate, not a successful strict-sandbox coding gate.

The successful Worker/Codex and capability-growth gates in
`CODEX_BACKEND_GATE.md`, `CAPABILITY_GROWTH_GATE.md`, and `NATURAL_ACTION_GATE.md`
remain historical evidence from the earlier built-in broad-read profiles. They
prove their recorded functional results, not credential confidentiality under
this new boundary. A successful full coding gate under a supported restricted
sandbox is still required before claiming current end-to-end availability.

## Validation and practical limits

The focused backend/config/lifecycle/environment suite passed **38 tests**.
Tests verify configuration defaults/validation, exact inline TOML profile
shape, read-only mode, credential exclusions, real Git-worktree metadata paths,
explicit model routing, structured completion, resume continuity, preflight
refusal/timeout, and prevention of model-process startup after refusal.

Permission profiles apply to local sandboxed command execution, not every
possible Codex network surface. Apps and web search are disabled separately;
managed integrations and additional tool surfaces need their own controls.
The platform-defined `:minimal` set and pre-execution dotenv snapshot are not a
claim that every possible credential location is enumerated. No strict Windows
elevated read/write or network-isolation success was demonstrated here.

Official documentation fetched during the audit:

- [Permissions](https://developers.openai.com/codex/permissions/): root-deny/minimal
  workspace-only example, inherited `.git`/`.codex` protection, path precedence,
  glob expansion, and refusal of unsupported Windows split policies.
- [Windows sandbox](https://developers.openai.com/codex/windows/): elevated versus
  unelevated modes and their security/setup differences.
- [Configuration reference](https://developers.openai.com/codex/config-reference/):
  canonical environment filters, app integration controls, and web-search setting.
