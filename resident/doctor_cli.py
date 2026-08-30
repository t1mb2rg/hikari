from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit
import webbrowser

from .paths import default_state_dir
from .soak_cli import SoakCheckpoint, build_checkpoint


WindowsProbe = Callable[[str], Mapping[str, object]]


@dataclass(frozen=True)
class DoctorFinding:
    level: str
    code: str
    summary: str
    action: str | None = None


@dataclass(frozen=True)
class NapCatSnapshot:
    root: str
    root_present: bool
    task_name: str
    task_present: bool
    task_state: str | None
    task_last_result: int | None
    task_error: str | None
    qq_pids: tuple[int, ...]
    bridge_listener_count: int
    bridge_established_count: int
    webui_listener_count: int
    webui_established_count: int
    webui_config_present: bool
    webui_enabled: bool | None
    webui_host: str | None
    webui_port: int | None
    webui_token_configured: bool
    webui_url: str | None
    file_logging_enabled: bool | None
    log_directory: str
    log_file_count: int
    latest_log_path: str | None
    latest_log_modified_at: str | None
    onebot_websocket_urls: tuple[str, ...]
    qrcode_path: str
    qrcode_present: bool
    qrcode_modified_at: str | None
    config_errors: tuple[str, ...]
    probe_error: str | None


@dataclass(frozen=True)
class DoctorReport:
    captured_at: str
    status: str
    platform: str
    checkpoint: SoakCheckpoint
    napcat: NapCatSnapshot
    findings: tuple[DoctorFinding, ...]
    recent_log_clues: Mapping[str, tuple[str, ...]]

    def as_dict(self) -> dict[str, object]:
        return {
            "captured_at": self.captured_at,
            "status": self.status,
            "platform": self.platform,
            "checkpoint": self.checkpoint.as_dict(),
            "napcat": asdict(self.napcat),
            "findings": [asdict(item) for item in self.findings],
            "recent_log_clues": {
                name: list(lines) for name, lines in self.recent_log_clues.items()
            },
        }


_WINDOWS_PROBE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$taskName = $env:HIKARI_DOCTOR_TASK_NAME
$taskPayload = $null
try {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $taskInfo = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
    $taskPayload = [ordered]@{
        present = $true
        state = [string]$task.State
        last_result = [int64]$taskInfo.LastTaskResult
        error = $null
    }
} catch {
    $taskPayload = [ordered]@{
        present = $false
        state = $null
        last_result = $null
        error = $_.Exception.Message
    }
}

$ports = [ordered]@{}
foreach ($port in @(8081, 6099)) {
    try {
        $connections = @(Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue)
        $ports[[string]$port] = [ordered]@{
            listeners = @($connections | Where-Object State -eq 'Listen').Count
            established = @($connections | Where-Object State -eq 'Established').Count
        }
    } catch {
        $ports[[string]$port] = [ordered]@{
            listeners = 0
            established = 0
        }
    }
}

$qqPids = @(
    Get-Process -Name 'QQ' -ErrorAction SilentlyContinue |
        ForEach-Object { [int]$_.Id }
)

[ordered]@{
    task = $taskPayload
    ports = $ports
    qq_pids = $qqPids
} | ConvertTo-Json -Depth 6 -Compress
"""


def _default_napcat_root() -> Path:
    configured = os.environ.get("HIKARI_NAPCAT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        candidates = sorted(
            (path for path in Path("D:/").glob("NapCat-Shell-v*") if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
        return Path(r"D:\NapCat-Shell-v4.18.19")
    return Path.home() / ".napcat"


def _run_windows_probe(task_name: str) -> Mapping[str, object]:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        raise RuntimeError("PowerShell is unavailable")
    environment = os.environ.copy()
    environment["HIKARI_DOCTOR_TASK_NAME"] = task_name
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _WINDOWS_PROBE_SCRIPT,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
        env=environment,
        creationflags=creation_flags,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown PowerShell failure"
        raise RuntimeError(detail)
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("PowerShell probe returned a non-object payload")
    return payload


def _read_json_object(path: Path) -> tuple[dict[str, object] | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path.name}: {type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, f"{path.name}: expected a JSON object"
    return payload, None


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _port_counts(
    probe: Mapping[str, object], port: int
) -> tuple[int, int]:
    ports = probe.get("ports")
    if not isinstance(ports, Mapping):
        return 0, 0
    item = ports.get(str(port))
    if not isinstance(item, Mapping):
        return 0, 0
    return _safe_int(item.get("listeners")) or 0, _safe_int(item.get("established")) or 0


def _resolve_account_config(root: Path) -> Path | None:
    candidates = sorted(
        path
        for path in (root / "config").glob("napcat_*.json")
        if not path.name.startswith("napcat_protocol_")
    )
    return candidates[0] if candidates else None


def _safe_endpoint_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    if not parsed.scheme or not parsed.hostname:
        return "<invalid-url>"
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = f"{hostname}:{parsed.port}" if parsed.port is not None else hostname
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _inspect_napcat(
    root: Path,
    *,
    task_name: str,
    probe: Mapping[str, object],
    probe_error: str | None,
) -> NapCatSnapshot:
    config_errors: list[str] = []
    webui, error = _read_json_object(root / "config" / "webui.json")
    if error:
        config_errors.append(error)
    global_config, error = _read_json_object(root / "config" / "napcat.json")
    if error:
        config_errors.append(error)
    account_path = _resolve_account_config(root) if root.is_dir() else None
    account_config: dict[str, object] | None = None
    if account_path is not None:
        account_config, error = _read_json_object(account_path)
        if error:
            config_errors.append(error)

    webui_enabled = None if webui is None else not bool(webui.get("disableWebUI", False))
    webui_host = str(webui.get("host")) if webui and webui.get("host") is not None else None
    webui_port = _safe_int(webui.get("port")) if webui else None
    token_configured = bool(webui.get("token")) if webui else False
    webui_url = f"http://127.0.0.1:{webui_port}" if webui_enabled and webui_port else None

    file_logging: bool | None = None
    if global_config is not None and "fileLog" in global_config:
        file_logging = bool(global_config["fileLog"])
    if account_config is not None and "fileLog" in account_config:
        file_logging = bool(account_config["fileLog"])

    websocket_urls: list[str] = []
    for path in sorted((root / "config").glob("onebot11_*.json")) if root.is_dir() else ():
        payload, error = _read_json_object(path)
        if error:
            config_errors.append(error)
            continue
        network = payload.get("network") if payload else None
        clients = network.get("websocketClients") if isinstance(network, Mapping) else None
        if not isinstance(clients, list):
            continue
        for client in clients:
            if not isinstance(client, Mapping) or not bool(client.get("enable", False)):
                continue
            url = client.get("url")
            if isinstance(url, str) and url:
                websocket_urls.append(_safe_endpoint_url(url))

    task = probe.get("task")
    task_mapping = task if isinstance(task, Mapping) else {}
    qq_pids_raw = probe.get("qq_pids")
    qq_pids = tuple(
        int(pid)
        for pid in qq_pids_raw
        if isinstance(qq_pids_raw, list)
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
    ) if isinstance(qq_pids_raw, list) else ()
    bridge_listeners, bridge_established = _port_counts(probe, 8081)
    webui_listeners, webui_established = _port_counts(probe, 6099)

    qrcode = root / "cache" / "qrcode.png"
    qrcode_modified_at: str | None = None
    if qrcode.is_file():
        try:
            qrcode_modified_at = datetime.fromtimestamp(
                qrcode.stat().st_mtime, timezone.utc
            ).isoformat()
        except OSError:
            pass

    log_directory = root / "logs"
    log_files: list[Path] = []
    if log_directory.is_dir():
        try:
            log_files = sorted(
                (path for path in log_directory.glob("*.log") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            log_files = []
    latest_log = log_files[0] if log_files else None
    latest_log_modified_at: str | None = None
    if latest_log is not None:
        try:
            latest_log_modified_at = datetime.fromtimestamp(
                latest_log.stat().st_mtime, timezone.utc
            ).isoformat()
        except OSError:
            pass

    return NapCatSnapshot(
        root=str(root),
        root_present=root.is_dir(),
        task_name=task_name,
        task_present=bool(task_mapping.get("present", False)),
        task_state=(
            str(task_mapping.get("state"))
            if task_mapping.get("state") is not None
            else None
        ),
        task_last_result=_safe_int(task_mapping.get("last_result")),
        task_error=(
            str(task_mapping.get("error"))
            if task_mapping.get("error")
            else None
        ),
        qq_pids=qq_pids,
        bridge_listener_count=bridge_listeners,
        bridge_established_count=bridge_established,
        webui_listener_count=webui_listeners,
        webui_established_count=webui_established,
        webui_config_present=webui is not None,
        webui_enabled=webui_enabled,
        webui_host=webui_host,
        webui_port=webui_port,
        webui_token_configured=token_configured,
        webui_url=webui_url,
        file_logging_enabled=file_logging,
        log_directory=str(log_directory),
        log_file_count=len(log_files),
        latest_log_path=str(latest_log) if latest_log is not None else None,
        latest_log_modified_at=latest_log_modified_at,
        onebot_websocket_urls=tuple(dict.fromkeys(websocket_urls)),
        qrcode_path=str(qrcode),
        qrcode_present=qrcode.is_file(),
        qrcode_modified_at=qrcode_modified_at,
        config_errors=tuple(config_errors),
        probe_error=probe_error,
    )


_LOG_CLUE_PATTERN = re.compile(
    r"error|warning|failed|failure|exception|traceback|deferred|uncertain|"
    r"disconnect|closed|失败|错误|异常|断开|延迟",
    re.IGNORECASE,
)
_LOG_SECRET_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)\bbearer\s+[^\s,;]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"),
)


def _redact_log_line(value: str) -> str:
    rendered = value
    for pattern in _LOG_SECRET_PATTERNS:
        rendered = pattern.sub("credential=<redacted>", rendered)
    return rendered


def _recent_log_clues(state_dir: Path, *, limit: int) -> dict[str, tuple[str, ...]]:
    if limit <= 0:
        return {}
    clues: dict[str, tuple[str, ...]] = {}
    for name in ("resident.log", "qq_bridge.log"):
        path = state_dir / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        matches = [
            _redact_log_line(line[-600:])
            for line in lines[-500:]
            if _LOG_CLUE_PATTERN.search(line)
        ]
        if matches:
            clues[name] = tuple(matches[-limit:])
    return clues


def _console_safe(value: str, *, encoding: str | None = None) -> str:
    """Keep diagnostic evidence printable on legacy Windows code pages."""

    selected = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return value.encode(selected, errors="backslashreplace").decode(selected)
    except LookupError:
        return value.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _build_findings(
    checkpoint: SoakCheckpoint,
    napcat: NapCatSnapshot,
) -> tuple[DoctorFinding, ...]:
    findings: list[DoctorFinding] = []

    def add(level: str, code: str, summary: str, action: str | None = None) -> None:
        findings.append(DoctorFinding(level, code, summary, action))

    if not checkpoint.resident_running:
        add("error", "resident.down", "Hikari Resident 未运行", "运行 hikari-resident start")
    if checkpoint.host_state_error:
        add("error", "resident.state", "Resident host.json 无法读取", checkpoint.host_state_error)
    if checkpoint.process_error:
        add("warning", "resident.process_probe", "Resident 进程树检查失败", checkpoint.process_error)
    for database, error in checkpoint.sqlite_errors.items():
        add("error", "sqlite.read", f"{database} 无法只读检查", error)

    qq_pending = int(checkpoint.qq_spool_states.get("pending", 0))
    if qq_pending:
        add(
            "warning",
            "qq.pending",
            f"QQ durable spool 有 {qq_pending} 条 pending",
            "检查 qq_bridge.log 中的 deferred/recovered 记录和模型 provider",
        )
    uncertain = int(checkpoint.delivery_states.get("uncertain", 0))
    sending = int(checkpoint.delivery_states.get("sending", 0))
    if uncertain or sending:
        add(
            "warning",
            "delivery.incomplete",
            f"主动投递存在 sending={sending}, uncertain={uncertain}",
            "检查 delivery audit，避免盲目重发",
        )

    if not napcat.root_present:
        add("error", "napcat.root", "NapCat Shell 目录不存在", napcat.root)
    if napcat.probe_error:
        add("error", "napcat.probe", "Windows NapCat 探针失败", napcat.probe_error)
    elif not napcat.task_present:
        add("error", "napcat.task_missing", "NapCat 计划任务不存在", napcat.task_error)
    elif (napcat.task_state or "").casefold() != "running":
        add(
            "error",
            "napcat.task_stopped",
            f"NapCat 计划任务状态为 {napcat.task_state or 'unknown'}",
            f"启动计划任务 {napcat.task_name}",
        )
    if not napcat.qq_pids:
        add("error", "qq.down", "未发现 QQ 进程", "恢复 QQ 登录后重新检查")
    if napcat.bridge_listener_count == 0:
        add("error", "bridge.not_listening", "Hikari QQ Bridge 未监听 8081")
    elif napcat.bridge_established_count == 0:
        add(
            "warning",
            "bridge.disconnected",
            "8081 正在监听，但 NapCat 尚未建立连接",
            "检查 QQ 登录和 NapCat 计划任务",
        )
    if napcat.webui_enabled and napcat.webui_listener_count == 0:
        add("warning", "webui.not_listening", "NapCat WebUI 已配置但 6099 未监听")
    if napcat.webui_enabled and not napcat.webui_token_configured:
        add("error", "webui.no_token", "NapCat WebUI 未配置访问 token")
    if napcat.webui_host in {"::", "0.0.0.0", "*", "+"}:
        add(
            "warning",
            "webui.wide_bind",
            f"NapCat WebUI 当前监听所有网卡（{napcat.webui_host}）",
            "只需本机访问时改为 127.0.0.1",
        )
    if napcat.file_logging_enabled is False:
        add(
            "warning",
            "napcat.file_log_disabled",
            "NapCat 文件日志当前关闭",
            "启用 fileLog 后再依赖后台模式排障",
        )
    elif napcat.file_logging_enabled and napcat.log_file_count == 0:
        add(
            "warning",
            "napcat.file_log_pending",
            "NapCat 文件日志已启用，但尚未生成日志文件",
            f"等待下一条 NapCat 运行事件后检查 {napcat.log_directory}",
        )
    if napcat.config_errors:
        for error in napcat.config_errors:
            add("warning", "napcat.config", "NapCat 配置读取异常", error)
    if not napcat.onebot_websocket_urls:
        add("warning", "napcat.onebot", "未发现启用的 OneBot WebSocket client")

    return tuple(findings)


def collect_doctor_report(
    state_dir: str | Path,
    napcat_root: str | Path,
    *,
    task_name: str = "Hikari NapCat Shell",
    windows_probe: WindowsProbe | None = None,
    system_name: str | None = None,
    recent_error_limit: int = 6,
) -> DoctorReport:
    state_root = Path(state_dir).expanduser().resolve()
    napcat_path = Path(napcat_root).expanduser().resolve()
    checkpoint = build_checkpoint(state_root)
    detected_system = system_name or platform.system()
    probe_payload: Mapping[str, object] = {}
    probe_error: str | None = None
    if detected_system.casefold() == "windows":
        try:
            probe_payload = (windows_probe or _run_windows_probe)(task_name)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            probe_error = f"{type(exc).__name__}: {exc}"
    else:
        probe_error = f"Windows-only NapCat probe unavailable on {detected_system}"

    napcat = _inspect_napcat(
        napcat_path,
        task_name=task_name,
        probe=probe_payload,
        probe_error=probe_error,
    )
    findings = _build_findings(checkpoint, napcat)
    levels = {finding.level for finding in findings}
    status = "error" if "error" in levels else "warning" if "warning" in levels else "healthy"
    captured = datetime.now(timezone.utc).isoformat()
    return DoctorReport(
        captured_at=captured,
        status=status,
        platform=detected_system,
        checkpoint=checkpoint,
        napcat=napcat,
        findings=findings,
        recent_log_clues=_recent_log_clues(state_root, limit=recent_error_limit),
    )


def _print_text(report: DoctorReport) -> None:
    checkpoint = report.checkpoint
    napcat = report.napcat
    label = {"healthy": "HEALTHY", "warning": "WARNING", "error": "ERROR"}[report.status]
    print(f"Hikari Doctor：{label}")
    print(
        "Resident："
        + (f"running pid={checkpoint.resident_pid}" if checkpoint.resident_running else "down")
    )
    print(
        "NapCat："
        f"task={napcat.task_state or '-'}, qq_pids="
        + (",".join(str(pid) for pid in napcat.qq_pids) if napcat.qq_pids else "-")
    )
    print(
        "连接："
        f"bridge 8081 listen={napcat.bridge_listener_count} established={napcat.bridge_established_count}; "
        f"webui 6099 listen={napcat.webui_listener_count} established={napcat.webui_established_count}"
    )
    print(
        "队列："
        f"qq pending={checkpoint.qq_spool_states.get('pending', 0)}, "
        f"sent={checkpoint.qq_spool_states.get('sent', 0)}; "
        f"delivery uncertain={checkpoint.delivery_states.get('uncertain', 0)}; "
        f"receipts={checkpoint.conversation_receipts if checkpoint.conversation_receipts is not None else '-'}"
    )
    print(
        "WebUI："
        f"{napcat.webui_url or '-'} "
        f"token={'configured' if napcat.webui_token_configured else 'missing'} "
        f"bind={napcat.webui_host or '-'}"
    )
    print(
        "二维码："
        + (napcat.qrcode_path if napcat.qrcode_present else f"未生成（预期路径 {napcat.qrcode_path}）")
    )
    print(
        "NapCat 文件日志："
        f"{'on' if napcat.file_logging_enabled else 'off/unknown'} "
        f"files={napcat.log_file_count} latest={napcat.latest_log_path or '-'}"
    )

    if report.findings:
        print("诊断：")
        for finding in report.findings:
            print(f"  [{finding.level.upper()}] {finding.summary}")
            if finding.action:
                print(f"    建议：{finding.action}")
    else:
        print("诊断：未发现异常")

    if report.recent_log_clues:
        print("最近日志线索（可能包含已恢复的历史故障）：")
        for name, lines in report.recent_log_clues.items():
            print(f"  {name}：")
            for line in lines:
                print(f"    {_console_safe(line)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hikari-doctor",
        description="只读检查 Hikari Resident、NapCat Shell、QQ、端口、队列和登录入口。",
    )
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--napcat-root", default=None)
    parser.add_argument("--task-name", default="Hikari NapCat Shell")
    parser.add_argument("--recent-errors", type=int, default=6)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--open-webui",
        action="store_true",
        help="诊断完成后打开本机 NapCat WebUI；不会输出 token",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.recent_errors < 0 or args.recent_errors > 50:
        raise SystemExit("--recent-errors 必须在 0 到 50 之间")
    state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir()
    napcat_root = (
        Path(args.napcat_root).expanduser().resolve()
        if args.napcat_root
        else _default_napcat_root()
    )
    report = collect_doctor_report(
        state_dir,
        napcat_root,
        task_name=args.task_name,
        recent_error_limit=args.recent_errors,
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        _print_text(report)

    if args.open_webui:
        if report.napcat.webui_url is None:
            print("NapCat WebUI 未启用或端口未知。")
        else:
            webbrowser.open(report.napcat.webui_url)

    return 2 if report.status == "error" else 1 if report.status == "warning" else 0


if __name__ == "__main__":
    raise SystemExit(main())
