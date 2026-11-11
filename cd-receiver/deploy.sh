#!/usr/bin/env bash
# ==============================================================================
# GitOps Deployment Automation Script
# Executed asynchronously by receiver.py upon valid Webhook trigger.
# Implements flock non-blocking file locking to prevent race conditions.
# ==============================================================================

set -euo pipefail

# 1. 关键目录与文件定义
APP_DIR="/opt/gitops-app"
LOG_DIR="${APP_DIR}/logs"
LOG_FILE="${LOG_DIR}/deploy.log"
LOCK_FILE="/var/run/gitops-deploy.lock"

# 确保日志目录存在
mkdir -p "${LOG_DIR}"

# 格式化日志输出函数
log() {
    local level="$1"
    shift
    local msg="$*"
    local timestamp
    timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[${timestamp}] [${level}] ${msg}" | tee -a "${LOG_FILE}"
}

# 2. 排他性文件锁 (flock 防并发踩踏)
# 打开文件描述符 200 并尝试以非阻塞方式获取排他锁
exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
    log "WARN" "Another deployment process is already in progress! Current trigger gracefully skipped."
    exit 0
fi

log "INFO" "==================== Starting GitOps Deployment ===================="
log "INFO" "Working directory: ${APP_DIR}"

cd "${APP_DIR}"

# 3. 检查 docker-compose.yml 配置文件是否存在
if [ ! -f "docker-compose.yml" ]; then
    log "ERROR" "docker-compose.yml not found in ${APP_DIR}!"
    exit 1
fi

# 4. 执行最新镜像拉取 (经由 Phase 1 注入的 Docker 本地透明代理加速)
log "INFO" "Step 1/3: Pulling latest container images via Docker daemon proxy..."
if docker compose pull >> "${LOG_FILE}" 2>&1; then
    log "INFO" "Image pull completed successfully."
else
    log "ERROR" "docker compose pull failed! Check proxy status or network connectivity."
    exit 1
fi

# 5. 重新启动业务容器 (无缝更新容器实例，清理遗留孤儿容器)
log "INFO" "Step 2/3: Applying updated container specification (docker compose up -d)..."
if docker compose up -d --remove-orphans >> "${LOG_FILE}" 2>&1; then
    log "INFO" "Containers updated and running in background."
else
    log "ERROR" "docker compose up failed!"
    exit 1
fi

# 6. 清理陈旧虚悬镜像 (回收磁盘空间，防止磁盘耗尽)
log "INFO" "Step 3/3: Pruning dangling Docker images to preserve disk space..."
docker image prune -f >> "${LOG_FILE}" 2>&1 || true

# 7. 容器状态自检并写入日志
log "INFO" "Verifying container runtime status..."
docker compose ps >> "${LOG_FILE}" 2>&1 || true

log "INFO" "==================== Deployment Finished Successfully ===================="
