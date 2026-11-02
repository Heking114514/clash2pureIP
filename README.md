# clash2pureIP

将 Clash 订阅中的每个节点映射为独立代理端口，并生成可批量导入代理池的地址。端口中的代理入口使用 mihomo `mixed` listener，因此同一个端口同时接受 HTTP、SOCKS5 和 SOCKS5H。

## 功能

- 固定使用 `10000-10999` 端口范围，每个订阅节点对应一个出口
- HTTP 和 SOCKS5 共用监听端口，并支持用户名/密码认证
- 七种导出格式：三种带协议头 URL 和四种传统代理池格式
- 单个节点复制、全格式复制、服务端测速
- WebUI 密码登录，运行状态和认证信息持久化
- 订阅内容只在转换时读取，不写入仓库

## 格式

每个节点都会生成以下七种值：

```text
http://username:password@hostname:port
socks5://username:password@hostname:port
socks5h://username:password@hostname:port
hostname:port:username:password
username:password:hostname:port
username:password@hostname:port
hostname:port@username:password
```

`socks5://` 和 `socks5h://` 都指向真实的 SOCKS5 服务；区别是客户端是否把域名解析交给代理解析。

## 部署

### 1. 安装依赖

```bash
sudo mkdir -p /opt/clash-converter-10000
sudo chown "$USER" /opt/clash-converter-10000
cd /opt/clash-converter-10000
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，至少设置一个强 WebUI 密码。公网代理还需要设置 `LISTEN_HOST=0.0.0.0`、`PUBLIC_HOST` 和强 `PROXY_PASS`。

### 2. 安装 mihomo

从 [MetaCubeX/mihomo Releases](https://github.com/MetaCubeX/mihomo/releases) 下载 Linux 版本，并安装到 `/usr/local/bin/mihomo`。

创建首次启动用的占位配置：

```bash
mkdir -p /opt/clash-converter-10000/data/mihomo
cat > /opt/clash-converter-10000/data/mihomo/config.yaml <<'EOF'
mixed-port: 0
allow-lan: false
mode: rule
log-level: warning
proxies: []
rules:
  - MATCH,DIRECT
EOF
```

### 3. 安装 systemd 服务

```bash
sudo cp deploy/mihomo-conv.service /etc/systemd/system/mihomo-conv-10000.service
sudo cp deploy/clash-converter.service /etc/systemd/system/clash-converter.service
sudo systemctl daemon-reload
sudo systemctl enable --now mihomo-conv-10000 clash-converter
```

WebUI 默认监听 `8090/tcp`。公网代理还需在防火墙和云安全组中放行实际使用的端口范围。

## 远程部署脚本

`deploy/deploy_remote.py` 使用 SSH/SFTP 上传程序并重启服务。先安装 Paramiko：

```powershell
python -m pip install paramiko
```

再通过环境变量传入凭据，脚本不会在仓库中保存这些值：

```powershell
$env:DEPLOY_HOST = "203.0.113.10"
$env:DEPLOY_USER = "root"
$env:SSH_PASS = "ssh-password"
$env:WEBUI_USER = "admin"
$env:WEBUI_PASS = "webui-password"
$env:PROXY_USER = "proxyuser"
$env:PROXY_PASS = "proxy-password"
python deploy/deploy_remote.py
```

可选变量：

- `PUBLIC_HOST`：写入导出地址的主机名或 IP，默认等于 `DEPLOY_HOST`
- `OLD_ROOT`：旧安装目录；设置后，脚本会在目标文件不存在时迁移密钥、状态和 mihomo 配置

## 使用

1. 打开 `http://SERVER:8090/` 并登录。
2. 填入 Clash 订阅地址。
3. 需要覆盖服务端配置时展开「高级设置」，可选填起始端口、代理账号和代理密码；留空即沿用 `.env` 中的默认值。
4. 转换后选择需要的输出格式。
5. 使用单个复制按钮，或复制当前格式的全部地址。
6. 用「全部测速」检查端口连通性与延迟；测速协议跟随当前选择的 HTTP、SOCKS5 或 SOCKS5H 格式。

## API

```http
POST /api/convert  {"url":"https://example.com/subscription","port_start":10000}
GET  /api/status
GET  /api/test?port=10000&scheme=http|socks5|socks5h
GET  /healthz
```

除 `/healthz`、`/login` 和 `/static/*` 外，接口都要求登录。

## 安全注意

- 不要把 `.env`、`data/`、订阅链接、代理密码或 SSH 密码提交到 Git
- 公网监听必须设置强代理密码，并尽量通过防火墙限制来源
- 每个节点占用一个端口，订阅节点数量不能超过 `11000 - 起始端口`
- 重新转换会重启 mihomo，现有代理连接会短暂中断
