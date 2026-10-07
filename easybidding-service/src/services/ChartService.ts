/**
 * 图表服务 - 集成验证与缓存功能
 * 
 * @version v3.1
 */

import { join, resolve } from "path";
import { Buffer } from "buffer";
import { ChartCache } from "./ChartCache";
import type { ValidationResult } from "./chartValidators";
import {
  validateArchitectureTree,
  detectArchitectureSubtype,
  triggerMatch,
  detectChartTypeFromText,
} from "./chartValidators";

export interface ChartRenderOptions {
  fmt?: "png" | "svg";
  cacheDir?: string;
  autoValidate?: boolean;
}

export class ChartService {
  private cache: ChartCache;
  private autoValidate: boolean;

  constructor(options: ChartRenderOptions = {}) {
    this.cache = new ChartCache(resolve(join(process.cwd(), options.cacheDir || "./cache/charts")));
    this.autoValidate = options.autoValidate ?? true;
  }

  /**
   * 验证架构图树
   */
  async validateArchitecture(data: Record<string, any>, subtype: string | null = null): Promise<ValidationResult> {
    const result = validateArchitectureTree(data, subtype);

    // 自动验证
    if (this.autoValidate && !result.valid) {
      console.warn("架构图验证失败:", result.errors);
    }

    return result;
  }

  /**
   * 检测架构子类型（基于数据内容和章节标题）
   */
  detectSubtype(data: Record<string, any>, sectionTitle: string | null = null): string | null {
    return detectArchitectureSubtype(data, sectionTitle);
  }

  /**
   * 渲染图表并缓存结果（伪接口，实际需集成渲染引擎）
   * @param mermaidCode Mermaid 代码或 JSON 数据
   * @param chartType 图表类型
   * @param options 渲染选项
   * @returns 图片二进制数据或 null
   */
  async renderAndCache(mermaidCode: string, chartType: string, options?: ChartRenderOptions): Promise<Uint8Array | null> {
    try {
      // 尝试从缓存获取
      const cached = this.cache.get(mermaidCode, options?.fmt || "png");
      if (cached) {
        console.log("[ChartService] 缓存命中:", cached);
        const fs = await import("fs");
        return new Uint8Array(fs.readFileSync(cached));
      }

      // 实际渲染调用（此处为占位符，需接入真实渲染引擎）
      const imageBytes = await this._renderMermaid(mermaidCode, chartType);

      if (imageBytes) {
        // Convert Uint8Array to Buffer for cache set
        const buffer = Buffer.from(imageBytes);
        // 缓存结果
        this.cache.set(mermaidCode, buffer, options?.fmt || "png");
        console.log("[ChartService] 已缓存渲染结果");
      }

      return imageBytes;
    } catch (error) {
      console.error("[ChartService] 渲染失败:", error);
      return null;
    }
  }

  /**
   * 内部渲染方法（需接入实际渲染引擎如 mermaid-cli 或 PIL）
   */
  private async _renderMermaid(code: string, type: string): Promise<Uint8Array | null> {
    // TODO: 接入实际渲染逻辑
    // 当前返回模拟数据
    const mockData = new TextEncoder().encode(`[Mock SVG/PNG data for ${type}]`);
    return mockData;
  }

  /**
   * 获取缓存统计信息
   */
  getCacheStats(): { hits: number; misses: number; hitRate: string; fileCount: number; totalSizeMB: number } {
    const stats = this.cache.getStats();
    return {
      hits: stats.hits,
      misses: stats.misses,
      hitRate: stats.hitRate,
      fileCount: stats.fileCount,
      totalSizeMB: Number(stats.totalSizeMB),
    };
  }

  /**
   * 清理过期缓存
   */
  cleanupOldCache(days: number = 7): void {
    this.cache.clear(days);
  }

  /**
   * 设置自定义缓存目录
   */
  setCacheDir(dir: string): void {
    this.cache = new ChartCache(dir);
  }
}

export default ChartService;