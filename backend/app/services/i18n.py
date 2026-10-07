"""多语言支持（i18n）服务

提供中、英、阿拉伯文等多语言支持。
包含翻译管理、语言检测、Prompt多语言化等功能。
"""

import json
import logging
from pathlib import Path


logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = [
    {"code": "zh-CN", "name": "简体中文", "native_name": "中文", "direction": "ltr"},
    {"code": "en-US", "name": "English", "native_name": "English", "direction": "ltr"},
    {"code": "ar-SA", "name": "Arabic", "native_name": "العربية", "direction": "rtl"},
    {"code": "ja-JP", "name": "Japanese", "native_name": "日本語", "direction": "ltr"},
    {"code": "ko-KR", "name": "Korean", "native_name": "한국어", "direction": "ltr"},
]

DEFAULT_LANGUAGE = "zh-CN"


class I18nService:
    def __init__(self):
        self.translations = {}
        self.current_language = DEFAULT_LANGUAGE

    def load_translations(self, language):
        translation_file = TRANSLATIONS.get(language)
        if not translation_file:
            logger.warning("不支持的语言: %s，使用默认语言", language)
            translation_file = TRANSLATIONS.get(DEFAULT_LANGUAGE)
        try:
            with open(translation_file, encoding="utf-8") as f:
                translations = json.load(f)
            self.translations[language] = translations
            return translations
        except Exception as e:
            logger.error("加载翻译文件失败: %s, 错误: %s", translation_file, e)
            return {}

    def translate(self, key, language=None, **kwargs):
        lang = language or self.current_language
        if lang not in self.translations:
            self.load_translations(lang)
        translations = self.translations.get(lang, {})
        value = self._get_nested_value(translations, key)
        if value is None:
            if lang != DEFAULT_LANGUAGE:
                return self.translate(key, DEFAULT_LANGUAGE, **kwargs)
            return key
        if kwargs:
            for k, v in kwargs.items():
                value = value.replace("{" + k + "}", str(v))
        return value

    @staticmethod
    def _get_nested_value(d, key):
        keys = key.split(".")
        current = d
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return None
        return current if isinstance(current, str) else None

    def get_supported_languages(self):
        return SUPPORTED_LANGUAGES

    def set_language(self, language):
        if language in TRANSLATIONS:
            self.current_language = language
            if language not in self.translations:
                self.load_translations(language)
        else:
            logger.warning("不支持的语言: %s", language)


i18n_service = I18nService()

TRANSLATIONS_DIR = Path(__file__).parent.parent.parent.parent / "translations"
# BUG-FIX 2026-09-29：补 parents=True——原 mkdir(exist_ok=True) 在父目录不存在的
# 环境（测试临时目录/全新部署）会抛 FileNotFoundError 导致模块导入即失败。
TRANSLATIONS_DIR.mkdir(parents=True, exist_ok=True)

# 默认翻译数据（内嵌）
DEFAULT_TRANSLATIONS = {
    "zh-CN": {
        "common": {
            "title": "招投标方案编写平台",
            "loading": "加载中...",
            "save": "保存",
            "cancel": "取消",
            "confirm": "确认",
            "delete": "删除",
            "edit": "编辑",
            "view": "查看",
            "export": "导出",
            "import": "导入",
            "upload": "上传",
            "download": "下载",
            "search": "搜索",
            "filter": "筛选",
            "next": "下一步",
            "previous": "上一步",
            "submit": "提交",
            "retry": "重试",
            "back": "返回",
            "forward": "前进",
            "close": "关闭",
            "open": "打开",
            "yes": "是",
            "no": "否",
            "ok": "确定",
            "error": "错误",
            "success": "成功",
            "warning": "警告",
            "info": "信息",
        },
        "project": {
            "create": "创建项目",
            "list": "项目列表",
            "detail": "项目详情",
            "name": "项目名称",
            "description": "项目描述",
            "status": "项目状态",
            "created_at": "创建时间",
            "updated_at": "更新时间",
        },
        "file": {
            "upload": "上传文件",
            "list": "文件列表",
            "name": "文件名",
            "type": "文件类型",
            "size": "文件大小",
            "role": "文件角色",
            "main": "主文件",
            "supporting": "辅助文件",
        },
        "section": {
            "generate": "生成内容",
            "pause": "暂停",
            "resume": "恢复",
            "title": "章节标题",
            "content": "章节内容",
            "status": "状态",
            "pending": "待生成",
            "generating": "生成中",
            "completed": "已完成",
            "paused": "已暂停",
            "failed": "生成失败",
        },
        "export": {
            "docx": "导出为Word",
            "pdf": "导出为PDF",
            "markdown": "导出为Markdown",
            "config": "导出配置",
        },
        "ai": {
            "config": "AI配置",
            "provider": "AI提供商",
            "model": "模型",
            "api_key": "API密钥",
            "temperature": "温度",
            "max_tokens": "最大令牌数",
        },
    },
    "en-US": {
        "common": {
            "title": "Bidding Proposal Platform",
            "loading": "Loading...",
            "save": "Save",
            "cancel": "Cancel",
            "confirm": "Confirm",
            "delete": "Delete",
            "edit": "Edit",
            "view": "View",
            "export": "Export",
            "import": "Import",
            "upload": "Upload",
            "download": "Download",
            "search": "Search",
            "filter": "Filter",
            "next": "Next",
            "previous": "Previous",
            "submit": "Submit",
            "retry": "Retry",
            "back": "Back",
            "forward": "Forward",
            "close": "Close",
            "open": "Open",
            "yes": "Yes",
            "no": "No",
            "ok": "OK",
            "error": "Error",
            "success": "Success",
            "warning": "Warning",
            "info": "Info",
        },
        "project": {
            "create": "Create Project",
            "list": "Project List",
            "detail": "Project Details",
            "name": "Project Name",
            "description": "Description",
            "status": "Status",
            "created_at": "Created At",
            "updated_at": "Updated At",
        },
        "file": {
            "upload": "Upload File",
            "list": "File List",
            "name": "File Name",
            "type": "File Type",
            "size": "File Size",
            "role": "File Role",
            "main": "Main File",
            "supporting": "Supporting File",
        },
        "section": {
            "generate": "Generate Content",
            "pause": "Pause",
            "resume": "Resume",
            "title": "Section Title",
            "content": "Content",
            "status": "Status",
            "pending": "Pending",
            "generating": "Generating",
            "completed": "Completed",
            "paused": "Paused",
            "failed": "Failed",
        },
        "export": {
            "docx": "Export as Word",
            "pdf": "Export as PDF",
            "markdown": "Export as Markdown",
            "config": "Export Config",
        },
        "ai": {
            "config": "AI Configuration",
            "provider": "AI Provider",
            "model": "Model",
            "api_key": "API Key",
            "temperature": "Temperature",
            "max_tokens": "Max Tokens",
        },
    },
}

# 初始化翻译文件
for lang_code, data in DEFAULT_TRANSLATIONS.items():
    lang_file = TRANSLATIONS_DIR / f"{lang_code}.json"
    if not lang_file.exists():
        lang_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))

# 构建TRANSLATIONS字典
TRANSLATIONS = {}
for lang_code in DEFAULT_TRANSLATIONS:
    lang_file = TRANSLATIONS_DIR / f"{lang_code}.json"
    if lang_file.exists():
        TRANSLATIONS[lang_code] = str(lang_file)

# 添加其他语言（阿拉伯文、日文、韩文）的翻译文件路径
for extra_lang in ["ar-SA", "ja-JP", "ko-KR"]:
    lang_file = TRANSLATIONS_DIR / f"{extra_lang}.json"
    TRANSLATIONS[extra_lang] = str(lang_file)
