"""Celery 异步任务定义

所有后台异步任务在此定义。
"""
import logging
from pathlib import Path
from typing import Any
from ..celery_app import celery_app
logger = logging.getLogger(__name__)

@celery_app.task(name='tasks.file.parse_and_extract', bind=True, max_retries=3)
def parse_and_extract_file(self, file_path: str, project_id: int, file_id: int, create_task_row: bool=True) -> dict:
    """异步解析文件并提取文本

    Args:
        file_path: 文件路径
        project_id: 项目ID
        file_id: 文件ID
        create_task_row: 是否新建 file_parse_tasks 行。
            BUG-FIX 2026-10-06（R3）：async-reparse 端点派发前已建一行
            （让状态端点立刻可见 processing），任务内再建一行会让同一次
            解析在任务表出现两行 processing、终态后留一条孤儿行。端点派发
            时传 False；Celery 直连/重试等自建行场景保持默认 True（向后兼容）。

    Returns:
        解析结果
    """
    try:
        from ..db import get_db
        from ..services.files import EMPTY_TEXT_MESSAGE, extract_text_from_path, is_extraction_notice

        logger.info('开始异步解析文件: %s (project=%d)', file_path, project_id)
        filename = Path(file_path).name
        # ENHANCE 2026-10-05（G7 异步链路与主链路口径对齐）：此前本任务不写
        # file_parse_tasks、也不写空文本提示，与 save_upload 的主链路口径不一致 ——
        # GET /tasks/files/status 看不到异步解析的任何痕迹，异步解析出空文本时
        # 前端也不会有任何提示。补齐（任务行 + 空文本提示 + 缓存失效已存在）。
        from ..services.files import _create_parse_task, _update_parse_task

        if create_task_row:
            _create_parse_task(project_id, filename, Path(file_path).suffix.lower())
        with get_db() as conn:
            conn.execute("UPDATE uploaded_files SET status = 'processing' WHERE id = ?", (file_id,))
        extracted_text = extract_text_from_path(file_path)
        from ..config import PROJECTS_DATA_DIR
        texts_dir = PROJECTS_DATA_DIR / str(project_id) / 'texts'
        texts_dir.mkdir(parents=True, exist_ok=True)
        # BUG-FIX 2026-09-18（P2）：命名与主管线 save_upload 对齐——主管线写
        # 「完整原文件名.txt」（Path(filename).name），此处原写 stem.txt：
        # 同目录两文件 "招标.docx" 与 "招标.pdf" 在主管线下文本互不覆盖，
        # 而走本任务则会互相覆盖。同时补齐合并文本缓存失效（主管线变更后
        # _text_cache 不清会让 AI 读到旧文本）。
        text_filename = f'{Path(file_path).name}.txt'
        text_path = texts_dir / text_filename
        text_path.write_text(extracted_text, encoding='utf-8')
        # ENHANCE 2026-10-05（B4/G7）：与 save_upload 同口径 —— 解析成功但文本为空时
        # 写入空文本提示，避免前端把「已入库但无内容」当成正常解析成功。
        # BUG-FIX 2026-10-06（H1）：再与 save_upload 同口径 —— 老格式（.doc/.ppt/.wps）
        # 提取失败时返回的「[提示] 无法自动提取…」降级串不是正文，不能写
        # status='success'，否则 _pick_main_file 会把它选作项目主文本喂给下游 AI。
        if is_extraction_notice(extracted_text):
            _status, _msg = 'failed', extracted_text.strip()
        else:
            _status, _msg = 'success', ('' if extracted_text.strip() else EMPTY_TEXT_MESSAGE)
        with get_db() as conn:
            conn.execute("UPDATE uploaded_files SET status = ?, extracted_text_path = ?, message = ? WHERE id = ?", (_status, str(text_path), _msg, file_id))
        try:
            from ..services.files import _text_cache_clear
            _text_cache_clear(project_id)
        except Exception as _tc_e:
            logger.debug('清理项目文本缓存失败 project=%d: %s', project_id, _tc_e)
        _update_parse_task(project_id, 'completed', 100, filename=filename)
        logger.info('文件解析完成: %s, 提取文本长度: %d', file_path, len(extracted_text))
        return {'status': _status, 'file_id': file_id, 'text_length': len(extracted_text), 'text_path': str(text_path)}
    except Exception as exc:
        logger.error('文件解析失败: %s, 错误: %s', file_path, exc, exc_info=True)
        try:
            from ..services.files import _update_parse_task

            _update_parse_task(project_id, 'failed', 50, str(exc), Path(file_path).name)
        except Exception as _e:
            logger.debug('[silent-except] __init__.py 更新解析任务行失败 - %s', _e)
        try:
            from ..db import get_db
            with get_db() as conn:
                conn.execute("UPDATE uploaded_files SET status = 'failed', message = ? WHERE id = ?", (str(exc), file_id))
        except Exception as _e:
            logger.debug('[silent-except] __init__.py: line 91 - %s', _e)
        if self.request.retries >= self.max_retries:
            return {'status': 'failed', 'file_id': file_id, 'error': str(exc)}
        raise exc from exc

@celery_app.task(name='tasks.content.generate_sections', bind=True, max_retries=5, time_limit=3600)
def generate_section_content(self, project_id: int, section_ids: list[int], writing_length: str='standard') -> dict:
    """异步生成章节内容

    Args:
        project_id: 项目ID
        section_ids: 章节ID列表
        writing_length: 写作长度 (short/standard/long)

    Returns:
        生成结果统计
    """
    try:
        from ..services.ai.workflows import _safe_asyncio_run, generate_section_content as _generate
        # BUG-FIX 2026-09-16（低）：空 section_ids 会生成 `WHERE id IN ()` 非法 SQL，
        # sqlite 报 OperationalError 后被外层 except 吞成 failed；空批次应直接返回完成。
        if not section_ids:
            return {'status': 'completed', 'total': 0, 'completed': 0, 'failed': 0}
        logger.info('开始异步生成章节内容: project=%d, sections=%d', project_id, len(section_ids))

        # BUG-FIX 2026-09-16（中）：接入「content-generation」任务组锁。
        # 原 Celery 路径对同一批 sections 直接 UPDATE status/content，既不调
        # task_lock 也不走 generation_manager —— 若与 API 的 SSE 生成路径
        # （或另一个 worker 实例）并发，就是两个循环并发写同一批 sections。
        # 与 API 路径保持同一互斥语义：抢不到锁直接返回冲突，不启动生成。
        from ..services import task_lock as _task_lock
        try:
            _lock_record = _task_lock.acquire(
                'content-generation',
                project_id=project_id,
                note='celery tasks.content.generate_sections',
            )
        except _task_lock.TaskLockConflict as _conflict:
            logger.warning('Celery 章节生成被任务组锁拦截: project=%d: %s', project_id, _conflict)
            return {'status': 'failed', 'error': f'任务冲突: {_conflict}'}
        _lock_key = _lock_record.get('lock_key', '')
        _lock_owner = _lock_record.get('owner_id', '')

        async def _run() -> dict:
            # P-1（2026-10-06）：asyncio 在此局部导入使用（to_thread 派发降级链健康检查）
            import asyncio
            from ..db import get_db
            from ..services.ai.industry_profiles import get_industry_profile
            from ..services.ai.provider_factory import create_provider_with_fallback
            from ..services.files import latest_extracted_text
            from ..services.tender_context import extract_tender_context_async
            placeholders = ','.join(('?' for _ in section_ids))
            with get_db() as conn:
                section_rows = conn.execute(f'SELECT id, title, level, section_type, target_words FROM sections WHERE id IN ({placeholders}) AND project_id = ?', (*section_ids, project_id)).fetchall()
                analysis_row = conn.execute('SELECT summary_json, scoring_points_json, global_facts_json FROM analysis WHERE project_id = ?', (project_id,)).fetchone()
                project_row = conn.execute('SELECT industry FROM projects WHERE id = ?', (project_id,)).fetchone()
            if not section_rows:
                return {'status': 'completed', 'total': 0, 'completed': 0, 'failed': 0}
            summary = analysis_row['summary_json'] if analysis_row else ''
            scoring_standard = (analysis_row['scoring_points_json'] if analysis_row else '') or ''
            industry = project_row['industry'] if project_row else ''
            industry_profile = get_industry_profile(industry) if industry else ''
            try:
                full_text = await latest_extracted_text(project_id)
                tender_context = await extract_tender_context_async(full_text) if full_text else ''
            except Exception:
                tender_context = ''
            global_facts = None
            if analysis_row and analysis_row['global_facts_json']:
                # BUG-FIX 2026-09-29（占位泄漏 + 调用链断裂）：原写法先 _json.loads
                # 得到未过滤列表，再「尝试」从 routers.sse.sse_utils 导入
                # _parse_global_facts_items 做占位过滤。Celery worker 里导入路由层
                # 会连带拖进 FastAPI/SSE 依赖，一旦抛错仅 logger.debug 静默降级，
                # global_facts 保持未过滤原值——placeholder 策略的【待填写】依旧
                # 被当真实事实写进正文，与 SSE 主管线口径不一致。
                # 现直接走服务层正本（自带解析容错 + 占位剔除，不依赖路由层）。
                from ..services.facts_service import facts_for_generation
                global_facts = facts_for_generation(analysis_row['global_facts_json']) or None
            with get_db() as conn:
                all_rows = conn.execute('SELECT id, parent_id, title FROM sections WHERE project_id = ?', (project_id,)).fetchall()
            id_map = {r['id']: r for r in all_rows}

            def _build_path(sid: int) -> str:
                chain: list[str] = []
                cur = id_map.get(sid)
                hops = 0
                while cur is not None and hops < 8:
                    chain.append(cur['title'])
                    pid = cur['parent_id']
                    cur = id_map.get(pid) if pid is not None else None
                    hops += 1
                return ' > '.join(reversed(chain))
            provider, _provider_name = await asyncio.to_thread(create_provider_with_fallback)
            completed = 0
            failed = 0
            for idx, row in enumerate(section_rows):
                sid = row['id']
                section_title = row['title']
                section_path = _build_path(sid) or section_title
                # 长批次下每章续约一次任务组锁，防止 TTL 过期后被其他任务抢走
                if _lock_key and _lock_owner:
                    try:
                        _task_lock.heartbeat(_lock_key, _lock_owner)
                    except Exception as _hb_e:
                        logger.debug('Celery 任务锁续约失败 project=%d: %s', project_id, _hb_e)
                with get_db() as conn:
                    conn.execute("UPDATE sections SET status = 'generating', progress = ? WHERE id = ? AND project_id = ?", (int(idx / len(section_rows) * 100), sid, project_id))
                try:
                    # BUG-FIX 2026-09-14（高）：generate_section_content 的签名是
                    # (provider, section, context=None, progress_callback=None)，
                    # section 是**必填位置参数**。原调用只传关键字参数 → 必然
                    # TypeError，被下面的 except 吞掉后每个章节都记为 failed，
                    # 整批"生成"实际什么都没生成。这里按真实签名构造 section/context。
                    _task_section = {
                        "id": sid,
                        "title": section_title,
                        "level": row["level"] or 3,
                        "path": section_path,
                        "section_type": row["section_type"] or "text",
                        "target_words": row["target_words"],
                        "project_id": project_id,
                    }
                    _task_context = {
                        "facts": global_facts,
                        "summary": summary,
                        "tender_context": tender_context,
                        "industry_profile": industry_profile,
                        "scoring_context": scoring_standard,
                        "writing_length": writing_length,
                        "target_words": row["target_words"],
                        "section_level": row["level"] or 3,
                        "content_type": row["section_type"] or "text",
                        "project_id": project_id,
                        "extract_charts": False,
                    }
                    result = await _generate(provider, _task_section, _task_context)
                    content = result if isinstance(result, str) else (result or {}).get('content', '')
                    if content:
                        completed += 1
                        with get_db() as conn:
                            conn.execute("UPDATE sections SET content = ?, status = 'completed', progress = 100 WHERE id = ?", (content, sid))
                    else:
                        failed += 1
                        with get_db() as conn:
                            conn.execute("UPDATE sections SET status = 'pending', progress = 0 WHERE id = ?", (sid,))
                except Exception as e:
                    failed += 1
                    logger.warning('章节 %d 生成失败: %s', sid, e)
                    with get_db() as conn:
                        conn.execute("UPDATE sections SET status = 'pending', progress = 0 WHERE id = ?", (sid,))
            return {'status': 'completed', 'total': len(section_rows), 'completed': completed, 'failed': failed}
        try:
            result = _safe_asyncio_run(_run())
        finally:
            # 无论成败都必须释放锁；release 内部校验 owner，不会误删别人的锁
            if _lock_key and _lock_owner:
                try:
                    _task_lock.release(_lock_key, _lock_owner)
                except Exception as _rel_e:
                    logger.warning('Celery 任务释放组锁失败 project=%d: %s', project_id, _rel_e)
        logger.info('章节内容生成完成: 成功%d, 失败%d', result.get('completed', 0), result.get('failed', 0))
        return result
    except Exception as exc:
        logger.error('章节内容生成失败: %s', exc, exc_info=True)
        return {'status': 'failed', 'error': str(exc)}

@celery_app.task(name='tasks.export.generate_docx', bind=True, max_retries=3, time_limit=1800)
def generate_docx_export(self, project_id: int, config: dict | None=None) -> dict:
    """异步生成 DOCX 导出文件

    Args:
        project_id: 项目ID
        config: 导出配置

    Returns:
        导出结果
    """
    try:
        # BUG-FIX 2026-09-18（P2）：接入「export」组锁，与 routers/export.py 同步/SSE
        # 导出路径互斥。原 Celery 路径不持锁，同项目可被两条链路同时全量
        # build_docx：重复渲染全部图表（双倍 CPU）+ 并发写 _EXPORT_CACHE 丢更新。
        from ..services import task_lock as _task_lock
        try:
            _lock_record = _task_lock.acquire(
                'export',
                project_id=project_id,
                note='celery tasks.export.generate_docx',
            )
        except _task_lock.TaskLockConflict as _conflict:
            logger.warning('Celery 导出被任务组锁拦截: project=%d: %s', project_id, _conflict)
            return {'status': 'failed', 'error': f'任务冲突: {_conflict}'}
        # BUG-FIX 2026-09-18（高）：改用 export_to_docx_recorded 直接拿 build_docx
        # 已写入的导出记录 ID。原实现在此处**重复 INSERT 一条 exports**，
        # 且是残缺的（config_json 硬编码 '{}'、filename 硬编码 project_{id}_{ts}.docx、
        # created_at 用本地时间），而前端按 id 取最新一条 → 拿到的是残缺记录。
        from ..services.export import export_to_docx_recorded
        logger.info('开始异步导出 DOCX: project=%d', project_id)
        # ENHANCE 2026-10-04（T4）：Celery 长导出同样心跳续约（time_limit=1800
        # 恰等于锁 TTL，无续约时临界长任务会在被 kill 前一刻丢锁）。
        _stop_keepalive = _task_lock.start_keepalive(
            _lock_record.get('lock_key', ''), _lock_record.get('owner_id', ''),
        )
        try:
            output_path, export_id = export_to_docx_recorded(project_id, config=config or {})
        finally:
            _stop_keepalive()
            # 无论成败都释放组锁；release 内部校验 owner，不会误删别人的锁
            if _lock_record.get('lock_key'):
                try:
                    _task_lock.release(_lock_record['lock_key'], _lock_record.get('owner_id', ''))
                except Exception as _rel_e:
                    logger.warning('Celery 导出释放组锁失败 project=%d: %s', project_id, _rel_e)
        logger.info('DOCX 导出完成: %s', output_path)
        return {
            'status': 'completed',
            'export_id': export_id,
            'file_path': str(output_path),
            'filename': output_path.name,
        }
    except Exception as exc:
        logger.error('DOCX 导出失败: %s', exc, exc_info=True)
        # ENHANCE 2026-10-04（T2）：Celery 失败同样写入导出历史（与同步/SSE 同口径）；
        # 配置为 dict 形态，不强行转 ExportConfig，写 '{}' 占位避免记录链路再抛异常。
        try:
            from ..services.export import record_failed_export
            record_failed_export(project_id, f'{type(exc).__name__}: {str(exc)[:200]}')
        except Exception as _rec_e:  # pragma: no cover
            logger.warning('写入失败导出历史异常 project=%d: %s', project_id, _rec_e)
        return {'status': 'failed', 'error': str(exc)}

@celery_app.task(name='tasks.knowledge.process_document', bind=True, max_retries=3, time_limit=3600)
def process_knowledge_document(self, doc_id: int, file_path: str, industry: str='') -> dict:
    """异步处理知识库文档

    Args:
        doc_id: 文档ID
        file_path: 文件路径
        industry: 行业分类

    Returns:
        处理结果

    注（BUG-FIX 2026-09-18）：`file_path` / `industry` 为保留形参 —— 服务层
    `run_pipeline(doc_id)` 只接受 doc_id，文件与行业信息由文档行自身携带。
    此前两者被静默忽略，现显式记录，避免调用方误以为传入即生效。
    """
    try:
        from ..services.ai.workflows import _safe_asyncio_run
        from ..services.knowledge import run_pipeline
        if file_path or industry:
            logger.debug(
                '知识库处理以 doc_id 为准，file_path/industry 仅作备注: doc_id=%s, file_path=%s, industry=%s',
                doc_id, file_path, industry,
            )
        logger.info('开始处理知识库文档: doc_id=%d', doc_id)
        _safe_asyncio_run(run_pipeline(doc_id))
        logger.info('知识库文档处理完成: doc_id=%d', doc_id)
        return {'status': 'completed', 'doc_id': doc_id}
    except Exception as exc:
        logger.error('知识库文档处理失败: %s', exc, exc_info=True)
        return {'status': 'failed', 'doc_id': doc_id, 'error': str(exc)}

@celery_app.task(name='tasks.check.rejection_check', bind=True, max_retries=3, time_limit=1800)
def run_rejection_check(self, task_id: int, project_id: int | None=None) -> dict:
    """异步执行废标检查

    Args:
        task_id: 检查任务ID
        project_id: 项目ID（可选）

    Returns:
        检查结果

    注（BUG-FIX 2026-09-18）：`project_id` 为保留形参 —— 服务层
    `run_rejection_check(task_id, dimension=None)` 只按 task_id 取上下文，
    不支持按项目过滤。此前该参数被静默忽略，现于下方日志中显式说明。
    """
    try:
        from ..services.ai.workflows import _safe_asyncio_run
        from ..services.rejection import run_rejection_check as _run_check
        if project_id:
            logger.debug(
                '废标检查按 task_id 执行，project_id 仅作上下文备注: task_id=%d, project_id=%s',
                task_id, project_id,
            )
        logger.info('开始异步废标检查: task_id=%d', task_id)
        _safe_asyncio_run(_run_check(task_id))
        logger.info('废标检查完成: task_id=%d', task_id)
        return {'status': 'completed', 'task_id': task_id}
    except Exception as exc:
        logger.error('废标检查失败: %s', exc, exc_info=True)
        return {'status': 'failed', 'task_id': task_id, 'error': str(exc)}

@celery_app.task(name='tasks.check.duplicate_check', bind=True, max_retries=3, time_limit=1800)
def run_duplicate_check(self, task_id: int, file_ids: list[int] | None=None) -> dict:
    """异步执行标书查重

    Args:
        task_id: 检查任务ID
        file_ids: 文件ID列表

    Returns:
        查重结果
    """
    try:
        from ..services.ai.workflows import _safe_asyncio_run
        from ..services.duplicate import run_duplicate_check as _run_check
        # BUG-FIX 2026-09-18（中）：`file_ids` 此前被静默忽略 —— 服务层
        # `run_duplicate_check(task_id)` 只从任务行取文件列表，不支持按 id 过滤。
        # 调用方传了 file_ids 会以为「只查这几个文件」，实际查的是任务关联的全部文件。
        # 这里不再假装支持：显式告警并在返回值里标注 ignored，避免静默误判。
        if file_ids:
            logger.warning(
                '标书查重不支持按 file_ids 过滤（服务层仅按 task_id 取文件），'
                '本次将忽略该参数: task_id=%d, file_ids=%s', task_id, file_ids,
            )
        logger.info('开始异步标书查重: task_id=%d', task_id)
        _safe_asyncio_run(_run_check(task_id))
        logger.info('标书查重完成: task_id=%d', task_id)
        return {
            'status': 'completed',
            'task_id': task_id,
            **({'ignored_params': {'file_ids': list(file_ids)}} if file_ids else {}),
        }
    except Exception as exc:
        logger.error('标书查重失败: %s', exc, exc_info=True)
        return {'status': 'failed', 'task_id': task_id, 'error': str(exc)}
