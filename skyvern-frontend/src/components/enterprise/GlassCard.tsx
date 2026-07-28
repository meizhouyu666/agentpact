/**
 * Frosted-glass card container used throughout enterprise pages.
 */

import { cn } from "@/util/utils";
import type { ReactNode } from "react";

type GlassCardProps = {
  children: ReactNode;
  className?: string;
  hoverable?: boolean;
  padding?: "sm" | "md" | "lg";
  onClick?: () => void;
};

const paddingMap = {
  sm: "p-4",
  md: "p-6",
  lg: "p-8",
};

export function GlassCard({
  children,
  className,
  hoverable = true,
  padding = "md",
  onClick,
}: GlassCardProps) {
  return (
    <div
      className={cn(
        hoverable ? "glass-card" : "glass-card-static",
        paddingMap[padding],
        onClick && "cursor-pointer",
        className,
      )}
      onClick={onClick}
    >
      {children}
    </div>
  );
}
