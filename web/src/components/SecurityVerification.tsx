import {useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {Link} from 'react-router-dom';
import {api,post} from '../lib/api';
import {passkeyCeremony,passkeysAvailable} from '../lib/passkeys';
import {Button,Dialog,ErrorNote,Field,Input,QueryState} from './ui';

export function SecurityVerification({open,onClose,onVerified}: {open:boolean;onClose:()=>void;onVerified:()=>void}) {
  const state=useQuery({queryKey:['passkeys'],queryFn:()=>api<{company_only?:boolean}>('/api/auth/passkeys'),enabled:open});
  const [password,setPassword]=useState(''),[code,setCode]=useState(''),[error,setError]=useState<unknown>(null),[busy,setBusy]=useState(false);
  const close=()=>{if(!busy){setPassword('');setCode('');onClose();}};
  const verify=async(passkey:boolean)=>{
    if(busy)return;setBusy(true);setError(null);
    try {
      if(passkey)await passkeyCeremony('verify');else await post('/api/auth/passkeys/reauth',{password,code});
      setPassword('');setCode('');onVerified();
    } catch(e){setError(e);} finally{setBusy(false);}
  };
  return <Dialog open={open} onClose={close} title="Verify your sign-in" dismissible={false}
    description="Verify before reviewing Security changes. Verification and the five-minute preview are bound to this signed-in browser."
    footer={<Button disabled={busy} onClick={close}>Cancel</Button>}>
    <div className="space-y-3"><ErrorNote error={error}/>
      <Button disabled={busy||!passkeysAvailable()} onClick={()=>void verify(true)}>Verify with a passkey</Button>
      <QueryState query={state}>{data=>data.company_only?<p className="text-sm text-muted">Use an existing passkey, or sign in again through your company with multi-factor authentication. <Link className="underline" to="/settings#passkeys">Passkey help</Link></p>:
        <form className="space-y-3" onSubmit={e=>{e.preventDefault();void verify(false);}}>
          <Field label="Verification password"><Input type="password" autoComplete="current-password" value={password} onChange={e=>setPassword(e.target.value)} disabled={busy}/></Field>
          <Field label="Verification authenticator code"><Input autoComplete="one-time-code" inputMode="numeric" maxLength={6} value={code} onChange={e=>setCode(e.target.value.replace(/\D/g,''))} disabled={busy}/></Field>
          <Button type="submit" disabled={busy||!password||code.length!==6}>Verify with password and authenticator</Button>
        </form>}
      </QueryState>
    </div>
  </Dialog>;
}

