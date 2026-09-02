/**
 * Scroll-driven reveal and count-up hooks.
 *
 * Both fire once and then disconnect. An animation that replays every time a
 * section scrolls back into view stops reading as considered and starts
 * reading as a gimmick, which is the opposite of what this page is for.
 *
 * Both also check prefers-reduced-motion and skip straight to the final state
 * when it is set - the content must never depend on the animation to appear.
 */

import { useEffect, useRef, useState } from "react";

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * True once the element has entered the viewport.
 *
 * `delayMs` staggers a group: passing an increasing delay per card makes them
 * arrive in sequence rather than all at once.
 */
export function useReveal<T extends HTMLElement = HTMLDivElement>(
  delayMs = 0,
): [React.RefObject<T | null>, boolean] {
  const ref = useRef<T>(null);
  const [revealed, setRevealed] = useState(() => prefersReducedMotion());

  useEffect(() => {
    if (revealed) return;
    const node = ref.current;
    if (!node) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        // Disconnect immediately: this fires once, never on scroll-back.
        observer.disconnect();
        window.setTimeout(() => setRevealed(true), delayMs);
      },
      // Fires slightly before the element is fully on screen, so the motion
      // has finished by the time the reader's eye reaches it.
      { threshold: 0.15, rootMargin: "0px 0px -80px 0px" },
    );

    observer.observe(node);
    return () => observer.disconnect();
  }, [delayMs, revealed]);

  return [ref, revealed];
}

/**
 * Counts from zero to `target` once `active` becomes true.
 *
 * Eased out rather than linear so the number settles into place instead of
 * stopping dead - the difference between a figure that lands and one that
 * merely ticks.
 */
export function useCountUp(target: number, active: boolean, durationMs = 1200) {
  const [value, setValue] = useState(0);

  useEffect(() => {
    if (!active) return;
    if (prefersReducedMotion()) {
      setValue(target);
      return;
    }

    let frame = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min((now - start) / durationMs, 1);
      setValue(target * (1 - Math.pow(1 - t, 3)));
      if (t < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target, active, durationMs]);

  return value;
}

/** Fraction of the page scrolled, 0 to 1, for the progress rail. */
export function useScrollProgress(): number {
  const [progress, setProgress] = useState(0);

  useEffect(() => {
    const onScroll = () => {
      const scrollable =
        document.documentElement.scrollHeight - window.innerHeight;
      setProgress(scrollable <= 0 ? 0 : window.scrollY / scrollable);
    };

    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
    };
  }, []);

  return progress;
}
