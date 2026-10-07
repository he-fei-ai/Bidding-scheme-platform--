"""多语言支持路由

提供语言切换、翻译查询、AI Prompt多语言化等功能。
"""
import logging
from fastapi import APIRouter, HTTPException, Query
from ..services.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, i18n_service
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/i18n', tags=['i18n'])

@router.get('/languages')
async def get_languages():
    """获取支持的语言列表"""
    return {'status': 'ok', 'default_language': DEFAULT_LANGUAGE, 'languages': SUPPORTED_LANGUAGES}

@router.get('/translate')
async def translate_text(key: str=Query(..., description='翻译键'), language: str=Query(DEFAULT_LANGUAGE, description='目标语言代码')):
    """翻译文本

    Args:
        key: 翻译键（如 'common.title'）
        language: 目标语言（如 'en-US', 'zh-CN', 'ar-SA'）

    Returns:
        翻译后的文本
    """
    try:
        text = i18n_service.translate(key, language)
        return {'status': 'ok', 'key': key, 'language': language, 'text': text}
    except Exception as e:
        logger.error('翻译失败: %s, 错误: %s', key, e)
        raise HTTPException(status_code=500, detail=f'翻译失败: {e!s}') from e

@router.post('/set-language')
async def set_user_language(language: str=Query(..., description='用户选择的语言代码')):
    """设置用户偏好语言

    Args:
        language: 语言代码（如 'en-US'）

    Returns:
        设置结果
    """
    try:
        i18n_service.set_language(language)
        return {'status': 'ok', 'message': f'语言已设置为: {language}', 'current_language': language}
    except Exception as e:
        logger.error('设置语言失败: %s, 错误: %s', language, e)
        raise HTTPException(status_code=400, detail=f'设置语言失败: {e!s}') from e

@router.get('/prompt-templates')
async def get_prompt_templates(language: str=Query(DEFAULT_LANGUAGE)):
    """获取多语言AI Prompt模板

    Args:
        language: 语言代码

    Returns:
        Prompt模板字典
    """
    prompt_templates = get_multilingual_prompts(language)
    return {'status': 'ok', 'language': language, 'prompts': prompt_templates}

def get_multilingual_prompts(language: str) -> dict:
    """获取指定语言的AI Prompt模板

    Args:
        language: 语言代码

    Returns:
        Prompt模板字典
    """
    prompts = {'zh-CN': {'section_generation': '你是一位专业的招投标方案编写专家。请根据以下招标要求和目录结构，生成专业的章节内容。\n\n要求：\n1. 内容专业、详实\n2. 符合招标文件要求\n3. 逻辑清晰、层次分明\n4. 字数不少于{min_words}字\n\n章节标题：{section_title}\n\n请开始生成：', 'quality_review': '你是一位资深的标书评审专家。请对以下章节内容进行质量评估：\n\n评估维度：\n1. 完整性（是否覆盖所有要点）\n2. 专业性（术语使用是否准确）\n3. 逻辑性（结构是否清晰）\n4. 合规性（是否符合招标要求）\n\n内容：{content}\n\n请给出评分（0-100）和改进建议。'}, 'en-US': {'section_generation': 'You are a professional bidding proposal writer. Please generate professional chapter content based on the following tender requirements and outline structure.\n\nRequirements:\n1. Professional and detailed content\n2. Comply with tender document requirements\n3. Clear logic and well-structured\n4. Minimum word count: {min_words} words\n\nSection title: {section_title}\n\nPlease start generating:', 'quality_review': 'You are an experienced bid evaluation expert. Please assess the quality of the following chapter content:\n\nEvaluation dimensions:\n1. Completeness (covers all key points)\n2. Professionalism (accurate terminology)\n3. Logic (clear structure)\n4. Compliance (meets tender requirements)\n\nContent: {content}\n\nPlease provide a score (0-100) and improvement suggestions.'}, 'ar-SA': {'section_generation': 'أنت كاتب مقترحات عروض محترف. يرجى إنشاء محتوى فصل احترافي بناءً على متطلبات المناقصة وهيكل الفهرس التالي.\n\nالمتطلبات:\n1. محتوى احترافي ومفصل\n2. الامتثال لوثائق المناقصة\n3. منطق واضح وهيكل جيد\n4. الحد الأدنى لعدد الكلمات: {min_words} كلمة\n\nعنوان القسم: {section_title}\n\nيرجى البدء في التوليد:', 'quality_review': 'أنت خبير تقييم عروض ذو خبرة. يرجى تقييم جودة محتوى الفصل التالي:\n\nأبعاد التقييم:\n1. الاكتمال (يغطي جميع النقاط الرئيسية)\n2. الاحترافية (المصطلحات الدقيقة)\n3. المنطق (هيكل واضح)\n4. الامتثال (يلبي متطلبات المناقصة)\n\nالمحتوى: {content}\n\nيرجى تقديم درجة (0-100) واقتراحات التحسين.'}}
    return prompts.get(language, prompts.get(DEFAULT_LANGUAGE, {}))
MULTILINGUAL_AI_CONFIGS = {'zh-CN': {'system_prompt': '你是一个专业的招投标方案编写助手。你的任务是帮助用户编写高质量的投标方案。', 'max_retries': 3, 'timeout_seconds': 120}, 'en-US': {'system_prompt': 'You are a professional bidding proposal writing assistant. Your task is to help users write high-quality bidding proposals.', 'max_retries': 3, 'timeout_seconds': 120}, 'ar-SA': {'system_prompt': 'أنت مساعد متخصص في كتابة مقترحات العروض. مهمتك هي مساعدة المستخدمين في كتابة مقترحات عروض عالية الجودة.', 'max_retries': 3, 'timeout_seconds': 120}}

@router.post('/terms')
async def set_terms(language: str=Query(..., description='目标语言代码')):
    """设置语言翻译术语（预留接口）"""
    try:
        return {'status': 'ok', 'message': f'术语已设置，语言: {language}', 'language': language}
    except Exception as e:
        logger.error('设置术语失败: %s, 错误: %s', language, e)
        raise HTTPException(status_code=400, detail=f'设置术语失败: {e!s}') from e

def get_ai_config_for_language(language: str) -> dict:
    """获取指定语言的AI配置

    Args:
        language: 语言代码

    Returns:
        AI配置字典
    """
    return MULTILINGUAL_AI_CONFIGS.get(language, MULTILINGUAL_AI_CONFIGS[DEFAULT_LANGUAGE])
