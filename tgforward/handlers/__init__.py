"""消息处理器包。导入本包即完成全部处理器注册（pyrogram 装饰器副作用）。"""

from tgforward.handlers import (  # noqa: F401
    admin,
    auth,
    cancel,
    comments,
    menu,
    relay,
    settings,
    start,
)

# isort: split
# 同组只执行首个匹配的处理器，回调直接 return 并不会继续匹配。
# 通用私聊路由必须最后注册，避免吞掉 /start、/help、/setting 等命令。
from tgforward.handlers import router  # noqa: F401,E402
