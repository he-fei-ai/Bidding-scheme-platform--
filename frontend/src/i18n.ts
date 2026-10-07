/**
 * 前端多语言支持
 */

export type LanguageCode = 'zh-CN' | 'en-US' | 'ar-SA' | 'ja-JP' | 'ko-KR';

export interface LanguageInfo {
  code: LanguageCode;
  name: string;
  nativeName: string;
  direction: 'ltr' | 'rtl';
}

// 支持的语言列表
export const SUPPORTED_LANGUAGES: LanguageInfo[] = [
  { code: 'zh-CN', name: '简体中文', nativeName: '中文', direction: 'ltr' },
  { code: 'en-US', name: 'English', nativeName: 'English', direction: 'ltr' },
  { code: 'ar-SA', name: 'Arabic', nativeName: 'العربية', direction: 'rtl' },
  { code: 'ja-JP', name: 'Japanese', nativeName: '日本語', direction: 'ltr' },
  { code: 'ko-KR', name: 'Korean', nativeName: '한국어', direction: 'ltr' },
];

export const DEFAULT_LANGUAGE: LanguageCode = 'zh-CN';

// 翻译字典
const translations: Record<LanguageCode, Record<string, string>> = {
  'zh-CN': {
    'app.title': '招投标方案编写平台',
    'common.loading': '加载中...',
    'common.save': '保存',
    'common.cancel': '取消',
    'common.confirm': '确认',
    'common.delete': '删除',
    'common.edit': '编辑',
    'common.view': '查看',
    'common.export': '导出',
    'common.upload': '上传',
    'common.search': '搜索',
    'common.next': '下一步',
    'common.previous': '上一步',
    'common.submit': '提交',
    'common.retry': '重试',
    'common.back': '返回',
    'common.close': '关闭',
    'common.error': '错误',
    'common.success': '成功',
    'common.warning': '警告',
    'project.create': '创建项目',
    'project.list': '项目列表',
    'project.name': '项目名称',
    'file.upload': '上传文件',
    'file.list': '文件列表',
    'section.generate': '生成内容',
    'section.pause': '暂停',
    'section.resume': '恢复',
    'export.docx': '导出为Word',
    'export.pdf': '导出为PDF',
  },
  'en-US': {
    'app.title': 'Bidding Proposal Platform',
    'common.loading': 'Loading...',
    'common.save': 'Save',
    'common.cancel': 'Cancel',
    'common.confirm': 'Confirm',
    'common.delete': 'Delete',
    'common.edit': 'Edit',
    'common.view': 'View',
    'common.export': 'Export',
    'common.upload': 'Upload',
    'common.search': 'Search',
    'common.next': 'Next',
    'common.previous': 'Previous',
    'common.submit': 'Submit',
    'common.retry': 'Retry',
    'common.back': 'Back',
    'common.close': 'Close',
    'common.error': 'Error',
    'common.success': 'Success',
    'common.warning': 'Warning',
    'project.create': 'Create Project',
    'project.list': 'Project List',
    'project.name': 'Project Name',
    'file.upload': 'Upload File',
    'file.list': 'File List',
    'section.generate': 'Generate Content',
    'section.pause': 'Pause',
    'section.resume': 'Resume',
    'export.docx': 'Export as Word',
    'export.pdf': 'Export as PDF',
  },
  'ar-SA': {
    'app.title': 'منصة عرض المناقصة',
    'common.loading': 'جاري التحميل...',
    'common.save': 'حفظ',
    'common.cancel': 'إلغاء',
    'common.confirm': 'تأكيد',
    'common.delete': 'حذف',
    'common.edit': 'تعديل',
    'common.view': 'عرض',
    'common.export': 'تصدير',
    'common.upload': 'رفع',
    'common.search': 'بحث',
    'common.next': 'التالي',
    'common.previous': 'السابق',
    'common.submit': 'إرسال',
    'common.retry': 'إعادة المحاولة',
    'common.back': 'رجوع',
    'common.close': 'إغلاق',
    'common.error': 'خطأ',
    'common.success': 'نجح',
    'common.warning': 'تحذير',
    'project.create': 'إنشاء مشروع',
    'project.list': 'قائمة المشاريع',
    'project.name': 'اسم المشروع',
    'file.upload': 'رفع ملف',
    'file.list': 'قائمة الملفات',
    'section.generate': 'توليد المحتوى',
    'section.pause': 'إيقاف مؤقت',
    'section.resume': 'استئناف',
    'export.docx': 'تصدير كـ Word',
    'export.pdf': 'تصدير كـ PDF',
  },
  'ja-JP': {
    'app.title': '入札提案プラットフォーム',
    'common.loading': '読み込み中...',
    'common.save': '保存',
    'common.cancel': 'キャンセル',
    'common.confirm': '確認',
    'common.delete': '削除',
    'common.edit': '編集',
    'common.view': '表示',
    'common.export': 'エクスポート',
    'common.upload': 'アップロード',
    'common.search': '検索',
    'common.next': '次へ',
    'common.previous': '前へ',
    'common.submit': '送信',
    'common.retry': '再試行',
    'common.back': '戻る',
    'common.close': '閉じる',
    'common.error': 'エラー',
    'common.success': '成功',
    'common.warning': '警告',
    'project.create': 'プロジェクト作成',
    'project.list': 'プロジェクト一覧',
    'project.name': 'プロジェクト名',
    'file.upload': 'ファイルをアップロード',
    'file.list': 'ファイル一覧',
    'section.generate': 'コンテンツを生成',
    'section.pause': '一時停止',
    'section.resume': '再開',
    'export.docx': 'Wordとしてエクスポート',
    'export.pdf': 'PDFとしてエクスポート',
  },
  'ko-KR': {
    'app.title': '입찰 제안 플랫폼',
    'common.loading': '로딩 중...',
    'common.save': '저장',
    'common.cancel': '취소',
    'common.confirm': '확인',
    'common.delete': '삭제',
    'common.edit': '편집',
    'common.view': '보기',
    'common.export': '내보내기',
    'common.upload': '업로드',
    'common.search': '검색',
    'common.next': '다음',
    'common.previous': '이전',
    'common.submit': '제출',
    'common.retry': '재시도',
    'common.back': '뒤로',
    'common.close': '닫기',
    'common.error': '오류',
    'common.success': '성공',
    'common.warning': '경고',
    'project.create': '프로젝트 생성',
    'project.list': '프로젝트 목록',
    'project.name': '프로젝트 이름',
    'file.upload': '파일 업로드',
    'file.list': '파일 목록',
    'section.generate': '콘텐츠 생성',
    'section.pause': '일시정지',
    'section.resume': '재개',
    'export.docx': 'Word로 내보내기',
    'export.pdf': 'PDF로 내보내기',
  },
};

// 当前语言
let currentLanguage: LanguageCode = (localStorage.getItem('language') as LanguageCode) || DEFAULT_LANGUAGE;

/**
 * 获取当前语言
 */
export function getCurrentLanguage(): LanguageCode {
  return currentLanguage;
}

/**
 * 设置当前语言
 */
export function setCurrentLanguage(language: LanguageCode): void {
  currentLanguage = language;
  localStorage.setItem('language', language);
  // 更新HTML的lang属性
  document.documentElement.lang = language;
  // 更新方向
  const langInfo = SUPPORTED_LANGUAGES.find(l => l.code === language);
  if (langInfo) {
    document.documentElement.dir = langInfo.direction;
  }
}

/**
 * 翻译文本
 */
export function t(key: string, params?: Record<string, string>): string {
  let text = translations[currentLanguage]?.[key] || translations['zh-CN']?.[key] || key;
  
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      text = text.replace(`{${k}}`, v);
    }
  }
  
  return text;
}

/**
 * 获取语言信息
 */
export function getLanguageInfo(code: LanguageCode): LanguageInfo | undefined {
  return SUPPORTED_LANGUAGES.find(l => l.code === code);
}

/**
 * 获取所有支持的语言
 */
export function getAllLanguages(): LanguageInfo[] {
  return SUPPORTED_LANGUAGES;
}
