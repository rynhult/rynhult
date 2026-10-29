/** Shared JavaScript setting reader; presence (including empty) selects the new name. */
export function environmentValue(key, fallback, values = process.env) {
  const suffix = key.startsWith('ACC_') ? key.slice(4) : key.startsWith('RYNHULT_') ? key.slice(8) : null;
  if (suffix === null) return values[key] ?? fallback;
  const current = `RYNHULT_${suffix}`;
  const legacy = `ACC_${suffix}`;
  return Object.hasOwn(values, current) ? values[current] : values[legacy] ?? fallback;
}
