/**
 * 图表渲染缓存服务 - 基于文件系统的实现
 * 
 * 避免重复渲染相同图表，提升批量导出性能。
 * 支持缓存统计、自动清理等功能。
 * 
 * @version v3.1
 */

import fs from "fs";
import path from "path";
import crypto from "crypto";

export interface ChartCacheStats {
  hits: number;
  misses: number;
  hitRate: string;
  fileCount: number;
  totalSizeMB: number;
}

export class ChartCache {
  private cacheDir: string;
  private stats: { hits: number; misses: number; totalSize: number };

  constructor(cacheDir: string = "./cache/charts") {
    this.cacheDir = path.resolve(cacheDir);
    this.stats = { hits: 0, misses: 0, totalSize: 0 };

    // 创建缓存目录（如果不存在）
    if (!fs.existsSync(this.cacheDir)) {
      fs.mkdirSync(this.cacheDir, { recursive: true });
    }
  }

  /**
   * 生成缓存键（基于 Mermaid 代码的 MD5 哈希）
   */
  private generateCacheKey(mermaidCode: string): string {
    const hash = crypto.createHash("md5");
    hash.update(mermaidCode, "utf8");
    return hash.digest("hex");
  }

  /**
   * 获取缓存的图表文件
   * @param mermaidCode - Mermaid 代码或 JSON 数据字符串
   * @param fmt - 图片格式（png/svg），默认 png
   * @returns 缓存文件路径，未命中返回 null
   */
  public get(mermaidCode: string, fmt: string = "png"): string | null {
    const cacheKey = this.generateCacheKey(mermaidCode);
    const cachePath = path.join(this.cacheDir, `${cacheKey}.${fmt}`);

    if (fs.existsSync(cachePath)) {
      this.stats.hits++;
      const stat = fs.statSync(cachePath);
      this.stats.totalSize += stat.size;
      return cachePath;
    }

    this.stats.misses++;
    return null;
  }

  /**
   * 缓存图表文件
   * @param mermaidCode - Mermaid 代码或 JSON 数据字符串
   * @param content - 图片二进制内容
   * @param fmt - 图片格式（png/svg），默认 png
   * @returns 缓存文件路径
   */
  public set(mermaidCode: string, content: Buffer | string, fmt: string = "png"): string {
    const cacheKey = this.generateCacheKey(mermaidCode);
    const cachePath = path.join(this.cacheDir, `${cacheKey}.${fmt}`);

    // 确保目录存在
    const dir = path.dirname(cachePath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }

    // 写入文件
    if (typeof content === "string") {
      fs.writeFileSync(cachePath, content, { encoding: "binary" });
    } else {
      fs.writeFileSync(cachePath, content);
    }

    console.log(`[ChartCache] 已缓存图表: ${cacheKey.substring(0, 12)}.${fmt}`);
    return cachePath;
  }

  /**
   * 获取缓存的图表或重新渲染
   * @param mermaidCode - Mermaid 代码或 JSON 数据字符串
   * @param chartType - 图表类型（flowchart/gantt/architecture 等）
   * @param fmt - 图片格式（png/svg），默认 png
   * @returns 图片的二进制内容，如果渲染失败返回 null
   */
  public getOrRender(
    mermaidCode: string,
    chartType: string,
    fmt: string = "png",
    renderFunction: (code: string, type: string) => Buffer | null
  ): Buffer | null {
    const cached = this.get(mermaidCode, fmt);
    if (cached) {
      try {
        return fs.readFileSync(cached);
      } catch (error) {
        console.warn(`[ChartCache] 读取缓存文件失败: ${error}, 将重新渲染`);
      }
    }

    // 渲染新图表
    const result = renderFunction(mermaidCode, chartType);
    if (result) {
      this.set(mermaidCode, result, fmt);
      return result;
    }

    return null;
  }

  /**
   * 清理缓存
   * @param maxAgeDays - 仅清理超过 N 天的缓存；null 表示全部清理
   */
  public clear(maxAgeDays: number | null = null): void {
    if (!maxAgeDays) {
      // 删除所有文件
      const files = fs.readdirSync(this.cacheDir);
      files.forEach((file) => {
        const filePath = path.join(this.cacheDir, file);
        try {
          fs.unlinkSync(filePath);
        } catch (error) {
          console.warn(`[ChartCache] 删除文件失败: ${filePath}, 错误: ${error}`);
        }
      });
      console.log("[ChartCache] 已清空全部缓存");
      this.stats = { hits: 0, misses: 0, totalSize: 0 };
      return;
    }

    // 清理过期文件
    const cutoffDate = new Date();
    cutoffDate.setDate(cutoffDate.getDate() - maxAgeDays);
    let count = 0;

    const files = fs.readdirSync(this.cacheDir);
    files.forEach((file) => {
      const filePath = path.join(this.cacheDir, file);
      try {
        const stat = fs.statSync(filePath);
        if (stat.mtime < cutoffDate) {
          fs.unlinkSync(filePath);
          count++;
        }
      } catch (error) {
        console.warn(`[ChartCache] 检查文件失败: ${filePath}, 错误: ${error}`);
      }
    });

    console.log(`[ChartCache] 已清理 ${count} 个超过 ${maxAgeDays} 天的缓存文件`);
  }

  /**
   * 获取缓存统计信息
   */
  public getStats(): ChartCacheStats {
    const files = fs.readdirSync(this.cacheDir);
    const fileCount = files.length;
    let totalSize = 0;

    files.forEach((file) => {
      const filePath = path.join(this.cacheDir, file);
      try {
        const stat = fs.statSync(filePath);
        totalSize += stat.size;
      } catch (error) {
        console.warn(`[ChartCache] 获取文件大小失败: ${filePath}, 错误: ${error}`);
      }
    });

    const totalRequests = this.stats.hits + this.stats.misses;
    const hitRate = totalRequests > 0 ? ((this.stats.hits / totalRequests) * 100).toFixed(1) : "0.0";

    return {
      hits: this.stats.hits,
      misses: this.stats.misses,
      hitRate: `${hitRate}%`,
      fileCount,
      totalSizeMB: Number((totalSize / (1024 * 1024)).toFixed(2)),
    };
  }

  /**
   * 重置统计信息
   */
  public resetStats(): void {
    this.stats = { hits: 0, misses: 0, totalSize: 0 };
  }

  /**
   * 获取缓存目录路径
   */
  getCacheDirectory(): string {
    return this.cacheDir;
  }
}

export default ChartCache;