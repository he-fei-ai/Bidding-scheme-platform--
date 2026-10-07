/**
 * Vitest 全局 Setup —— jsdom 环境 polyfill
 *
 * 解决问题：antd Table 组件依赖 `window.getComputedStyle(elt, pseudoElt)`
 * 来测量滚动条宽度，但 jsdom 不支持该签名，会抛出
 * "Not implemented: window.getComputedStyle(elt, pseudoElt)" 错误，
 * 导致 rc-table 组件渲染极慢（首跑 ~140s，远超 120s 超时）。
 *
 * 方案：全局 polyfill getComputedStyle，当传入第二个参数 pseudoElt 时
 * 返回空样式对象（滚动条宽度测量在 jsdom 里本就无意义，直接返回 0 即可）。
 */

// 仅在 jsdom 环境下生效（node 环境无 window/document）
if (typeof window !== "undefined" && typeof document !== "undefined") {
  const originalGetComputedStyle = window.getComputedStyle.bind(window);

  window.getComputedStyle = (
    elt: Element,
    pseudoElt?: string | null,
  ): CSSStyleDeclaration => {
    // jsdom 不支持 pseudoElt 参数，直接返回空样式（滚动条宽度=0）
    if (pseudoElt) {
      return {} as CSSStyleDeclaration;
    }
    return originalGetComputedStyle(elt);
  };

  // polyfill matchMedia（antd 响应式组件需要）
  if (!window.matchMedia) {
    window.matchMedia = (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    });
  }
}
