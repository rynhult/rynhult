import {expect, test} from '@playwright/test';
import {readPickerMessage} from '../src/lib/previewPicker';

const origin = 'https://preview-1-8001.example.com';
const source = {};
const id = 'person-initiated-pick';
const element = {selector: 'body:nth-of-type(1) > h1:nth-of-type(1)', text: 'Demo', box: {x: 8, y: 10, width: 100, height: 30}};
const value = () => ({v: 1, type: 'selected', id, element: structuredClone(element)});
const event = (data: unknown = JSON.stringify(value())) => ({origin, source, data});

test('picker accepts only the exact origin, current frame and armed one-use id', () => {
  expect(readPickerMessage(event(), origin, source, id)).toEqual({type: 'selected', element});
  for (const wrong of ['https://preview-2-8001.example.com', 'https://preview-1-8001.example.com.evil.example', 'http://preview-1-8001.example.com', 'null']) {
    expect(readPickerMessage({...event(), origin: wrong}, origin, source, id)).toBeNull();
  }
  expect(readPickerMessage({...event(), source: {}}, origin, source, id)).toBeNull();
  expect(readPickerMessage(event(), origin, source, null)).toBeNull();
  expect(readPickerMessage(event(), origin, source, 'older-pick')).toBeNull();
  expect(readPickerMessage(event(JSON.stringify({v: 1, type: 'ready'})), origin, source, null)).toEqual({type: 'ready'});
});

test('picker rejects malformed, oversized, executable and unknown payload fields', () => {
  for (const data of [value(), null, '', '{', 'x'.repeat(4097), '[]', JSON.stringify({...value(), html: '<script>bad()</script>'})]) {
    expect(readPickerMessage(event(data), origin, source, id)).toBeNull();
  }
  for (const fields of [
    {text: 'x'.repeat(1001)}, {selector: 'x'.repeat(513)}, {selector: 'h1:nth-of-type(1)\n'},
    {selector: '#secret[data-password="value"]'}, {html: '<script>bad()</script>'},
    {box: {...element.box, width: 0}}, {box: {...element.box, x: -1}}, {box: {...element.box, width: 1000001}},
    {box: {...element.box, x: null}}, {box: {...element.box, x: '8'}}, {box: {...element.box, z: 1}},
    {selector: Array(9).fill('div:nth-of-type(1)').join(' > ')},
  ]) {
    expect(readPickerMessage(event(JSON.stringify({...value(), element: {...element, ...fields}})), origin, source, id)).toBeNull();
  }
});

test('picker strips terminal and bidi controls and preserves harmless Unicode as inert text', () => {
  const parsed = readPickerMessage(event(JSON.stringify({...value(), element: {...element, text: '\u202eDemo\x1b\x00\n café 🍎 <img>'}})), origin, source, id);
  expect(parsed).toEqual({type: 'selected', element: {...element, text: 'Demo café 🍎 <img>'}});
});

test('preview sends have a readable audit sentence without copying page context', async () => {
  const {auditWords} = await import('../src/lib/words');
  expect(auditWords({actor:'alex', action:'preview.send_element', target:'Demo', detail:{port:8001, chars:600}}))
    .toEqual({sentence:'alex sent a preview element to Demo', known:true});
});
