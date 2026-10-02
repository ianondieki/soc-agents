import type { SVGProps } from "react";

/**
 * The four marks the UI draws itself, so no page leans on a Unicode glyph for an icon.
 * One stroke weight, 12 px box, `currentColor`; size with `font-size` or the `size` prop.
 */
type P = SVGProps<SVGSVGElement> & { size?: number };

function Base({ size = 12, children, ...rest }: P) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

/** Done. */
export function IconCheck(p: P) {
  return (
    <Base {...p}>
      <path d="M2.5 6.5 5 9l4.5-6" />
    </Base>
  );
}

/** Failed or broken. */
export function IconAlert(p: P) {
  return (
    <Base {...p}>
      <path d="M6 2.5v4.2" />
      <path d="M6 9.4v.1" />
    </Base>
  );
}

/** Waiting for a person. */
export function IconPause(p: P) {
  return (
    <Base {...p}>
      <path d="M4 2.8v6.4M8 2.8v6.4" />
    </Base>
  );
}

/** A state dot (colour from `currentColor`). */
export function IconDot(p: P) {
  return (
    <Base {...p} stroke="none">
      <circle cx="6" cy="6" r="3.2" fill="currentColor" />
    </Base>
  );
}
