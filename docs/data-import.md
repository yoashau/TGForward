# 数据导入

## 已有 SQLite 实例

配置位于 `/etc/tgforward/tgforward.env`、数据库位于
`/var/lib/tgforward/state/tgforward.sqlite3` 的实例，直接运行 `scripts/deploy.sh` 更新。
不要重新生成密钥或初始化空库。

如果原实例使用其他名称或目录，先在**原 Compose 配置下**停止机器人，再调整路径：

1. 将原配置移至 `/etc/tgforward/tgforward.env`，权限设为 `0600`，保持全部密钥原值。
2. 将整个持久数据目录移至 `/var/lib/tgforward`，包含 `thumbs/`、`state/` 和其他文件。
   目标目录已有内容时先核对，不要覆盖。
3. 将 `state/` 中的数据库改名为 `tgforward.sqlite3`。如果存在同名的 `-wal`、`-shm`
   文件，必须一起修改前缀，不能丢弃或只移动主数据库文件。
4. 执行 `chown -R 10001:10001 /var/lib/tgforward`，再更新源码并运行
   `sudo bash scripts/deploy.sh`。使用自定义 UID/GID 时，填写对应值。

Compose 项目名为 `tgforward`，启动新实例前确认原实例已停止，避免两个机器人同时运行。

## MongoDB 实例

下面用于将现有 MongoDB 实例的数据导入 SQLite。导入期间保持原机器人停止，
完成验证前保留原数据库和原数据卷。导入工具不会修改源数据。

### 1. 从原实例导出

在原 Compose 目录中操作。以下示例使用数据库名 `telegram_downloader`，
如果你设置过 `DB_NAME`，替换成实际名称。

```bash
sudo docker compose stop bot
sudo install -d -m 0700 /var/lib/tgforward-import
sudo sh -c 'umask 077; docker compose exec -T mongo mongosh --quiet telegram_downloader --eval '\''
print(EJSON.stringify({users:db.users.find({}).toArray()},null,0,{relaxed:false}))
'\'' > /var/lib/tgforward-import/state.json'
```

把原配置和持久数据移到仓库外。下面的目标目录须尚未使用；如果已经存在，先核对内容，
不要覆盖已有配置或数据库。以下命令在 root shell 中执行：

```bash
set -eu
install -d -m 0750 /etc/tgforward
test ! -e /etc/tgforward/tgforward.env
install -m 0600 .env /etc/tgforward/tgforward.env
install -d -o 10001 -g 10001 -m 0750 /var/lib/tgforward
test -z "$(ls -A /var/lib/tgforward)"
docker compose run --rm --no-deps --user 0 \
  -v /var/lib/tgforward:/new-data --entrypoint sh bot \
  -c 'cp -a /app/data/. /new-data/'
install -d -o 10001 -g 10001 -m 0750 /var/lib/tgforward/{state,thumbs}
chown -R 10001:10001 /var/lib/tgforward
```

`MASTER_KEY`、`SALT_KEY`（或 `IV_KEY`）保持原值。账号凭据以原密文导入，
无需重新登录或绑定辅助 bot。

### 2. 导入并启动

更新到当前应用代码后，在 root shell 中运行：

```bash
docker compose build bot
# 先验证，不写入目标文件。
docker compose run --rm -T --no-deps bot python -m tgforward.tools.import_mongo \
  --from-json /dev/stdin < /var/lib/tgforward-import/state.json
# 验证通过且原机器人保持停机后，正式导入。
docker compose run --rm -T --no-deps bot python -m tgforward.tools.import_mongo \
  --from-json /dev/stdin --source-stopped --apply < /var/lib/tgforward-import/state.json
bash scripts/deploy.sh
```

工具验证全部用户文档和白名单后才提交；错误会回滚。
重复导入同一份数据不会覆盖导入后修改的设置；不同来源或已有业务数据的目标会终止导入。
源用户数为零时，先核对数据库名，不要直接跳过检查。

确认登录、辅助 bot、设置、评论和封面都正常，并验证一次重启后，再移除原 Mongo 容器。
原数据库不包含启用 SQLite 后的新数据，不要让原、新两个机器人同时写入。

外部 MongoDB 也可以直接读取：在独立 Python 环境安装 `requirements/import.txt`，
设置 `MONGO_DB`，使用 `python -m tgforward.tools.import_mongo --mongo --database <数据库名>`。
默认仍是仅验证；正式导入同样需要 `--source-stopped --apply`。
