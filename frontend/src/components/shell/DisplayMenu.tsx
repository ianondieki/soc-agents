import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FocusEvent, type KeyboardEvent } from "react";
import { Check, ChevronDown, Siren, SunMoon } from "lucide-react";
import { PROJECTOR_MEANING, QUIET_MEANING } from "../../lib/display";
import {
  ALERT_LEVEL_LABEL,
  ALERT_LEVEL_MEANING,
  ALERT_SOUND_MEANING,
  setAlertLevel,
  setAlertSound,
  testAlarm,
  useAlerts,
  type AlertLevel,
} from "../../lib/feedback";
import { THEME_LABEL, type ThemePref } from "../../lib/theme";
import { useSuppressedCount } from "../../realtime/RealtimeContext";
import { roveFocus } from "./nav";

/**
 * The Display controls: the theme (Auto, Day, Night), Quiet mode, Projector and Alerts (how
 * the console buzzes, sounds and signals: lib/feedback.ts). In the top bar
 * they sit behind one "Display" button, in a fixed-position disclosure panel that Escape, an
 * outside press or Tab-out closes (focus goes back to the button). On a phone the same controls
 * render inline in the menu sheet (`DisplayControls`).
 */

export type DisplayState = {
  pref: ThemePref;
  setPref: (p: ThemePref) => void;
  quiet: boolean;
  onQuiet: () => void;
  projector: boolean;
  onProjector: () => void;
};

const THEME_MEANING: Record<ThemePref, string> = {
  auto: "Day from 06:00 to 18:59 in Nairobi, Night otherwise.",
  day: "A lit office: white sheets on a cool desk, ink text.",
  night: "Lights down: graphite, the same signals a step brighter.",
};

const PREFS: ThemePref[] = ["auto", "day", "night"];

/** A switch named by its title alone; the one-line meaning is its description. */
export function Switch({
  on,
  onToggle,
  title,
  meaning,
  note,
  id,
}: {
  on: boolean;
  onToggle: () => void;
  title: string;
  meaning: string;
  note?: string;
  id: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-labelledby={`${id}-title`}
      aria-describedby={`${id}-desc`}
      className="menu-item menu-switch"
      onClick={onToggle}
    >
      <span className="menu-item-text">
        <span className="menu-item-title" id={`${id}-title`}>
          {title}
        </span>
        <span className="menu-item-desc" id={`${id}-desc`}>
          {meaning}
          {note ? ` ${note}` : ""}
        </span>
      </span>
      <span className="switch-track" aria-hidden="true">
        <span className="switch-thumb" />
      </span>
    </button>
  );
}

/** The controls themselves: a theme radio group and two switches. */
export function DisplayControls({ d, idPrefix = "display" }: { d: DisplayState; idPrefix?: string }) {
  const suppressed = useSuppressedCount();
  const groupRef = useRef<HTMLDivElement>(null);
  const onRadioKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const radios = Array.from(groupRef.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]') ?? []);
    const before = document.activeElement;
    if (roveFocus(radios, e, before)) {
      const now = document.activeElement as HTMLButtonElement | null;
      const p = now?.dataset.pref as ThemePref | undefined;
      if (p) d.setPref(p);
    }
  };
  return (
    <>
      <div className="menu-section">
        <div className="menu-label" id={`${idPrefix}-theme-label`}>
          Theme
        </div>
        <div role="radiogroup" aria-labelledby={`${idPrefix}-theme-label`} ref={groupRef} onKeyDown={onRadioKey}>
          {PREFS.map((p) => {
            const checked = d.pref === p;
            return (
              <button
                key={p}
                type="button"
                role="radio"
                aria-checked={checked}
                aria-labelledby={`${idPrefix}-${p}-title`}
                aria-describedby={`${idPrefix}-${p}-desc`}
                data-pref={p}
                tabIndex={checked ? 0 : -1}
                className="menu-item menu-radio"
                onClick={() => d.setPref(p)}
              >
                <span className="menu-check" aria-hidden="true">
                  {checked && <Check size={14} strokeWidth={2.25} />}
                </span>
                <span className="menu-item-text">
                  <span className="menu-item-title" id={`${idPrefix}-${p}-title`}>
                    {THEME_LABEL[p]}
                  </span>
                  <span className="menu-item-desc" id={`${idPrefix}-${p}-desc`}>
                    {THEME_MEANING[p]}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </div>
      <div className="menu-sep" role="separator" />
      <div className="menu-section">
        <Switch
          id={`${idPrefix}-quiet`}
          on={d.quiet}
          onToggle={d.onQuiet}
          title="Quiet mode"
          meaning={QUIET_MEANING}
          note={d.quiet && suppressed > 0 ? `${suppressed} non-critical lines held back.` : undefined}
        />
        <Switch id={`${idPrefix}-projector`} on={d.projector} onToggle={d.onProjector} title="Projector" meaning={PROJECTOR_MEANING} />
      </div>
      <div className="menu-sep" role="separator" />
      <AlertControls idPrefix={idPrefix} />
    </>
  );
}

const LEVELS: AlertLevel[] = ["all", "alarms", "off"];

/** Alerts: how much the console buzzes and signals, whether it sounds, and a test press. */
function AlertControls({ idPrefix }: { idPrefix: string }) {
  const { level, sound } = useAlerts();
  const groupRef = useRef<HTMLDivElement>(null);
  const onRadioKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const radios = Array.from(groupRef.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]') ?? []);
    const before = document.activeElement;
    if (roveFocus(radios, e, before)) {
      const now = document.activeElement as HTMLButtonElement | null;
      const v = now?.dataset.level as AlertLevel | undefined;
      if (v) setAlertLevel(v);
    }
  };
  return (
    <div className="menu-section">
      <div className="menu-label" id={`${idPrefix}-alerts-label`}>
        Alerts
      </div>
      <div role="radiogroup" aria-labelledby={`${idPrefix}-alerts-label`} ref={groupRef} onKeyDown={onRadioKey}>
        {LEVELS.map((v) => {
          const checked = level === v;
          return (
            <button
              key={v}
              type="button"
              role="radio"
              aria-checked={checked}
              aria-labelledby={`${idPrefix}-alerts-${v}-title`}
              aria-describedby={`${idPrefix}-alerts-${v}-desc`}
              data-level={v}
              tabIndex={checked ? 0 : -1}
              className="menu-item menu-radio"
              onClick={() => setAlertLevel(v)}
            >
              <span className="menu-check" aria-hidden="true">
                {checked && <Check size={14} strokeWidth={2.25} />}
              </span>
              <span className="menu-item-text">
                <span className="menu-item-title" id={`${idPrefix}-alerts-${v}-title`}>
                  {ALERT_LEVEL_LABEL[v]}
                </span>
                <span className="menu-item-desc" id={`${idPrefix}-alerts-${v}-desc`}>
                  {ALERT_LEVEL_MEANING[v]}
                </span>
              </span>
            </button>
          );
        })}
      </div>
      {level !== "off" && (
        <>
          <Switch id={`${idPrefix}-sound`} on={sound} onToggle={() => setAlertSound(!sound)} title="Sound" meaning={ALERT_SOUND_MEANING} />
          <div className="menu-test">
            <button type="button" className="btn sm" onClick={testAlarm}>
              <Siren size={15} strokeWidth={1.75} aria-hidden="true" />
              Test the P1 alarm
            </button>
            <span className="menu-item-desc">Buzzes on a phone or tablet that can.</span>
          </div>
        </>
      )}
    </div>
  );
}

export function DisplayMenu({ d }: { d: DisplayState }) {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; right: number }>({ top: 56, right: 16 });

  const place = useCallback(() => {
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    setPos({ top: Math.round(r.bottom + 6), right: Math.max(8, Math.round(window.innerWidth - r.right)) });
  }, []);

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) btnRef.current?.focus({ preventScroll: true });
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    place();
    const panel = panelRef.current;
    const first = panel?.querySelector<HTMLElement>('[role="radio"][aria-checked="true"]') ?? panel?.querySelector<HTMLElement>("button");
    first?.focus({ preventScroll: true });
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: Event) => {
      const t = e.target as Node | null;
      if (!t || panelRef.current?.contains(t) || btnRef.current?.contains(t)) return;
      close(false);
    };
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close(true);
      }
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", place);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", place);
    };
  }, [open, close, place]);

  const onPanelBlur = (e: FocusEvent<HTMLDivElement>) => {
    const to = e.relatedTarget as Node | null;
    if (!to || panelRef.current?.contains(to) || btnRef.current?.contains(to)) return;
    close(false);
  };

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className={"btn sm topbar-display-btn" + (open ? " is-open" : "")}
        aria-expanded={open}
        aria-controls="display-menu"
        title="Theme, quiet mode and projector"
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        <SunMoon size={16} strokeWidth={1.75} aria-hidden="true" />
        <span>Display</span>
        <ChevronDown size={14} strokeWidth={1.75} aria-hidden="true" className="btn-chevron" />
      </button>
      {open && (
        <div
          ref={panelRef}
          id="display-menu"
          className="menu display-menu"
          role="dialog"
          aria-label="Display"
          style={{ top: pos.top, right: pos.right }}
          onBlur={onPanelBlur}
        >
          <DisplayControls d={d} />
        </div>
      )}
    </>
  );
}
