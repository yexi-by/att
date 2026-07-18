"""针对游戏数据库修改命令的统一高层阻断入口。"""

from __future__ import annotations

from app.persistence import GameRegistry, TargetGameSession, build_db_path


async def open_game_for_mutation(
    game_registry: GameRegistry,
    game_title: str,
) -> TargetGameSession:
    """先阻断未完成写事务，再协调翻译恢复并收束中断运行。"""
    session = await game_registry.open_game_with_mutation_lease(game_title)
    try:
        await session.assert_no_unfinished_write_transaction()
        _ = await session.reconcile_translation_run_recovery()
        _ = await session.reconcile_interrupted_translation_runs()
    except BaseException as error:
        try:
            await session.close()
        except BaseException as close_error:
            error.add_note(f"关闭修改会话时还发生了次生错误：{type(close_error).__name__}: {close_error}")
        raise
    return session


async def open_game_for_recovery(
    game_registry: GameRegistry,
    game_title: str,
) -> TargetGameSession:
    """打开恢复会话；独占修改租约，但允许读取未完成写事务。"""
    return await game_registry.open_game_with_mutation_lease(game_title)


async def assert_game_mutation_allowed(
    game_registry: GameRegistry,
    game_title: str,
) -> None:
    """在 CLI 读取修改载荷前检查已注册目标的未完成写事务。"""
    if not build_db_path(game_title, game_registry.db_directory).is_file():
        # 未注册游戏没有可恢复事务；保留后续命令原有的领域错误和报告格式。
        return
    session = await open_game_for_mutation(game_registry, game_title)
    await session.close()


__all__ = ["assert_game_mutation_allowed", "open_game_for_mutation", "open_game_for_recovery"]
