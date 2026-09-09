# Jarvis daily-driver operations

这份指南区分三个状态：代码已实现、候选已验证、日常运行已启用。测试和隔离 physical gate 只能证明各自覆盖的路径。当前候选仍在整合；本文不是生产切换记录，也不宣告全部长期运行目标完成。

## 先确定正在使用哪一套配置

运行版本由以下项目共同决定，不能只看编辑器打开的仓库：

| 项目 | 证据来源 |
| --- | --- |
| 代码 checkout | Resident `host.json` / `hikari-resident status` 中的 repository；登录项保存的 repository。 |
| Python 与依赖 | 启动命令实际使用的解释器；登录启动器选择的已验证环境或已保存 fallback Python。 |
| env 文件 | 启动命令和登录项的 `env_file`；当前进程同名变量优先于文件。 |
| 持久状态 | 显式 `--state-dir`；Windows 默认 `%LOCALAPPDATA%\Hikari\resident`。 |
| 依赖环境指针 | `<state>\environments\current.json`，包含当前和上一环境。它不等于源码版本指针。 |

在已知稳定安装中先运行只读命令，不输出密钥：

```powershell
hikari-resident status
hikari-autostart status
hikari-doctor --json
```

把实际的旧 checkout、Python、env 路径、state 路径和 Git commit 记下来，供回滚使用。不要根据“刚保存了面板设置”推断进程已经换了模型或后端。

## 独立候选构建与验证

以下从候选 checkout 根目录执行。`$LiveRepo` 是本机现有安装示例，应以刚读取的启动配置为准。候选不能是正在运行的 checkout，也不能借用它的 `.venv`。

```powershell
$CandidateRepo = (Resolve-Path '.').Path
$LiveRepo = 'G:\work\LAB\code\hikari'
$LiveState = Join-Path $env:LOCALAPPDATA 'Hikari\resident'
$LiveEnv = Join-Path $LiveRepo '.env'
if ($CandidateRepo -eq $LiveRepo) { throw '请从独立候选 checkout 执行' }
$CandidateState = Join-Path (Split-Path $CandidateRepo -Parent) ('hikari-candidate-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $CandidateState | Out-Null
$CandidateEnvFile = Join-Path $CandidateState 'candidate.env'
Copy-Item -LiteralPath $LiveEnv -Destination $CandidateEnvFile
.\scripts\bootstrap.ps1
$BootstrapPython = Join-Path $CandidateRepo '.venv\Scripts\python.exe'
```

候选 state 位于候选 worktree 之外。保留这个精确路径，后续所有候选命令都显式使用它。只在本机编辑 `candidate.env`，初次测试至少设置：

```dotenv
HIKARI_CONVERSATION_HOST=127.0.0.1
HIKARI_CONVERSATION_PORT=8875
HIKARI_CONVERSATION_URL=ws://127.0.0.1:8875
HIKARI_QQ_ENABLED=false
HIKARI_NAPCAT_LOGIN_GUARD_ENABLED=false
HIKARI_ENGINEERING_ENABLED=false
```

这样可先验证候选的启动和对话，不占用日常 QQ 入口。若 PowerShell 已设置这些同名变量，文件中的值不会覆盖它们；在面板查看 process override 或使用一份清理过相关覆盖项的专用终端。确认模型配置后，用独立 state 建立候选依赖环境：

```powershell
& $BootstrapPython -m resident.windows_host doctor --env-file $CandidateEnvFile
$Built = & $BootstrapPython -m resident.environment_manager --repo $CandidateRepo --state-dir $CandidateState build | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw '候选构建失败；查看 candidate state 下的 environments/logs' }
$Verified = & $BootstrapPython -m resident.environment_manager --repo $CandidateRepo --state-dir $CandidateState validate $Built.environment_id | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $Verified.status -ne 'verified') { throw '候选验证未通过' }
$CandidatePython = Join-Path $Verified.path 'Scripts\python.exe'
```

`validate` 先检查嵌套子进程，再运行完整 pytest。验证失败保留日志和候选记录；不要把失败改写成 `verified`。此阶段没有更新 live environment pointer，也没有修改运行中的 Python。

新的环境 ID 同时绑定源码 checkout 路径、内容指纹/版本、lock hash、Python 和 extras；候选用 `uv sync --locked --no-editable` 构建。不同 checkout 或源码变化会产生新的 ID。匹配且尚未提升的已构建候选可以复用；正在运行、已经提升或曾作为回滚目标的环境不能原地重建/重验，未知或部分构建路径也不会被覆盖。验证与提升会再次核对源码是否改变。

旧记录若没有源码绑定，不能冒充新候选通过验证/提升；现有旧版 runtime pointer 仍可用于读取和恢复解释器。继续使用独立 CandidateState，验证后修改源码时重新构建/验证，不要手工搬运或编辑记录。

## 候选试运行

```powershell
Set-Location $CandidateRepo
& $CandidatePython -m resident.windows_host start $CandidateRepo --state-dir $CandidateState --env-file $CandidateEnvFile --output console
& $CandidatePython -m resident.windows_host status --state-dir $CandidateState
& $CandidatePython -m dashboard.app $CandidateRepo --state-dir $CandidateState --env-file $CandidateEnvFile --port 8788
```

面板位于 `http://127.0.0.1:8788`。最后一条命令是前台面板服务，可放在单独终端；Resident 自身在后台。依次确认：

1. Resident 和 Conversation 有来自当前 PID 的新鲜观测；一次实际对话后模型观测反映真实调用结果。
2. 保存设置只显示待重启，密钥不回显；重启后再核对后端和模型观测。
3. 需要测试工程时，在候选 env 中启用一个已登录的后端并重启候选。请求应保留完整约束/验收标准，状态查询应返回对应持久任务。
4. 只对明确授权的隔离项目/分支测试外部效果。保持默认关闭的 GitHub 自动合并策略；不要把 physical-gate scratch policy 复制到日常 state。

测试结束后停止候选：

```powershell
& $CandidatePython -m resident.windows_host stop --state-dir $CandidateState
```

面板服务另行正常退出。保留候选环境、日志和失败记录，方便核对。

## 批准后的受控日常试用与回滚

这一步才会替换正在运行的 Resident。先完成所需测试、记录旧启动配置，并确认已到达用户授权的启用阶段。候选 `.env` 中的测试端口和 QQ 禁用项不应直接覆盖 live env。

`hikari-resident start` 使用启动它的那个 Python；`hikari-autostart run-now` 则按保存的启动配置和 environment pointer 选择解释器。下面使用显式 Python 做一次可回退的受控试用，避免把这两条路径混为一谈。

```powershell
$PreviousRepo = $LiveRepo
$PreviousPython = Join-Path $PreviousRepo '.venv\Scripts\python.exe' # 改为实际记录的旧解释器
$PreviousEnv = $LiveEnv
Set-Location $PreviousRepo
& $PreviousPython -m resident.windows_host stop --state-dir $LiveState
```

确认旧 Resident 及其被管理子进程已停止后，可把 live state 复制到一个新的、受保护的备份目录。SQLite/outbox 备份必须在停止后取，不能把热拷贝当作一致性快照。然后从已验证候选启动，继续使用原来的 durable state 和明确选择的日常 env：

```powershell
Set-Location $CandidateRepo
& $CandidatePython -m resident.windows_host start $CandidateRepo --state-dir $LiveState --env-file $LiveEnv
& $CandidatePython -m resident.windows_host status --state-dir $LiveState
```

旧登录自启动配置此时仍是旧配置；这不是永久自启动升级。受控试用期间不要再调用旧 `run-now`，也不要把临时 `work` 目录直接注册为长期启动位置。观察真实私聊、任务状态和投递记录后，再决定是否进行单独的持久启动配置迁移。

若试用失败，停止候选后按已记录的旧 checkout、解释器、env 和同一份 state 启动：

```powershell
& $CandidatePython -m resident.windows_host stop --state-dir $LiveState
Set-Location $PreviousRepo
& $PreviousPython -m resident.windows_host start $PreviousRepo --state-dir $LiveState --env-file $PreviousEnv
& $PreviousPython -m resident.windows_host status --state-dir $LiveState
```

代码/依赖回滚通常应保留当前任务和投递凭据。不要直接覆盖为较旧数据库：旧快照可能丢失已经完成的外部效果记录。若涉及不兼容的数据迁移，应先完成专门的数据恢复检查。

## 环境指针与永久自启动

对同一套受控 state 管理的已验证依赖候选，命令格式是：

```powershell
hikari-environment --repo <checkout> --state-dir <state> status
hikari-environment --repo <checkout> --state-dir <state> promote <verified-environment-id>
hikari-environment --repo <checkout> --state-dir <state> rollback
```

`promote` 校验候选源码后更新 `<state>\environments\current.json`，同时记录其源码路径/指纹。登录启动器在下一次受控启动时读取它；现有进程不热切换。`rollback` 回到上一已验证解释器；第一次 promote 没有前任时，rollback 会移除指针并恢复已保存的 fallback Python。回滚不会修改源码 checkout，也不会替换任务数据库。

CandidateState 中验证过的记录不属于 LiveState；不要手工复制记录、改 JSON 或猜一个 ID 来完成迁移。永久启用必须核对稳定 checkout、fallback Python、目标 state 中的 verified environment、现有 pointer 和 env-file 是否一致。若尚未完成这一步，维持受控显式启动与现有回滚配置，不能宣称“下次登录也已升级”。

在上述坐标已经一致、且确认需要注册当前用户登录自启动后，使用对应稳定安装的解释器执行：

```powershell
python -m resident.windows_autostart install <stable-checkout> --state-dir <live-state> --env-file <live-env-file>
python -m resident.windows_autostart status
python -m resident.windows_autostart run-now
```

此处 `python` 必须替换为刚核对的完整解释器路径，不能依赖不明 PATH。注册是显式操作；移除注册用同一安装的 `resident.windows_autostart uninstall`。已运行的同 state Resident 不会因 `run-now` 自动换代码，切换前仍需受控停止。

## GitHub、增长与面板设置

GitHub 默认仓库来自 origin / `HIKARI_GITHUB_REPOSITORY`，允许列表来自 `HIKARI_GITHUB_ALLOWED_REPOSITORIES`。持久 action receipt、PR 所有权和真实验收凭据属于原 runtime state；策略文件为其下的 `github_policy.json`。操作人策略使用 revision 校验保存，不能由对话参数或候选 PR 修改。

启用条件合并时必须明确目标 base 和 GitHub 实际检查名称，默认要求当前 head 的物理验收。权限、部署、验收逻辑和已存在测试修改、保护路径重命名等会阻塞自动放行。失败工作流重跑还需工作流路径和精确 blob SHA 的操作人授权，缺失或变更的 pin 会返回实际观测值供审查，不会自动授予权限。

能力增长中的 `candidate_tested`、`candidate_implemented` 都不是“已安装”。Recipe 仅在精确 digest 获得操作人激活后成为可调用版本；Native 候选还需要独立执行边界、验证和部署。不要把 scratch 的能力注册库当作 live 注册库。

面板中的组件事实来自进程/心跳/真实调用；设置值只是配置。QQ WebSocket 已连接、QQ 已登录、消息已处理、回复已发送分别验证。面板本身维持本机访问和同源写入边界。

## QQ 扫码边界

生产 QQ 验收需要用户完成手机扫码或风控确认。NapCat Login Guard 在一次断连中最多做一次命名任务恢复；二维码仍需用户本人确认。不要为了让面板变绿而反复重启 NapCat、扩大名单或绕过登录。私人名单、群名单和群成员名单分别配置；群聊成员不会因此取得私人记忆或任务权限。

## 已记录的真实验收

| 记录 | 已经证实 | 没有据此宣称 |
| --- | --- | --- |
| [Natural action](NATURAL_ACTION_GATE.md) | 问候和讨论不建任务；真实 Gemma 同意接单、约束/验收传到 Goal/turn、真实 Codex 修改/提交、精确文件验收和不建新任务的完成查询。保留一次接单语气生成超时。 | QQ 投递、所有表达方式、永久启用或全系统验收。 |
| [Codex backend](CODEX_BACKEND_GATE.md) | 首次错误完成判定被保留；修复后真实 Worker → Codex → 文件修改 → Hikari 提交。 | QQ、GitHub 发布、生产启用或所有未来工程任务通过。 |
| [GitHub #82](GITHUB_PHYSICAL_GATE.md) | 隔离 base/head、草稿 PR、两次实际成功检查、精确文件回读、Draft→ready→仅合并隔离 base；main 未改变。 | 生产策略已打开；`update_pr` / `rerun_failed` 已做远端验收。 |
| [Capability growth](CAPABILITY_GROWTH_GATE.md) | 真实 Codex 生成 TODO recipe、15 个宿主执行案例、精确 scratch 激活、调用和原请求重启恢复。 | Native 自动安装、生产 QQ 或 live capability 激活。 |

每条记录都保留原始状态、失败和范围。最终日常可用性仍应以实际启用后的进程、私聊/群聊、任务和投递证据判断；这份指南不会把候选测试组合成尚未发生的整体上线。
