#!/usr/bin/env python3
"""GitOps CD Webhook Receiver Daemon.

Zero-dependency, production-grade HTTP daemon using Python standard library.
Features:
- HMAC-SHA256 zero-trust signature verification
- Clock skew defense (+/- 300s window)
- Replay attack defense with in-memory signature cache
- Constant-time signature comparison to defeat timing attacks
- Non-blocking async dispatch returning HTTP 202 Accepted immediately
- Health check probe endpoint (/health)
"""

import hashlib
import hmac
import http.server
import json
import logging
import os
import subprocess
import threading
import time

# 配置日志输出格式与级别
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [Thread-%(thread)d] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("gitops-receiver")

# 环境配置与安全参数
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "9000"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DEPLOY_SCRIPT = os.environ.get("DEPLOY_SCRIPT", "/opt/gitops-app/cd-receiver/deploy.sh")
ALLOWED_TIME_SKEW_SECONDS = 300  # 允许的时钟偏差最大秒数（5分钟）

# 防重放签名缓存：维护最近已处理签名的 (signature -> expire_at) 映射
seen_signatures: dict[str, float] = {}
cache_lock = threading.Lock()


def purge_expired_signatures(now: float) -> None:
    """清理内存中超过有效期的历史签名，防止内存无限膨胀."""
    with cache_lock:
        expired_keys = [sig for sig, exp in seen_signatures.items() if exp <= now]
        for sig in expired_keys:
            del seen_signatures[sig]


def record_signature_if_new(sig: str) -> bool:
    """原子化判定并记录签名。如果签名已存在则返回 False（命中重放拦截），否则记录并返回 True."""
    now = time.time()
    purge_expired_signatures(now)
    with cache_lock:
        if sig in seen_signatures:
            return False
        # 缓存保留时长为时间偏差窗口的 2 倍（10分钟），确保即使网络抖动也无法重放
        seen_signatures[sig] = now + (ALLOWED_TIME_SKEW_SECONDS * 2)
        return True


def execute_deploy_async(commit_sha: str, repository: str) -> None:
    """在后台独立守护线程中异步调用部署脚本，杜绝阻塞当前 HTTP 连接."""
    def _run() -> None:
        short_sha = commit_sha[:8] if commit_sha else "unknown"
        logger.info(f"Triggering background deployment: repo={repository}, commit={short_sha}")
        try:
            if not os.path.exists(DEPLOY_SCRIPT):
                logger.error(f"Deploy script not found at {DEPLOY_SCRIPT}!")
                return
            # 调度 deploy.sh 执行更新
            proc = subprocess.run(
                ["/bin/bash", DEPLOY_SCRIPT],
                capture_output=True,
                text=True,
                check=False
            )
            if proc.returncode == 0:
                logger.info(f"Deployment succeeded! (Exit Code 0)\n--- Deploy Output ---\n{proc.stdout.strip()}")
            else:
                logger.error(f"Deployment failed! (Exit Code {proc.returncode})\n--- Stdout ---\n{proc.stdout}\n--- Stderr ---\n{proc.stderr}")
        except Exception as e:
            logger.exception(f"Unexpected exception while executing deploy script: {e}")

    deploy_thread = threading.Thread(target=_run, daemon=True)
    deploy_thread.start()


class GitOpsWebhookHandler(http.server.BaseHTTPRequestHandler):
    """Webhook HTTP 请求处理器，严格遵循零信任通信协议."""

    server_version = "GitOps-CD-Receiver/1.0"

    def log_message(self, format: str, *args) -> None:
        # 重载默认访问日志，统一使用 logging 模块格式输出
        logger.info(f"{self.client_address[0]} - {format % args}")

    def send_json_response(self, status_code: int, data: dict) -> None:
        """向客户端响应标准 JSON 报文."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """提供基础连通性探测与心跳接口."""
        if self.path in ("/", "/health"):
            self.send_json_response(200, {
                "status": "healthy",
                "service": "gitops-cd-receiver",
                "port": WEBHOOK_PORT
            })
        else:
            self.send_json_response(404, {"error": "Not Found"})

    def do_POST(self) -> None:
        """核心 Webhook 接收、鉴权与异步调度逻辑."""
        if self.path != "/webhook":
            self.send_json_response(404, {"error": "Endpoint Not Found", "expected": "/webhook"})
            return

        # 1. 检查服务端自身密钥配置
        if not WEBHOOK_SECRET:
            logger.critical("Security Alert: WEBHOOK_SECRET is not configured on receiver! Rejecting all requests.")
            self.send_json_response(500, {"error": "Server misconfigured: WEBHOOK_SECRET missing"})
            return

        # 2. 提取必备安全请求头
        timestamp_header = self.headers.get("X-Timestamp")
        signature_header = self.headers.get("X-Signature-256")

        if not timestamp_header or not signature_header:
            logger.warning(f"Security Alert: Request rejected from {self.client_address[0]} - Missing required security headers.")
            self.send_json_response(400, {
                "error": "Bad Request",
                "message": "Missing X-Timestamp or X-Signature-256 headers"
            })
            return

        # 3. 时钟偏差校验 (Clock Skew Check)
        try:
            req_timestamp = int(timestamp_header)
        except ValueError:
            logger.warning(f"Security Alert: Invalid timestamp format from {self.client_address[0]}: {timestamp_header}")
            self.send_json_response(400, {"error": "Bad Request", "message": "Invalid timestamp format"})
            return

        now = int(time.time())
        skew = abs(now - req_timestamp)
        if skew > ALLOWED_TIME_SKEW_SECONDS:
            logger.warning(f"Security Alert: Clock skew exceeded from {self.client_address[0]} (Skew: {skew}s > {ALLOWED_TIME_SKEW_SECONDS}s). Replay suspected.")
            self.send_json_response(403, {
                "error": "Forbidden",
                "message": f"Timestamp outside acceptable window (skew: {skew}s)"
            })
            return

        # 4. 防重放去重校验 (Replay Attack Check)
        if not record_signature_if_new(signature_header):
            logger.warning(f"Security Alert: Replay attack intercepted from {self.client_address[0]}! Duplicate signature: {signature_header[:16]}...")
            self.send_json_response(403, {
                "error": "Forbidden",
                "message": "Replay attack detected: signature already processed"
            })
            return

        # 5. 读取请求体原始字节流
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length)
            body_str = raw_body.decode("utf-8")
        except Exception as e:
            logger.error(f"Failed to read request body: {e}")
            self.send_json_response(400, {"error": "Bad Request", "message": "Malformed body"})
            return

        # 6. 恒定时间 HMAC-SHA256 签名比对 (免疫时序攻击)
        # 协议规定：HMAC(Secret, Timestamp + Body)
        message_to_verify = (timestamp_header + body_str).encode("utf-8")
        expected_sig = hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            message_to_verify,
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature_header, expected_sig):
            logger.warning(f"Security Alert: Signature mismatch from {self.client_address[0]}! Access Denied.")
            self.send_json_response(403, {
                "error": "Forbidden",
                "message": "Invalid HMAC signature"
            })
            return

        # 7. 鉴权通过：解析 Payload 关键参数
        logger.info(f"Authentication verified successfully for request from {self.client_address[0]}")
        commit_sha = ""
        repo_name = ""
        try:
            payload = json.loads(body_str)
            commit_sha = payload.get("commit", "")
            repo_name = payload.get("repository", "")
        except json.JSONDecodeError:
            logger.warning("Payload is not valid JSON, but signature was valid.")

        # 8. 立即响应 202 Accepted 释放 Actions HTTP 客户端
        self.send_json_response(202, {
            "status": "accepted",
            "message": "Webhook authenticated. Deployment process triggered in background.",
            "commit": commit_sha[:8] if commit_sha else "unknown"
        })

        # 9. 异步派发后台更新子进程
        execute_deploy_async(commit_sha, repo_name)


def run_server() -> None:
    """启动多线程 HTTP 服务守护."""
    if not WEBHOOK_SECRET:
        logger.warning("[WARNING] WEBHOOK_SECRET environment variable is empty. Receiver will reject POST requests!")

    server_address = ("0.0.0.0", WEBHOOK_PORT)
    httpd = http.server.ThreadingHTTPServer(server_address, GitOpsWebhookHandler)
    logger.info(f"GitOps CD Webhook Receiver listening on 0.0.0.0:{WEBHOOK_PORT} (PID: {os.getpid()})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down server.")
    finally:
        httpd.server_close()
        logger.info("Server terminated.")


if __name__ == "__main__":
    run_server()
