import { post } from './api';

const decode = (value: string): Uint8Array<ArrayBuffer> => Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
const encode = (value: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
export const passkeysAvailable = () => window.isSecureContext && typeof PublicKeyCredential !== 'undefined';

// Explicit serialization works on browsers predating PublicKeyCredential.toJSON().
function serialize(credential: PublicKeyCredential) {
  const response = credential.response;
  const result: Record<string, unknown> = { clientDataJSON: encode(response.clientDataJSON) };
  if (response instanceof AuthenticatorAttestationResponse) {
    result.attestationObject = encode(response.attestationObject);
    result.transports = response.getTransports?.() ?? [];
  } else {
    const assertion = response as AuthenticatorAssertionResponse;
    result.authenticatorData = encode(assertion.authenticatorData);
    result.signature = encode(assertion.signature);
    result.userHandle = assertion.userHandle ? encode(assertion.userHandle) : null;
  }
  return { id: credential.id, rawId: encode(credential.rawId), type: credential.type, response: result };
}

export async function passkeyCeremony(kind: 'register' | 'login' | 'verify', name?: string) {
  if (!passkeysAvailable()) throw new Error('Passkeys need a supported browser and a trusted HTTPS connection to Rynhult.');
  const begin = await post<{ challenge_id: string; options: Record<string, unknown> }>(`/api/auth/passkeys/${kind}/begin`);
  const options = begin.options;
  options.challenge = decode(options.challenge as string);
  for (const list of ['allowCredentials', 'excludeCredentials']) {
    if (options[list]) options[list] = (options[list] as { id: string }[]).map(c => ({ ...c, id: decode(c.id) }));
  }
  let credential: Credential | null;
  try {
    if (kind === 'register') {
      const user = options.user as { id: string };
      options.user = { ...user, id: decode(user.id) };
      credential = await navigator.credentials.create({ publicKey: options as unknown as PublicKeyCredentialCreationOptions });
    } else credential = await navigator.credentials.get({ publicKey: options as unknown as PublicKeyCredentialRequestOptions });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'SecurityError') throw new Error('Passkeys need a hostname and the exact configured Rynhult address. Open Rynhult at its trusted HTTPS hostname.');
    if (error instanceof DOMException && error.name === 'NotAllowedError') throw new Error('Passkey request was cancelled or timed out. Try again when your authenticator is ready.');
    throw error;
  }
  if (!credential) throw new Error('No passkey was returned. Try again.');
  return post(`/api/auth/passkeys/${kind}/complete`, { challenge_id: begin.challenge_id, name, credential: serialize(credential as PublicKeyCredential) });
}
