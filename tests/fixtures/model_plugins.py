"""测试用供数插件——验证 `GET /models` 的三级取值与错误隔离（不进正式 pando/plugins/）。"""


class ModelProviderPlugin:
    """正常供数：返回一份非空列表，应当胜过配置表与内置兜底。"""

    def provide_models(self):
        return [{"id": "plugin-model-a", "label": "Plugin A"}]


class EmptyModelProviderPlugin:
    """返回空列表——视为「没供上数」，核心应继续往下落。"""

    def provide_models(self):
        return []


class BrokenModelProviderPlugin:
    """provide_models 必炸；核心的 _call_hook 应吞掉并继续往下落。"""

    def provide_models(self):
        raise RuntimeError("boom: provide_models")


class LooseShapeModelProviderPlugin:
    """混写形态 + 脏条目：二元组 / 裸字符串 / 缺 label / 非法项，验证 normalize_models。"""

    def provide_models(self):
        return [
            ("claude-opus-5", "Opus 5"),
            "claude-sonnet-5",
            {"id": "claude-fable-5"},
            {"id": "   "},          # 空 id → 丢弃
            {"label": "无 id"},      # 缺 id → 丢弃
            42,                      # 非法类型 → 丢弃
            "claude-opus-5",        # 重复 id → 去重
        ]
