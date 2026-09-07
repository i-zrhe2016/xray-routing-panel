# Cloudflare R2 灾备上传

本项目生成包含 SQLite 数据库和部署配置的灾备归档，使用应用侧 AES-256-GCM 加密后，通过 Cloudflare R2 的 S3 兼容 API 保存。R2 只用于低频、异地、离线灾难恢复，不参与故障切换或快速恢复。

## 流程

```text
backup_db.py                 SQLite 一致性快照
collect_remote_backup.py     只读采集普通数据面配置；远端 AI 模式可选
build_backup_bundle.py       tar.gz + SHA-256 manifest
upload_backup_r2.py          R2 S3 API 上传
```

当 `DB_BACKUP_R2_ENABLED=0` 时，只生成本地归档；当设置为 `1` 时，必须同时提供完整 R2 配置，上传失败会返回非零，但不会删除本地归档。

## 配置

```dotenv
DB_BACKUP_R2_ENABLED=1
DB_BACKUP_R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
DB_BACKUP_R2_BUCKET=xray-routing-panel-disaster
DB_BACKUP_R2_ACCESS_KEY_ID=<r2-access-key-id>
DB_BACKUP_R2_SECRET_ACCESS_KEY=<r2-secret-access-key>
DB_BACKUP_R2_REGION=auto
DB_BACKUP_R2_PREFIX=xray-routing-panel
DB_BACKUP_R2_RECORD_PATH=/backups/r2-upload-record.json
```

R2 凭据只能通过部署环境、Docker Secret 或外部 Secret 管理注入，不能提交到 Git、灾备归档或日志。上传记录不保存 secret，只保存 endpoint、bucket、object key、大小、SHA-256 和时间。

## 对象命名与校验

对象 key 格式为：

```text
<prefix>/<YYYY>/<MM>/<DD>/<UTC时间>-<sha256前16位>-<归档文件名>
```

归档内部的 `backup-manifest.json` 记录每个文件的来源、大小和 SHA-256。R2 上传记录由 `DB_BACKUP_R2_RECORD_PATH` 指定，默认是 `/backups/r2-upload-record.json`。

## 测试上传

测试使用 mock S3 client，不连接真实 R2：

```bash
python3 -m unittest tests.test_upload_backup_r2
```

真实执行前，确认 endpoint、bucket 和两项密钥已经注入，并先使用独立测试 bucket 验证。不要在命令行参数、聊天记录或 shell 历史中粘贴 secret。

## 灾难阶段

从 R2 下载对应对象到隔离目录，校验 SHA-256 和归档内 manifest，再解包到隔离目录，最后人工恢复数据库和配置。恢复不会自动覆盖正在运行的服务。

R2 对象生命周期和保留策略在 Cloudflare 控制台配置；应用不删除远端历史对象。
