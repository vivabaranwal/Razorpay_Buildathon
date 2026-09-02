/**
 * Minimal two-route router built on the History API.
 *
 * react-router would be a reasonable dependency for a real app, but this one
 * has exactly two routes and no nesting, params, or loaders. Twenty lines here
 * beats a dependency whose entire feature set goes unused - and it still gets
 * the browser back and forward buttons right, which is the part hand-rolled
 * routing usually breaks.
 */

import { useCallback, useEffect, useState } from "react";

export function usePath(): [string, (to: string) => void] {
  const [path, setPath] = useState(() => window.location.pathname);

  useEffect(() => {
    // popstate fires on back/forward, which pushState does not trigger.
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((to: string) => {
    if (to === window.location.pathname) return;
    window.history.pushState({}, "", to);
    setPath(to);
    window.scrollTo(0, 0);
  }, []);

  return [path, navigate];
}
