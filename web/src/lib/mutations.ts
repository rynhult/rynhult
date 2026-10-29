import { useRef } from 'react';
import { useMutation, type UseMutationOptions } from '@tanstack/react-query';

/** Serialize a person's submits before the pending-state render disables the control. */
export function useSingleMutation<TData = unknown, TError = Error, TVariables = void, TContext = unknown>(
  options: UseMutationOptions<TData, TError, TVariables, TContext>,
) {
  const submitting = useRef(false);
  const mutation = useMutation({
    ...options,
    onSettled: async (...args) => {
      try { await options.onSettled?.(...args); }
      finally { submitting.current = false; }
    },
  });
  const mutate: typeof mutation.mutate = (...args) => {
    if (submitting.current) return;
    submitting.current = true;
    mutation.mutate(...args);
  };
  return { ...mutation, mutate };
}
