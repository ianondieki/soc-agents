import { humanEnum } from "../lib/agents";
import { isPeriod, periodWords, recentMonths, SCORECARD_STATUSES } from "./scorecardModel";

/**
 * `VendorPeriodPicker` (spec §7.10): the three filters `GET /scorecards` takes, which are
 * vendor code, period (`YYYY-MM`, an EAT calendar month) and status.
 *
 * All five statuses are offered to every role on purpose. Which ones a caller may see is the
 * server's decision (`_visible_statuses`), and `/api/v1/session` reports the demo role
 * switcher, not the signed-in principal. Hiding options here would sometimes hide what the
 * server would have shown. The page explains an empty result instead.
 */

export type PickerValue = { vendor: string; period: string; status: string };

export default function VendorPeriodPicker({
  vendors,
  value,
  onChange,
  disabled,
}: {
  /** `{code, name}` pairs; the code is what the API filters on. */
  vendors: { code: string; name: string }[];
  value: PickerValue;
  onChange: (next: PickerValue) => void;
  disabled?: boolean;
}) {
  const periodBad = value.period !== "" && !isPeriod(value.period);
  return (
    <div className="vpp">
      <label>
        <span>Vendor</span>
        <select
          value={value.vendor}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, vendor: e.target.value })}
        >
          <option value="">All vendors</option>
          {vendors.map((v) => (
            <option key={v.code} value={v.code}>
              {v.name && v.name !== v.code ? v.name : v.code}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>Month (EAT)</span>
        {/* A list of real months, not a bare month field (which shows "--------- ----" empty). */}
        <select
          value={periodBad ? "" : value.period}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, period: e.target.value })}
        >
          <option value="">Every month</option>
          {recentMonths(18, false, value.period).map((p) => (
            <option key={p} value={p}>
              {periodWords(p)}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>Status</span>
        <select
          value={value.status}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, status: e.target.value })}
        >
          <option value="ALL">All I may see</option>
          {SCORECARD_STATUSES.map((s) => (
            <option key={s} value={s}>
              {humanEnum(s).replace(/^./, (c) => c.toUpperCase())}
            </option>
          ))}
        </select>
      </label>
      {periodBad && <span className="chip warn">A month is written YYYY-MM; this one is ignored</span>}
    </div>
  );
}
