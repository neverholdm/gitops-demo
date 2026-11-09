# ==============================================================================
# 阶段：生产级轻量 FastAPI 运行时镜像
# 遵循 OCI 标准、分层缓存最佳实践与最小权限安全原则
# ==============================================================================

# 1. 基础镜像：选用官方精简版 Python 3.12 (Debian slim)，兼顾轻量与 glibc 兼容性
FROM python:3.12-slim

# 2. 运行时环境变量配置：
# - PYTHONDONTWRITEBYTECODE=1: 禁止生成 .pyc 字节码缓存，减少镜像体积与容器层写操作
# - PYTHONUNBUFFERED=1: 强制 Python 标准输出和标准错误即时刷新，保证容器日志无延迟输出到 docker logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 3. 设置工作目录
WORKDIR /app

# 4. 安全防护：创建非 root 运行组与专用系统用户 (appuser)
# 规避以 root 用户运行容器的安全风险（防范容器逃逸与提权攻击）
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -M appuser

# 5. 核心层级缓存优化 (Docker Layer Cache Optimization)：
# 仅先复制依赖描述文件并执行安装。只要 requirements.txt 不变，该层构建结果将被永久复用，
# 避免每次修改业务代码时重复下载安装第三方包，极大加速 CI/CD 流水线。
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 6. 复制业务代码并赋予 appuser 属权
COPY app/ ./app/
RUN chown -R appuser:appgroup /app

# 7. 切换至非 root 用户运行
USER appuser

# 8. 声明容器监听端口
EXPOSE 8000

# 9. 容器级健康检查指令：
# 基于 Python 标准库 urllib 发起探测，无需容器额外安装 curl 或 wget，杜绝无谓体积膨胀
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

# 10. 容器启动入口：启动 Uvicorn ASGI 服务器托管 FastAPI
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
