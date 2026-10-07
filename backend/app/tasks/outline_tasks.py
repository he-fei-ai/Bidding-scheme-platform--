"""【已废弃】目录生成 Celery 异步任务

原计划用于将大型目录生成任务移至后台执行（Worker 重启后进度可恢复）。
生产路径已全部迁移至 FastAPI 进程内 asyncio.create_task + SSE 流，
本文件中的任务签名与 V5.1 工作流接口不兼容（缺 summary 参数、多了无用 kwarg），
且无任何调用方，已确认是死代码。

若未来需要 Redis 集群级任务队列，请基于 workflows_outline_v51.py 的新签名重新实现。
"""
