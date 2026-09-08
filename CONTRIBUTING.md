# 开发与贡献

## 本地运行

使用 Python 3.12 和 ffmpeg。配置、密钥和数据放在仓库外：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements/dev.txt -c requirements/runtime.lock

mkdir -p "$HOME/.config/tgforward" "$HOME/.local/share/tgforward"
cp .env.example "$HOME/.config/tgforward/bot.env"  # 填写配置及独立密钥
chmod 600 "$HOME/.config/tgforward/bot.env"
export TGFORWARD_ENV_FILE="$HOME/.config/tgforward/bot.env"
export DATA_DIR="$HOME/.local/share/tgforward"
python -m tgforward.tools.init_state             # 仅全新开发实例
python -m tgforward
```

## 修改与测试

```bash
ruff format tgforward tests
ruff check .
python -m pytest
```

- 命令和按钮放在 `handlers/`；交互状态放在 `ui/`。
- 传输和评论逻辑分别放在 `transfers/`、`comments/`，不直接操作数据库。
- 用户数据通过 `storage/users.py` 访问；SQL、事务和文件连接集中在 `storage/sqlite.py`。
- Telegram 的版本适配集中在 `telegram/`，不要分散修改依赖库行为。
- 纯工具函数放在 `utils/`，不要反向依赖处理器或任务调度。
- 测试跟随功能目录。保留取消、部分成功、持久化和实际协议序列化的验证，
  不用只检查 Mock 调用次数来替代行为验证。测试不向 Telegram 发送真实消息。

运行依赖在 `requirements/runtime.txt`，开发依赖在 `requirements/dev.txt`。
变更运行依赖时更新锁文件：

```bash
uv pip compile requirements/runtime.txt --python-version 3.12 -o requirements/runtime.lock
```

## 界面语言

界面文案通过 `tgforward.ui.i18n.tr` 生成，英文词条位于 `tgforward/ui/locales/en.json`。
长文案使用语义键，并在 `zh.json` 中提供中文。动态值使用编号占位符，
不要对源消息、用户输入或已经拼接的内容做查找替换。语言通过请求上下文隔离，后台任务继承启动时的语言。
新增交互需覆盖两种语言、按钮路由和动态值保真测试。

## 发布

版本号只维护 `tgforward/__init__.py` 中的 `__version__`，可通过
`python -m tgforward --version` 查看。发布标签使用 `v<版本号>`，CI 检查标签与代码一致。

提交通过代码检查、测试和镜像入口验证后，CI 发布同一提交的镜像。
`main` 对应 `latest`，版本标签对应版本镜像；部署不依赖额外的 GitHub API 检查。
