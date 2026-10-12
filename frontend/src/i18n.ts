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
    // ---- 上传页（FileUploadPage）文案 ----
    // R-5 2026-10-12：上传页此前全部硬编码中文，i18n.ts 只有 common.upload /
    // file.upload / file.list 三个 key 且无调用方。此处补齐该页文案 key。
    'upload.pageTitle': '招标文件上传',
    'upload.pageDesc': '支持 Word（含 .wps）、PDF、Excel 清单（.xlsx/.xls）、PPT、Markdown、HTML、图片扫描件（png/jpg 等，需 OCR）等格式，系统将自动解析正文内容，用于后续要点提取与方案生成。清单类文件请命名为含"清单/工程量"的文件名，便于工程量自动分析。',
    'upload.clearTexts': '清空提取',
    'upload.confirmClearTitle': '确认清空已提取文本？',
    'upload.confirmClearDesc': '将清空所有文件的提取文本并重置为「待提取」状态（不会删除已上传的原始文件）。解析失败记录也会一起清理。',
    'upload.okClear': '确认清空',
    'upload.nextExtract': '下一步：要点提取',
    'upload.dragTitle': '点击选择文件，或拖拽文件到此处',
    'upload.dragHint': '支持 Word / PDF / Excel 清单 / PPT / Markdown / HTML 等格式，可同时上传多个文件',
    'upload.uploadedFiles': '已上传文件',
    'upload.refresh': '刷新',
    'upload.empty': '暂无文件，请先上传招标文件',
    'upload.extractedReady': '已有 {count} 个文件完成文本提取，可以进入下一步',
    'upload.previewTooltip': '预览提取文本',
    'upload.previewText': '预览文本',
    'upload.setSupporting': '设为辅助',
    'upload.setMain': '设为主文件',
    'upload.asyncParse': '后台解析',
    'upload.reExtract': '重新提取',
    'upload.delete': '删除',
    'upload.mainFile': '主文件',
    'upload.supportingFile': '辅助文件',
    'upload.docType': '{type} 文档',
    'upload.afterUploadHint': '上传后系统会自动提取文本内容。若提取失败可点击"重新提取"，或删除后重新上传。',
    'upload.previewModalTitle': '提取文本预览',
    'upload.previewTotal': '文本共 {total} 字',
    'upload.previewTruncated': '文本共 {total} 字，已截断显示前 {shown} 字',
    'upload.noPreview': '该文件没有可预览的提取文本。',
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
    'upload.pageTitle': 'Upload Tender Documents',
    'upload.pageDesc': 'Supports Word (incl. .wps), PDF, Excel schedules (.xlsx/.xls), PPT, Markdown, HTML and scanned images (png/jpg, OCR required). The system extracts the body text automatically for downstream fact extraction and proposal generation. Name schedule files with "schedule/quantities" so quantities can be analysed automatically.',
    'upload.clearTexts': 'Clear Extracted',
    'upload.confirmClearTitle': 'Clear all extracted text?',
    'upload.confirmClearDesc': 'All extracted text will be cleared and files reset to "pending" (uploaded originals are NOT deleted). Failed parse records are cleared as well.',
    'upload.okClear': 'Confirm Clear',
    'upload.nextExtract': 'Next: Fact Extraction',
    'upload.dragTitle': 'Click to select files, or drag them here',
    'upload.dragHint': 'Supports Word / PDF / Excel / PPT / Markdown / HTML, multiple files at once',
    'upload.uploadedFiles': 'Uploaded Files',
    'upload.refresh': 'Refresh',
    'upload.empty': 'No files yet — please upload the tender documents first',
    'upload.extractedReady': '{count} file(s) have completed text extraction; you may proceed',
    'upload.previewTooltip': 'Preview extracted text',
    'upload.previewText': 'Preview',
    'upload.setSupporting': 'Set as supporting',
    'upload.setMain': 'Set as main file',
    'upload.asyncParse': 'Parse in background',
    'upload.reExtract': 'Re-extract',
    'upload.delete': 'Delete',
    'upload.mainFile': 'Main',
    'upload.supportingFile': 'Supporting',
    'upload.docType': '{type} document',
    'upload.afterUploadHint': 'Text is extracted automatically after upload. If extraction fails, click "Re-extract" or delete and upload again.',
    'upload.previewModalTitle': 'Extracted Text Preview',
    'upload.previewTotal': '{total} characters in total',
    'upload.previewTruncated': '{total} characters in total, showing the first {shown}',
    'upload.noPreview': 'No extracted text available for preview.',
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
    'upload.pageTitle': 'رفع مستندات المناقصة',
    'upload.pageDesc': 'يدعم Word (بما في ذلك .wps) و PDF وجداول Excel (.xlsx/.xls) و PPT و Markdown و HTML والصور الممسوحة ضوئيًا (png/jpg، تتطلب OCR). يستخرج النظام النص تلقائيًا لاستخدامه لاحقًا في استخراج الحقائق وتوليد العرض. سمِّ ملفات الجداول بما يحتوي على "جدول/كميات" لتحليل الكميات تلقائيًا.',
    'upload.clearTexts': 'مسح النص المستخرج',
    'upload.confirmClearTitle': 'مسح كل النص المستخرج؟',
    'upload.confirmClearDesc': 'سيتم مسح النص المستخرج من جميع الملفات وإعادتها إلى حالة «قيد الاستخراج» (لن تُحذف الملفات الأصلية المرفوعة). كما سيتم مسح سجلات الفشل.',
    'upload.okClear': 'تأكيد المسح',
    'upload.nextExtract': 'التالي: استخراج الحقائق',
    'upload.dragTitle': 'انقر لاختيار الملفات، أو اسحبها إلى هنا',
    'upload.dragHint': 'يدعم Word / PDF / Excel / PPT / Markdown / HTML، ويمكن رفع عدة ملفات معًا',
    'upload.uploadedFiles': 'الملفات المرفوعة',
    'upload.refresh': 'تحديث',
    'upload.empty': 'لا توجد ملفات — يرجى رفع مستندات المناقصة أولًا',
    'upload.extractedReady': 'أكمل {count} ملفًا استخراج النص؛ يمكنك المتابعة',
    'upload.previewTooltip': 'معاينة النص المستخرج',
    'upload.previewText': 'معاينة',
    'upload.setSupporting': 'تعيين كملف مساند',
    'upload.setMain': 'تعيين كملف رئيسي',
    'upload.asyncParse': 'تحليل في الخلفية',
    'upload.reExtract': 'إعادة الاستخراج',
    'upload.delete': 'حذف',
    'upload.mainFile': 'رئيسي',
    'upload.supportingFile': 'مساند',
    'upload.docType': 'مستند {type}',
    'upload.afterUploadHint': 'يُستخرج النص تلقائيًا بعد الرفع. إذا فشل الاستخراج، انقر «إعادة الاستخراج» أو احذف الملف وأعد رفعه.',
    'upload.previewModalTitle': 'معاينة النص المستخرج',
    'upload.previewTotal': 'إجمالي {total} حرفًا',
    'upload.previewTruncated': 'إجمالي {total} حرفًا، يعرض أول {shown}',
    'upload.noPreview': 'لا يوجد نص مستخرج للمعاينة.',
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
    'upload.pageTitle': '入札文書のアップロード',
    'upload.pageDesc': 'Word（.wps を含む）、PDF、Excel 明細（.xlsx/.xls）、PPT、Markdown、HTML、スキャン画像（png/jpg 等、OCR 必要）に対応しています。本文は自動で抽出され、後の要点抽出と提案生成に利用されます。明細ファイルは「明細/数量」を含む名前にすると数量分析が自動化されます。',
    'upload.clearTexts': '抽出をクリア',
    'upload.confirmClearTitle': '抽出済みテキストをすべてクリアしますか？',
    'upload.confirmClearDesc': 'すべてのファイルの抽出テキストをクリアし、「抽出待ち」に戻します（アップロード済みの元ファイルは削除されません）。失敗レコードも併せて削除されます。',
    'upload.okClear': 'クリアを確定',
    'upload.nextExtract': '次へ：要点抽出',
    'upload.dragTitle': 'クリックしてファイルを選択、またはここにドラッグ',
    'upload.dragHint': 'Word / PDF / Excel / PPT / Markdown / HTML に対応、複数ファイル同時アップロード可',
    'upload.uploadedFiles': 'アップロード済みファイル',
    'upload.refresh': '更新',
    'upload.empty': 'ファイルがありません — まず入札文書をアップロードしてください',
    'upload.extractedReady': '{count} 件のファイルのテキスト抽出が完了しました。次へ進めます',
    'upload.previewTooltip': '抽出テキストをプレビュー',
    'upload.previewText': 'プレビュー',
    'upload.setSupporting': '補助に設定',
    'upload.setMain': '主ファイルに設定',
    'upload.asyncParse': 'バックグラウンド解析',
    'upload.reExtract': '再抽出',
    'upload.delete': '削除',
    'upload.mainFile': '主ファイル',
    'upload.supportingFile': '補助ファイル',
    'upload.docType': '{type} ドキュメント',
    'upload.afterUploadHint': 'アップロード後に本文が自動抽出されます。失敗した場合は「再抽出」をクリックするか、削除して再アップロードしてください。',
    'upload.previewModalTitle': '抽出テキストのプレビュー',
    'upload.previewTotal': '全 {total} 文字',
    'upload.previewTruncated': '全 {total} 文字、先頭 {shown} 文字を表示',
    'upload.noPreview': 'プレビューできる抽出テキストがありません。',
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
    'upload.pageTitle': '입찰 문서 업로드',
    'upload.pageDesc': 'Word(.wps 포함), PDF, Excel 명세(.xlsx/.xls), PPT, Markdown, HTML, 스캔 이미지(png/jpg 등, OCR 필요)를 지원합니다. 본문은 자동으로 추출되어 이후 사실 추출과 제안서 생성에 사용됩니다. 명세 파일은 "명세/물량"이 포함된 이름으로 지정하면 물량 분석이 자동화됩니다.',
    'upload.clearTexts': '추출 초기화',
    'upload.confirmClearTitle': '추출된 텍스트를 모두 초기화할까요?',
    'upload.confirmClearDesc': '모든 파일의 추출 텍스트가 초기화되고 "추출 대기" 상태로 돌아갑니다(업로드된 원본 파일은 삭제되지 않습니다). 실패 기록도 함께 정리됩니다.',
    'upload.okClear': '초기화 확인',
    'upload.nextExtract': '다음: 사실 추출',
    'upload.dragTitle': '클릭하여 파일 선택 또는 여기로 드래그',
    'upload.dragHint': 'Word / PDF / Excel / PPT / Markdown / HTML 지원, 여러 파일 동시 업로드 가능',
    'upload.uploadedFiles': '업로드된 파일',
    'upload.refresh': '새로고침',
    'upload.empty': '파일이 없습니다 — 먼저 입찰 문서를 업로드하세요',
    'upload.extractedReady': '{count}개 파일의 텍스트 추출이 완료되었습니다. 다음으로 진행할 수 있습니다',
    'upload.previewTooltip': '추출 텍스트 미리보기',
    'upload.previewText': '미리보기',
    'upload.setSupporting': '보조로 설정',
    'upload.setMain': '주 파일로 설정',
    'upload.asyncParse': '백그라운드 파싱',
    'upload.reExtract': '다시 추출',
    'upload.delete': '삭제',
    'upload.mainFile': '주 파일',
    'upload.supportingFile': '보조 파일',
    'upload.docType': '{type} 문서',
    'upload.afterUploadHint': '업로드 후 본문이 자동으로 추출됩니다. 실패 시 "다시 추출"을 클릭하거나 삭제 후 다시 업로드하세요.',
    'upload.previewModalTitle': '추출 텍스트 미리보기',
    'upload.previewTotal': '총 {total}자',
    'upload.previewTruncated': '총 {total}자, 앞 {shown}자만 표시',
    'upload.noPreview': '미리볼 수 있는 추출 텍스트가 없습니다.',
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
