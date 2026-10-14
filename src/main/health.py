from aiohttp import web
from dishka import AsyncContainer

from src.system.services import HealthService

CONTAINER_KEY = web.AppKey("container", AsyncContainer)


async def live(request: web.Request) -> web.Response:
    """Answers while the process runs: the Docker healthcheck, no dependencies."""
    return web.json_response({"status": "ok"})


async def ready(request: web.Request) -> web.Response:
    async with request.app[CONTAINER_KEY]() as scope:
        readiness = await (await scope.get(HealthService)).check_readiness()
    body = {
        "status": "ok" if readiness.ok else "unavailable",
        "postgres": readiness.postgres,
        "redis": readiness.redis,
    }
    return web.json_response(body, status=200 if readiness.ok else 503)


async def health(request: web.Request) -> web.Response:
    async with request.app[CONTAINER_KEY]() as scope:
        report = await (await scope.get(HealthService)).get_report()
    return web.json_response(
        {
            "status": report.status,
            "version": report.version,
            "mode": report.mode,
            "postgres": report.postgres,
            "redis": report.redis,
            "redis_memory_used_ratio": report.redis_memory_used_ratio,
        }
    )


def create_web_app(container: AsyncContainer) -> web.Application:
    """The aiohttp application both modes serve. Probes only here; the
    webhook mode adds the Telegram route to the same application."""
    app = web.Application()
    app[CONTAINER_KEY] = container
    # GET routes answer HEAD as well (aiohttp's allow_head defaults to True).
    app.router.add_get("/live", live)
    app.router.add_get("/ready", ready)
    app.router.add_get("/health", health)
    return app
