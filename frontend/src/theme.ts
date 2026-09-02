/**
 * Theme resolution and persistence.
 *
 * The OS preference is the default, a manual choice overrides it, and that
 * choice survives a reload. Applied by stamping `data-theme` on the root
 * element, which is what every token in index.css keys off.
 */

import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "settletrace-theme";

function systemTheme(): Theme {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function storedTheme(): Theme | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    // Private browsing and blocked site data both throw on access. A theme is
    // a convenience, so it degrades to the OS preference rather than failing.
    return null;
  }
}

function apply(theme: Theme): void {
  document.documentElement.setAttribute("data-theme", theme);
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(
    () => storedTheme() ?? systemTheme(),
  );

  useEffect(() => {
    apply(theme);
  }, [theme]);

  // Follow the OS while the user has expressed no preference of their own.
  // Once they choose, their choice wins until they change it.
  useEffect(() => {
    if (storedTheme()) return;

    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (e: MediaQueryListEvent) =>
      setTheme(e.matches ? "dark" : "light");

    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch {
        // Persisting failed; the theme still applies for this session.
      }
      return next;
    });
  }, []);

  return { theme, toggle };
}

/**
 * Stamp the theme before React mounts.
 *
 * Called from an inline module in index.html so the correct palette is on the
 * element for the first paint. Without it a stored dark preference renders one
 * light frame first - a visible flash on every load.
 */
export function initTheme(): void {
  apply(storedTheme() ?? systemTheme());
}
