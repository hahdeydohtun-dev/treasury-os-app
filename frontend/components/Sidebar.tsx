"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV_ITEMS } from "@/lib/nav-items";

export function Sidebar() {
  const pathname = usePathname();

  return (
    <nav className="sidebar">
      <div className="sidebar__brand">Treasury OS</div>
      {NAV_ITEMS.map((item) => {
        const isActive = pathname === item.href;
        const className = `sidebar__nav-item ${item.enabled ? "enabled" : "disabled"}`;
        const style = isActive
          ? { background: "var(--color-surface-alt)", color: "var(--color-text)" }
          : undefined;

        if (!item.enabled) {
          return (
            <span key={item.href} className={className} title="Not yet implemented">
              {item.label}
            </span>
          );
        }
        return (
          <Link key={item.href} href={item.href} className={className} style={style}>
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
