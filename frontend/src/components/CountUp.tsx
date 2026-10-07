import { useCalm, useCountUp } from "../lib/motion";

/**
 * A figure that counts up once to its value (lib/motion.ts). The moving number is for the eye
 * only; a screen reader reads the final value once, so a live region around it never chatters.
 * Calm (quiet mode or reduced motion): the value at once.
 */
export default function CountUp({ value, format, ms = 1000 }: { value: number; format: (n: number) => string; ms?: number }) {
  const calm = useCalm();
  const shown = useCountUp(value, calm, ms);
  return (
    <>
      <span aria-hidden="true">{format(shown ?? value)}</span>
      <span className="sr-only">{format(value)}</span>
    </>
  );
}
