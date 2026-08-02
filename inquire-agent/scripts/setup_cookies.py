"""
scripts/setup_cookies.py — 首次配置：从浏览器导出广材网/云筑网 cookie
用法：
  1. 关闭 Chrome → 运行本脚本（自动带调试端口启动 Chrome，登录态保留）
  2. 在弹出窗口手动登录广材网 + 云筑网（各一次，勾"记住我"）
  3. 按 Enter → 脚本从 CDP 导出 cookie → 存 accounts/*.json
"""

import subprocess, time, json, os, base64, socket, struct
import urllib.request

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER_DATA = r"C:\Users\54519\AppData\Local\Google\Chrome\User Data"
PORT = 9222
DOMAINS = {
    "gldjc": "gldjc.com",   # → accounts/gldjc_cookies.json
    "yzw": "yzw.cn",        # → accounts/yzw_cookies.json
}

# 审查 P2-6：路径可配置（环境变量优先），换机不失效
CHROME_PATH = os.environ.get("CHROME_PATH", CHROME_PATH)
USER_DATA = os.environ.get("CHROME_USER_DATA", USER_DATA)
if not os.path.exists(CHROME_PATH):
    raise SystemExit(f"❌ 找不到 Chrome: {CHROME_PATH}\n请设置环境变量 CHROME_PATH 或修改脚本路径")
if not os.path.isdir(USER_DATA):
    raise SystemExit(f"❌ 找不到 Chrome 用户数据: {USER_DATA}\n请设置环境变量 CHROME_USER_DATA 或修改脚本路径")


class MiniWS:  # 已验证可用的最小 WebSocket 客户端（复用今日验证代码）
    def __init__(self, url):
        self.sock = socket.create_connection(("localhost", PORT), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        path = url.split(f"localhost:{PORT}", 1)[1]
        req = (f"GET {path} HTTP/1.1\r\nHost: localhost:{PORT}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        self.msg_id = 0

    def send(self, method, params=None):
        self.msg_id += 1
        msg = json.dumps({"id": self.msg_id, "method": method, "params": params or {}}).encode()
        mask = os.urandom(4)
        hdr = bytearray([0x81]); ln = len(msg)
        if ln < 126: hdr.append(0x80 | ln)
        else: hdr.append(0x80 | 126); hdr += struct.pack(">H", ln)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(msg))
        self.sock.sendall(bytes(hdr) + mask + masked)
        while True:
            h = self.sock.recv(2)
            if not h: return None
            op = h[0] & 0x0F; ln = h[1] & 0x7F
            if ln == 126: ln = struct.unpack(">H", self.sock.recv(2))[0]
            elif ln == 127: ln = struct.unpack(">Q", self.sock.recv(8))[0]
            payload = self.sock.recv(ln)
            if op == 1:
                d = json.loads(payload.decode())
                if d.get("id") == self.msg_id:
                    return d


def main():
    print("1. 请确认已关闭所有 Chrome 窗口")
    input("2. 按 Enter 启动带调试端口的 Chrome（登录态保留）...")
    subprocess.Popen([CHROME_PATH, f"--remote-debugging-port={PORT}",
                      f"--user-data-dir={USER_DATA}", "--no-first-run",
                      "https://www.gldjc.com/"])

    # 等待 CDP
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}/json/version", timeout=2)
            break
        except Exception:
            time.sleep(0.5)

    input("3. 在弹出窗口登录广材网 + 云筑网，完成后按 Enter...")

    tabs = json.loads(urllib.request.urlopen(f"http://localhost:{PORT}/json").read().decode())
    page = [t for t in tabs if t["type"] == "page"][0]
    ws = MiniWS(page["webSocketDebuggerUrl"])
    r = ws.send("Network.getAllCookies", {})
    cookies = r.get("result", {}).get("cookies", [])

    for name, domain in DOMAINS.items():
        picked = [{"name": c["name"], "value": c["value"], "domain": c["domain"]}
                  for c in cookies if domain in c.get("domain", "")]
        path = f"accounts/{name}_cookies.json"
        os.makedirs("accounts", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({c["name"]: c["value"] for c in picked}, f, ensure_ascii=False)
        print(f"✅ {name}: 已保存 {len(picked)} 个 cookie → {path}")

    input("4. 按 Enter 关闭调试 Chrome...")
    # 注意：不杀用户后续手动的 Chrome，只提示手动关闭


if __name__ == "__main__":
    main()
