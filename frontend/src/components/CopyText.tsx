import { useEffect, useId, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";

/**
 * A ready-to-paste message: the text as it will read, and one button that puts it on the
 * clipboard. Where the browser refuses the clipboard (an embedded page, an old browser), the text
 * is selected instead and the line under the button says how to copy it. Nothing is sent.
 */
export default function CopyText({
  title,
  lead,
  text,
  label = "Copy",
}: {
  title: string;
  lead?: string;
  text: string;
  label?: string;
}) {
  const id = useId();
  const pre = useRef<HTMLPreElement>(null);
  const [said, setSaid] = useState<{ ok: boolean; text: string } | null>(null);
  const timer = useRef<number | undefined>(undefined);

  // A changed text is a new message: the old "Copied" no longer describes it.
  useEffect(() => setSaid(null), [text]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const selectText = () => {
    const el = pre.current;
    const sel = window.getSelection();
    if (!el || !sel) return;
    const range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
  };

  const copy = () => {
    window.clearTimeout(timer.current);
    const done = () => {
      setSaid({ ok: true, text: "Copied. Paste it in the group." });
      timer.current = window.setTimeout(() => setSaid(null), 6000);
    };
    const refused = () => {
      selectText();
      setSaid({ ok: false, text: "This browser would not copy it. The text is selected: press Ctrl+C, or hold it and choose Copy." });
    };
    try {
      if (!navigator.clipboard?.writeText) return refused();
      navigator.clipboard.writeText(text).then(done, refused);
    } catch {
      refused();
    }
  };

  return (
    <section className="panel copy-text" aria-labelledby={`${id}-t`}>
      <h2 id={`${id}-t`} className="panel-title">
        {title}
      </h2>
      {lead && <p className="muted copy-text-lead">{lead}</p>}
      <pre ref={pre} className="pre copy-text-body" tabIndex={0} aria-label={`${title}, the text`}>
        {text}
      </pre>
      <div className="copy-text-row">
        <button type="button" className="btn sm" onClick={copy}>
          {said?.ok ? <Check size={15} strokeWidth={2.2} aria-hidden="true" /> : <Copy size={15} strokeWidth={2} aria-hidden="true" />}
          {said?.ok ? "Copied" : label}
        </button>
        <p className={"copy-text-said" + (said && !said.ok ? " warn" : "")} role="status">
          {said?.text ?? ""}
        </p>
      </div>
    </section>
  );
}
