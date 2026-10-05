/**
 * The mark: the agent dial. A ring, half of it lit in the agents' blue (the work they do on their
 * own), ending in the violet dot where a person decides; inside, three ascending bars for the
 * network the desk keeps up. The same story as the dial on the front page, in one glyph.
 *
 * Drawn on a 24 grid, crisp from 20 px. The dot cuts the ring with the colour behind the mark:
 * `--mark-cut` (the nav surface unless a page sets it). public/favicon.svg is the same drawing
 * with fixed colours.
 */
export function BrandMark({ size = 24 }: { size?: number }) {
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="9" stroke="var(--line-strong)" strokeWidth="2.4" />
      <path d="M12 3a9 9 0 0 1 0 18" stroke="var(--accent)" strokeWidth="2.4" strokeLinecap="round" />
      <circle cx="12" cy="21" r="2.6" fill="var(--hitl)" stroke="var(--mark-cut, var(--surface-nav))" strokeWidth="1.6" />
      <g fill="var(--text-strong)">
        <rect x="7.4" y="12.9" width="2.2" height="3.4" rx="1.1" />
        <rect x="10.9" y="10.5" width="2.2" height="5.8" rx="1.1" />
        <rect x="14.4" y="8.1" width="2.2" height="8.2" rx="1.1" />
      </g>
    </svg>
  );
}
