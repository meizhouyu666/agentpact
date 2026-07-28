/**
 * Timeline component for audit log and task step visualization.
 */

import { cn } from "@/util/utils";
import { Icon, type IconName } from "@/components/Icon";
import type { ReactNode } from "react";

export type TimelineItem = {
  id: string;
  title: string;
  description?: string;
  timestamp: string;
  icon?: IconName;
  status?: "success" | "error" | "warning" | "info";
  children?: ReactNode;
};

const statusDotColor: Record<string, string> = {
  success: "bg-green-500",
  error: "bg-red-500",
  warning: "bg-amber-500",
  info: "bg-blue-500",
};

type TimelineProps = {
  items: TimelineItem[];
  className?: string;
};

export function Timeline({ items, className }: TimelineProps) {
  return (
    <div className={cn("relative", className)}>
      {items.map((item, index) => (
        <div key={item.id} className="relative flex gap-4 pb-8 last:pb-0">
          {/* Vertical line */}
          {index < items.length - 1 && (
            <div className="absolute left-[15px] top-8 h-full w-px bg-gray-200" />
          )}

          {/* Dot / Icon */}
          <div className="relative z-10 flex h-8 w-8 shrink-0 items-center justify-center">
            {item.icon ? (
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-white shadow-sm ring-1 ring-gray-200">
                <Icon name={item.icon} size={16} color="var(--finrpa-blue)" />
              </div>
            ) : (
              <div
                className={cn(
                  "h-3 w-3 rounded-full ring-4 ring-white",
                  statusDotColor[item.status ?? "info"],
                )}
              />
            )}
          </div>

          {/* Content */}
          <div className="flex-1 pt-0.5">
            <div className="flex items-center justify-between">
              <h4
                className="text-sm font-medium"
                style={{ color: "var(--finrpa-text-primary)" }}
              >
                {item.title}
              </h4>
              <time
                className="text-xs"
                style={{ color: "var(--finrpa-text-muted)" }}
              >
                {item.timestamp}
              </time>
            </div>
            {item.description && (
              <p
                className="mt-1 text-sm"
                style={{ color: "var(--finrpa-text-secondary)" }}
              >
                {item.description}
              </p>
            )}
            {item.children && <div className="mt-2">{item.children}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}
