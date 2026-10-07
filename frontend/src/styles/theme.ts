import type { ThemeConfig } from "antd";

/**
 * 全局设计令牌 —— 「静谧工作台」
 * 保留品牌主色 #1677ff（与存量页面 40+ 处硬编码一致），
 * 通过中性色板、字体、圆角、阴影与组件级 token 建立统一、克制的高级感。
 */
export const brandTheme: ThemeConfig = {
  token: {
    colorPrimary: "#1677ff",
    colorInfo: "#1677ff",
    colorSuccess: "#16a34a",
    colorWarning: "#d97706",
    colorError: "#dc2626",
    colorLink: "#1677ff",

    colorBgLayout: "#f3f5f9",
    colorText: "#1f2733",
    colorTextSecondary: "#5b6675",
    colorTextTertiary: "#8a94a6",
    colorTextQuaternary: "#b4bccb",
    colorBorder: "#e3e7ef",
    colorBorderSecondary: "#edf0f5",

    borderRadius: 8,
    borderRadiusLG: 10,
    borderRadiusSM: 6,

    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
    fontSize: 14,

    controlHeight: 32,
    controlHeightLG: 40,

    boxShadow:
      "0 1px 2px rgba(16, 24, 40, 0.04), 0 1px 3px rgba(16, 24, 40, 0.06)",
    boxShadowSecondary:
      "0 4px 12px rgba(16, 24, 40, 0.08), 0 2px 4px rgba(16, 24, 40, 0.04)",

    wireframe: false,
  },
  components: {
    Layout: {
      headerBg: "#ffffff",
      headerHeight: 56,
      headerPadding: "0 20px",
      siderBg: "#0d1a36",
      bodyBg: "#f3f5f9",
    },
    Menu: {
      itemBorderRadius: 8,
      itemMarginInline: 10,
      itemMarginBlock: 3,
      itemHeight: 40,
      darkItemBg: "transparent",
      darkSubMenuItemBg: "transparent",
      darkItemColor: "rgba(226, 232, 240, 0.68)",
      darkItemHoverColor: "#ffffff",
      darkItemHoverBg: "rgba(255, 255, 255, 0.08)",
      darkItemSelectedBg: "linear-gradient(135deg, #1f6fff 0%, #0e53d6 100%)",
      darkItemSelectedColor: "#ffffff",
      darkGroupTitleColor: "rgba(226, 232, 240, 0.4)",
    },
    Card: {
      paddingLG: 20,
      headerFontSize: 15,
      headerHeight: 52,
      colorBorderSecondary: "#e8ecf2",
    },
    Table: {
      headerBg: "#f8fafc",
      headerColor: "#5b6675",
      headerSplitColor: "transparent",
      rowHoverBg: "#f5f9ff",
      borderColor: "#edf0f5",
      cellPaddingBlock: 12,
    },
    Button: {
      fontWeight: 500,
      primaryShadow: "0 1px 2px rgba(22, 119, 255, 0.25)",
    },
    Tag: {
      borderRadiusSM: 5,
    },
    Modal: {
      borderRadiusLG: 12,
    },
    Input: {
      activeShadow: "0 0 0 3px rgba(22, 119, 255, 0.12)",
    },
    Select: {
      optionSelectedBg: "#eef4ff",
    },
    Steps: {
      colorPrimary: "#1677ff",
    },
    Progress: {
      remainingColor: "#edf0f5",
    },
    Tooltip: {
      borderRadius: 8,
    },
    Empty: {
      colorTextDisabled: "#8a94a6",
    },
  },
};
