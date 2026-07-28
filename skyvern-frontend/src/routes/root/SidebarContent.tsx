import { Logo } from "@/components/Logo";
import { LogoMinimized } from "@/components/LogoMinimized";
import { useSidebarStore } from "@/store/SidebarStore";
import { Link } from "react-router-dom";
import { EnterpriseSideNav } from "@/components/enterprise/EnterpriseSideNav";
import { cn } from "@/util/utils";
import { Button } from "@/components/ui/button";
import { ChevronLeftIcon, ChevronRightIcon } from "@radix-ui/react-icons";

type Props = {
  useCollapsedState?: boolean;
};

function SidebarContent({ useCollapsedState }: Props) {
  const { collapsed: collapsedState, setCollapsed } = useSidebarStore();
  const collapsed = useCollapsedState ? collapsedState : false;

  return (
    <div className="flex h-full flex-col overflow-y-auto px-4">
      <Link to={window.location.origin}>
        <div className="flex h-20 items-center justify-center">
          {collapsed ? <LogoMinimized /> : <Logo />}
        </div>
      </Link>
      <EnterpriseSideNav />
      <div
        className={cn("mt-auto flex min-h-12", {
          "justify-center": collapsed,
          "justify-end": !collapsed,
        })}
      >
        <Button
          size="icon"
          variant="ghost"
          onClick={() => {
            setCollapsed(!collapsed);
          }}
        >
          {collapsed ? (
            <ChevronRightIcon className="h-5 w-5" />
          ) : (
            <ChevronLeftIcon className="hidden h-5 w-5 lg:block" />
          )}
        </Button>
      </div>
    </div>
  );
}

export { SidebarContent };
