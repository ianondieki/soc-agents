import { useState } from "react";
import { Moon, Sun } from "lucide-react";
import { THEME_KEY, applyTheme, type Theme } from "../../lib/theme";

/**
 * The sun or moon on a standalone page (the public complaint form): pins the other theme in the
 * same localStorage key the console's Display menu and index.html's bootstrap read.
 */
export default function ThemeToggle({ className = "" }: { className?: string }) {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return document.documentElement.getAttribute("data-theme") === "day" ? "day" : "night";
    } catch {
      return "night";
    }
  });
  const flip = () => {
    const next: Theme = theme === "day" ? "night" : "day";
    try {
      window.localStorage.setItem(THEME_KEY, next);
    } catch {
      /* storage blocked: the choice lasts until the tab closes */
    }
    applyTheme(next);
    setTheme(next);
  };
  const label = theme === "day" ? "Switch to the night theme" : "Switch to the day theme";
  return (
    <button type="button" className={className} onClick={flip} aria-label={label} title={label}>
      {theme === "day" ? <Moon size={18} strokeWidth={1.75} aria-hidden="true" /> : <Sun size={18} strokeWidth={1.75} aria-hidden="true" />}
    </button>
  );
}
