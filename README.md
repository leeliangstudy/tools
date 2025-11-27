# LAN 探针（LAN Probe）与 Web SSH 终端

一款轻量级的局域网拨测工具，包含内置的 Web 可视化界面和可选的基于浏览器的 SSH 终端（通过 WebSocket 转发）。

该项目通过一个简洁的 HTTP UI 持续对多个 IP 进行 ICMP Ping 和 TCP 端口连通性检测，并提供 JSON 状态接口，此外对可达主机可通过内置 Web SSH 终端进行交互式连接。

---

## 功能 ✅
- 周期性对目标 IP 列表进行 Ping（支持单个 IP、CIDR 段和范围）
- 对每个 IP 的多个 TCP 端口进行连通性检测（默认端口：22、80、443）
- 简洁的 Web UI 用于查看和过滤 IP 状态
- `/status` 提供 JSON 状态，便于自动化或抓取
- 基于 WebSocket 的 SSH 终端（使用 Paramiko 在服务器端建立 SSH 连接），可通过 `/terminal?ip=...` 访问
- `/ssh?ip=...` 快速构造本地 `ssh://` 或 iTerm2 链接用于在本机打开 SSH 客户端

---

## 运行环境要求 🔧
- 建议 Python 3.10 以上
- 系统需自带 ping 工具（Linux/Windows 均有）

Python 依赖在 `requirements.txt` 中列出，可按以下步骤安装：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

依赖要点（来自 `requirements.txt`）：
- `websockets` — WebSocket 服务器，处理浏览器到后端的终端数据通道
- `paramiko` — 在服务端建立并控制 SSH 会话

---


## 快速开始（本机运行） ▶️

使用默认配置（扫描 192.168.50.0/24）运行脚本：

```bash
python probe_service.py
```

默认启动：
- HTTP UI（主页面）: http://localhost:8000/
- JSON 状态： http://localhost:8000/status
- WebSocket 终端服务器（用于 `/terminal` 页面）： ws://localhost:8001/

在浏览器打开主页面即可查看拨测结果并点击 `Terminal` 打开 Web SSH（前提是目标主机可达且 SSH 可连接）。

---

## 命令行参数说明
```text
--ips        以逗号分隔的 IP 列表（例如: 192.168.50.1,192.168.50.2）
--cidr       CIDR 网络段（例如: 192.168.50.0/24）
--range      IP 范围（起始-结束），例如: 192.168.50.1-192.168.50.254
--http-port  HTTP UI 监听端口（默认 8000）
--ws-port    WebSocket 终端监听端口（默认 8001）
--ports      要检测的 TCP 端口，逗号分隔（默认 22,80,443）
--concurrency 最大并发探测线程（默认 100）
--ping-timeout Ping 超时时间（秒，默认 1）
--tcp-timeout  TCP 连接超时时间（秒，默认 1）
--interval   探测间隔（秒，默认 5）
```

示例：只扫描两个 IP，检测 22 和 80 端口，并减小并发和更新间隔：

```bash
python probe_service.py --ips 192.168.50.10,192.168.50.11 --ports 22,80 --concurrency 50 --interval 10 --http-port 9000 --ws-port 9001
```

---

## 提供的接口与使用方式 🔍
- `/` — Web UI 页面（列出 IP、Ping 状态、延迟、TCP 端口检测结果）
- `/status` — JSON 状态，包含 `status`, `ips`, `ports`, `interval`, `updated_at` 等字段
- `/terminal?ip=<ip>&port=<port>&user=<user>&password=<password>` — 注入参数后打开 HTML 终端页面（前端通过 WebSocket 向后端发送凭据，后端使用 Paramiko 建立 SSH）
- `/ssh?ip=<ip>&port=<port>&user=<user>` — 生成 `ssh://` 或 iTerm2 链接，便于本机客户端打开

示例: `http://localhost:8000/terminal?ip=192.168.50.10&port=22&user=root&password=secret`

注意：终端页面会将凭据作为 JSON 数据通过 WebSocket 发送到服务器，请谨慎使用（详见“安全”章节）。

---

## 工作原理（简要） 🧭
- 扫描器以并发方式对目标 IP 执行 ICMP Ping；若 Ping 可达，则会进一步尝试建立 TCP 连接以检测端口开放性。
- 结果以字典形式保存在内存变量 `status` 中，由 `http.server.HTTPServer` 提供 HTTP 服务。
- `/terminal` 页面在客户端运行 xterm.js（通过 CDN 加载），前端和后端通过 WebSocket 交换终端字节数据，后端通过 Paramiko 使用 SSH 连接目标主机，转发终端 I/O。

---

## 安全与最佳实践 ⚠️
- 当前服务对 HTTP UI 和 WebSocket 终端都没有内置认证或加密，请勿直接将服务对公网开放。
- 终端会接受浏览器传来的密码并在服务端使用这些凭据发起 SSH 连接；请避免在日志或公共页面中泄露凭据信息。
- 推荐将服务放在内网或受控网络中使用，并在生产环境下：
  - 使用反向代理（如 Nginx）做 TLS 终止与鉴权。
  - 使用 HTTP 认证或 OAuth（视需求）保护访问。
  - 禁用默认密码，或使用更安全的认证方式（密钥认证等）。
- 扫描网络可能触发 IDS/IPS、网管告警或违反网络策略，执行前请确保你有相应授权。

---

## 故障排查 & 运行提示 🛠️
- 在 Linux 环境下，`ping` 命令应可用（脚本通过 `subprocess` 调用系统 ping）。部分系统或容器环境可能需要特殊权限或工具。
- 确认端口 8000/8001 可用，或使用命令行参数调整：`--http-port` / `--ws-port`。
- 若 WebSocket 服务无法绑定或失败，请确认依赖已正确安装：

```bash
pip install websockets paramiko
```

- 前端的 xterm.js 通过 CDN 引入，如在离线或受限制网络中，请考虑本地托管该静态资源。

---

## 扩展建议与贡献 🤝
- 为 HTTP 和 WebSocket 接口添加认证（例如 JWT、Basic Auth 或 OAuth）。
- 提供 HTTPS/TLS 支持或示例 Nginx 配置以进行 TLS 终止与防护。
- 增加更多协议检测，如 HTTP 状态码检测、SSL/TLS 握手检测、SNMP 等。
- 将状态写入轻量型数据库（例如 SQLite、InfluxDB）以持久化和分析历史趋势数据。

欢迎 PR 和 issue！

---

## 许可证
本项目按“原样”提供，使用风险自负；如需许可，请在此处加入合适的许可证信息（例如 MIT / Apache-2.0）。

---

## 作者
仓库包含 `probe_service.py` —— 适合在可信网络环境中用于快速局域网诊断与交互式 SSH 访问。

如果你需要，我也可以：
- 添加 `docker-compose.yml` 示例以容器化部署；
- 提供 Nginx 反向代理和 TLS 示例配置；
- 添加 CI 测试与自动化脚本；
- 或者在 README 中添加更多安全与部署细节。💡
# LAN Probe & Web SSH Terminal

A lightweight LAN probing tool with a built-in web interface and an optional web-based SSH terminal.

This project offers a simple HTTP UI that continuously pings and checks TCP ports for a list of IPs, returning status JSON for integration and allowing an interactive SSH terminal (via WebSocket) for reachable hosts.

---

## Features ✅
- Periodically ping lists of target IPs (supports single IPs, CIDR, and inclusive ranges).
- Check TCP reachability for multiple ports per IP (default ports: 22, 80, 443).
- Small web-based UI to view status and filter IPs.
- `/status` JSON endpoint for automation or scraping.
- Web-based SSH terminal using WebSocket and Paramiko, reachable via `/terminal?ip=...`.
- Quick SSH launcher page `/ssh?ip=...` that constructs a local `ssh://` or iTerm2 URL.

---

## Requirements 🔧
- Python 3.10+ recommended.
- system ping installed (standard on Linux/Windows).

Python dependencies are listed in `requirements.txt` and can be installed with:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Core Python packages used (from `requirements.txt`):
- `websockets` — WebSocket server for terminal connections
- `paramiko` — SSH client for remote shells
- `requests` — (optional, used in environment)
- `flask` — (not required by current script but included in requirements)

---

## Quick Start (Run locally) ▶️

Run the basic server with default settings (scans local 192.168.50.0/24 range):

```bash
python probe_service.py
```

This will start:
- HTTP UI server at: http://localhost:8000/
- JSON status at: http://localhost:8000/status
- WebSocket terminal server at: ws://localhost:8001/

Open the main page in your browser to view status and navigate to `Terminal` for SSH-enabled hosts.

---

## CLI Options
```text
--ips        Comma-separated IP addresses to probe (e.g. 192.168.50.1,192.168.50.2)
--cidr       A CIDR range to probe (e.g. 192.168.50.0/24)
--range      IP range (start-end) e.g. 192.168.50.1-192.168.50.254
--http-port  HTTP UI port (default 8000)
--ws-port    WebSocket port for terminal connections (default 8001)
--ports      Comma separated TCP ports to check (default 22,80,443)
--concurrency Number of concurrent ping workers (default 100)
--ping-timeout Ping timeout seconds (default 1)
--tcp-timeout TCP connection timeout seconds (default 1)
--interval   Probe interval seconds (default 5)
```

Example (scan just a few hosts, smaller concurrency and update interval):

```bash
python probe_service.py --ips 192.168.50.10,192.168.50.11 --ports 22,80 --concurrency 50 --interval 10 --http-port 9000 --ws-port 9001
```

---

## Endpoints & Usage 🔍
- `/` — Web UI.
- `/status` — JSON status (includes `status`, `ips`, `ports`, `interval` and `updated_at`).
- `/terminal?ip=<ip>&port=<port>&user=<user>&password=<password>` — Launch the server-side HTML terminal, which will use the WebSocket server to connect to the host via SSH using provided credentials.
- `/ssh?ip=<ip>&port=<port>&user=<user>` — A small page providing quick `ssh://` or iTerm2 links to open a local SSH client.

Example: `http://localhost:8000/terminal?ip=192.168.50.10&port=22&user=root&password=secret`

Important: the terminal HTML page sends credentials to the server via a WebSocket JSON message — treat with caution (see Security section).

---

## How it works (short) 🧭
- The scanner periodically pings all configured IPs concurrently and, for any IP that is reachable by ICMP ping, checks the specified TCP ports by attempting a TCP connection.
- Results are stored in-memory (`status`) and served by a simple `http.server.HTTPServer` instance.
- The `/terminal` UI uses the `websockets` package and `paramiko` to forward a remote SSH shell over WebSocket. The web terminal uses xterm.js on the client side.

---

## Security & Best Practices ⚠️
- The server currently has no authentication or encryption built-in for either the HTTP UI or the WebSocket terminal — this means **do not expose to the public internet**.
- The terminal accepts passwords from the browser and will forward them to the SSH client; change default passwords and do not store credentials in logs or public pages.
- Consider running behind a reverse proxy (NGINX) and add TLS + authentication, or place the service behind a VPN.
- For production, implement hard authentication, remove default passwords, and consider restricting the IPs scanned.
- Be aware that running aggressive scanning on networks might trigger detection systems or network policies.

---

## Troubleshooting & Notes 🛠️
- On Linux, `ping` typically requires the standard ping tool; no root privileges are required when using the system ping binary (it uses `subprocess` to call `ping`), but some environments may require elevated privileges.
- Ensure ports 8000/8001 are free or change them with `--http-port` and `--ws-port`.
- If websockets do not bind, check dependencies: `pip install websockets paramiko`.
- The UI uses inlined JavaScript and third-party CDN for xterm.js; you may want to host xterm.js locally in restricted networks.

---

## Extending & Contribution 🤝
- Add authentication for the HTTP and WebSocket endpoints.
- Add HTTPS support or provide sample nginx config for TLS termination.
- Add additional checks, e.g., HTTP status code, SSL handshake, SNMP, or other protocols.
- Add persistent storage or a lightweight database for historical trends.

Contributions are welcome — open issues or PRs.

---

## License
This project is provided as-is, use at your own risk. You can add your preferred license here.

---

## Author
This repository contains `probe_service.py` — helpful for quick LAN diagnostics and interactive SSH when run in a trusted environment.

If you'd like, I can also add an example `docker-compose.yml`, CI tests, or an improved README section about security or deployment. 💡
# tools