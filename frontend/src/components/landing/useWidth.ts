import { useLayoutEffect, useRef, useState, type RefObject } from "react";

/**
 * The rendered width of an element, in CSS pixels, kept current as it resizes. The landing's
 * drawings set their SVG viewBox to the measured width, so one user unit is one pixel and the
 * labels stay the size the stylesheet says at every width (a scaled viewBox would shrink them
 * on a phone). 0 until the first layout, which runs before the first paint.
 */
export function useWidth<T extends HTMLElement>(): [RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => setWidth(Math.round(el.getBoundingClientRect().width));
    read();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", read);
      return () => window.removeEventListener("resize", read);
    }
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}
