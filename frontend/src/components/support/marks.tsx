import { BookOpen, Inbox, Split, UserRound, Wrench, type LucideIcon } from "lucide-react";
import { AGENT_WORD, ROUTE_WORD, STATUS_WORD, TOOL_STATUS_WORD, withPerson, type Agent, type Route, type Status, type ToolStatus } from "../../lib/support";

/**
 * The small marks the Support desk repeats: a route with its icon (the person's in violet), an
 * agent's icon for the trace, a status word, a tool call's status word. Ink on paper everywhere;
 * violet is the one colour, and it means a person decides.
 */

export const ROUTE_ICON: Record<Route, LucideIcon> = { resolver: BookOpen, action: Wrench, human: UserRound };
export const AGENT_ICON: Record<Agent, LucideIcon> = {
  intake: Inbox,
  triage: Split,
  resolver: BookOpen,
  action: Wrench,
  escalation: UserRound,
  human: UserRound,
};

export function RouteMark({ route, size = 12, word = true }: { route: Route; size?: number; word?: boolean }) {
  const Icon = ROUTE_ICON[route] ?? Split;
  return (
    <span className={"sd-route" + (route === "human" ? " human" : "")} title={word ? undefined : ROUTE_WORD[route]}>
      <Icon size={size} strokeWidth={1.75} aria-hidden="true" />
      {word ? ROUTE_WORD[route] : <span className="sr-only">{ROUTE_WORD[route]}</span>}
    </span>
  );
}

export function StatusWord({ status, claimedBy }: { status: Status; claimedBy?: string | null }) {
  const person = withPerson(status);
  const word = status === "in_progress" && claimedBy ? `Claimed by ${claimedBy}` : STATUS_WORD[status] ?? status;
  return <span className={"sd-status" + (person ? " hitl" : "")}>{word}</span>;
}

/** A tool call's status as a state word (lib/icons idiom: colour only for the exceptions). */
export function ToolStatusWord({ status }: { status: ToolStatus }) {
  const tone =
    status === "needs_approval" ? " hitl" : status === "failed" || status === "refused" || status === "rejected" ? " danger" : status === "approved" ? " ok" : "";
  return <span className={"state" + tone}>{TOOL_STATUS_WORD[status] ?? status}</span>;
}

export function AgentDot({ agent }: { agent: Agent }) {
  const Icon = AGENT_ICON[agent] ?? Split;
  const person = agent === "human" || agent === "escalation";
  return (
    <span className={"sd-step-dot" + (person ? " hitl" : "")} aria-hidden="true">
      <Icon size={13} strokeWidth={1.75} />
    </span>
  );
}

export const agentWord = (a: Agent): string => AGENT_WORD[a] ?? a;
