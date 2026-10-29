"""Shared generated-image comparison policy for broker evidence and main validation."""

PASS_OUTCOMES = ('pass', 'pass, 1 character misread')


def assessment(expected, answer, code):
    if code or not isinstance(answer, dict) or set(answer) != {'code', 'shapes'}:
        return 'error'
    if (not isinstance(answer['code'], str) or len(answer['code']) > 4
            or not isinstance(answer['shapes'], list) or len(answer['shapes']) > 3
            or any(not isinstance(v, str) or len(v) > 32 for v in answer['shapes'])):
        return 'error'
    if (not isinstance(expected, dict) or not isinstance(expected.get('code'), str)
            or len(expected['code']) != 4 or not isinstance(expected.get('shapes'), list)
            or len(expected['shapes']) != 3):
        return 'error'
    if answer['shapes'] != expected['shapes'] or len(answer['code']) != 4:
        return 'fail'
    wrong = sum(a != b for a, b in zip(expected['code'], answer['code']))
    return PASS_OUTCOMES[wrong] if wrong <= 1 else 'fail'
