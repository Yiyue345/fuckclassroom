from __future__ import annotations

import asyncio

from ...core.plugins import PluginContext


async def startup(context: PluginContext) -> None:
    ketangpai_client = context.services.get("ketangpai_client")

    async def verify_session() -> None:
        try:
            result = await asyncio.to_thread(
                ketangpai_client.verify_session,
                auto_relogin=True,
            )
        except Exception as exc:  # noqa: BLE001 - optional attendance check must not stop app.
            context.logger.warning("启动时课堂派会话检测失败：%s", exc)
            return
        context.logger.info("启动时课堂派会话状态：%s", result.message)

    context.create_task(verify_session())


__all__ = ["startup"]
