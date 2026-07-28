/**
 * FinRPA Enterprise unified SVG Icon component.
 *
 * Usage:
 *   <Icon name="approval" size={20} />
 *   <Icon name="task" size={24} color="#1A3A5C" />
 */

import { iconPaths } from "./icons";

export type IconName = keyof typeof iconPaths;

type IconProps = {
  name: IconName;
  size?: 16 | 20 | 24 | number;
  color?: string;
  className?: string;
};

export function Icon({ name, size = 20, color, className }: IconProps) {
  const renderPath = iconPaths[name];
  if (!renderPath) {
    console.warn(`[Icon] Unknown icon name: "${name}"`);
    return null;
  }

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      style={{ color, flexShrink: 0 }}
      className={className}
    >
      {renderPath()}
    </svg>
  );
}
