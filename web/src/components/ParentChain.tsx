/** Server-recorded provenance, shown independently of agent-supplied request text. */
export function ParentChain({ chain, owner }: { chain?: string[]; owner?: string | null }) {
  if (!chain?.length) return null;
  return <p className="mt-1 text-xs text-muted [overflow-wrap:anywhere]">
    Parent chain: {chain.join(' → ')}{owner && <> · Owning person: {owner === '@api' ? 'Owner via API' : owner}</>}
  </p>;
}
