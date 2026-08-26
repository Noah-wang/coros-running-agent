# systemd 单元

这些文件原来只存在服务器上，改一次定时就得手工 `systemctl edit`，仓库里看不出
「同步到底多久跑一次」。放进仓库之后，节奏本身也是可 review、可回滚的。

安装：

```bash
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coros-running-agent-bili-sync.timer
```

查看下次触发时间：

```bash
systemctl list-timers coros-running-agent-bili-sync.timer
```
