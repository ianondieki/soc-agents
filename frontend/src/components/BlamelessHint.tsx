/**
 * `BlamelessHint` (spec §7.10) — the postmortem rule, shown *before* it is broken.
 *
 * `PATCH /api/v1/pir/{id}` runs a blameless validator over `root_causes` and
 * `contributing_factors` and answers **422** with one exact sentence when either names a
 * person on the incident (§7.7.3). A validator a writer only meets at save time teaches
 * them that the form is hostile; a validator whose rule is on the screen while they type
 * teaches them the rule. So this block is always visible above those two fields, and it
 * simply gets louder — same words, same colours — when the 422 comes back.
 *
 * WHAT IT DELIBERATELY DOES NOT DO. The backend never echoes the name it matched: a
 * rejection that quotes the person is a written accusation with a timestamp on it, which is
 * the whole thing the lane exists to prevent. This component has no access to that name and
 * does not try to infer one — it names the two validated fields and the vocabulary to use
 * instead, and stops there.
 *
 * 3 a.m. rules (§7.10): the tripped state is an amber chip **with the word REJECTED in it**,
 * never colour alone; nothing animates.
 */
export default function BlamelessHint({ tripped = false }: { tripped?: boolean }) {
  return (
    <div className="pir-hint" data-tripped={tripped ? "yes" : "no"}>
      <div className="pir-hint-head">
        <span className={tripped ? "chip warn" : "chip"}>
          {tripped ? "REJECTED · BLAMELESS RULE" : "BLAMELESS RULE"}
        </span>
        <strong>Describe what the system allowed, not who did it.</strong>
      </div>
      <p className="muted" style={{ margin: "0.4rem 0 0" }}>
        <strong>Root causes</strong> and <strong>contributing factors</strong> are checked
        against the people recorded on this incident. Write the role, not the person —{" "}
        <code>RNIO</code>, <code>FE</code>, <code>MSP_POWER</code>. The same rule applies to an
        action item&rsquo;s owner, which must be a role token (§7.7.6): an action outlives
        whoever is on shift tonight.
      </p>
      <div className="pir-hint-pair">
        <div>
          <div className="pir-hint-label">Rejected</div>
          <div className="pir-hint-eg bad">
            &ldquo;&lsaquo;engineer&rsquo;s name&rsaquo; forgot to re-arm the generator alarm&rdquo;
          </div>
        </div>
        <div>
          <div className="pir-hint-label">Accepted</div>
          <div className="pir-hint-eg ok">
            &ldquo;the generator alarm can be left disarmed after a manual test with nothing
            that re-arms it or reports it &mdash; MSP_POWER runbook has no closing step&rdquo;
          </div>
        </div>
      </div>
      {tripped && (
        <p className="muted" style={{ margin: "0.5rem 0 0" }}>
          The server does not repeat the name it matched, and neither does this screen &mdash; a
          rejection that quotes the person is the accusation the rule exists to prevent. Re-read
          the two fields above and replace any person with the role that owns the gap.
        </p>
      )}
    </div>
  );
}
