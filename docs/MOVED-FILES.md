# 视频未完成、移动文件和下次去重

`videos` 中以 `.part` 结尾的文件是未完成片段。点击“开始 / 继续”会先核对已落盘片段，再继续下载；正式文件写完并通过长度检查后才改为 `.mp4`。失败任务可点“重试失败”，再点“开始 / 继续”。服务端明确拒绝访问的任务不反复重试。

去重记录在原程序目录的 `state/tasks.sqlite3`。请保留整个 `state` 文件夹，继续使用原程序和同一账号。相同账号、频道、消息和媒体的完成记录会复用。不同消息重新发布的同一视频仍可能算作不同任务。

0.1.1 重新扫描会检查完成记录里的路径和大小。文件已移动、重命名、删除，或所在盘没有连接，都会导致旧路径失效，可能重新排队。移动文件后先更新记录；保留文件名有助于自动找到新位置。外接盘或其他盘符上的已完成文件应保持可访问。

本次新增两个本机辅助脚本，无需 Telegram 登录和联网，不改主程序 EXE：

- `scripts/audit_downloads.py`：只读任务数据库，查找原目录和指定的新目录。核对已完成视频的大小、完整 SHA-256 和 MP4 顶层结构；检查 `.part` 的已提交前缀校验值。输出 JSON 报告，不修改媒体或任务。
- `scripts/relink_downloads.py`：只处理报告中经校验确认的已移动完成视频，再次读取完整文件确认 SHA-256。先用 SQLite 备份接口保存可恢复的任务数据库，再以一个事务更新这些记录的文件路径。保留完成时间、状态和原保存目录；校验不符、目标任务记录变化或目标已不再是完成状态时拒绝修改。不修改其他下载任务；事务中重新检查每条目标记录，任意一条不匹配就全部回滚。

在项目目录打开 PowerShell，按实际路径执行；关联期间保持这些文件的位置不变：

```powershell
.\.venv\Scripts\python.exe scripts\audit_downloads.py --database 'E:\TelegramBatchDownloader\state\tasks.sqlite3' --search-root 'F:\魔镜摄影' --report '.\docs\media-audit.json'
.\.venv\Scripts\python.exe scripts\relink_downloads.py --database 'E:\TelegramBatchDownloader\state\tasks.sqlite3' --report '.\docs\media-audit.json' --backup-directory 'E:\TelegramBatchDownloader\state\backups' --result '.\docs\media-relink.json'
```

关联完成后正常重新扫描频道，已完成且在新位置存在的文件会跳过。保持原保存目录可继续利用原 `.part`；扫描时换保存目录会重置本次扫描涉及的未完成任务，旧片段仍保留。

SHA-256 与下载完成时本地记录比较，可以发现完成后文件变化，不能证明源视频原本没有损坏。MP4 顶层结构检查不等于逐帧解码或播放器兼容测试。

辅助脚本的离线测试仅使用虚构文件和临时数据库：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_audit_downloads tests.test_relink_downloads -v
```
