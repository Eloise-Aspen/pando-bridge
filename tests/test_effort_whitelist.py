"""Task 1：effort 白名单校验单测（完成标准 2、3）。

纯函数 normalize_effort，不经 WS/子进程——flag 组装与透传已在 run_claude 覆盖。
覆盖：合法档原样透传、空/未选回落默认、非法值（含手工 WS 注入的异常类型）忽略且不崩。"""

from pando.server import normalize_effort, VALID_EFFORTS


def test_whitelist_matches_cli():
    # 与 CLI `--effort <...>` 档位对齐，钉住清单防漂移
    assert VALID_EFFORTS == ("low", "medium", "high", "xhigh", "max")


def test_valid_efforts_pass_through():
    for v in VALID_EFFORTS:
        assert normalize_effort(v) == v


def test_empty_or_unset_returns_none():
    # 未选/空 = 不传 flag，回落 CLI 默认（完成标准 2 的「默认」分支）
    assert normalize_effort(None) is None
    assert normalize_effort("") is None


def test_illegal_values_ignored():
    # 非法字符串忽略为 None（大小写敏感、带空格、越界档均视为非法）
    for bad in ["ultra", "HIGH", "high ", " low", "0", "medium; rm -rf /"]:
        assert normalize_effort(bad) is None


def test_injected_nonstring_types_do_not_crash():
    # 完成标准 3：手工 WS 注入任意类型，服务端忽略且不抛异常
    for bad in [123, 0, [], {}, ["high"], {"x": 1}, True]:
        assert normalize_effort(bad) is None
