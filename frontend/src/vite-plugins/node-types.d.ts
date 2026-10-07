// Type declarations for Node.js built-in modules used by vite plugins.
// @types/node is not installed to avoid package manager conflicts.
declare module "fs" {
  export interface FSWatcher {
    close(): void;
  }
  export function readFileSync(path: string, encoding?: string): string | Buffer;
  export function watch(
    filename: string,
    options: { persistent: boolean },
    callback: (event: string, filename?: string) => void,
  ): FSWatcher;
}

declare module "path" {
  export function resolve(...paths: string[]): string;
}

declare const process: {
  cwd(): string;
  env: Record<string, string | undefined>;
};

declare class IncomingMessage {
  url?: string;
  on(event: string, listener: (...args: any[]) => void): this;
}
