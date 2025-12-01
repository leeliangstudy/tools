import argparse
import json
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import ipaddress
from concurrent.futures import ThreadPoolExecutor, as_completed
import asyncio
import websockets
import paramiko

status = {}
lock = threading.Lock()

def ping_ip(ip, timeout_sec):
    try:
        import platform
        system = platform.system().lower()
        
        if system == "windows":
            cmd = ["ping", "-n", "1", "-w", str(timeout_sec * 1000), ip]
        else:
            cmd = ["ping", "-c", "1", "-W", str(timeout_sec), ip]
            
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        ok = p.returncode == 0
        latency = None
        out = p.stdout or ""
        for line in out.splitlines():
            if "time=" in line or "time<" in line or "时间=" in line:
                m = re.search(r"time[=<]([0-9.]+)\s*ms|时间[=<]([0-9.]+)\s*ms", line)
                if m:
                    try:
                        latency = float(m.group(1) if m.group(1) else m.group(2))
                    except Exception:
                        latency = None
                break
        return ok, latency
    except subprocess.TimeoutExpired:
        return False, None
    except Exception:
        return False, None

def tcp_check(ip, port, timeout_sec):
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout_sec):
            return True
    except Exception:
        return False

def probe_loop(ips, ports, interval, ping_timeout, tcp_timeout, concurrency):
    while True:
        ping_results = {}
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            future_to_ip = {ex.submit(ping_ip, ip, ping_timeout): ip for ip in ips}
            for fut in as_completed(future_to_ip):
                ip = future_to_ip[fut]
                try:
                    reachable, latency = fut.result()
                except Exception:
                    reachable, latency = False, None
                ping_results[ip] = (reachable, latency)
        for ip in ips:
            reachable, latency = ping_results.get(ip, (None, None))
            tcp_results = {}
            for port in ports:
                tcp_results[str(port)] = tcp_check(ip, port, tcp_timeout) if reachable else False
            with lock:
                status[ip] = {
                    "ip": ip,
                    "reachable": reachable,
                    "latency_ms": latency,
                    "tcp": tcp_results,
                    "timestamp": int(time.time())
                }
        time.sleep(interval)

import asyncio
import websockets
import paramiko

async def handle_terminal(websocket):
    try:
        print("有新的WebSocket连接建立")
        # 等待接收前端发送的连接参数
        init_message = await websocket.recv()
        init_data = json.loads(init_message)
        
        ip = init_data.get('ip', '')
        port = int(init_data.get('port', 22))
        username = init_data.get('user', 'root')
        password = init_data.get('password') or 'Databuff@123'
        cols = int(init_data.get('cols', 120))
        import base64
        import os
        rows = int(init_data.get('rows', 40))

        print(f"尝试SSH连接到 {ip}:{port} 用户: {username} (终端尺寸: {cols}x{rows})")
        
        # 发送连接成功消息给前端
        await websocket.send(json.dumps({"type": "connected", "conn_id": f"{ip}:{port}"}))
        
        # 创建SSH客户端
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # 连接SSH服务器
        ssh.connect(ip, port=port, username=username, password=password, timeout=10)
        
        print(f"SSH连接成功: {ip}:{port}")
        
        # 创建交互式shell，使用前端发送的终端尺寸
        shell = ssh.invoke_shell(term='xterm-256color', width=cols, height=rows)
        print("SSH会话已创建")
        
        # 设置会话超时为 5 分钟（300秒），用户可通过设置 TMOUT=0 来禁用
        shell.send('export TMOUT=300\n')
        # 给一点时间让命令执行
        await asyncio.sleep(0.1)

        # 由于paramiko是同步的，我们需要使用线程来处理异步通信
        import threading
        import queue
        
        # 创建数据队列
        terminal_to_ws_queue = queue.Queue()
        ws_to_terminal_queue = queue.Queue()
        
        # 从终端读取数据并放入队列的线程
        def terminal_reader():
            try:
                while True:
                    data = shell.recv(4096)
                    if not data:
                        break
                    # 直接将原始字节写入队列，避免在这里做不完整的UTF-8解码造成乱码
                    terminal_to_ws_queue.put(data)
            except Exception as e:
                print(f"从终端读取数据时出错: {str(e)}")
        
        # 启动终端读取线程
        terminal_thread = threading.Thread(target=terminal_reader, daemon=True)
        terminal_thread.start()
        
        async def terminal_to_websocket():
            try:
                while True:
                    # 检查队列中是否有数据
                    try:
                        data = terminal_to_ws_queue.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.01)
                        continue
                    
                    try:
                        import base64
                        encoded_data = base64.b64encode(data).decode('ascii')
                        await websocket.send(json.dumps({"type": "data", "data": encoded_data}))
            except websockets.exceptions.ConnectionClosed:
                        print("WebSocket连接已关闭，停止发送数据")
                        break
                    except Exception as send_error:
                        print(f"发送终端数据时出错: {send_error}")
                        break
            except websockets.exceptions.ConnectionClosed:
                print("terminal_to_websocket: WebSocket连接已关闭")
            except Exception as e:
                print(f"terminal_to_websocket 出错: {str(e)}")

        async def websocket_to_terminal():
            try:
                # 保存上传会话状态: upload_id -> {temp_path, filename, remote_path, received}
                upload_states = {}
                async for message in websocket:
                    try:
                    message_data = json.loads(message)
                    except json.JSONDecodeError as e:
                        print(f"JSON解析错误: {e}")
                        continue
                    
                    if message_data.get('type') == 'data':
                        # 处理前端发送的终端输入数据
                        try:
                            import base64
                            encoded_data = message_data.get('data', '')
                            # 解码base64数据
                            decoded_data = base64.b64decode(encoded_data)
                            # 将解码后的字节数据发送到终端
                            shell.send(decoded_data)
                        except Exception as decode_error:
                            print(f"处理终端输入出错: {str(decode_error)}")
                    elif message_data.get('type') == 'resize':
                        try:
                            cols = int(message_data.get('cols', 120))
                            rows = int(message_data.get('rows', 40))
                            
                            # 验证尺寸的合理性
                            if cols < 20 or rows < 5:
                                print(f"忽略不合理的终端大小: {cols}x{rows}")
                                continue
                                
                            if cols > 500 or rows > 200:
                                print(f"忽略过大的终端大小: {cols}x{rows}")
                                continue
                            
                            shell.resize_pty(width=cols, height=rows)
                            print(f"终端已调整大小: {cols}x{rows}")
                        except ValueError as e:
                            print(f"终端大小参数无效: {e}")
                            except Exception as e:
                            print(f"调整终端大小失败: {e}")
                    elif message_data.get('type') == 'disconnect':
                        # 处理断开连接请求
                        print("客户端请求断开连接")
                        return
                    elif message_data.get('type') == 'upload-init':
                        # 初始化一次上传会话，创建临时文件并记录状态
                        try:
                            import uuid, tempfile
                            upload_id = message_data.get('upload_id') or str(uuid.uuid4())
                            filename = message_data.get('filename', 'file.bin')
                            total = int(message_data.get('size', 0))
                            remote_path = message_data.get('remote_path', '/tmp') or '/tmp'

                            fd, temp_file = tempfile.mkstemp(prefix='ws_upload_')
                            os.close(fd)
                            # 初始化状态
                            upload_states[upload_id] = {
                                'temp_path': temp_file,
                                'filename': filename,
                                'remote_path': remote_path,
                                'received': 0,
                                'total': total
                            }
                            await websocket.send(json.dumps({'type': 'upload-ack', 'upload_id': upload_id, 'status': 'ready'}))
                            print(f'upload-init: {upload_id} -> {temp_file} (remote: {remote_path})')
                        except Exception as e:
                            print(f'upload-init failed: {e}')
                            try:
                                await websocket.send(json.dumps({'type': 'error', 'message': f'upload-init failed: {str(e)}'}))
                            except:
                                pass
                    elif message_data.get('type') == 'upload-chunk':
                        # 接收分片并追加到临时文件
                        try:
                            import base64
                            upload_id = message_data.get('upload_id')
                            chunk_b64 = message_data.get('data', '')
                            if not upload_id or upload_id not in upload_states:
                                await websocket.send(json.dumps({'type': 'error', 'message': 'invalid upload_id'}))
                                continue
                            state = upload_states[upload_id]
                            chunk = base64.b64decode(chunk_b64)
                            with open(state['temp_path'], 'ab') as f:
                                f.write(chunk)
                            state['received'] += len(chunk)
                            # 向前端回传进度
                            try:
                                await websocket.send(json.dumps({'type': 'upload-progress', 'upload_id': upload_id, 'received': state['received'], 'total': state.get('total', 0)}))
                            except:
                                pass
                        except Exception as e:
                            print(f'upload-chunk failed: {e}')
                            try:
                                await websocket.send(json.dumps({'type': 'error', 'message': f'upload-chunk failed: {str(e)}'}))
                            except:
                                pass
                    elif message_data.get('type') == 'upload-complete':
                        # 分片上传完成，使用 sftp 上传到远端路径并删除临时文件
                        try:
                            upload_id = message_data.get('upload_id')
                            if not upload_id or upload_id not in upload_states:
                                await websocket.send(json.dumps({'type': 'error', 'message': 'invalid upload_id'}))
                                continue
                            state = upload_states.pop(upload_id)
                            temp_file = state['temp_path']
                            filename = state['filename']
                            remote_dir = state['remote_path']
                            remote_path = os.path.join(remote_dir, filename)

                            sftp = ssh.open_sftp()
                            sftp.put(temp_file, remote_path)
                            sftp.close()
                            size = os.path.getsize(temp_file)
                            os.remove(temp_file)
                            # 通知前端完成
                            try:
                                await websocket.send(json.dumps({'type': 'upload-done', 'upload_id': upload_id, 'filename': filename, 'size': size, 'remote_path': remote_path}))
                            except:
                                pass
                            # 也在远程 shell 打印提示
                            try:
                                shell.send(f"echo '>>> 文件 {filename} 上传成功 ({size} 字节) 到 {remote_path}'\n".encode())
                            except:
                                pass
                            print(f'upload complete: {remote_path} ({size} bytes)')
                        except Exception as e:
                            print(f'upload-complete failed: {e}')
                            try:
                                await websocket.send(json.dumps({'type': 'error', 'message': f'upload-complete failed: {str(e)}'}))
                            except:
                                pass
                    elif message_data.get('type') == 'upload':
                        # 兼容旧版一次性上传
                        try:
                            import base64, tempfile
                            filename = message_data.get('filename', 'file.bin')
                            file_content = base64.b64decode(message_data.get('data', ''))
                            temp_file = os.path.join(tempfile.gettempdir(), filename)
                            with open(temp_file, 'wb') as f:
                                f.write(file_content)
                            print(f"文件已接收: {temp_file}")
                            sftp = ssh.open_sftp()
                            remote_path = f"/tmp/{filename}"
                            sftp.put(temp_file, remote_path)
                            sftp.close()
                            os.remove(temp_file)
                            shell.send(f"echo '>>> 文件 {filename} 上传成功 ({len(file_content)} 字节)'\n".encode())
                            print(f"文件上传到远程: {remote_path}")
                        except Exception as e:
                            print(f"文件上传失败: {e}")
                            shell.send(f"echo '>>> 文件上传失败: {str(e)}'\n".encode())
                    elif message_data.get('type') == 'download':
                        # 处理文件下载
                        try:
                            remote_file = message_data.get('filename', '')
                            if not remote_file:
                                print("未指定下载文件")
                                continue
                            
                            sftp = ssh.open_sftp()
                            import tempfile
                            temp_file = os.path.join(tempfile.gettempdir(), os.path.basename(remote_file))
                            sftp.get(remote_file, temp_file)
                            sftp.close()
                            
                            # 读取文件并发送到前端
                            with open(temp_file, 'rb') as f:
                                file_content = f.read()
                            
                            import base64
                            await websocket.send(json.dumps({
                                'type': 'download',
                                'filename': os.path.basename(remote_file),
                                'data': base64.b64encode(file_content).decode('utf-8'),
                                'size': len(file_content)
                            }))
                            
                            # 清理临时文件
                            os.remove(temp_file)
                            
                            print(f"文件已下载: {remote_file}")
                        except Exception as e:
                            print(f"文件下载失败: {e}")
                            try:
                                await websocket.send(json.dumps({
                                    'type': 'error',
                                    'message': f'文件下载失败: {str(e)}'
                                }))
                            except:
                                pass
            except websockets.exceptions.ConnectionClosed:
                print("WebSocket连接已关闭")
            except Exception as e:
                print(f"websocket_to_terminal 出错: {str(e)}")

        try:
            # 使用 wait_for 给任务添加超时，防止任务无限挂起
            await asyncio.gather(
                terminal_to_websocket(),
                websocket_to_terminal(),
                return_exceptions=True
            )
        except Exception as e:
            print(f"asyncio.gather 出错: {e}")
        finally:
            # 确保SSH连接被正确关闭
            try:
            shell.close()
            ssh.close()
                print("SSH连接已关闭")
            except Exception as e:
                print(f"关闭SSH连接时出错: {e}")
    except websockets.exceptions.ConnectionClosed:
        print("WebSocket连接已关闭")
    except Exception as e:
        error_msg = f"服务器错误: {str(e)}"
        print(error_msg)
        try:
            await websocket.send(json.dumps({"type": "error", "message": error_msg}))
        except Exception:
            print("无法发送错误消息给客户端")

def render_html(ips, ports):
    headers = ''.join([f"<th>TCP {p}</th>" for p in ports])
    html = ("<html><head><meta charset='utf-8'><title>LAN Probe</title>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
            "<style>"
            "body{font-family:system-ui,Arial;background:#0f172a;color:#e2e8f0;margin:0;font-size:14px}"
            ".wrap{max-width:1000px;margin:20px auto;padding:0 12px}"
            ".card{background:#111827;border:1px solid #1f2937;border-radius:12px;box-shadow:0 10px 25px rgba(0,0,0,.3);padding:14px}"
            ".head{display:flex;align-items:center;gap:8px;justify-content:space-between;margin-bottom:10px}"
            ".title{font-size:18px;font-weight:600}"
            ".controls{display:flex;gap:8px;align-items:center}"
            "input[type=text]{background:#0b1220;color:#e2e8f0;border:1px solid #203049;border-radius:8px;padding:6px 8px;outline:none;font-size:13px}"
            ".badge{display:inline-block;padding:3px 6px;border-radius:999px;font-size:11px}"
            ".ok{background:#064e3b;color:#a7f3d0}"
            ".fail{background:#3f1d2b;color:#fecaca}"
            ".unknown{background:#1f2937;color:#9ca3af}"
            "table{width:100%;border-collapse:collapse;margin-top:6px;font-size:13px}"
            "th,td{border-bottom:1px solid #1f2937;padding:8px;text-align:left}"
            "thead th{background:#0b1220;color:#93c5fd}"
            "tr:hover{background:#0b1220}"
            "</style>"
            "</head><body><div class='wrap'><div class='card'>"
            "<div class='head'><div class='title'>LAN IP 拨测</div><div class='controls'>"
            "<input id='filter' type='text' placeholder='按IP过滤' oninput='render()'/>"
            "<label style='display:flex;align-items:center;gap:6px'>"
            "<input id='onlyFail' type='checkbox' onchange='render()'/>仅不可达"
            "</label>"
            "<label style='display:flex;align-items:center;gap:6px'>"
            "<input id='onlySuccess' type='checkbox' onchange='render()'/>仅可达"
            "</label>"
            "<button id='copyFail' onclick='copyFail()' style='background:#1f2937;color:#93c5fd;border:1px solid #203049;border-radius:8px;padding:8px 12px;cursor:pointer'>复制不可达</button>"
            "<span id='summary' class='badge unknown'>-</span>"
            "</div></div>"
            "<div><a href='/status' style='color:#93c5fd'>/status</a></div>"
            f"<table><thead><tr><th>IP</th><th>Ping</th><th>延迟</th>{headers}</tr></thead><tbody id='tbody'></tbody></table>"
            "</div></div>"
            "<script>"
            "let data={status:{},ips:[],ports:[],interval:5};"
            "async function fetchStatus(){"
            "  try{const r=await fetch('/status');data=await r.json();render();}catch(e){}"
            "}"
            "function fmtLatency(v){return (typeof v==='number'? v.toFixed(2)+' ms':'-');}"
            "function badge(v){if(v===true)return '<span class=\"badge ok\">可达</span>';if(v===false)return '<span class=\"badge fail\">不可达</span>';return '<span class=\"badge unknown\">未知</span>';}"
            "function portCell(ok,p,ip){"
            "  if(ok===true){"
            "    if(p===22){return p+' ✅ <a style=\"color:#93c5fd\" href=\"/terminal?ip='+ip+'\" target=\"_blank\">Terminal</a>';}"
            "    if(p===80){return p+' ✅ <a style=\"color:#4ade80\" href=\"http://'+ip+':80'+'\" target=\"_blank\">Open</a>';}"
            "    if(p===443){return p+' ✅ <a style=\"color:#22d3ee\" href=\"https://'+ip+':443'+'\" target=\"_blank\">Open</a>';}"
            "    return p+' ✅';"
            "  }"
            "  if(ok===false) return p+' ❌';"
            "  return p+' ?';"
            "}"
            "function getFailIps(){const ips=(data.ips||[]);const res=[];for(const ip of ips){const st=(data.status||{})[ip];if(!st||st.reachable===false)res.push(ip);}return res;}"
            "async function copyText(t){try{await navigator.clipboard.writeText(t);return true;}catch(e){const ta=document.createElement('textarea');ta.value=t;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');document.body.removeChild(ta);return true;}catch(e2){document.body.removeChild(ta);return false;}}}"
            "async function copyFail(){const arr=getFailIps();const ok=await copyText(arr.join('\\n'));const sum=document.getElementById('summary');sum.textContent= ok? ('已复制不可达 '+arr.length+' 个IP') : '复制失败';setTimeout(()=>{render();},1500);}"
            "function ipParts(ip){const p=ip.split('.');return [parseInt(p[0]||'0'),parseInt(p[1]||'0'),parseInt(p[2]||'0'),parseInt(p[3]||'0')];}"
            "function ipCompare(a,b){const A=ipParts(a),B=ipParts(b);for(let i=0;i<4;i++){if(A[i]!==B[i]) return A[i]-B[i];}return 0;}"
            "function render(){"
            "  const tbody=document.getElementById('tbody');const filter=document.getElementById('filter').value.trim();const onlyFail=document.getElementById('onlyFail').checked;const onlySuccess=document.getElementById('onlySuccess').checked;"
            "  const ips=(data.ips||[]).slice();ips.sort(ipCompare);"
            "  let okc=0,failc=0,total=ips.length;"
            "  let html='';"
            "  for(const ip of ips){const info=(data.status||{})[ip]||{};const reach=info.reachable;const lat=info.latency_ms;const tcp=info.tcp||{};if(filter && !ip.includes(filter)) continue; if(onlyFail && reach!==false) continue; if(onlySuccess && reach!==true) continue;"
            "    if(reach===true) okc++; else failc++;"
            "    let tds=''; for(const p of (data.ports||[])){const ok=tcp[String(p)];tds += '<td>'+portCell(ok,p,ip)+'</td>';}"
            "    html += '<tr><td>'+ip+'</td><td>'+badge(reach)+'</td><td>'+fmtLatency(lat)+'</td>'+tds+'</tr>';"
            "  }"
            "  tbody.innerHTML=html;"
            "  const sum=document.getElementById('summary');sum.textContent='总计 '+total+' | 可达 '+okc+' | 不可达 '+failc;"
            "}"
            "fetchStatus();setInterval(fetchStatus, Math.max(3000,(data.interval||5)*1000));"
            "</script>"
            "</body></html>")
    return html

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/status'):
            with lock:
                data = {
                    "status": status,
                    "updated_at": int(time.time()),
                    "ips": self.server.ips,
                    "ports": self.server.ports,
                    "interval": self.server.interval
                }
            body = json.dumps(data)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())
        elif self.path.startswith('/terminal'):
            q = parse_qs(urlparse(self.path).query)
            ip = (q.get('ip') or [''])[0]
            user = (q.get('user') or ['root'])[0]
            port = int((q.get('port') or ['22'])[0])
            password = (q.get('password') or [''])[0] or 'Databuff@123'
            # 使用f-string并转义所有大括号，避免CSS/JS大括号被误解为占位符
            ws_port = getattr(self.server, 'ws_port', self.server.server_port)
            html = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>SSH Terminal</title>
    <style>
        :root {{
            color-scheme: dark;
        }}
        body, html {{
            margin: 0;
            padding: 0;
            height: 100%;
            width: 100%;
            overflow: hidden;
            font-family: Consolas, Monaco, "Courier New", monospace;
            background-color: #050b17;
            color: #e2e8f0;
        }}
        #terminal-container {{
            position: fixed;
            inset: 0;
            width: 100vw;
            height: 100vh;
            box-sizing: border-box;
            padding: 4px;
        }}
        #terminal-container .xterm {{
            height: 100%;
        }}
        /* 右上角菜单按钮 */
        #menu-toggle {{
            position: fixed;
            top: 12px;
            right: 12px;
            z-index: 1000;
            width: 40px;
            height: 40px;
            padding: 0;
            border: 1px solid #374151;
            border-radius: 6px;
            background: #111827;
            color: #93c5fd;
            cursor: pointer;
            font-size: 18px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.3s ease;
            opacity: 0.7;
        }}
        #menu-toggle:hover {{
            background: #1f2937;
            border-color: #4b5563;
            opacity: 1;
        }}
        #menu-toggle:active {{
            transform: scale(0.95);
        }}
        /* 功能菜单容器 */
        .menu-container {{
            position: fixed;
            top: 60px;
            right: 12px;
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 6px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
            padding: 12px;
            min-width: 220px;
            z-index: 999;
            display: none;
        }}
        .menu-container.show {{
            display: block;
        }}
        .menu-item {{
            margin-bottom: 8px;
        }}
        .menu-item.label {{
            color: #9ca3af;
            font-size: 11px;
            text-transform: uppercase;
            margin-bottom: 8px;
            font-weight: 600;
            margin-top: 8px;
        }}
        .menu-item:first-of-type.label {{
            margin-top: 0;
        }}
        .menu-item input {{
            width: 100%;
            padding: 6px 8px;
            background: #0b1220;
            border: 1px solid #203049;
            border-radius: 4px;
            color: #e2e8f0;
            font-family: inherit;
            font-size: 12px;
            box-sizing: border-box;
        }}
        .menu-item input:focus {{
            outline: none;
            border-color: #3b82f6;
            box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
        }}
        .menu-item.action-btn {{
            background: #1f2937;
            border: 1px solid #374151;
            color: #93c5fd;
            cursor: pointer;
            text-align: center;
            padding: 6px 8px;
            border-radius: 4px;
            font-size: 12px;
            transition: all 0.2s ease;
        }}
        .menu-item.action-btn:hover {{
            background: #2d3748;
            border-color: #4b5563;
        }}
        .menu-item.close-btn {{
            background: #7f1d1d;
            border: 1px solid #991b1b;
            color: #fca5a5;
            cursor: pointer;
            text-align: center;
            padding: 6px 8px;
            border-radius: 4px;
            font-size: 12px;
            transition: all 0.2s ease;
        }}
        .menu-item.close-btn:hover {{
            background: #991b1b;
            border-color: #b91c1c;
        }}
        .menu-item.file-btn {{
            background: #1e40af;
            border: 1px solid #1e3a8a;
            color: #93c5fd;
            cursor: pointer;
            text-align: center;
            padding: 6px 8px;
            border-radius: 4px;
            font-size: 12px;
            transition: all 0.2s ease;
        }}
        .menu-item.file-btn:hover {{
            background: #1e3a8a;
            border-color: #1e40af;
        }}
        #upload-input {{
            display: none;
        }}
        #download-modal {{
            display: none;
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 8px;
            padding: 20px;
            z-index: 1000;
            min-width: 300px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
        }}
        #download-modal.show {{
            display: block;
        }}
        #download-modal input {{
            width: 100%;
            padding: 6px 8px;
            background: #0b1220;
            border: 1px solid #203049;
            border-radius: 4px;
            color: #e2e8f0;
            margin-bottom: 10px;
            box-sizing: border-box;
        }}
        #download-modal button {{
            background: #1e40af;
            color: #93c5fd;
            border: 1px solid #1e3a8a;
            padding: 6px 12px;
            border-radius: 4px;
            cursor: pointer;
            margin-right: 5px;
            font-size: 12px;
        }}
        #download-modal button:hover {{
            background: #1e3a8a;
        }}
        #download-modal-backdrop {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.7);
            z-index: 999;
        }}
        #download-modal-backdrop.show {{
            display: block;
        }}
        /* 上传进度模态 */
        #upload-modal-backdrop {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.6);
            z-index: 1005;
        }}
        #upload-modal {{
            display: none;
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background: #0f172a;
            border: 1px solid #1f2937;
            padding: 14px;
            border-radius: 8px;
            color: #e2e8f0;
            z-index: 1006;
            min-width: 320px;
        }}
        #upload-modal .progress-bar {{
            width: 100%;
            height: 12px;
            background: #0b1220;
            border: 1px solid #203049;
            border-radius: 6px;
            overflow: hidden;
            margin-bottom: 8px;
        }}
        #upload-modal .progress-bar > i {{
            display: block;
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg,#4f46e5,#06b6d4);
        }}
        #upload-modal .meta {{
            font-size: 12px;
            color: #9ca3af;
            margin-bottom: 8px;
        }}
        /* 连接失败提示横幅 */
        #connection-banner {{
            display: none;
            position: fixed;
            top: 12px;
            left: 50%;
            transform: translateX(-50%);
            background: #7f1d1d;
            color: #ffd4d4;
            border: 1px solid #991b1b;
            padding: 8px 12px;
            border-radius: 6px;
            z-index: 1100;
            font-size: 13px;
            box-shadow: 0 6px 18px rgba(0,0,0,0.4);
        }}
        #connection-banner button {{
            margin-left: 10px;
            background: transparent;
            border: 1px solid #ffd4d4;
            color: #ffd4d4;
            padding: 4px 8px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
        }}
    </style>
    <script src="https://cdn.jsdelivr.net/npm/xterm@4.14.1/lib/xterm.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@4.14.1/css/xterm.css">
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.5.0/lib/xterm-addon-fit.js"></script>
</head>
<body>
    <!-- 右上角菜单按钮 -->
    <button id="menu-toggle" title="菜单">☰</button>
    
    <!-- 功能菜单 -->
    <div class="menu-container" id="menu-container">
        <div class="menu-item label">重新连接</div>
        <div class="menu-item">
            <input type="password" id="password-input" placeholder="输入新密码" />
        </div>
        <div class="menu-item action-btn" id="reconnect-btn">✓ 确认连接</div>
        
        <div class="menu-item label">文件传输</div>
        <div class="menu-item">
            <input type="text" id="upload-remote-path" placeholder="远端目标路径，例如: /tmp" />
        </div>
        <div class="menu-item file-btn" id="upload-btn">↑ 上传文件</div>
        <div class="menu-item file-btn" id="download-btn">↓ 下载文件</div>
        
        <div class="menu-item label">操作</div>
        <div class="menu-item close-btn" id="close-btn">✕ 关闭终端</div>
    </div>
    
    <!-- 文件上传 input (隐藏) -->
    <input type="file" id="upload-input" />
    <!-- 上传进度模态 -->
    <div id="upload-modal-backdrop"></div>
    <div id="upload-modal">
        <div class="meta" id="upload-meta">准备上传</div>
        <div class="progress-bar"><i id="upload-progress-bar"></i></div>
        <div style="text-align:right;"><button id="upload-cancel" style="background:#7f1d1d;color:#ffd4d4;border:1px solid #991b1b;padding:6px 8px;border-radius:4px;cursor:pointer;">取消</button></div>
    </div>
    
    <!-- 文件下载对话框 -->
    <div id="download-modal-backdrop"></div>
    <div id="download-modal">
        <div style="margin-bottom: 10px; color: #e2e8f0;">请输入要下载的文件路径</div>
        <input type="text" id="download-filepath" placeholder="例如: /tmp/file.txt" />
        <button id="download-confirm">确认</button>
        <button id="download-cancel">取消</button>
    </div>

    <!-- 连接状态横幅（连接失败时显示，点击可打开更换密码） -->
    <div id="connection-banner">连接失败 — <button id="connection-banner-open">更换密码</button></div>
    <div id="terminal-container"></div>
    <script>
        const encoder = new TextEncoder();
        
        // 使用简单的计算方法，而不是依赖 proposeGeometry
        function calculateTerminalSize() {
            const container = document.getElementById('terminal-container');
            // 根据容器大小和字体大小估算
            // Consolas 14px: 字符宽度约 8.4px，字符高度约 17px
            const charWidth = 8.4;
            const charHeight = 17;
            
            const containerWidth = container.clientWidth - 8;  // 减去左右padding
            const containerHeight = container.clientHeight - 8; // 减去上下padding
            
            const cols = Math.floor(containerWidth / charWidth);
            const rows = Math.floor(containerHeight / charHeight);
            
            return {
                cols: Math.max(Math.min(cols, 200), 80),
                rows: Math.max(Math.min(rows, 100), 24)
            };
        }
        
        const size = calculateTerminalSize();
        
        const term = new Terminal({
            fontFamily: 'Consolas, Monaco, "Courier New", monospace',
            fontSize: 14,
            cursorBlink: true,
            scrollback: 10000,
            cols: size.cols,
            rows: size.rows
        });
        const fitAddon = new FitAddon.FitAddon();
        term.loadAddon(fitAddon);
        
        const container = document.getElementById('terminal-container');
        term.open(container);
        
        // 打开后立即调整大小
        fitAddon.fit();
        
        // 使用计算的大小作为参数（proposeGeometry 可能不可用）
        const actualGeometry = {{
            cols: size.cols,
            rows: size.rows
        }};
        
        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = wsProtocol + '//' + window.location.hostname + ':__WS_PORT__/';
        
        const params = {{ 
            ip: "__IP__", 
            port: __PORT__, 
            user: "__USER__", 
            password: "__PASSWORD__",
            cols: actualGeometry.cols || size.cols,
            rows: actualGeometry.rows || size.rows
        }};
        
        let ws = new WebSocket(wsUrl);
        let wsConnected = false;
        const decoder = new TextDecoder('utf-8', {{ fatal: false, ignoreBOM: true }});
        let lastContextClick = 0;

        ws.onopen = () => {{
            wsConnected = true;
            try {{
                const banner = document.getElementById('connection-banner');
                if (banner) banner.style.display = 'none';
            }} catch (e) {{}}
            ws.send(JSON.stringify(params));
        }};

        ws.onmessage = (event) => {{
            try {{
                const data = JSON.parse(event.data);
                if (data.type === 'data') {{
                    const bytes = Uint8Array.from(atob(data.data), c => c.charCodeAt(0));
                    let decoded = decoder.decode(bytes, {{ stream: true }});
                    term.write(decoded.replace(/\\x00/g, ''));
                }} else if (data.type === 'download') {{
                    // 处理文件下载
                    try {{
                        const filename = data.filename;
                        const fileData = atob(data.data);
                        const bytes = new Uint8Array(fileData.length);
                        for (let i = 0; i < fileData.length; i++) {{
                            bytes[i] = fileData.charCodeAt(i);
                        }}
                        
                        const blob = new Blob([bytes], {{ type: 'application/octet-stream' }});
                        const url = URL.createObjectURL(blob);
                        const link = document.createElement('a');
                        link.href = url;
                        link.download = filename;
                        document.body.appendChild(link);
                        link.click();
                        document.body.removeChild(link);
                        URL.revokeObjectURL(url);
                        
                        term.write(`\\r\\n>>> 文件已下载: ${{filename}} (${{data.size}} 字节)\\r\\n`);
                    }} catch (err) {{
                        console.error('文件下载处理失败:', err);
                        term.write(`\\r\\n>>> 下载失败: ${{err.message}}\\r\\n`);
                    }}
                }} else if (data.type === 'upload-progress') {{
                    try {{
                        const id = data.upload_id;
                        const received = data.received || 0;
                        const total = data.total || 0;
                        const pct = total ? Math.floor((received/total)*100) : 0;
                        const progressBar = document.getElementById('upload-progress-bar');
                        const uploadModal = document.getElementById('upload-modal');
                        const uploadBackdrop = document.getElementById('upload-modal-backdrop');
                        const uploadMeta = document.getElementById('upload-meta');
                        if (progressBar) progressBar.style.width = pct + '%';
                        if (uploadMeta) uploadMeta.textContent = `上传中: ${{id}} (${{pct}}%)`;
                        if (pct >= 100) {{ if (uploadModal) uploadModal.style.display = 'none'; if (uploadBackdrop) uploadBackdrop.style.display = 'none'; }}
                    }} catch (err) {{ console.error('upload-progress handle failed', err); }}
                }} else if (data.type === 'upload-done') {{
                    try {{
                        const filename = data.filename;
                        const size = data.size;
                        const remote = data.remote_path || '';
                        const progressBar = document.getElementById('upload-progress-bar');
                        const uploadModal = document.getElementById('upload-modal');
                        const uploadBackdrop = document.getElementById('upload-modal-backdrop');
                        if (progressBar) progressBar.style.width = '100%';
                        if (uploadModal) uploadModal.style.display = 'none';
                        if (uploadBackdrop) uploadBackdrop.style.display = 'none';
                        term.write(`\\r\\n>>> 文件上传完成: ${{filename}} (${{size}} 字节) 到 ${{remote}}\\r\\n`);
                    }} catch (err) {{ console.error('upload-done handle failed', err); }}
                }} else if (data.type === 'upload-ack') {{
                    // 上传确认
                }} else if (data.type === 'error') {{
                    term.write('\\r\\n' + (data.message || data.error || '错误') + '\\r\\n');
                }}
            }} catch (e) {{
                console.error('处理消息错误:', e);
            }}
        }};

        ws.onerror = (error) => {{
            console.error('WebSocket error:', error);
            term.write('\r\n连接出错\r\n');
            try {{
                const banner = document.getElementById('connection-banner');
                if (banner) banner.style.display = 'block';
            }} catch (e) {{}}
        }};

        ws.onclose = (event) => {{
            try {{
                const banner = document.getElementById('connection-banner');
                if (banner && event && event.code !== 1000) banner.style.display = 'block';
            }} catch (e) {{}}
            if (event.code === 1000) {{
                // 正常关闭
            }} else {{
                term.write('\r\n会话已超时或已关闭\r\n');
            }}
        }};

        // 将 Uint8Array 转为 base64 的安全方法（避免 apply 限制）
        function bytesToBase64(uint8arr) {{
            let CHUNK_SIZE = 0x8000;
            let index = 0;
            let length = uint8arr.length;
            let result = '';
            let slice;
            while (index < length) {{
                slice = uint8arr.subarray(index, Math.min(index + CHUNK_SIZE, length));
                result += String.fromCharCode.apply(null, slice);
                index += CHUNK_SIZE;
            }}
            return btoa(result);
        }}

        function sendTerminalData(text) {{
            if (!wsConnected || !ws || text === undefined || text === null) return;
            try {{
                const bytes = encoder.encode(text);
                const b64 = bytesToBase64(bytes);
                ws.send(JSON.stringify({{ type: 'data', data: b64 }}));
            }} catch (e) {{
                console.error('发送终端数据失败:', e);
            }}
        }}

        // 使用 onKey 捕获所有按键（包括方向键、组合键），提高兼容性
        term.onKey(e => {{
            try {{
                const key = e.key || '';
                sendTerminalData(key);
            }} catch (err) {{
                console.error('onKey 发送失败:', err);
            }}
        }});

        // 支持全局粘贴（例如 mac 的两指触控板粘贴或 Cmd+V）
        document.addEventListener('paste', (ev) => {{
            try {{
                const clipboardData = ev.clipboardData || window.clipboardData;
                const text = clipboardData.getData('text');
                if (text) {{
                    sendTerminalData(text);
                }}
            }} catch (err) {{
                console.error('粘贴事件处理失败:', err);
            }}
        }});

        async function writeClipboard(text) {{
            if (!text) return;
            if (navigator.clipboard && window.isSecureContext) {{
                try {{
                    await navigator.clipboard.writeText(text);
                    return;
                }} catch (e) {{}}
            }}
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            textarea.style.pointerEvents = 'none';
            document.body.appendChild(textarea);
            textarea.focus();
            textarea.select();
            try {{
                document.execCommand('copy');
            }} catch (e) {{
                console.error('复制失败:', e);
            }} finally {{
                document.body.removeChild(textarea);
            }}
        }}

        async function readClipboard() {{
            if (navigator.clipboard && navigator.clipboard.readText) {{
                try {{
                    const text = await navigator.clipboard.readText();
                    if (text) return text;
                }} catch (e) {{}}
            }}
            // 无法访问剪贴板时静默失败，交给系统快捷键 Ctrl+V / Cmd+V 处理
            return '';
        }}

        // 选中即复制（兼容非安全上下文）
        term.onSelectionChange(() => {{
            const selection = term.getSelection();
            if (selection) {{
                writeClipboard(selection);
            }}
        }});

        // 右键双击（两次间隔<=1s）粘贴
        container.addEventListener('contextmenu', async (e) => {{
            e.preventDefault();
            const now = Date.now();
            if (now - lastContextClick <= 1000) {{
                lastContextClick = 0;
                try {{
                    const text = await readClipboard();
                    if (text) {{
                        sendTerminalData(text);
                    }}
                }} catch (err) {{
                    console.error('读取剪贴板失败:', err);
                }}
            }} else {{
                lastContextClick = now;
            }}
        }});

        // 右侧控制面板功能
        const menuToggleBtn = document.getElementById('menu-toggle');
        const menuContainer = document.getElementById('menu-container');
        const passwordInput = document.getElementById('password-input');
        const reconnectBtn = document.getElementById('reconnect-btn');
        const closeBtn = document.getElementById('close-btn');

        // 菜单显示/隐藏切换
        menuToggleBtn.addEventListener('click', () => {{
            menuContainer.classList.toggle('show');
            passwordInput.focus();
        }});

        // 点击其他地方关闭菜单
        document.addEventListener('click', (e) => {{
            if (!e.target.closest('#menu-toggle') && !e.target.closest('.menu-container')) {{
                menuContainer.classList.remove('show');
            }}
        }});

        // 连接失败横幅按钮：快速打开更换密码菜单
        const bannerOpenBtn = document.getElementById('connection-banner-open');
        if (bannerOpenBtn) {{
            bannerOpenBtn.addEventListener('click', () => {{
                menuContainer.classList.add('show');
                passwordInput.focus();
            }});
        }}

        // 重新连接功能
        reconnectBtn.addEventListener('click', () => {{
            const newPassword = passwordInput.value.trim();
            if (!newPassword) {{
                alert('请输入新密码');
                passwordInput.focus();
                return;
            }}
            
            // 关闭当前连接（结束当前会话）
            if (ws && ws.readyState === WebSocket.OPEN) {{
                ws.close();
            }}
            
            // 等待连接关闭
            setTimeout(() => {{
                // 更新params中的密码
                const newParams = {{ 
                    ip: params.ip, 
                    port: params.port, 
                    user: params.user, 
                    password: newPassword,
                    cols: actualGeometry.cols || params.cols,
                    rows: actualGeometry.rows || params.rows
                }};
                
                // 清空终端
                term.clear();
                term.writeln('\\x1b[33m正在建立新会话...\\x1b[m');
                
                // 重新建立连接（新起一个会话）
                ws = new WebSocket(wsUrl);
                wsConnected = false;
                
                ws.onopen = () => {{
                    wsConnected = true;
                    try {{ const banner = document.getElementById('connection-banner'); if (banner) banner.style.display = 'none'; }} catch (e) {{}}
                    ws.send(JSON.stringify(newParams));
                    menuContainer.classList.remove('show');
                    passwordInput.value = '';
                    term.focus();
                }};

                ws.onmessage = (event) => {{
                    try {{
                        const data = JSON.parse(event.data);
                        if (data.type === 'data') {{
                            const bytes = Uint8Array.from(atob(data.data), c => c.charCodeAt(0));
                            let decoded = decoder.decode(bytes, {{ stream: true }});
                            term.write(decoded.replace(/\\x00/g, ''));
                        }} else if (data.type === 'download') {{
                            // 处理文件下载
                            try {{
                                const filename = data.filename;
                                const fileData = atob(data.data);
                                const bytes = new Uint8Array(fileData.length);
                                for (let i = 0; i < fileData.length; i++) {{
                                    bytes[i] = fileData.charCodeAt(i);
                                }}
                                
                                const blob = new Blob([bytes], {{ type: 'application/octet-stream' }});
                                const url = URL.createObjectURL(blob);
                                const link = document.createElement('a');
                                link.href = url;
                                link.download = filename;
                                document.body.appendChild(link);
                                link.click();
                                document.body.removeChild(link);
                                URL.revokeObjectURL(url);
                                
                                term.write(`\\r\\n>>> 文件已下载: ${{filename}} (${{data.size}} 字节)\\r\\n`);
                            }} catch (err) {{
                                console.error('文件下载处理失败:', err);
                                term.write(`\\r\\n>>> 下载失败: ${{err.message}}\\r\\n`);
                            }}
                        }} else if (data.type === 'upload-progress') {{
                            try {{
                                const id = data.upload_id;
                                const received = data.received || 0;
                                const total = data.total || 0;
                                const pct = total ? Math.floor((received/total)*100) : 0;
                                const progressBar = document.getElementById('upload-progress-bar');
                                const uploadModal = document.getElementById('upload-modal');
                                const uploadBackdrop = document.getElementById('upload-modal-backdrop');
                                const uploadMeta = document.getElementById('upload-meta');
                                if (progressBar) progressBar.style.width = pct + '%';
                                if (uploadMeta) uploadMeta.textContent = `上传中: ${{id}} (${{pct}}%)`;
                                if (pct >= 100) {{ if (uploadModal) uploadModal.style.display = 'none'; if (uploadBackdrop) uploadBackdrop.style.display = 'none'; }}
                            }} catch (err) {{ console.error('upload-progress handle failed', err); }}
                        }} else if (data.type === 'upload-done') {{
                            try {{
                                const filename = data.filename;
                                const size = data.size;
                                const remote = data.remote_path || '';
                                const progressBar = document.getElementById('upload-progress-bar');
                                const uploadModal = document.getElementById('upload-modal');
                                const uploadBackdrop = document.getElementById('upload-modal-backdrop');
                                if (progressBar) progressBar.style.width = '100%';
                                if (uploadModal) uploadModal.style.display = 'none';
                                if (uploadBackdrop) uploadBackdrop.style.display = 'none';
                                term.write(`\\r\\n>>> 文件上传完成: ${{filename}} (${{size}} 字节) 到 ${{remote}}\\r\\n`);
                            }} catch (err) {{ console.error('upload-done handle failed', err); }}
                        }} else if (data.type === 'upload-ack') {{
                            // 上传确认
                        }} else if (data.type === 'error') {{
                            term.write('\\r\\n' + (data.message || data.error || '错误') + '\\r\\n');
                        }}
                    }} catch (e) {{
                        console.error('处理消息错误:', e);
                    }}
                }};

                    ws.onerror = (error) => {{
                        term.write('\\r\\n连接失败，请检查密码\\r\\n');
                        try {{ const banner = document.getElementById('connection-banner'); if (banner) banner.style.display = 'block'; }} catch (e) {{}}
                    }};

                    ws.onclose = (event) => {{
                        try {{ const banner = document.getElementById('connection-banner'); if (banner && event && event.code !== 1000) banner.style.display = 'block'; }} catch (e) {{}}
                        if (!event.wasClean) {{
                            term.write('\\r\\n会话已超时或已关闭\\r\\n');
                        }}
                    }};
            }}, 200);
        }});

        // 关闭终端功能
        closeBtn.addEventListener('click', () => {{
            if (ws) {{
                ws.close();
            }}
            term.write('\\r\\n*** 会话已关闭 ***\\r\\n');
            // 延迟后返回首页
            setTimeout(() => {{
                window.location.href = '/';
            }}, 500);
        }});

        // 文件上传功能
        const uploadBtn = document.getElementById('upload-btn');
        const uploadInput = document.getElementById('upload-input');
        
        uploadBtn.addEventListener('click', () => {{
            uploadInput.click();
        }});
        
        uploadInput.addEventListener('change', async (e) => {{
            const file = e.target.files[0];
            if (!file) return;

            const remotePathInput = document.getElementById('upload-remote-path');
            const remotePath = (remotePathInput && remotePathInput.value.trim()) || '/tmp';

            term.write(`\\r\\n>>> 准备上传: ${{file.name}} -> ${{remotePath}}\\r\\n`);

            const uploadModal = document.getElementById('upload-modal');
            const uploadBackdrop = document.getElementById('upload-modal-backdrop');
            const progressBar = document.getElementById('upload-progress-bar');
            const uploadMeta = document.getElementById('upload-meta');
            const uploadCancelBtn = document.getElementById('upload-cancel');

            let cancelled = false;
            uploadCancelBtn.onclick = () => {{ cancelled = true; uploadModal.style.display = 'none'; uploadBackdrop.style.display = 'none'; term.write(`\\r\\n>>> 已取消上传\\r\\n`); }};

            uploadMeta.textContent = `正在处理中: ${{file.name}}`;
            progressBar.style.width = '0%';
            uploadBackdrop.style.display = 'block';
            uploadModal.style.display = 'block';

            const reader = new FileReader();
            reader.onload = async (event) => {{
                try {{
                    const arrayBuffer = event.target.result;
                    const total = arrayBuffer.byteLength;
                    const upload_id = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2,8);

                    // 发送 init
                    if (!(wsConnected && ws && ws.readyState === WebSocket.OPEN)) {{
                        term.write('\\r\\n>>> WebSocket 未连接，无法上传\\r\\n');
                        uploadModal.style.display = 'none'; uploadBackdrop.style.display = 'none';
                        return;
                    }}
                    ws.send(JSON.stringify({{ type: 'upload-init', upload_id: upload_id, filename: file.name, size: total, remote_path: remotePath }}));

                    const CHUNK_SIZE = 64 * 1024; // 64KB
                    let offset = 0;
                    let index = 0;
                    while (offset < total) {{
                        if (cancelled) break;
                        const end = Math.min(offset + CHUNK_SIZE, total);
                        const chunk = new Uint8Array(arrayBuffer.slice(offset, end));
                        const b64 = bytesToBase64(chunk);
                        ws.send(JSON.stringify({{ type: 'upload-chunk', upload_id: upload_id, index: index, data: b64 }}));
                        offset = end;
                        index += 1;
                        const percent = Math.floor((offset / total) * 100);
                        progressBar.style.width = percent + '%';
                        uploadMeta.textContent = `上传中: ${{file.name}} (${{percent}}%)`;
                        // 短暂等待，避免阻塞 UI
                        await new Promise(r => setTimeout(r, 20));
                    }}

                    if (!cancelled) {{
                        ws.send(JSON.stringify({{ type: 'upload-complete', upload_id: upload_id }}));
                    }} else {{
                        // 如果取消，通知后端（可选）
                        try {{ ws.send(JSON.stringify({{ type: 'upload-cancel', upload_id: upload_id }})); }} catch(e){{}}
                    }}

                    // 等待服务器响应 upload-done 更新（由 ws.onmessage 处理）
                }} catch (err) {{
                    console.error('上传失败:', err);
                    term.write(`\\r\\n>>> 上传失败: ${{err.message}}\\r\\n`);
                    uploadModal.style.display = 'none'; uploadBackdrop.style.display = 'none';
                }}
            }};
            reader.readAsArrayBuffer(file);

            // 清空 input，以便再次选择同一文件
            uploadInput.value = '';
        }});
        
        // 文件下载功能
        const downloadBtn = document.getElementById('download-btn');
        const downloadModal = document.getElementById('download-modal');
        const downloadModalBackdrop = document.getElementById('download-modal-backdrop');
        const downloadFilepath = document.getElementById('download-filepath');
        const downloadConfirm = document.getElementById('download-confirm');
        const downloadCancel = document.getElementById('download-cancel');
        
        downloadBtn.addEventListener('click', () => {{
            downloadModal.classList.add('show');
            downloadModalBackdrop.classList.add('show');
            downloadFilepath.focus();
        }});
        
        downloadCancel.addEventListener('click', () => {{
            downloadModal.classList.remove('show');
            downloadModalBackdrop.classList.remove('show');
            downloadFilepath.value = '';
        }});
        
        downloadModalBackdrop.addEventListener('click', () => {{
            downloadModal.classList.remove('show');
            downloadModalBackdrop.classList.remove('show');
            downloadFilepath.value = '';
        }});
        
        downloadConfirm.addEventListener('click', () => {{
            const filepath = downloadFilepath.value.trim();
            if (!filepath) {{
                alert('请输入文件路径');
                downloadFilepath.focus();
                return;
            }}
            
            if (wsConnected && ws && ws.readyState === WebSocket.OPEN) {{
                ws.send(JSON.stringify({{
                    type: 'download',
                    filename: filepath
                }}));
            }}
            
            downloadModal.classList.remove('show');
            downloadModalBackdrop.classList.remove('show');
            downloadFilepath.value = '';
            term.write(`\\r\\n>>> 正在下载文件: ${{filepath}}...\\r\\n`);
        }});
        
        downloadFilepath.addEventListener('keypress', (e) => {{
            if (e.key === 'Enter') {{
                downloadConfirm.click();
            }}
        }});

        // 回车键快速提交密码
        passwordInput.addEventListener('keypress', (e) => {{
            if (e.key === 'Enter') {{
                reconnectBtn.click();
            }}
        }});

        // Resize 事件防抖处理
        let resizeTimeout = null;
        let lastResizeCols = actualGeometry.cols || params.cols;
        let lastResizeRows = actualGeometry.rows || params.rows;
        
        window.addEventListener('resize', () => {{
            // 清除之前的防抖计时器
            if (resizeTimeout) {{
                clearTimeout(resizeTimeout);
            }}
            
            // 使用防抖延迟 300ms 后才调整大小，避免频繁调整
            resizeTimeout = setTimeout(() => {{
                fitAddon.fit();
                const newSize = calculateTerminalSize();
                const geometry = {{
                    cols: newSize.cols,
                    rows: newSize.rows
                }};
                
                if (!geometry) return;
                
                // 只有当尺寸确实改变时才发送 resize 消息
                if (geometry.cols === lastResizeCols && geometry.rows === lastResizeRows) {{
                    return;
                }}
                
                lastResizeCols = geometry.cols;
                lastResizeRows = geometry.rows;
                
                if (wsConnected && ws && ws.readyState === WebSocket.OPEN) {{
                    ws.send(JSON.stringify({{ 
                        type: 'resize', 
                        cols: geometry.cols, 
                        rows: geometry.rows 
                    }}));
                }}
            }}, 300);
        }});
        
        term.focus();
    </script>
</body>
</html>
"""
            # 将模板中用于转义的大括号还原为单大括号，
            # 这样 `${{var}}` 等会变为正确的 `${var}`，避免 JS 语法错误
            html = html.replace('{{', '{').replace('}}', '}')
            # 填充占位符为实际值，避免在 HTML 中使用 f-string 导致大量大括号需要转义
            html = html.replace('__WS_PORT__', str(ws_port))
            html = html.replace('__IP__', ip)
            html = html.replace('__PORT__', str(port))
            html = html.replace('__USER__', user)
            html = html.replace('__PASSWORD__', password)

            # (已移除) 不再在服务器端写出调试 HTML 快照

            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(html.encode())))
            self.end_headers()
            self.wfile.write(html.encode())
        elif self.path.startswith('/ssh'):
            q = parse_qs(urlparse(self.path).query)
            ip = (q.get('ip') or [''])[0]
            user = (q.get('user') or ['root'])[0]
            port = int((q.get('port') or ['22'])[0])
            ssh_url = f"ssh://{user}@{ip}:{port}" if user else f"ssh://{ip}:{port}"
            iterm_url = f"iterm2://open?url={ssh_url}"
            html = ("<html><head><meta charset='utf-8'><title>SSH 跳转</title>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
                "<style>body{font-family:system-ui,Arial;background:#0f172a;color:#e2e8f0;margin:0;font-size:14px}"
                ".wrap{max-width:720px;margin:24px auto;padding:0 12px}"
                ".card{background:#111827;border:1px solid #1f2937;border-radius:12px;box-shadow:0 10px 25px rgba(0,0,0,.3);padding:14px}"
                "input{background:#0b1220;color:#e2e8f0;border:1px solid #203049;border-radius:8px;padding:6px 8px;outline:none;font-size:13px}"
                "button{background:#1f2937;color:#93c5fd;border:1px solid #203049;border-radius:8px;padding:8px 12px;cursor:pointer;margin-right:8px}"
                "a{color:#93c5fd}"
                "code{background:#0b1220;border:1px solid #203049;border-radius:6px;padding:4px 6px;display:inline-block}"
                "</style></head><body><div class='wrap'><div class='card'>"
                f"<h3 style='margin:0 0 12px'>SSH 跳转：{ip}:{port}</h3>"
                "<div style='margin-bottom:10px'>"
                "用户 <input id='user' value='" + user + "'/>"
                "</div>"
                "<div style='margin-bottom:12px'>"
                "<button onclick=\"openSSH()\">打开 ssh://</button>"
                "<button onclick=\"copyCmd()\">复制 SSH 命令</button>"
                f"<a href='{iterm_url}' target='_blank'>尝试使用 iTerm2 打开</a>"
                "</div>"
                "<div>命令： <code id='cmd'></code></div>"
                "<div style='margin-top:12px'><a href='/'>&larr; 返回列表</a></div>"
                "</div></div><script>"
                f"const ip='{ip}'; const port={port}; let user='{user}';"
                "function buildCmd(){user=document.getElementById('user').value.trim()||'root';return `ssh ${user}@${ip} -p ${port}`;}"
                "function render(){document.getElementById('cmd').textContent=buildCmd();}"
                "async function copyCmd(){const t=buildCmd();try{await navigator.clipboard.writeText(t);}catch(e){const ta=document.createElement('textarea');ta.value=t;document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);}}"
                "function openSSH(){user=document.getElementById('user').value.trim(); const u = user? `ssh://${user}@${ip}:${port}` : `ssh://${ip}:${port}`; location.href = u;}"
                "render();"
                "</script></body></html>")
            body = html.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            html = render_html(self.server.ips, self.server.ports)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html.encode())))
            self.end_headers()
            self.wfile.write(html.encode())

def expand_ips(ips_str, cidr, ip_range):
    ips = []
    if ips_str:
        ips.extend([i.strip() for i in ips_str.split(',') if i.strip()])
    if cidr:
        net = ipaddress.ip_network(cidr, strict=False)
        for ip in net.hosts():
            ips.append(str(ip))
    if ip_range:
        parts = ip_range.split('-')
        if len(parts) == 2:
            start = ipaddress.ip_address(parts[0].strip())
            end = ipaddress.ip_address(parts[1].strip())
            cur = int(start)
            while cur <= int(end):
                ips.append(str(ipaddress.ip_address(cur)))
                cur += 1
    seen = set()
    result = []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            result.append(ip)
    return result

def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ips', type=str, required=False)
    parser.add_argument('--cidr', type=str, required=False)
    parser.add_argument('--range', type=str, required=False)
    parser.add_argument('--http-port', type=int, default=8000)
    parser.add_argument('--ws-port', type=int, default=8001)
    parser.add_argument('--ports', default='22,80,443')
    parser.add_argument('--concurrency', type=int, default=100)
    parser.add_argument('--ping-timeout', type=float, default=1)
    parser.add_argument('--tcp-timeout', type=float, default=1)
    parser.add_argument('--interval', type=int, default=5, help='probe interval seconds')
    args = parser.parse_args()
    
    ips = []
    if args.ips:
        ips.extend(args.ips.split(','))
    if args.cidr:
        try:
            network = ipaddress.ip_network(args.cidr)
            ips.extend([str(ip) for ip in network.hosts()])
        except ValueError:
            print(f"invalid cidr: {args.cidr}")
            exit(1)
    if args.range:
        try:
            start_ip, end_ip = args.range.split('-')
            start = int(ipaddress.ip_address(start_ip))
            end = int(ipaddress.ip_address(end_ip))
            if start > end:
                print(f"invalid range: start > end")
                exit(1)
            ips.extend([str(ipaddress.ip_address(i)) for i in range(start, end + 1)])
        except Exception as e:
            print(f"invalid range: {args.range}, {e}")
            exit(1)
            
    if not ips:
        # 如果没有提供IP，使用默认IP范围 192.168.50.0-192.168.50.255
        start_ip = '192.168.50.0'
        end_ip = '192.168.50.255'
        start = int(ipaddress.ip_address(start_ip))
        end = int(ipaddress.ip_address(end_ip))
        ips.extend([str(ipaddress.ip_address(i)) for i in range(start, end + 1)])
        print(f"no ips provided. use default ip range {start_ip}-{end_ip}")

    ports = [int(p) for p in args.ports.split(',')]
    
    print(f"probe {len(ips)} ips, {len(ports)} ports, interval {args.interval}s")
    if not ips:
        raise SystemExit('no ips provided. use --ips, --cidr or --range')
    ports = [int(p.strip()) for p in args.ports.split(',') if p.strip()] if args.ports else []

    t = threading.Thread(target=probe_loop, args=(ips, ports, args.interval, args.ping_timeout, args.tcp_timeout, args.concurrency), daemon=True)
    t.start()

    # 启动WebSocket服务器
    async def start_websocket_server():
        # 使用0.0.0.0监听所有接口
        async with websockets.serve(handle_terminal, "0.0.0.0", args.ws_port):
            print(f"WebSocket服务器已启动，监听端口 {args.ws_port}")
            await asyncio.Future()  # 永远运行
    
    # 在单独线程中运行WebSocket服务器
    def run_websocket_server():
        asyncio.set_event_loop(asyncio.new_event_loop())
        loop = asyncio.get_event_loop()
        loop.run_until_complete(start_websocket_server())
    
    ws_thread = threading.Thread(target=run_websocket_server, daemon=True)
    ws_thread.start()

    # 启动HTTP服务器
    server = HTTPServer(('0.0.0.0', args.http_port), Handler)
    server.ips = ips
    server.ports = ports
    server.interval = args.interval
    server.ws_port = args.ws_port
    print(f"HTTP服务器已启动，访问地址: http://localhost:{args.http_port}/")
    server.serve_forever()

if __name__ == '__main__':
    run()







