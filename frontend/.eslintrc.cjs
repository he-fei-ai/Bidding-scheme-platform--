/* 前端 ESLint 配置（CommonJS，兼容 pre-commit mirrors-eslint v8） */
module.exports = {
  root: true,
  env: {
    browser: true,
    es2022: true,
    node: true,
  },
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  plugins: ['@typescript-eslint', 'react', 'react-hooks'],
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],
  settings: {
    react: { version: 'detect' },
  },
  rules: {
    // React 17+ 不需要显式 import React
    'react/react-in-jsx-scope': 'off',
    'react/prop-types': 'off',
    // 未使用变量仅警告，且不阻塞构建；以下划线开头的参数允许未使用
    '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    // any 类型警告（团队目标：逐步收紧为 error）
    '@typescript-eslint/no-explicit-any': 'warn',
    '@typescript-eslint/no-non-null-assertion': 'warn',
    'no-console': ['warn', { allow: ['warn', 'error'] }],
  },
  ignorePatterns: ['dist', 'build', 'node_modules', '*.config.ts', '*.config.js'],
};
