import type { ReactNode } from "react";
import { Space } from "antd";

interface PageHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  extra?: ReactNode;
}

export default function PageHeader({ title, description, icon, extra }: PageHeaderProps) {
  return (
    <div className="page-header">
      <div className="page-header__titles">
        <h2>
          {icon && (
            <span
              style={{
                display: "inline-grid",
                placeItems: "center",
                width: 34,
                height: 34,
                borderRadius: 9,
                color: "#1677ff",
                background: "#e8f1ff",
                fontSize: 17,
              }}
            >
              {icon}
            </span>
          )}
          <span>{title}</span>
        </h2>
        {description ? <p className="page-header__desc">{description}</p> : null}
      </div>
      {extra ? (
        <Space className="page-header__actions" size={10} wrap>
          {extra}
        </Space>
      ) : null}
    </div>
  );
}
