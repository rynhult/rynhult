/** This protocol transports untrusted data, never authority to act in Rynhult. */
export interface PreviewElement {
  selector: string;
  text: string;
  box: {x: number; y: number; width: number; height: number};
}
type PickerMessage = {type: 'ready' | 'cancelled' | 'unavailable'} | {type: 'selected'; element: PreviewElement};
const segment = '[a-z][a-z0-9-]{0,63}:nth-of-type\\([1-9][0-9]{0,5}\\)';
const selector = new RegExp(`^${segment}( > ${segment}){0,7}$`);
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const keys = (value: Record<string, unknown>, expected: string[]) => Object.keys(value).length === expected.length && expected.every(key => Object.hasOwn(value, key));

export function readPickerMessage(event: {origin: string; source: unknown; data: unknown}, origin: string, source: unknown, id: string | null): PickerMessage | null {
  if (!source || event.source !== source || event.origin !== origin || typeof event.data !== 'string' || event.data.length > 4096) return null;
  let data: unknown;
  try { data = JSON.parse(event.data); } catch { return null; }
  if (!record(data) || data.v !== 1) return null;
  if (data.type === 'ready') return keys(data, ['v', 'type']) ? {type: 'ready'} : null;
  if (!id || data.id !== id) return null;
  if (data.type === 'cancelled' || data.type === 'unavailable') return keys(data, ['v', 'type', 'id']) ? {type: data.type} : null;
  if (data.type !== 'selected' || !keys(data, ['v', 'type', 'id', 'element']) || !record(data.element)) return null;
  const element = data.element;
  if (!keys(element, ['selector', 'text', 'box']) || typeof element.selector !== 'string' || element.selector.length > 512
    || /[^\x20-\x7e]/.test(element.selector) || !selector.test(element.selector) || typeof element.text !== 'string' || element.text.length > 1000
    || !record(element.box) || !keys(element.box, ['x', 'y', 'width', 'height'])) return null;
  const {x, y, width, height} = element.box;
  if (![x, y, width, height].every(value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1000000)
    || (width as number) <= 0 || (height as number) <= 0) return null;
  return {type: 'selected', element: {selector: element.selector,
    text: element.text.replace(/[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ud800-\udfff]/gu, ' ').replace(/\s+/gu, ' ').trim(),
    box: {x: x as number, y: y as number, width: width as number, height: height as number}}};
}
