"""FastAPI GitOps Demo Application.

Provides basic status and health check endpoints for container lifecycle management.
"""

from fastapi import FastAPI

app = FastAPI(
    title="GitOps Demo App",
    description="Lightweight business service for GitOps automated deployment pipeline",
    version="v1.0.1",
)


@app.get("/", tags=["Status"])
def read_root() -> dict[str, str]:
    """Root endpoint returning service status, version, and environment.

    Used by CD pipelines and users to verify current deployed release.
    """
    return {
        "status": "healthy",
        "version": "v1.0.1",
        "environment": "production",
    }


@app.get("/health", tags=["Health"])
def health_check() -> dict[str, str]:
    """Lightweight health check endpoint.

    Used by Docker Compose and Kubernetes liveness/readiness probes.
    Must return 200 OK with minimal resource consumption.
    """
    return {"status": "ok"}
