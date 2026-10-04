import { Fragment, type ReactNode } from "react";

/**
 * The knowledge base's bodies are "markdown-ish": paragraphs, `- ` bullets, `1. ` steps, `**bold**`
 * and `#`/`##` headings. Rendered as the HTML they mean, with no dependency: a blank line ends a
 * block, a bullet line starts or continues a list, everything else is a paragraph. No HTML is
 * interpreted, so an article can never inject markup.
 */

type Block = { kind: "p"; text: string } | { kind: "ul" | "ol"; items: string[] } | { kind: "h"; level: number; text: string };

function parse(src: string): Block[] {
  const lines = String(src ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let para: string[] = [];
  let list: { kind: "ul" | "ol"; items: string[] } | null = null;
  const flushPara = () => {
    if (para.length) blocks.push({ kind: "p", text: para.join(" ") });
    para = [];
  };
  const flushList = () => {
    if (list) blocks.push(list);
    list = null;
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    const t = line.trim();
    if (!t) {
      flushPara();
      flushList();
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(t);
    if (h) {
      flushPara();
      flushList();
      blocks.push({ kind: "h", level: h[1].length, text: h[2] });
      continue;
    }
    const ul = /^[-*•]\s+(.*)$/.exec(t);
    const ol = /^\d+[.)]\s+(.*)$/.exec(t);
    if (ul || ol) {
      flushPara();
      const kind = ul ? "ul" : "ol";
      const item = (ul ?? ol)![1];
      if (!list || list.kind !== kind) {
        flushList();
        list = { kind, items: [] };
      }
      list.items.push(item);
      continue;
    }
    // A wrapped continuation of the last bullet (indented), else prose.
    if (list && /^\s{2,}/.test(raw)) {
      list.items[list.items.length - 1] += " " + t;
      continue;
    }
    flushList();
    para.push(t);
  }
  flushPara();
  flushList();
  return blocks;
}

/** `**bold**` and `` `code` `` inside a line. */
function inline(text: string): ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return parts.map((p, i) => {
    if (p.startsWith("**") && p.endsWith("**")) return <strong key={i}>{p.slice(2, -2)}</strong>;
    if (p.startsWith("`") && p.endsWith("`")) return <code key={i}>{p.slice(1, -1)}</code>;
    return <Fragment key={i}>{p}</Fragment>;
  });
}

export default function Markdownish({ text, className }: { text: string; className?: string }) {
  const blocks = parse(text);
  return (
    <div className={className}>
      {blocks.map((b, i) => {
        if (b.kind === "p") return <p key={i}>{inline(b.text)}</p>;
        if (b.kind === "h") {
          const Tag = (b.level <= 2 ? "h3" : "h4") as "h3" | "h4";
          return <Tag key={i}>{inline(b.text)}</Tag>;
        }
        const Tag = b.kind;
        return (
          <Tag key={i}>
            {b.items.map((it, j) => (
              <li key={j}>{inline(it)}</li>
            ))}
          </Tag>
        );
      })}
    </div>
  );
}
