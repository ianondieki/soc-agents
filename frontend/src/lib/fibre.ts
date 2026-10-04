import { LIFECYCLE_NODES } from "./agents";

/**
 * The twelve agents wear the twelve colours of a fibre ribbon (TIA-598), in pipeline order:
 * blue, orange, green, brown, slate, white, red, black, yellow, violet, rose, aqua. Every field
 * engineer on the floor knows the sequence by heart. Identity marks only (docs/DESIGN_SYSTEM.md):
 * never a status, never a fill behind text. White and black get a 1px `--fibre-outline` so they
 * read on both themes. The colour values live in styles.css as `--fibre-1` .. `--fibre-12`;
 * nothing here, or on any page, hard-codes a hex.
 */
export interface Fibre {
  /** 1-based position in the ribbon, which is also the agent's position in the pipeline. */
  n: number;
  /** The lifecycle node id ("INGEST", "HITL", ...). */
  node: string;
  /** The one label every page shows for the hop ("Approval", "Exec brief"). */
  label: string;
  /** The fibre colour's name as the floor says it ("Blue", "Slate"). */
  colour: string;
  /** The CSS custom property that holds the colour for the current theme. */
  cssVar: string;
  /** White and black need the `--fibre-outline` stroke to read on both themes. */
  outlined: boolean;
}

const COLOURS = ["Blue", "Orange", "Green", "Brown", "Slate", "White", "Red", "Black", "Yellow", "Violet", "Rose", "Aqua"] as const;

export const FIBRES: readonly Fibre[] = LIFECYCLE_NODES.map((node, i) => ({
  n: i + 1,
  node: node.id,
  label: node.label,
  colour: COLOURS[i],
  cssVar: `--fibre-${i + 1}`,
  outlined: COLOURS[i] === "White" || COLOURS[i] === "Black",
}));

/** The fibre an agent wears, by its node id; undefined for a node that is not one of the twelve. */
export function fibreOf(node: unknown): Fibre | undefined {
  return FIBRES.find((f) => f.node === node);
}

/** `var(--fibre-7)`: the colour as a CSS value, for inline styles and SVG attributes. */
export function fibreColour(f: Fibre): string {
  return `var(${f.cssVar})`;
}
