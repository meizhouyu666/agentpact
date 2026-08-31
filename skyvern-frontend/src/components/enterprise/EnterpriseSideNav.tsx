/**
 * Enterprise sidebar navigation with frosted-glass style and i18n support.
 */

import type { ReactNode } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import {
  CheckCircledIcon,
  CounterClockwiseClockIcon,
  ExitIcon,
  GearIcon,
  LightningBoltIcon,
  MagnifyingGlassIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import { useSidebarStore } from "@/store/SidebarStore";
import { useI18n } from "@/i18n/useI18n";
import { useAuthStore } from "@/store/AuthStore";
import type { MessageKey } from "@/i18n/locales";

type NavItem = {
  labelKey: MessageKey;
  to: string;
  icon: ReactNode;
};

const buildSection: NavItem[] = [
  { labelKey: "nav.discover", to: "/discover", icon: <MagnifyingGlassIcon className="size-5" /> },
  { labelKey: "nav.workflows", to: "/workflows", icon: <LightningBoltIcon className="size-5" /> },
  { labelKey: "nav.runs", to: "/runs", icon: <CounterClockwiseClockIcon className="size-5" /> },
];

const enterpriseSection: NavItem[] = [
  { labelKey: "nav.agentRuns", to: "/enterprise/agent-runs", icon: <ReloadIcon className="size-5" /> },
  { labelKey: "nav.approvals", to: "/enterprise/approvals", icon: <CheckCircledIcon className="size-5" /> },
];

const generalSection: NavItem[] = [
  { labelKey: "nav.settings", to: "/settings", icon: <GearIcon className="size-5" /> },
];

function NavSection({
  titleKey,
  items,
  collapsed,
}: {
  titleKey: MessageKey;
  items: NavItem[];
  collapsed: boolean;
}) {
  const { t } = useI18n();

  return (
    <div className="mb-6">
      {!collapsed && (
        <div
          className="mb-2 px-3 text-[11px] font-semibold uppercase tracking-widest"
          style={{ color: "var(--finrpa-text-muted)" }}
        >
          {t(titleKey)}
        </div>
      )}
      <div className="space-y-1">
        {items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) =>
              cn("glass-nav-item", {
                active: isActive,
                "justify-center px-2": collapsed,
              })
            }
            title={collapsed ? t(item.labelKey) : undefined}
          >
            {item.icon}
            {!collapsed && <span>{t(item.labelKey)}</span>}
          </NavLink>
        ))}
      </div>
    </div>
  );
}

export function EnterpriseSideNav() {
  const { collapsed } = useSidebarStore();
  const { t } = useI18n();
  const navigate = useNavigate();
  const logout = useAuthStore((s) => s.logout);

  function handleLogout() {
    logout();
    navigate("/login");
  }

  return (
    <nav className="flex-1 overflow-y-auto py-2">
      <NavSection titleKey="nav.build"      items={buildSection}      collapsed={collapsed} />
      <NavSection titleKey="nav.enterprise" items={enterpriseSection} collapsed={collapsed} />
      <NavSection titleKey="nav.general"    items={generalSection}    collapsed={collapsed} />

      {/* Logout */}
      <div className="mt-2 border-t" style={{ borderColor: "var(--glass-border)" }}>
        <button
          type="button"
          onClick={handleLogout}
          className={cn("glass-nav-item w-full", {
            "justify-center px-2": collapsed,
          })}
          title={collapsed ? t("auth.logout") : undefined}
          style={{ cursor: "pointer", background: "none", border: "none" }}
        >
          <ExitIcon className="size-5" />
          {!collapsed && <span>{t("auth.logout")}</span>}
        </button>
      </div>
    </nav>
  );
}
