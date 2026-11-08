# Clash Converter v2 部署与永久方案

一个把 Clash 订阅转换成「独立 HTTP 代理端口」的小型 WebUI，带代理池管理与连通性测试。

## 功能

1. **转换订阅** — 填订阅链接与起始端口，为每个节点开一个独立 HTTP 代理端口。
2. **本次解析** — 列表展示本次结果，可逐条/批量测试（目标 Google `generate_204`），勾选后选择性入池。
3. **代理池** — 持久化在 `data/pool.json`，支持手动增删、批量测试、复制、清空。
4. **订阅缓存** — 服务器被订阅站按 IP 信誉拦截时，可由能拉取的机器推送订阅文本作兜底。

界面为黄黑（深色）/ 黄白（浅色）双主题，右上角切换，选择记在浏览器本地。

## 部署

```bash
sudo mkdir -p /opt/clash-converter-v2 && sudo chown $USER /opt/clash-converter-v2
# 上传本仓库文件到 /opt/clash-converter-v2
cd /opt/clash-converter-v2
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && vim .env      # 修改 WEBUI_PASS / PROXY_PASS / PUBLIC_HOST

sudo cp deploy/clash-converter-v2.service deploy/mihomo-conv-v2.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mihomo-conv-v2 clash-converter-v2
sudo ufw allow 8090/tcp
```

访问 `http://<server>:8090/`，用 `.env` 里的账号密码登录。

## 订阅被 Cloudflare 拦截时的永久方案

部分订阅站在 Cloudflare 后面按**来源 IP 信誉**拦截数据中心 IP：服务器直连 403，走自家节点出口也 403，
JA3 伪装（chrome124/safari17_0）与 Go 客户端（mihomo proxy-provider）同样 403；只有干净的住宅/教育网 IP + Clash UA 能过。

解决办法：在能正常拉取的机器上跑定时任务：

```
本机拉取订阅 → POST /api/upload-sub 推到服务器缓存
            → 服务器 /api/convert 直连失败时自动回退缓存 → 重建监听端口
```

脚本见 `deploy/refresh-sub.ps1`（Windows 计划任务示例，每 6 小时执行一次）。
需要把脚本顶部的 `$Server` / `$WebUser` / `$WebPass` 改成自己的值。

## 服务清单

| 服务 | 端口 | 说明 |
|---|---|---|
| clash-converter-v2 | 8090 | WebUI（转换 / 测试 / 代理池 / 缓存） |
| mihomo-conv-v2 | 10001+ | 订阅节点对应的 HTTP 代理监听 |
| mihomo-newsub | 21000-21018 | 订阅原始端口（可选，仅回环） |
| tls-wrap | 8155-8184 | TLS 包装入口（给需要加密到服务器的客户端用） |

## API

```http
POST /api/convert      {"url":"https://...","port_start":10001}
POST /api/upload-sub   正文为订阅 YAML（需要登录）
GET  /api/sub-cache
GET  /api/pool
POST /api/pool/add     {"urls":["http://user:pass@host:port"]}
POST /api/pool/remove  {"urls":[...]} 或 {"all":true}
POST /api/pool/test    {} 测试池内全部
POST /api/test         {"urls":[...]} 只测不存
POST /api/push         {"target":"http://127.0.0.1:8081","user":"...","password":"..."}
```

## 注意

- `.env`、`data/`、`*.log` 已被 `.gitignore` 排除，不要提交。
- 测试结果为 `ok` / `blocked` / `challenge` / `error` 四类：`blocked` 表示目标站按出口 IP 拒绝，
  与「线路不通」不同。
- 节点端口数量受 `LimitNOFILE` 与防火墙放行范围限制，百级节点以内无需调整。
