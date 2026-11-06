import { createContext, useContext } from 'react';

/** What to call `machine` in text. Requests keep the real value. */
const MachineLabel = createContext<string | undefined>(undefined);
export const MachineLabelScope = MachineLabel.Provider;
export const useMachineLabel = (machine?: string) => {
  const label = useContext(MachineLabel);
  return machine && label ? label : machine;
};
