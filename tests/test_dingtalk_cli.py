"""dws 命令拼装测试：替换 subprocess.run，不依赖真实的 dws 可执行文件。

重点覆盖写操作的确认语义：未确认走 ``--dry-run`` 预演，确认后才追加 ``--yes``。
"""

import pytest

from src.app.core.agents.dingtalk import cli
from src.app.core.config import settings


class FakeResult:
    """模拟 subprocess.CompletedProcess 的最小结构。"""

    def __init__(self, returncode: int = 0, stdout: str = '{"ok": true}') -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def capture_argv(monkeypatch: pytest.MonkeyPatch, result: FakeResult | None = None) -> list[list[str]]:
    """替换 subprocess.run，返回记录每次调用参数列表的容器。"""
    calls: list[list[str]] = []
    fake_result = result or FakeResult()

    def fake_run(args: list[str], **kwargs: object) -> FakeResult:
        calls.append(list(args))
        return fake_result

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return calls


def test_write_action_uses_dry_run_without_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """写操作未确认时追加 --dry-run 预演，且不能出现 --yes。"""
    calls = capture_argv(monkeypatch)

    cli.run_dws("todo_remind", task="写周报")

    argv = calls[0]
    assert argv[-1] == "--dry-run"
    assert "--yes" not in argv
    assert argv[1:3] == ["todo", "+remind"]
    assert "写周报" in argv


def test_write_action_uses_yes_with_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """写操作确认后追加 --yes 真实执行，且不再带 --dry-run。"""
    calls = capture_argv(monkeypatch)

    cli.run_dws(
        "calendar_create",
        title="评审会",
        start="2026-09-23T14:00:00+08:00",
        end="2026-09-23T15:00:00+08:00",
        confirm=True,
    )

    argv = calls[0]
    assert argv[-1] == "--yes"
    assert "--dry-run" not in argv


def test_read_action_adds_no_confirmation_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """读操作既不追加 --yes 也不追加 --dry-run。"""
    calls = capture_argv(monkeypatch)

    cli.run_dws("todo_list", status="false")

    argv = calls[0]
    assert "--yes" not in argv
    assert "--dry-run" not in argv


def test_write_action_blocked_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """写操作总开关关闭时直接报错，且不会执行任何命令。"""
    calls = capture_argv(monkeypatch)
    monkeypatch.setattr(settings, "DINGTALK_ENABLE_WRITE", False)

    with pytest.raises(RuntimeError, match="写操作未开启"):
        cli.run_dws("todo_remind", task="写周报")

    assert calls == []


def test_non_zero_exit_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """非零退出码要转成 RuntimeError，并带上 CLI 原始输出。"""
    capture_argv(monkeypatch, FakeResult(returncode=3, stdout='{"ok": false, "reason": "confirmation_required"}'))

    with pytest.raises(RuntimeError, match="exit 3"):
        cli.run_dws("todo_remind", task="写周报")
