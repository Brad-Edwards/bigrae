/**
 * Wall-time to UTC conversion for scheduled communications (#2052).
 *
 * One campaign has one UTC instant. The organizer picks a date and time in a
 * named IANA zone; a daylight-saving gap or overlap is refused rather than
 * silently shifted, so the instant shown before send is the instant stored.
 */

export type ZonedConversion = { ok: true; utc: Date } | { ok: false; reason: "invalid" | "nonexistent" | "ambiguous" };

const DAY_MS = 86_400_000;

function wallClockOf(instant: number, zone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(instant));
  const value = (type: string) => Number(parts.find((part) => part.type === type)?.value);
  return Date.UTC(value("year"), value("month") - 1, value("day"), value("hour"), value("minute"), value("second"));
}

function parseWallTime(date: string, time: string): number | null {
  const dateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  const timeMatch = /^(\d{2}):(\d{2})$/.exec(time);
  if (!dateMatch || !timeMatch) return null;
  const [year, month, day] = dateMatch.slice(1).map(Number);
  const [hour, minute] = timeMatch.slice(1).map(Number);
  const wall = Date.UTC(year, month - 1, day, hour, minute);
  const check = new Date(wall);
  const valid =
    check.getUTCFullYear() === year && check.getUTCMonth() === month - 1 && check.getUTCDate() === day && hour < 24 && minute < 60;
  return valid ? wall : null;
}

export function zonedWallTimeToUtc(date: string, time: string, zone: string): ZonedConversion {
  const wall = parseWallTime(date, time);
  if (wall === null) return { ok: false, reason: "invalid" };
  let offsets: number[];
  try {
    // The zone offsets a day either side bracket at most one transition.
    offsets = [wall - DAY_MS, wall, wall + DAY_MS].map((probe) => wallClockOf(probe, zone) - probe);
  } catch {
    return { ok: false, reason: "invalid" };
  }
  const candidates = [...new Set(offsets.map((offset) => wall - offset))].filter(
    (instant) => wallClockOf(instant, zone) === wall,
  );
  if (candidates.length === 0) return { ok: false, reason: "nonexistent" };
  if (candidates.length > 1) return { ok: false, reason: "ambiguous" };
  return { ok: true, utc: new Date(candidates[0]) };
}

export function formatInZone(instant: Date, zone: string): string {
  try {
    // dateStyle/timeStyle cannot be combined with timeZoneName, so name each part.
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
      timeZone: zone,
      timeZoneName: "short",
    }).format(instant);
  } catch {
    return instant.toISOString();
  }
}

export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}
