"""CLI 的进程内测试：不给它网络，只验退出码与输出契约。

为什么是「进程内」而不是 `subprocess`：函数测不到的才值得上子进程（信号、
kill -9 后的 resume、真实 stdout 缓冲）。业务分支用子进程测会又慢又脆。
"""

import json

import pytest

from oncall.__main__ import main
from oncall.api import Deps
from oncall.deps import ConfigError
from tests.v2.stubs import FINISH_OK, CountingTool, FakeClock, ScriptedLLM, build_registry


def _factory(script):
    async def _build(domain: str) -> Deps:
        return Deps(llm=ScriptedLLM(script), registry=build_registry(("kb_stats", {}, CountingTool())),
                    clock=FakeClock())

    return _build


def test_json_output_matches_contract(capsys):
    code = main(["ask", "知识库有多少条", "--json"], deps_factory=_factory([FINISH_OK]))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert set(payload) == {
        "run_id", "goal", "status", "conclusion", "root_cause_class",
        "evidence", "reason", "steps", "usage", "sensors", "pending_approval",
    }
    assert payload["evidence"][0]["field"]


def test_needs_human_maps_to_exit_2(capsys):
    weak = dict(FINISH_OK, evidence=[])
    code = main(["ask", "某问题", "--json"], deps_factory=_factory([weak]))

    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "needs_human"


def test_config_error_maps_to_exit_4(capsys):
    async def boom(domain: str):
        raise ConfigError("没有可用的 LLM Provider")

    code = main(["ask", "某问题"], deps_factory=boom)

    assert code == 4
    assert "配置错误" in capsys.readouterr().err


def test_text_output_is_human_readable(capsys):
    code = main(["ask", "某问题"], deps_factory=_factory([FINISH_OK]))

    out = capsys.readouterr().out
    assert code == 0
    assert "status=ok" in out
    assert "结论：" in out


def test_unimplemented_subcommands_fail_loudly(capsys):
    """I1 不做 resume/approve/serve：如实返回 4，不假装成功。"""
    assert main(["resume", "r-1"]) == 4
    assert main(["approve", "r-1", "--decision", "allow"]) == 4
    assert main(["serve"]) == 4
    assert "I2" in capsys.readouterr().err or "I3" in capsys.readouterr().err


def test_missing_argument_is_a_usage_error():
    with pytest.raises(SystemExit):
        main(["ask"])
