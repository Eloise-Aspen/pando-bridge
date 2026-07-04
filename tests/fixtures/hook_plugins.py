"""测试用插件——验证钩子异常隔离/插件加载失败跳过（不进正式 pando/plugins/）。"""


class BrokenOnStartupPlugin:
    """on_startup 必炸；用来验证一个插件初始化失败不阻塞其他插件、不阻塞应用启动。"""

    def on_startup(self, app, config_dict):
        raise RuntimeError("boom: on_startup")

    def on_user_message(self, session_id, text, is_new_session):
        # 若核心没有正确隔离，这里也会被调用并暴露出来
        raise RuntimeError("boom: on_user_message should never be called after on_startup failed")


class GoodPlugin:
    """全部钩子正常工作，且记录被调用过，供测试断言。"""

    calls: list[str] = []

    def on_startup(self, app, config_dict):
        GoodPlugin.calls.append("on_startup")

    def on_user_message(self, session_id, text, is_new_session):
        GoodPlugin.calls.append("on_user_message")
        return ""


class BrokenOnUserMessagePlugin:
    """on_startup 正常，但 on_user_message 每次都炸——验证单次钩子调用异常不冒泡。"""

    def on_startup(self, app, config_dict):
        pass

    def on_user_message(self, session_id, text, is_new_session):
        raise RuntimeError("boom: on_user_message")


class UnconstructiblePlugin:
    """构造函数直接炸——验证插件加载阶段（非钩子调用阶段）失败也会被跳过。"""

    def __init__(self):
        raise RuntimeError("boom: __init__")
