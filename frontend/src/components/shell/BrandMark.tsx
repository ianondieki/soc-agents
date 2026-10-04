/**
 * The mark: three fibre strands (blue, orange, green: the first three colours of a 12-fibre tube)
 * running into one splice. Drawn at 24 px, crisp at 22. Identity only, so it uses the fibre
 * colours and the ink; never a status colour.
 */
export function BrandMark({ size = 22 }: { size?: number }) {
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
      <path d="M2.5 5.5C9.5 5.5 12.5 12 19 12" stroke="var(--fibre-1)" strokeWidth="2.2" strokeLinecap="round" />
      <path d="M2.5 12H19" stroke="var(--fibre-2)" strokeWidth="2.2" strokeLinecap="round" />
      <path d="M2.5 18.5C9.5 18.5 12.5 12 19 12" stroke="var(--fibre-3)" strokeWidth="2.2" strokeLinecap="round" />
      <circle cx="19" cy="12" r="3" fill="var(--text-strong)" stroke="var(--surface-nav)" strokeWidth="1.5" />
    </svg>
  );
}
