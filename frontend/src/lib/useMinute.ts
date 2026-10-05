import { useEffect, useState } from "react";

/** The current time, re-rendered on the minute (not every second: nothing that uses it ticks). */
export function useMinute(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    let timer = 0;
    const arm = () => {
      const d = new Date();
      timer = window.setTimeout(() => {
        setNow(new Date());
        arm();
      }, 60_000 - (d.getSeconds() * 1000 + d.getMilliseconds()) + 50);
    };
    arm();
    return () => window.clearTimeout(timer);
  }, []);
  return now;
}
